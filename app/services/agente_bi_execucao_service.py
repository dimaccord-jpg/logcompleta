"""Fila durável das análises do AgenteBI.

O worker é outro processo. Este módulo não abre thread, não chama
provedor e não copia dataset para a mensagem. A recuperação de um
efeito externo duvidoso não recoloca o trabalho na fila.

A fila não tem responsabilidade financeira. Não reserva, não dispensa e
não liquida créditos: sem chamada externa não há consumo a contabilizar.
A cobrança de uma chamada real segue o caminho governado já existente
(autorização, chamada, IaConsumoEvento, motor oficial).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import text, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    utcnow_naive,
)
from app.services.agente_bi_analisar_service import AnalisarErro, montar_contexto, validar_saida
from app.services.agente_bi_perfil_semantico_service import PerfilErro, perfilar_dataset
from app.services.agente_bi_persistencia_service import AgenteBiErro, EscopoAgenteBi

logger = logging.getLogger(__name__)

MAX_TENTATIVAS = 3
MAX_RECUPERACOES = 3
LEASE_PADRAO_SEGUNDOS = 30
LEASE_MAX_SEGUNDOS = 120

_CHAVE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{7,79}$")
_CODIGO_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_WORKER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

TRANSICOES = {
    "criada": {"aguardando", "cancelada", "falha_definitiva"},
    "aguardando": {"adquirida", "cancelada", "falha_definitiva"},
    "adquirida": {"concluida", "falha_recuperavel", "falha_definitiva", "incerta", "aguardando"},
    "falha_recuperavel": {"adquirida", "falha_definitiva", "incerta"},
    "concluida": set(),
    "falha_definitiva": set(),
    "incerta": set(),
    "cancelada": set(),
}

_ANALISE_POR_ESTADO = {
    "criada": "aguardando_execucao",
    "aguardando": "aguardando_execucao",
    "adquirida": "em_execucao",
    "concluida": "concluida",
    "falha_recuperavel": "em_execucao",
    "falha_definitiva": "falha_validacao",
    "incerta": "incerta",
    "cancelada": "preparada",
}

def _sql_adquirir(filtro: str) -> str:
    """Uma linha por transação, sem esperar lock de outro worker.

    O filtro entra no candidato. Sem ele, a fila global segue o menor id.
    Com ele, a sentença não escolhe outro item no lugar.
    """
    return f"""
WITH candidato AS (
    SELECT id, estado
    FROM agente_bi_execucao
    WHERE estado IN ('aguardando', 'falha_recuperavel')
      AND efeito_externo = 'nenhum'
      AND tentativas < :max_tentativas
      {filtro}
    ORDER BY id
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE agente_bi_execucao AS alvo
SET estado = 'adquirida',
    fencing = alvo.fencing + 1,
    lease_worker = :worker,
    lease_ate = :expira,
    adquirida_em = :agora,
    tentativas = alvo.tentativas + 1,
    ultimo_erro = NULL,
    updated_at = :agora
FROM candidato
WHERE alvo.id = candidato.id
RETURNING alvo.id AS id,
          candidato.estado AS estado_anterior,
          alvo.fencing AS fencing,
          alvo.tentativas AS tentativas
"""


# PostgreSQL: fila global. A variante direta só acrescenta o id pedido.
SQL_ADQUIRIR = _sql_adquirir("")
SQL_ADQUIRIR_DIRETA = _sql_adquirir("AND id = :execucao_id")


class ExecucaoErro(Exception):
    def __init__(self, codigo: str):
        super().__init__(codigo)
        self.codigo = codigo


class EntregaProcessador:
    """Código da fila e, quando houver, o documento analítico.

    Processadores legados continuam devolvendo só o código, como string.
    A confirmação revalida o documento na fronteira de persistência. O
    marcador declarado pelo conteúdo não autoriza a gravação.
    """

    __slots__ = ("codigo", "documento")

    def __init__(self, codigo: str, documento: dict | None = None):
        self.codigo = codigo
        self.documento = documento


def transicao_permitida(origem: str, destino: str) -> bool:
    return destino in TRANSICOES.get(origem, set())


def enfileirar(escopo: EscopoAgenteBi, analise_id: int, *, chave_idempotencia: str | None = None) -> dict:
    analise = _analise_no_escopo(escopo, analise_id)
    if analise is None:
        raise ExecucaoErro("nao_encontrado")
    _exigir_dataset_da_analise(analise)
    chave = chave_idempotencia or f"agente-bi.{escopo.usuario_id}.{analise.id}"
    if not isinstance(chave, str) or _CHAVE_RE.fullmatch(chave) is None:
        raise ExecucaoErro("chave_invalida")
    existente = _por_chave(chave)
    if existente is not None:
        if not _mesma_identidade(existente, escopo) or existente.analise_id != analise.id:
            raise ExecucaoErro("identidade_inconsistente")
        return execucao_para_dict(existente)
    if analise.resultado_json is not None:
        raise ExecucaoErro("historico_preservado")
    por_analise = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one_or_none()
    if por_analise is not None:
        raise ExecucaoErro("execucao_existente")
    if analise.estado not in {"preparada", "aguardando_execucao"}:
        raise ExecucaoErro("estado_invalido")

    agora = utcnow_naive()
    linha = AgenteBiExecucao(
        operacao_id=str(uuid4()),
        analise_id=analise.id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        estado="criada",
        tentativas=0,
        recuperacoes=0,
        chave_idempotencia=chave,
        fencing=0,
        efeito_externo="nenhum",
        created_at=agora,
        updated_at=agora,
    )
    try:
        db.session.add(linha)
        db.session.flush()
        _registrar_evento(linha, None, "criada", codigo="criada")
        _aplicar_estado(linha, "aguardando", codigo="enfileirada", agora=agora)
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        de_novo = _por_chave(chave)
        if de_novo is not None and _mesma_identidade(de_novo, escopo) and de_novo.analise_id == analise.id:
            return execucao_para_dict(de_novo)
        raise ExecucaoErro("chave_idempotencia_indisponivel")
    except Exception:
        db.session.rollback()
        raise
    logger.info(
        "evento=agente_bi_execucao_enfileirada execucao_id=%s analise_id=%s usuario_id=%s",
        linha.id,
        linha.analise_id,
        linha.usuario_id,
    )
    return execucao_para_dict(linha)


def cancelar(escopo: EscopoAgenteBi, execucao_id: int, *, antes_da_gravacao=None) -> dict:
    linha = _no_escopo(escopo, execucao_id)
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    if linha.estado == "cancelada":
        return execucao_para_dict(linha)
    if antes_da_gravacao is not None:
        antes_da_gravacao()
        linha = _no_escopo(escopo, execucao_id)
        if linha is None:
            raise ExecucaoErro("nao_encontrado")
        if linha.estado == "cancelada":
            return execucao_para_dict(linha)
    analise = _analise_da_execucao(linha)
    db.session.refresh(analise)
    if analise.resultado_json is not None:
        raise ExecucaoErro("historico_preservado")
    origem = linha.estado
    momento = utcnow_naive()
    if not _atualizar_condicional(
        [
            AgenteBiExecucao.id == linha.id,
            AgenteBiExecucao.usuario_id == escopo.usuario_id,
            AgenteBiExecucao.conta_id == escopo.conta_id,
            AgenteBiExecucao.franquia_id == escopo.franquia_id,
            AgenteBiExecucao.estado.in_(("criada", "aguardando")),
        ],
        {
            "estado": "cancelada",
            "lease_worker": None,
            "lease_ate": None,
            "updated_at": momento,
        },
    ):
        db.session.rollback()
        atual = _no_escopo(escopo, execucao_id)
        if atual is not None and atual.estado == "cancelada":
            return execucao_para_dict(atual)
        raise ExecucaoErro("estado_invalido")
    try:
        _publicar_transicao(linha.id, origem=origem, destino="cancelada", codigo="cancelada", agora=momento)
    except Exception:
        db.session.rollback()
        raise
    return execucao_para_dict(db.session.get(AgenteBiExecucao, int(execucao_id)))


def obter(escopo: EscopoAgenteBi, execucao_id: int) -> dict:
    linha = _no_escopo(escopo, execucao_id)
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    return execucao_para_dict(linha)


def listar_eventos(escopo: EscopoAgenteBi, execucao_id: int) -> list[dict]:
    linha = _no_escopo(escopo, execucao_id)
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    eventos = (
        AgenteBiExecucaoEvento.query.filter_by(execucao_id=linha.id)
        .order_by(AgenteBiExecucaoEvento.id.asc())
        .all()
    )
    return [
        {
            "estado_anterior": evento.estado_anterior,
            "estado_novo": evento.estado_novo,
            "fencing": evento.fencing,
            "codigo": evento.codigo,
        }
        for evento in eventos
    ]


def recuperar_leases(*, agora: datetime | None = None) -> int:
    momento = agora or utcnow_naive()
    identificadores = [
        int(linha.id)
        for linha in (
            AgenteBiExecucao.query.filter(
                AgenteBiExecucao.estado == "adquirida",
                AgenteBiExecucao.lease_ate.isnot(None),
                AgenteBiExecucao.lease_ate <= momento,
            )
            .order_by(AgenteBiExecucao.id.asc())
            .all()
        )
    ]
    db.session.commit()
    total = 0
    for identificador in identificadores:
        linha = db.session.get(AgenteBiExecucao, identificador)
        if linha is None or linha.estado != "adquirida" or linha.lease_ate is None or linha.lease_ate > momento:
            continue
        fencing = int(linha.fencing)
        recuperacoes = int(linha.recuperacoes)
        if linha.efeito_externo == "incerto" or _chamada_externa_contabilizada(linha.id):
            destino, codigo = "incerta", "lease_expirado_incerto"
        elif recuperacoes + 1 >= MAX_RECUPERACOES:
            destino, codigo = "falha_definitiva", "recuperacoes_esgotadas"
        elif int(linha.tentativas) >= MAX_TENTATIVAS:
            destino, codigo = "falha_definitiva", "tentativas_esgotadas"
        else:
            destino, codigo = "aguardando", "lease_recuperado"
        origem = linha.estado
        if not _atualizar_condicional(
            [
                AgenteBiExecucao.id == linha.id,
                AgenteBiExecucao.estado == "adquirida",
                AgenteBiExecucao.fencing == fencing,
                AgenteBiExecucao.lease_ate.isnot(None),
                AgenteBiExecucao.lease_ate <= momento,
            ],
            {
                "estado": destino,
                "fencing": AgenteBiExecucao.fencing + 1,
                "recuperacoes": AgenteBiExecucao.recuperacoes + 1,
                "lease_worker": None,
                "lease_ate": None,
                "updated_at": momento,
                **({"efeito_externo": "incerto"} if destino == "incerta" else {}),
            },
        ):
            db.session.rollback()
            continue
        try:
            _publicar_transicao(linha.id, origem=origem, destino=destino, codigo=codigo, agora=momento)
        except Exception:
            db.session.rollback()
            raise
        total += 1
    return total


def promover_esgotadas(*, agora: datetime | None = None) -> int:
    momento = agora or utcnow_naive()
    identificadores = [
        int(linha.id)
        for linha in (
            AgenteBiExecucao.query.filter(AgenteBiExecucao.estado == "falha_recuperavel")
            .order_by(AgenteBiExecucao.id.asc())
            .all()
        )
    ]
    db.session.commit()
    total = 0
    for identificador in identificadores:
        linha = db.session.get(AgenteBiExecucao, identificador)
        if linha is None or linha.estado != "falha_recuperavel":
            continue
        if linha.efeito_externo == "incerto":
            destino, codigo = "incerta", "efeito_incerto"
        elif int(linha.tentativas) >= MAX_TENTATIVAS:
            destino, codigo = "falha_definitiva", "tentativas_esgotadas"
        else:
            continue
        origem = linha.estado
        efeito = linha.efeito_externo
        tentativas = int(linha.tentativas)
        if not _atualizar_condicional(
            [
                AgenteBiExecucao.id == linha.id,
                AgenteBiExecucao.estado == "falha_recuperavel",
                AgenteBiExecucao.efeito_externo == efeito,
                AgenteBiExecucao.tentativas == tentativas,
            ],
            {
                "estado": destino,
                "lease_worker": None,
                "lease_ate": None,
                "updated_at": momento,
            },
        ):
            db.session.rollback()
            continue
        try:
            _publicar_transicao(linha.id, origem=origem, destino=destino, codigo=codigo, agora=momento)
        except Exception:
            db.session.rollback()
            raise
        total += 1
    return total


def adquirir(
    *,
    worker_id: str,
    agora: datetime | None = None,
    lease_segundos: int = LEASE_PADRAO_SEGUNDOS,
    antes_do_commit=None,
    execucao_id: int | None = None,
) -> dict | None:
    """Reserva o próximo item livre, ou somente a execução informada.

    Sem ``execucao_id`` a fila global permanece. Com o filtro, uma execução
    ocupada ou ausente devolve ``None`` e nenhum outro item é escolhido.
    """
    worker = _worker(worker_id)
    alvo = _filtro_execucao(execucao_id)
    momento = agora or utcnow_naive()
    lease = _lease(lease_segundos)
    dialeto = db.session.get_bind().dialect.name
    try:
        if dialeto == "postgresql":
            adquirido = _adquirir_postgres(worker, momento, lease, alvo)
        else:
            adquirido = _adquirir_serial(worker, momento, lease, alvo)
        if adquirido is None:
            db.session.rollback()
            return None
        if antes_do_commit is not None:
            antes_do_commit()
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    linha = db.session.get(AgenteBiExecucao, int(adquirido["id"]))
    logger.info(
        "evento=agente_bi_execucao_adquirida execucao_id=%s analise_id=%s fencing=%s",
        linha.id,
        linha.analise_id,
        linha.fencing,
    )
    return execucao_para_dict(linha)


def confirmar(
    execucao_id: int,
    *,
    worker_id: str,
    fencing: int,
    agora: datetime | None = None,
    codigo_resultado: str = "simulado",
    documento: dict | None = None,
    antes_do_commit=None,
    antes_da_gravacao=None,
) -> dict:
    worker = _worker(worker_id)
    codigo = _codigo(codigo_resultado)
    momento = agora or utcnow_naive()
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    analise = _analise_da_execucao(linha)
    referencias = _referencias(linha, analise)
    try:
        resultado = _resultado_confirmado(codigo, referencias, documento, analise)
    except ExecucaoErro:
        db.session.rollback()
        raise
    if _confirmacao_repetida(linha, fencing, codigo):
        _exigir_resultado_igual(analise, resultado)
        return execucao_para_dict(linha)
    if antes_da_gravacao is not None:
        antes_da_gravacao()
        db.session.refresh(linha)
        db.session.refresh(analise)
        if _confirmacao_repetida(linha, fencing, codigo):
            _exigir_resultado_igual(analise, resultado)
            return execucao_para_dict(linha)
    db.session.refresh(analise)
    db.session.refresh(linha)
    if analise.resultado_json not in (None, resultado):
        db.session.rollback()
        raise ExecucaoErro("historico_preservado")
    _transicionar_dono(
        linha,
        worker=worker,
        fencing=fencing,
        agora=momento,
        destino="concluida",
        codigo="concluida",
        codigo_resultado=codigo,
        resultado=resultado,
        antes_do_commit=antes_do_commit,
        valores={
            "estado": "concluida",
            "efeito_externo": "simulado",
            "codigo_resultado": codigo,
            "concluida_em": momento,
            "lease_worker": None,
            "lease_ate": None,
            "updated_at": momento,
        },
        extra=_extra_da_confirmacao(linha.id, documento),
    )
    logger.info(
        "evento=agente_bi_execucao_concluida execucao_id=%s fencing=%s",
        execucao_id,
        fencing,
    )
    return execucao_para_dict(db.session.get(AgenteBiExecucao, int(execucao_id)))


def falhar(
    execucao_id: int,
    *,
    worker_id: str,
    fencing: int,
    codigo: str,
    agora: datetime | None = None,
    recuperavel: bool = True,
    antes_da_gravacao=None,
) -> dict:
    worker = _worker(worker_id)
    erro = _codigo(codigo)
    momento = agora or utcnow_naive()
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    if _falha_repetida(linha, fencing, erro):
        return execucao_para_dict(linha)
    if antes_da_gravacao is not None:
        antes_da_gravacao()
        db.session.refresh(linha)
        if _falha_repetida(linha, fencing, erro):
            return execucao_para_dict(linha)
    if linha.efeito_externo == "incerto" or not recuperavel or int(linha.tentativas) >= MAX_TENTATIVAS:
        destino = "incerta" if linha.efeito_externo == "incerto" else "falha_definitiva"
    else:
        destino = "falha_recuperavel"
    _transicionar_dono(
        linha,
        worker=worker,
        fencing=fencing,
        agora=momento,
        destino=destino,
        codigo=erro,
        codigo_falha=erro,
        valores={
            "estado": destino,
            "ultimo_erro": erro,
            "lease_worker": None,
            "lease_ate": None,
            "updated_at": momento,
        },
    )
    return execucao_para_dict(db.session.get(AgenteBiExecucao, int(execucao_id)))


def marcar_efeito_incerto(
    execucao_id: int,
    *,
    worker_id: str,
    fencing: int,
    agora: datetime | None = None,
    antes_da_gravacao=None,
    lease_vivo: bool = True,
) -> dict:
    """Estado terminal do efeito externo. Não chama provedor nem movimenta crédito.

    ``lease_vivo=False`` só cabe para quem já segurou o efeito e perdeu o
    prazo. O fencing continua obrigatório.
    """
    worker = _worker(worker_id)
    momento = agora or utcnow_naive()
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    if linha.estado == "incerta" and linha.efeito_externo == "incerto":
        return execucao_para_dict(linha)
    if antes_da_gravacao is not None:
        antes_da_gravacao()
        linha = db.session.get(AgenteBiExecucao, int(execucao_id))
        if linha is not None and linha.estado == "incerta" and linha.efeito_externo == "incerto":
            return execucao_para_dict(linha)
    _transicionar_dono(
        linha,
        worker=worker,
        fencing=fencing,
        agora=momento,
        destino="incerta",
        codigo="efeito_incerto",
        lease_vivo=lease_vivo,
        valores={
            "estado": "incerta",
            "efeito_externo": "incerto",
            "fencing": AgenteBiExecucao.fencing + 1,
            "lease_worker": None,
            "lease_ate": None,
            "ultimo_erro": "efeito_incerto",
            "updated_at": momento,
        },
    )
    return execucao_para_dict(db.session.get(AgenteBiExecucao, int(execucao_id)))


def processar_simulado(referencias: dict) -> str:
    """Trabalho local. Não lê dataset e não contacta provedor."""
    for campo in ("execucao_id", "analise_id", "dataset_id", "usuario_id"):
        if not isinstance(referencias.get(campo), int):
            raise ExecucaoErro("identidade_inconsistente")
    return "simulado"


def executar_um_ciclo(
    *,
    worker_id: str,
    agora: datetime | None = None,
    lease_segundos: int = LEASE_PADRAO_SEGUNDOS,
    processador=None,
    antes_de_confirmar=None,
    execucao_id: int | None = None,
) -> dict:
    trabalho = processador or processar_simulado
    alvo = _filtro_execucao(execucao_id)
    recuperar_leases(agora=agora)
    promover_esgotadas(agora=agora)
    reivindicada = adquirir(
        worker_id=worker_id,
        agora=agora,
        lease_segundos=lease_segundos,
        execucao_id=alvo,
    )
    if reivindicada is None:
        return {"adquirida": False}
    if alvo is not None and int(reivindicada["id"]) != alvo:
        logger.error(
            "evento=agente_bi_execucao_substituta_recusada solicitada=%s adquirida=%s",
            alvo,
            reivindicada["id"],
        )
        raise ExecucaoErro("identidade_inconsistente")
    referencias = _referencias_por_id(int(reivindicada["id"]))
    try:
        codigo, documento = _entrega_do_processador(trabalho(referencias))
    except ExecucaoErro as exc:
        db.session.rollback()
        atual = db.session.get(AgenteBiExecucao, int(reivindicada["id"]))
        if atual is not None and atual.estado in {"incerta", "falha_definitiva", "concluida", "cancelada"}:
            return {"adquirida": True, "execucao": execucao_para_dict(atual)}
        if exc.codigo == "efeito_incerto":
            fechada = _fechar_efeito_ja_seguro(
                int(reivindicada["id"]),
                worker_id=worker_id,
                fencing=int(reivindicada["fencing"]),
                agora=agora,
            )
            if fechada is not None:
                return {"adquirida": True, "execucao": fechada}
            return {"adquirida": True, "execucao": execucao_para_dict(atual)}
        # Chamada já contabilizada não volta à fila: a mesma chave não transporta de novo.
        contabilizada = _chamada_externa_contabilizada(int(reivindicada["id"]))
        recuperavel = (
            exc.codigo not in {"efeito_incerto", "historico_preservado", "identidade_inconsistente"}
            and not contabilizada
        )
        falha = falhar(
            int(reivindicada["id"]),
            worker_id=worker_id,
            fencing=int(reivindicada["fencing"]),
            codigo=exc.codigo,
            agora=agora,
            recuperavel=recuperavel,
        )
        return {"adquirida": True, "execucao": falha}
    except Exception:
        db.session.rollback()
        falha = falhar(
            int(reivindicada["id"]),
            worker_id=worker_id,
            fencing=int(reivindicada["fencing"]),
            codigo="falha_processamento",
            agora=agora,
            recuperavel=True,
        )
        return {"adquirida": True, "execucao": falha}
    try:
        concluida = confirmar(
            int(reivindicada["id"]),
            worker_id=worker_id,
            fencing=int(reivindicada["fencing"]),
            agora=agora,
            codigo_resultado=codigo,
            documento=documento,
            antes_do_commit=antes_de_confirmar,
        )
    except ExecucaoErro as exc:
        db.session.rollback()
        if exc.codigo in {"lease_expirado", "fencing_recusado"}:
            fechada = _fechar_efeito_ja_seguro(
                int(reivindicada["id"]),
                worker_id=worker_id,
                fencing=int(reivindicada["fencing"]),
                agora=agora,
            )
            if fechada is not None:
                return {"adquirida": True, "execucao": fechada}
        raise
    return {"adquirida": True, "execucao": concluida}


def execucao_para_dict(linha: AgenteBiExecucao) -> dict:
    return {
        "id": linha.id,
        "operacao_id": linha.operacao_id,
        "analise_id": linha.analise_id,
        "usuario_id": linha.usuario_id,
        "conta_id": linha.conta_id,
        "franquia_id": linha.franquia_id,
        "estado": linha.estado,
        "tentativas": linha.tentativas,
        "recuperacoes": linha.recuperacoes,
        "fencing": linha.fencing,
        "efeito_externo": linha.efeito_externo,
        "chave_idempotencia": linha.chave_idempotencia,
        "ultimo_erro": linha.ultimo_erro,
        "codigo_resultado": linha.codigo_resultado,
        "lease_worker": linha.lease_worker,
    }


def _adquirir_postgres(worker: str, agora: datetime, lease: int, execucao_id: int | None) -> dict | None:
    sentenca = SQL_ADQUIRIR if execucao_id is None else SQL_ADQUIRIR_DIRETA
    parametros = {
        "max_tentativas": MAX_TENTATIVAS,
        "worker": worker,
        "expira": agora + timedelta(seconds=lease),
        "agora": agora,
    }
    if execucao_id is not None:
        parametros["execucao_id"] = execucao_id
    registro = (
        db.session.execute(
            text(sentenca),
            parametros,
        )
        .mappings()
        .first()
    )
    if registro is None:
        return None
    linha = db.session.get(AgenteBiExecucao, int(registro["id"]))
    db.session.expire(linha)
    db.session.refresh(linha)
    _depois_da_aquisicao(linha, str(registro["estado_anterior"]), agora)
    return {"id": linha.id}


def _adquirir_serial(worker: str, agora: datetime, lease: int, execucao_id: int | None) -> dict | None:
    consulta = AgenteBiExecucao.query.filter(
        AgenteBiExecucao.estado.in_(("aguardando", "falha_recuperavel")),
        AgenteBiExecucao.efeito_externo == "nenhum",
        AgenteBiExecucao.tentativas < MAX_TENTATIVAS,
    )
    if execucao_id is not None:
        consulta = consulta.filter(AgenteBiExecucao.id == execucao_id)
    candidatos = consulta.order_by(AgenteBiExecucao.id.asc()).all()
    for candidato in candidatos:
        anterior = candidato.estado
        fencing = int(candidato.fencing)
        resultado = db.session.execute(
            update(AgenteBiExecucao)
            .where(
                AgenteBiExecucao.id == candidato.id,
                AgenteBiExecucao.estado == anterior,
                AgenteBiExecucao.fencing == fencing,
                AgenteBiExecucao.efeito_externo == "nenhum",
            )
            .values(
                estado="adquirida",
                fencing=AgenteBiExecucao.fencing + 1,
                lease_worker=worker,
                lease_ate=agora + timedelta(seconds=lease),
                adquirida_em=agora,
                tentativas=AgenteBiExecucao.tentativas + 1,
                ultimo_erro=None,
                updated_at=agora,
            )
        )
        if int(resultado.rowcount or 0) != 1:
            continue
        db.session.expire(candidato)
        db.session.refresh(candidato)
        _depois_da_aquisicao(candidato, anterior, agora)
        return {"id": candidato.id}
    return None


def _depois_da_aquisicao(linha: AgenteBiExecucao, estado_anterior: str, agora: datetime) -> None:
    if not transicao_permitida(estado_anterior, "adquirida"):
        raise ExecucaoErro("estado_invalido")
    _registrar_evento(linha, estado_anterior, "adquirida", codigo="adquirida")
    analise = _analise_da_execucao(linha)
    _sincronizar_analise(analise, "adquirida", agora=agora)


def _expirar_execucoes() -> None:
    identidade = db.session.identity_map
    estados = identidade.all_states() if hasattr(identidade, "all_states") else list(identidade.values())
    for estado in list(estados):
        objeto = estado.obj() if hasattr(estado, "obj") else estado
        if isinstance(objeto, AgenteBiExecucao):
            db.session.expire(objeto)


def _atualizar_condicional(condicoes, valores) -> bool:
    """Uma sentença: a condição e a gravação viajam juntas. Não altera o ORM antes."""
    _expirar_execucoes()
    sentenca = (
        update(AgenteBiExecucao)
        .where(*condicoes)
        .values(**valores)
        .returning(AgenteBiExecucao.id)
        .execution_options(synchronize_session=False)
    )
    return db.session.execute(sentenca).first() is not None


def _chamada_externa_contabilizada(execucao_id: int) -> bool:
    """A fila não inspeciona tentativa nem evento. Só pergunta ao adaptador."""
    from app.services.agente_bi_anthropic_service import chamada_externa_contabilizada

    return chamada_externa_contabilizada(int(execucao_id))


def _extra_da_confirmacao(execucao_id: int, documento) -> list:
    """O efeito incerto só vira sucesso junto com o documento já liquidado.

    Sem documento, ou com a chamada ainda incerta, a confirmação não passa.
    O contrato legado, sem tentativa, continua exigindo efeito nenhum.
    """
    if documento is not None and _chamada_liquidada(execucao_id):
        return [AgenteBiExecucao.efeito_externo.in_(("nenhum", "incerto"))]
    return [AgenteBiExecucao.efeito_externo != "incerto"]


def _chamada_liquidada(execucao_id: int) -> bool:
    from app.services.agente_bi_anthropic_service import chamada_liquidada

    return chamada_liquidada(int(execucao_id))


def _fechar_efeito_ja_seguro(
    execucao_id: int,
    *,
    worker_id: str,
    fencing: int,
    agora: datetime | None,
) -> dict | None:
    """Conclui a transição quando o efeito já foi segurado e o documento não entra."""
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        return None
    if linha.estado == "incerta" and linha.efeito_externo == "incerto":
        return execucao_para_dict(linha)
    if linha.estado != "adquirida":
        return None
    if linha.efeito_externo != "incerto" and not _chamada_externa_contabilizada(linha.id):
        return None
    try:
        return marcar_efeito_incerto(
            int(execucao_id),
            worker_id=worker_id,
            fencing=int(fencing),
            agora=agora,
            lease_vivo=False,
        )
    except ExecucaoErro:
        db.session.rollback()
        atual = db.session.get(AgenteBiExecucao, int(execucao_id))
        if atual is not None and atual.estado == "incerta" and atual.efeito_externo == "incerto":
            return execucao_para_dict(atual)
        return None


def _condicoes_dono(
    execucao_id: int,
    worker: str,
    fencing: int,
    agora: datetime,
    *,
    lease_vivo: bool = True,
) -> list:
    condicoes = [
        AgenteBiExecucao.id == int(execucao_id),
        AgenteBiExecucao.estado == "adquirida",
        AgenteBiExecucao.lease_worker == worker,
        AgenteBiExecucao.fencing == int(fencing),
    ]
    if lease_vivo:
        condicoes.append(AgenteBiExecucao.lease_ate > agora)
    return condicoes


def _publicar_transicao(
    execucao_id: int,
    *,
    origem: str,
    destino: str,
    codigo: str,
    agora: datetime,
    resultado: str | None = None,
    antes_do_commit=None,
) -> AgenteBiExecucao:
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    _registrar_evento(linha, origem, destino, codigo=codigo)
    analise = _analise_da_execucao(linha)
    db.session.refresh(analise)
    _sincronizar_analise(analise, destino, agora=agora, resultado=resultado)
    if antes_do_commit is not None:
        antes_do_commit()
    db.session.commit()
    return db.session.get(AgenteBiExecucao, int(execucao_id))


def _transicionar_dono(
    linha: AgenteBiExecucao,
    *,
    worker: str,
    fencing: int,
    agora: datetime,
    destino: str,
    codigo: str,
    valores: dict,
    codigo_resultado: str | None = None,
    codigo_falha: str | None = None,
    resultado: str | None = None,
    antes_do_commit=None,
    extra: list | None = None,
    lease_vivo: bool = True,
) -> AgenteBiExecucao:
    origem = linha.estado
    condicoes = _condicoes_dono(linha.id, worker, fencing, agora, lease_vivo=lease_vivo)
    if extra:
        condicoes.extend(extra)
    if not _atualizar_condicional(condicoes, valores):
        return _recusar_dono(
            linha.id,
            worker=worker,
            fencing=fencing,
            agora=agora,
            codigo_resultado=codigo_resultado,
            codigo_falha=codigo_falha,
            resultado=resultado,
        )
    try:
        return _publicar_transicao(
            linha.id,
            origem=origem,
            destino=destino,
            codigo=codigo,
            agora=agora,
            resultado=resultado,
            antes_do_commit=antes_do_commit,
        )
    except Exception:
        db.session.rollback()
        raise


def _recusar_dono(
    execucao_id: int,
    *,
    worker: str,
    fencing: int,
    agora: datetime,
    codigo_resultado: str | None = None,
    codigo_falha: str | None = None,
    resultado: str | None = None,
) -> AgenteBiExecucao:
    db.session.rollback()
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    if codigo_resultado is not None and _confirmacao_repetida(linha, fencing, codigo_resultado):
        if resultado is not None:
            _exigir_resultado_igual(_analise_da_execucao(linha), resultado)
        return linha
    if codigo_falha is not None and _falha_repetida(linha, fencing, codigo_falha):
        return linha
    if linha.estado == "incerta" and linha.efeito_externo == "incerto" and codigo_falha is None and codigo_resultado is None:
        return linha
    _exigir_dono(linha, worker_id=worker, fencing=fencing, agora=agora)
    if linha.efeito_externo == "incerto":
        raise ExecucaoErro("efeito_incerto")
    raise ExecucaoErro("fencing_recusado")


def _confirmacao_repetida(linha: AgenteBiExecucao, fencing: int, codigo: str) -> bool:
    return (
        linha.estado == "concluida"
        and int(linha.fencing) == int(fencing)
        and linha.codigo_resultado == codigo
    )


def _falha_repetida(linha: AgenteBiExecucao, fencing: int, erro: str) -> bool:
    return (
        linha.estado in {"falha_recuperavel", "falha_definitiva", "incerta"}
        and linha.ultimo_erro == erro
        and int(linha.fencing) == int(fencing)
    )


def _aplicar_estado(
    linha: AgenteBiExecucao,
    destino: str,
    *,
    codigo: str,
    agora: datetime,
    resultado: str | None = None,
) -> None:
    origem = linha.estado
    if not transicao_permitida(origem, destino):
        raise ExecucaoErro("estado_invalido")
    linha.estado = destino
    linha.updated_at = agora
    if destino in {"concluida", "falha_definitiva", "incerta", "cancelada", "falha_recuperavel"}:
        linha.lease_worker = None
        linha.lease_ate = None
    _registrar_evento(linha, origem, destino, codigo=codigo)
    analise = _analise_da_execucao(linha)
    _sincronizar_analise(analise, destino, agora=agora, resultado=resultado)


def _sincronizar_analise(
    analise: AgenteBiAnalise,
    estado_execucao: str,
    *,
    agora: datetime,
    resultado: str | None = None,
) -> None:
    analise.estado = _ANALISE_POR_ESTADO[estado_execucao]
    analise.updated_at = agora
    if estado_execucao == "adquirida":
        analise.etapa_jornada = "execucao"
        if analise.iniciado_em is None:
            analise.iniciado_em = agora
    if estado_execucao == "concluida":
        analise.concluido_em = agora
    if resultado is not None:
        if analise.resultado_json is None:
            analise.resultado_json = resultado
        elif analise.resultado_json != resultado:
            raise ExecucaoErro("historico_preservado")


def _registrar_evento(linha: AgenteBiExecucao, anterior: str | None, novo: str, *, codigo: str) -> None:
    db.session.add(
        AgenteBiExecucaoEvento(
            execucao_id=linha.id,
            estado_anterior=anterior,
            estado_novo=novo,
            fencing=int(linha.fencing),
            codigo=_codigo(codigo),
            created_at=utcnow_naive(),
        )
    )


def _exigir_dono(linha: AgenteBiExecucao, *, worker_id: str, fencing: int, agora: datetime) -> None:
    if linha.lease_worker != worker_id or int(linha.fencing) != int(fencing) or linha.estado != "adquirida":
        raise ExecucaoErro("fencing_recusado")
    if linha.lease_ate is None or linha.lease_ate <= agora:
        raise ExecucaoErro("lease_expirado")


def _analise_no_escopo(escopo: EscopoAgenteBi, analise_id: int) -> AgenteBiAnalise | None:
    return (
        AgenteBiAnalise.query.filter_by(
            id=int(analise_id),
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
        ).one_or_none()
    )


def _no_escopo(escopo: EscopoAgenteBi, execucao_id: int) -> AgenteBiExecucao | None:
    return (
        AgenteBiExecucao.query.filter_by(
            id=int(execucao_id),
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
        ).one_or_none()
    )


def _analise_da_execucao(linha: AgenteBiExecucao) -> AgenteBiAnalise:
    analise = AgenteBiAnalise.query.filter_by(
        id=linha.analise_id,
        usuario_id=linha.usuario_id,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
    ).one_or_none()
    if analise is None:
        raise ExecucaoErro("identidade_inconsistente")
    return analise


def _exigir_dataset_da_analise(analise: AgenteBiAnalise) -> AgenteBiDataset:
    dataset = AgenteBiDataset.query.filter_by(
        id=analise.dataset_id,
        usuario_id=analise.usuario_id,
        conta_id=analise.conta_id,
        franquia_id=analise.franquia_id,
        versao=analise.dataset_versao,
        impressao=analise.dataset_impressao,
    ).one_or_none()
    if dataset is None:
        raise ExecucaoErro("identidade_inconsistente")
    return dataset


def _referencias(linha: AgenteBiExecucao, analise: AgenteBiAnalise) -> dict:
    dataset = _exigir_dataset_da_analise(analise)
    return {
        "execucao_id": int(linha.id),
        "operacao_id": linha.operacao_id,
        "analise_id": int(analise.id),
        "dataset_id": int(dataset.id),
        "usuario_id": int(linha.usuario_id),
        "conta_id": int(linha.conta_id),
        "franquia_id": int(linha.franquia_id),
    }


def _referencias_por_id(execucao_id: int) -> dict:
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        raise ExecucaoErro("nao_encontrado")
    return _referencias(linha, _analise_da_execucao(linha))


def _entrega_do_processador(valor) -> tuple[str, dict | None]:
    """String continua sendo só o código. O documento viaja no objeto explícito."""
    if isinstance(valor, str):
        return valor, None
    if isinstance(valor, EntregaProcessador) and isinstance(valor.codigo, str):
        if valor.documento is not None and not isinstance(valor.documento, dict):
            raise ExecucaoErro("contrato_saida_invalido")
        return valor.codigo, valor.documento
    raise ExecucaoErro("codigo_invalido")


def _resultado_confirmado(
    codigo: str,
    referencias: dict,
    documento: dict | None,
    analise: AgenteBiAnalise,
) -> str:
    """Monta o JSON em memória. Não grava e não confirma.

    Documento ausente continua no contrato legado. Documento `analisar.v1`
    só segue depois da revalidação contra a análise e o dataset persistidos.
    """
    if documento is None:
        return _resultado_simulado(codigo, referencias)
    validado = _documento_analisar_revalidado(analise, referencias, documento)
    return json.dumps(
        validado,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _documento_analisar_revalidado(analise: AgenteBiAnalise, referencias: dict, documento) -> dict:
    """Reexecuta o contrato fechado. O marcador do documento não é prova."""
    if not isinstance(documento, dict):
        raise ExecucaoErro("contrato_saida_invalido")
    _exigir_trilha_persistida(documento.get("trilha"), analise, referencias)
    contexto = _contexto_persistido(analise, referencias)
    try:
        saida = validar_saida(contexto, documento)
    except AnalisarErro as exc:
        raise _erro_de_analise(exc) from exc
    _exigir_trilha_persistida(saida.get("trilha"), analise, referencias)
    return saida


def _contexto_persistido(analise: AgenteBiAnalise, referencias: dict) -> dict:
    """Recupera perfil e pedido da gravação. Não lê o documento recebido."""
    if int(referencias["dataset_id"]) != int(analise.dataset_id):
        raise ExecucaoErro("identidade_inconsistente")
    pedido = _pedido_persistido(analise)
    escopo = EscopoAgenteBi(
        conta_id=int(analise.conta_id),
        franquia_id=int(analise.franquia_id),
        usuario_id=int(analise.usuario_id),
    )
    try:
        perfil = perfilar_dataset(escopo, int(analise.dataset_id))
        contexto = montar_contexto(perfil, pedido)
    except (AnalisarErro, PerfilErro, AgenteBiErro) as exc:
        raise ExecucaoErro("identidade_inconsistente") from exc
    dataset = contexto.get("dataset") if isinstance(contexto.get("dataset"), dict) else {}
    if (
        dataset.get("id") != int(analise.dataset_id)
        or dataset.get("versao") != int(analise.dataset_versao)
        or dataset.get("impressao") != analise.dataset_impressao
    ):
        raise ExecucaoErro("identidade_inconsistente")
    return contexto


def _pedido_persistido(analise: AgenteBiAnalise) -> dict:
    try:
        regras = json.loads(analise.regras_negocio_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecucaoErro("identidade_inconsistente") from exc
    pedido = regras.get("pedido") if isinstance(regras, dict) else None
    if not isinstance(pedido, dict):
        raise ExecucaoErro("identidade_inconsistente")
    return pedido


def _exigir_trilha_persistida(trilha, analise: AgenteBiAnalise, referencias: dict) -> None:
    """A trilha tem de ser a da análise e a do dataset preso à execução."""
    if not isinstance(trilha, dict):
        raise ExecucaoErro("contrato_saida_invalido")
    dataset = _exigir_dataset_da_analise(analise)
    identidade_ok = (
        referencias.get("analise_id") == int(analise.id)
        and referencias.get("dataset_id") == int(dataset.id)
        and referencias.get("usuario_id") == int(analise.usuario_id)
        and referencias.get("conta_id") == int(analise.conta_id)
        and referencias.get("franquia_id") == int(analise.franquia_id)
        and int(analise.dataset_id) == int(dataset.id)
        and int(analise.dataset_versao) == int(dataset.versao)
        and analise.dataset_impressao == dataset.impressao
    )
    trilha_ok = (
        trilha.get("dataset_id") == int(dataset.id)
        and trilha.get("dataset_versao") == int(dataset.versao)
        and trilha.get("dataset_impressao") == dataset.impressao
    )
    if not identidade_ok or not trilha_ok:
        raise ExecucaoErro("identidade_inconsistente")


def _erro_de_analise(exc: AnalisarErro) -> ExecucaoErro:
    codigo = exc.codigo
    if isinstance(codigo, str) and _CODIGO_RE.fullmatch(codigo):
        return ExecucaoErro(codigo)
    return ExecucaoErro("contrato_saida_invalido")


def _exigir_resultado_igual(analise: AgenteBiAnalise, resultado: str) -> None:
    db.session.refresh(analise)
    if analise.resultado_json != resultado:
        db.session.rollback()
        raise ExecucaoErro("historico_preservado")


def _resultado_simulado(codigo: str, referencias: dict) -> str:
    return json.dumps(
        {
            "codigo": codigo,
            "contrato": "execucao_simulada.v1",
            "dataset_id": int(referencias["dataset_id"]),
            "operacao_id": referencias["operacao_id"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _por_chave(chave: str) -> AgenteBiExecucao | None:
    return AgenteBiExecucao.query.filter_by(chave_idempotencia=chave).one_or_none()


def _mesma_identidade(linha: AgenteBiExecucao, escopo: EscopoAgenteBi) -> bool:
    return (
        linha.usuario_id == escopo.usuario_id
        and linha.conta_id == escopo.conta_id
        and linha.franquia_id == escopo.franquia_id
    )


def _filtro_execucao(valor) -> int | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise ExecucaoErro("nao_encontrado")
    return valor


def _worker(valor: str) -> str:
    if not isinstance(valor, str) or _WORKER_RE.fullmatch(valor) is None:
        raise ExecucaoErro("worker_invalido")
    return valor


def _codigo(valor: str) -> str:
    if not isinstance(valor, str) or _CODIGO_RE.fullmatch(valor) is None:
        raise ExecucaoErro("codigo_invalido")
    return valor


def _lease(segundos: int) -> int:
    if not isinstance(segundos, int) or isinstance(segundos, bool) or segundos < 1 or segundos > LEASE_MAX_SEGUNDOS:
        raise ExecucaoErro("lease_invalido")
    return segundos
