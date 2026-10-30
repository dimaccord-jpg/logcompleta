"""Persistência inicial do AgenteBI.

Grava dataset versionado, rodada de análise e revisão de dashboard no banco.
Não processa arquivo, não chama IA e não cria tentativa financeira.

Um documento JSON continua limitado a 1,5 MB e 20 mil itens. Quando o
dataset preparado excede esse documento, os registros são gravados em
blocos da mesma versão, com manifesto e hash do conjunto. Bloco não é
versão nem preview. A gravação confirma todos os blocos ou nenhum.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy.exc import DataError, IntegrityError

from app.agente_bi_integridade import INTEIRO_COLUNA_MAX
from app.extensions import db
from app.models import (
    AGENTE_BI_ESTADOS_ANALISE_API,
    AGENTE_BI_ESTADOS_PUBLICACAO,
    AGENTE_BI_ETAPAS,
    AGENTE_BI_ORIGENS,
    AgenteBiAnalise,
    AgenteBiDashboard,
    AgenteBiDashboardDataset,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    IaChamadaTentativa,
    utcnow_naive,
)
from app.services.cleiton_ai_data_governance import (
    CONTENT_TYPE_DOCUMENT,
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_MINIMIZE,
    PURPOSE_AUDITORIA_FRETE,
    govern_outbound_content,
    governance_result_is_coherent,
)
from app.services.external_ai_masking import MASKABLE_FIELD_KEYS
from app.services.conta_multiuser_autorizacao_service import mesmo_owner_operacional

logger = logging.getLogger(__name__)

MAX_JSON_BYTES = 1_500_000
MAX_LISTA = 20_000
MAX_CHAVES = 80
MAX_TEXTO = 4_000
MAX_PROFUNDIDADE = 8
CONTRATO_DADOS_SEGMENTADOS = "dados.segmentado.v1"
_FOLGA_BLOCO = 8
LIMITE_CONSULTA = 50
LIMITE_GRAFICOS = 6
LIMITE_DATASETS_DASHBOARD = 12

_CHAVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
_ORIGEM_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CONTRATO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
_IMPRESSAO_RE = re.compile(r"^[0-9a-f]{64}$")
_RECUPERACAO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,79}$")
_REF_GRAFICO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")

_TOKENS_PROIBIDOS = frozenset(
    {
        "cpf",
        "email",
        "telefone",
        "phone",
        "celular",
        "placa",
        "senha",
        "password",
        "prompt",
        "prompts",
        "sql",
        "python",
    }
)
_COMPOSTOS_PROIBIDOS = frozenset(
    {
        "api_key",
        "prompt_bruto",
        "raw_prompt",
        "system_prompt",
        "codigo_fonte",
    }
)
_ORIGEM_REF_BLOQUEADA = ("cleide_upload", "cleiton_doc", "tempfile", "file:")
TIPOS_GRAFICO = frozenset({"barra", "linha", "pizza", "tabela", "indicador", "dispersao"})


class AgenteBiErro(Exception):
    def __init__(self, codigo: str, status: int):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


@dataclass(frozen=True)
class EscopoAgenteBi:
    conta_id: int
    franquia_id: int
    usuario_id: int


def escopo_do_usuario(usuario) -> EscopoAgenteBi:
    """Conta, franquia e usuário dono. Mesma conta, sozinha, não fecha o escopo."""
    if usuario is None or not getattr(usuario, "is_authenticated", False):
        raise AgenteBiErro("autenticacao_necessaria", 401)
    conta_id = _inteiro_ou_none(getattr(usuario, "conta_id", None))
    franquia_id = _inteiro_ou_none(getattr(usuario, "franquia_id", None))
    usuario_id = _inteiro_ou_none(getattr(usuario, "id", None))
    if conta_id is None or franquia_id is None or usuario_id is None:
        raise AgenteBiErro("identidade_incompleta", 403)
    if conta_id < 1 or franquia_id < 1 or usuario_id < 1:
        raise AgenteBiErro("identidade_incompleta", 403)
    return EscopoAgenteBi(conta_id=conta_id, franquia_id=franquia_id, usuario_id=usuario_id)


def calcular_impressao_dataset(
    *,
    chave: str,
    origem: str,
    origem_ref: str,
    versao: int,
    estrutura: dict,
    perfil: dict,
    dados_tipados,
    metadados_preparacao: dict,
) -> str:
    material = {
        "chave": chave,
        "dados_tipados": dados_tipados,
        "estrutura": estrutura,
        "metadados_preparacao": metadados_preparacao,
        "origem": origem,
        "origem_ref": origem_ref,
        "perfil": perfil,
        "versao": versao,
    }
    return hashlib.sha256(_canonico(material).encode("utf-8")).hexdigest()


def criar_dataset(escopo: EscopoAgenteBi, payload: dict, *, ponto_de_controle=None) -> dict:
    dados = _exigir_objeto(
        payload,
        obrigatorias={
            "chave",
            "origem",
            "origem_ref",
            "versao",
            "impressao",
            "estrutura",
            "perfil",
            "dados_tipados",
            "metadados_preparacao",
        },
        opcionais={"versao_anterior_id"},
    )
    chave = _texto_exato(dados["chave"], padrao=_CHAVE_RE)
    origem = _escolha(dados["origem"], AGENTE_BI_ORIGENS)
    origem_ref = _texto_exato(dados["origem_ref"], padrao=_ORIGEM_REF_RE)
    if any(marca in origem_ref for marca in _ORIGEM_REF_BLOQUEADA):
        raise AgenteBiErro("payload_invalido", 422)
    versao = _inteiro_minimo(dados["versao"], minimo=1)
    ponto = _acionar(ponto_de_controle)
    ponto()
    estrutura = _json_objeto(dados["estrutura"])
    perfil = _json_objeto(dados["perfil"])
    dados_tipados, coluna_dados, blocos = _planejar_dados(dados["dados_tipados"], ponto)
    metadados = _json_objeto(dados["metadados_preparacao"])
    ponto()
    impressao = calcular_impressao_dataset(
        chave=chave,
        origem=origem,
        origem_ref=origem_ref,
        versao=versao,
        estrutura=estrutura,
        perfil=perfil,
        dados_tipados=dados_tipados,
        metadados_preparacao=metadados,
    )
    if not isinstance(dados["impressao"], str) or dados["impressao"] != impressao:
        raise AgenteBiErro("identidade_invalida", 409)

    versao_anterior_id = _inteiro_opcional(dados.get("versao_anterior_id"))
    if versao == 1 and versao_anterior_id is not None:
        raise AgenteBiErro("identidade_invalida", 409)
    if versao > 1 and versao_anterior_id is None:
        raise AgenteBiErro("identidade_invalida", 409)
    if versao_anterior_id is not None:
        anterior = _obter_no_escopo(AgenteBiDataset, escopo, versao_anterior_id)
        if anterior is None:
            raise AgenteBiErro("nao_encontrado", 404)
        if anterior.chave != chave or anterior.versao != versao - 1:
            raise AgenteBiErro("identidade_invalida", 409)
        if not _linha_no_escopo(anterior, escopo):
            raise AgenteBiErro("nao_encontrado", 404)

    if _consulta(AgenteBiDataset, escopo).filter_by(chave=chave, versao=versao).first() is not None:
        raise AgenteBiErro("versao_imutavel", 409)

    agora = utcnow_naive()
    linha = AgenteBiDataset(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        chave=chave,
        origem=origem,
        origem_ref=origem_ref,
        versao=versao,
        impressao=impressao,
        estrutura_json=_canonico(estrutura),
        perfil_json=_canonico(perfil),
        dados_tipados_json=coluna_dados,
        metadados_preparacao_json=_canonico(metadados),
        versao_anterior_id=versao_anterior_id,
        created_at=agora,
        updated_at=agora,
    )
    _gravar_dataset(linha, blocos, escopo, ponto)
    logger.info(
        "evento=agente_bi_dataset_criado usuario_id=%s dataset_id=%s versao=%s",
        escopo.usuario_id,
        linha.id,
        linha.versao,
    )
    if blocos:
        logger.info(
            "evento=agente_bi_dataset_segmentado dataset_id=%s blocos=%s registros=%s",
            linha.id,
            len(blocos),
            sum(item.registros for item in blocos),
        )
    return dataset_para_dict(linha, completo=True)


def obter_dataset(escopo: EscopoAgenteBi, dataset_id: int) -> dict:
    linha = _obter_no_escopo(AgenteBiDataset, escopo, dataset_id)
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    return dataset_para_dict(linha, completo=True)


def listar_datasets(escopo: EscopoAgenteBi, chave: str | None = None) -> list[dict]:
    consulta = _consulta(AgenteBiDataset, escopo)
    if chave is not None:
        if not isinstance(chave, str) or _CHAVE_RE.fullmatch(chave) is None:
            raise AgenteBiErro("payload_invalido", 422)
        consulta = consulta.filter_by(chave=chave)
    linhas = consulta.order_by(AgenteBiDataset.id.desc()).limit(LIMITE_CONSULTA).all()
    return [
        dataset_para_dict(linha, completo=False)
        for linha in linhas
        if _linha_no_escopo(linha, escopo)
    ]


def criar_analise(escopo: EscopoAgenteBi, payload: dict, *, confirmar: bool = True) -> dict:
    dados = _exigir_objeto(
        payload,
        obrigatorias={
            "chave",
            "rodada",
            "dataset_id",
            "versao",
            "impressao",
            "etapa",
            "estado",
            "contexto",
            "regras_negocio",
            "esclarecimentos",
            "contrato_versao",
        },
        opcionais={"resultado", "ia_chamada_tentativa_id", "chave_recuperacao"},
    )
    chave = _texto_exato(dados["chave"], padrao=_CHAVE_RE)
    rodada = _inteiro_minimo(dados["rodada"], minimo=1)
    dataset_id = _inteiro_minimo(dados["dataset_id"], minimo=1)
    versao = _inteiro_minimo(dados["versao"], minimo=1)
    impressao = dados["impressao"]
    if not isinstance(impressao, str) or _IMPRESSAO_RE.fullmatch(impressao) is None:
        raise AgenteBiErro("identidade_invalida", 409)
    etapa = _escolha(dados["etapa"], AGENTE_BI_ETAPAS)
    estado = _escolha(dados["estado"], AGENTE_BI_ESTADOS_ANALISE_API)
    contexto = _json_objeto(dados["contexto"])
    regras = _json_objeto(dados["regras_negocio"])
    esclarecimentos = _json_objeto(dados["esclarecimentos"])
    contrato = _texto_exato(dados["contrato_versao"], padrao=_CONTRATO_RE)
    resultado = dados.get("resultado", None)
    if estado == "concluida":
        if resultado is None:
            raise AgenteBiErro("payload_invalido", 422)
        resultado = _json_objeto(resultado)
    elif resultado is not None:
        raise AgenteBiErro("payload_invalido", 422)

    dataset = _obter_no_escopo(AgenteBiDataset, escopo, dataset_id)
    if dataset is None:
        raise AgenteBiErro("nao_encontrado", 404)
    if dataset.versao != versao or dataset.impressao != impressao:
        raise AgenteBiErro("identidade_invalida", 409)

    tentativa_id = _inteiro_opcional(dados.get("ia_chamada_tentativa_id"))
    if tentativa_id is not None:
        tentativa = db.session.get(IaChamadaTentativa, tentativa_id)
        if tentativa is None or not _tentativa_no_escopo(tentativa, escopo):
            raise AgenteBiErro("nao_encontrado", 404)

    recuperacao = dados.get("chave_recuperacao", None)
    if recuperacao is not None:
        recuperacao = _texto_exato(recuperacao, padrao=_RECUPERACAO_RE)
        existente = (
            _consulta(AgenteBiAnalise, escopo)
            .filter_by(chave_recuperacao=recuperacao)
            .first()
        )
        if existente is not None:
            raise AgenteBiErro("chave_recuperacao_existente", 409)

    if _consulta(AgenteBiAnalise, escopo).filter_by(chave=chave, rodada=rodada).first() is not None:
        raise AgenteBiErro("rodada_existente", 409)

    agora = utcnow_naive()
    linha = AgenteBiAnalise(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        chave=chave,
        rodada=rodada,
        dataset_id=dataset.id,
        dataset_versao=dataset.versao,
        dataset_impressao=dataset.impressao,
        estado=estado,
        etapa_jornada=etapa,
        contexto_json=_canonico(contexto),
        regras_negocio_json=_canonico(regras),
        esclarecimentos_json=_canonico(esclarecimentos),
        resultado_json=None if resultado is None else _canonico(resultado),
        contrato_versao=contrato,
        ia_chamada_tentativa_id=tentativa_id,
        chave_recuperacao=recuperacao,
        iniciado_em=agora,
        concluido_em=agora if estado != "preparada" else None,
        created_at=agora,
        updated_at=agora,
    )
    _inserir(linha, confirmar=confirmar)
    logger.info(
        "evento=agente_bi_analise_criada usuario_id=%s analise_id=%s rodada=%s",
        escopo.usuario_id,
        linha.id,
        linha.rodada,
    )
    return analise_para_dict(linha)


def obter_analise(escopo: EscopoAgenteBi, analise_id: int) -> dict:
    linha = _obter_no_escopo(AgenteBiAnalise, escopo, analise_id)
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    return analise_para_dict(linha)


def listar_analises(escopo: EscopoAgenteBi, chave: str | None = None) -> list[dict]:
    consulta = _consulta(AgenteBiAnalise, escopo)
    if chave is not None:
        if not isinstance(chave, str) or _CHAVE_RE.fullmatch(chave) is None:
            raise AgenteBiErro("payload_invalido", 422)
        consulta = consulta.filter_by(chave=chave)
    linhas = consulta.order_by(AgenteBiAnalise.id.desc()).limit(LIMITE_CONSULTA).all()
    return [analise_para_dict(linha) for linha in linhas if _linha_no_escopo(linha, escopo)]


def criar_dashboard(escopo: EscopoAgenteBi, payload: dict, *, confirmar: bool = True) -> dict:
    dados = _exigir_objeto(
        payload,
        obrigatorias={
            "chave",
            "revisao",
            "estado_publicacao",
            "contrato_versao",
            "executor_versao",
            "definicoes_graficos",
            "datasets",
        },
        opcionais={"analise_id"},
    )
    chave = _texto_exato(dados["chave"], padrao=_CHAVE_RE)
    revisao = _inteiro_minimo(dados["revisao"], minimo=1)
    estado = _escolha(dados["estado_publicacao"], AGENTE_BI_ESTADOS_PUBLICACAO)
    contrato = _texto_exato(dados["contrato_versao"], padrao=_CONTRATO_RE)
    executor = _texto_exato(dados["executor_versao"], padrao=_CONTRATO_RE)
    definicoes = _validar_definicoes(dados["definicoes_graficos"], estado_publicacao=estado)
    refs = _validar_datasets_dashboard(escopo, dados["datasets"])
    analise_id = _inteiro_opcional(dados.get("analise_id"))
    if analise_id is not None:
        analise = _obter_no_escopo(AgenteBiAnalise, escopo, analise_id)
        if analise is None:
            raise AgenteBiErro("nao_encontrado", 404)
        from app.services.agente_bi_colaboracao_service import CONTRATO_TECNICO

        if analise.contrato_versao == CONTRATO_TECNICO:
            raise AgenteBiErro("identidade_invalida", 409)
        if not any(ref["dataset_id"] == analise.dataset_id for ref in refs):
            raise AgenteBiErro("identidade_invalida", 409)

    if revisao > 1:
        anterior = (
            _consulta(AgenteBiDashboard, escopo)
            .filter_by(chave=chave, revisao=revisao - 1)
            .first()
        )
        if anterior is None or not _linha_no_escopo(anterior, escopo):
            raise AgenteBiErro("cadeia_revisao_invalida", 409)
    if _consulta(AgenteBiDashboard, escopo).filter_by(chave=chave, revisao=revisao).first() is not None:
        raise AgenteBiErro("revisao_existente", 409)

    agora = utcnow_naive()
    linha = AgenteBiDashboard(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        chave=chave,
        revisao=revisao,
        estado_publicacao=estado,
        contrato_versao=contrato,
        executor_versao=executor,
        definicoes_json=_canonico(definicoes),
        datasets_json=_canonico(refs),
        analise_id=analise_id,
        created_at=agora,
        updated_at=agora,
    )
    db.session.add(linha)
    try:
        db.session.flush()
        for ref in refs:
            db.session.add(
                AgenteBiDashboardDataset(
                    dashboard_id=linha.id,
                    dataset_id=ref["dataset_id"],
                    conta_id=escopo.conta_id,
                    franquia_id=escopo.franquia_id,
                    usuario_id=escopo.usuario_id,
                    dataset_versao=ref["versao"],
                    dataset_impressao=ref["impressao"],
                    created_at=agora,
                )
            )
        if confirmar:
            db.session.commit()
        else:
            db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise AgenteBiErro("conflito_versao", 409) from exc
    except DataError as exc:
        db.session.rollback()
        raise AgenteBiErro("payload_invalido", 422) from exc
    logger.info(
        "evento=agente_bi_dashboard_criado usuario_id=%s dashboard_id=%s revisao=%s",
        escopo.usuario_id,
        linha.id,
        linha.revisao,
    )
    return dashboard_para_dict(linha)


def obter_dashboard(escopo: EscopoAgenteBi, dashboard_id: int) -> dict:
    linha = _obter_no_escopo(AgenteBiDashboard, escopo, dashboard_id)
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    return dashboard_para_dict(linha)


def listar_dashboards(escopo: EscopoAgenteBi, chave: str | None = None) -> list[dict]:
    consulta = _consulta(AgenteBiDashboard, escopo)
    if chave is not None:
        if not isinstance(chave, str) or _CHAVE_RE.fullmatch(chave) is None:
            raise AgenteBiErro("payload_invalido", 422)
        consulta = consulta.filter_by(chave=chave)
    linhas = consulta.order_by(AgenteBiDashboard.id.desc()).limit(LIMITE_CONSULTA).all()
    return [dashboard_para_dict(linha) for linha in linhas if _linha_no_escopo(linha, escopo)]


def dataset_para_dict(linha: AgenteBiDataset, *, completo: bool) -> dict:
    corpo = {
        "id": linha.id,
        "conta_id": linha.conta_id,
        "franquia_id": linha.franquia_id,
        "usuario_id": linha.usuario_id,
        "chave": linha.chave,
        "origem": linha.origem,
        "origem_ref": linha.origem_ref,
        "versao": linha.versao,
        "impressao": linha.impressao,
        "versao_anterior_id": linha.versao_anterior_id,
        "created_at": linha.created_at.isoformat(),
        "updated_at": linha.updated_at.isoformat(),
    }
    if completo:
        corpo["estrutura"] = json.loads(linha.estrutura_json)
        corpo["perfil"] = json.loads(linha.perfil_json)
        corpo["dados_tipados"] = _dados_do_armazenamento(linha)
        corpo["metadados_preparacao"] = json.loads(linha.metadados_preparacao_json)
    return corpo


def analise_para_dict(linha: AgenteBiAnalise) -> dict:
    return {
        "id": linha.id,
        "conta_id": linha.conta_id,
        "franquia_id": linha.franquia_id,
        "usuario_id": linha.usuario_id,
        "chave": linha.chave,
        "rodada": linha.rodada,
        "dataset_id": linha.dataset_id,
        "versao": linha.dataset_versao,
        "impressao": linha.dataset_impressao,
        "estado": linha.estado,
        "etapa": linha.etapa_jornada,
        "contexto": json.loads(linha.contexto_json),
        "regras_negocio": json.loads(linha.regras_negocio_json),
        "esclarecimentos": json.loads(linha.esclarecimentos_json),
        "resultado": None if linha.resultado_json is None else json.loads(linha.resultado_json),
        "contrato_versao": linha.contrato_versao,
        "ia_chamada_tentativa_id": linha.ia_chamada_tentativa_id,
        "chave_recuperacao": linha.chave_recuperacao,
        "iniciado_em": None if linha.iniciado_em is None else linha.iniciado_em.isoformat(),
        "concluido_em": None if linha.concluido_em is None else linha.concluido_em.isoformat(),
        "created_at": linha.created_at.isoformat(),
        "updated_at": linha.updated_at.isoformat(),
    }


def dashboard_para_dict(linha: AgenteBiDashboard) -> dict:
    return {
        "id": linha.id,
        "conta_id": linha.conta_id,
        "franquia_id": linha.franquia_id,
        "usuario_id": linha.usuario_id,
        "chave": linha.chave,
        "revisao": linha.revisao,
        "estado_publicacao": linha.estado_publicacao,
        "contrato_versao": linha.contrato_versao,
        "executor_versao": linha.executor_versao,
        "definicoes_graficos": json.loads(linha.definicoes_json),
        "datasets": _datasets_da_revisao(linha),
        "analise_id": linha.analise_id,
        "created_at": linha.created_at.isoformat(),
        "updated_at": linha.updated_at.isoformat(),
    }


def _datasets_da_revisao(linha: AgenteBiDashboard) -> list[dict]:
    """A tabela de vínculo é a referência. O JSON só cobre revisão sem vínculo."""
    vinculos = (
        AgenteBiDashboardDataset.query.filter_by(dashboard_id=linha.id)
        .order_by(AgenteBiDashboardDataset.id.asc())
        .all()
    )
    if vinculos:
        return [
            {
                "dataset_id": item.dataset_id,
                "versao": item.dataset_versao,
                "impressao": item.dataset_impressao,
            }
            for item in vinculos
        ]
    return json.loads(linha.datasets_json)


def _inserir(linha, *, confirmar: bool = True) -> None:
    db.session.add(linha)
    if confirmar:
        _commit_ou_erro()
        return
    _flush_sem_commit()


def _flush_sem_commit() -> None:
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise AgenteBiErro("conflito_versao", 409) from exc
    except DataError as exc:
        db.session.rollback()
        raise AgenteBiErro("payload_invalido", 422) from exc


def _commit_ou_erro() -> None:
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise AgenteBiErro("conflito_versao", 409) from exc
    except DataError as exc:
        db.session.rollback()
        raise AgenteBiErro("payload_invalido", 422) from exc


def _consulta(modelo, escopo: EscopoAgenteBi):
    return modelo.query.filter_by(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
    )


def _obter_no_escopo(modelo, escopo: EscopoAgenteBi, linha_id: int):
    if not isinstance(linha_id, int) or isinstance(linha_id, bool) or linha_id < 1:
        return None
    linha = _consulta(modelo, escopo).filter_by(id=int(linha_id)).one_or_none()
    if linha is None or not _linha_no_escopo(linha, escopo):
        return None
    return linha


def _ator(escopo: EscopoAgenteBi):
    return SimpleNamespace(
        id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
    )


def _linha_no_escopo(linha, escopo: EscopoAgenteBi) -> bool:
    return mesmo_owner_operacional(
        _ator(escopo),
        usuario_id=linha.usuario_id,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
    )


def _tentativa_no_escopo(tentativa: IaChamadaTentativa, escopo: EscopoAgenteBi) -> bool:
    if tentativa.usuario_id is None or tentativa.conta_id is None or tentativa.franquia_id is None:
        return False
    return mesmo_owner_operacional(
        _ator(escopo),
        usuario_id=int(tentativa.usuario_id),
        conta_id=int(tentativa.conta_id),
        franquia_id=int(tentativa.franquia_id),
    )


def _exigir_objeto(payload, *, obrigatorias: set[str], opcionais: set[str]) -> dict:
    if not isinstance(payload, dict):
        raise AgenteBiErro("payload_invalido", 422)
    desconhecidas = set(payload) - obrigatorias - opcionais
    if desconhecidas or any(nome not in payload for nome in obrigatorias):
        raise AgenteBiErro("payload_invalido", 422)
    return payload


def _texto_exato(valor, *, padrao: re.Pattern[str]) -> str:
    if not isinstance(valor, str) or padrao.fullmatch(valor) is None:
        raise AgenteBiErro("payload_invalido", 422)
    return valor


def _inteiro_ou_none(valor) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor


def _inteiro_minimo(valor, *, minimo: int, maximo: int = INTEIRO_COLUNA_MAX) -> int:
    if (
        isinstance(valor, bool)
        or not isinstance(valor, int)
        or valor < minimo
        or valor > maximo
    ):
        raise AgenteBiErro("payload_invalido", 422)
    return valor


def _escolha(valor, permitidos) -> str:
    if not isinstance(valor, str) or valor not in permitidos:
        raise AgenteBiErro("payload_invalido", 422)
    return valor


def _inteiro_opcional(valor) -> int | None:
    if valor is None:
        return None
    return _inteiro_minimo(valor, minimo=1)


def _json_objeto(valor) -> dict:
    if not isinstance(valor, dict):
        raise AgenteBiErro("payload_invalido", 422)
    _validar_json_controlado(valor, profundidade=0)
    _recusar_volume(valor)
    _autorizar_conteudo_persistivel(valor)
    return valor


def _json_dados(valor):
    if not isinstance(valor, (dict, list)):
        raise AgenteBiErro("payload_invalido", 422)
    _validar_json_controlado(valor, profundidade=0)
    _recusar_volume(valor)
    _autorizar_conteudo_persistivel(valor)
    return valor


class _BlocoPronto:
    def __init__(self, indice: int, registros: int, sha256: str, conteudo: str):
        self.indice = indice
        self.registros = registros
        self.sha256 = sha256
        self.conteudo = conteudo


def _acionar(controle):
    def ponto() -> None:
        if controle is not None:
            controle()

    return ponto


def _documento_cabe(valor) -> bool:
    try:
        _validar_json_controlado(valor, profundidade=0)
        _recusar_volume(valor)
    except AgenteBiErro as exc:
        if exc.codigo != "payload_invalido":
            raise
        return False
    return True


def _planejar_dados(valor, ponto):
    """Mantém o objeto completo para o hash e decide a coluna gravada.

    Lista que cabe no documento único segue o caminho de antes. Lista maior
    é fatiada em blocos que, cada um, continuam dentro do mesmo contrato.
    """
    if isinstance(valor, list) and not _documento_cabe(valor):
        blocos = _fatiar_registros(valor, ponto)
        manifesto = _manifesto(valor, blocos)
        ponto()
        _json_objeto(manifesto)
        return valor, _canonico(manifesto), blocos
    ponto()
    pronto = _json_dados(valor)
    return pronto, _canonico(pronto), []


def _fatiar_registros(dados: list, ponto) -> list[_BlocoPronto]:
    orcamento = MAX_JSON_BYTES - _FOLGA_BLOCO
    grupos: list[list] = []
    atual: list = []
    peso = 2
    for indice, item in enumerate(dados):
        if indice % 5000 == 0:
            ponto()
        canon_item = _canonico(item).encode("utf-8")
        if len(canon_item) + 2 > MAX_JSON_BYTES:
            raise AgenteBiErro("payload_invalido", 422)
        extra = len(canon_item) + (1 if atual else 0)
        if atual and (len(atual) >= MAX_LISTA or peso + extra > orcamento):
            grupos.append(atual)
            atual = []
            peso = 2
            extra = len(canon_item)
        atual.append(item)
        peso += extra
    if atual:
        grupos.append(atual)
    if not grupos:
        raise AgenteBiErro("payload_invalido", 422)
    prontos = []
    for indice, grupo in enumerate(grupos):
        ponto()
        _validar_json_controlado(grupo, profundidade=0)
        conteudo = _canonico(grupo)
        bruto = conteudo.encode("utf-8")
        if len(bruto) > MAX_JSON_BYTES or len(grupo) > MAX_LISTA:
            raise AgenteBiErro("payload_invalido", 422)
        _autorizar_conteudo_persistivel(grupo)
        prontos.append(
            _BlocoPronto(
                indice=indice,
                registros=len(grupo),
                sha256=hashlib.sha256(bruto).hexdigest(),
                conteudo=conteudo,
            )
        )
    return prontos


def _manifesto(dados: list, blocos: list[_BlocoPronto]) -> dict:
    digest = hashlib.sha256()
    digest.update(b"[")
    for indice, item in enumerate(dados):
        if indice:
            digest.update(b",")
        digest.update(_canonico(item).encode("utf-8"))
    digest.update(b"]")
    return {
        "armazenamento": "segmentado",
        "contrato": CONTRATO_DADOS_SEGMENTADOS,
        "registros": sum(bloco.registros for bloco in blocos),
        "sha256": digest.hexdigest(),
        "blocos": [
            {
                "indice": bloco.indice,
                "registros": bloco.registros,
                "bytes": len(bloco.conteudo.encode("utf-8")),
                "sha256": bloco.sha256,
            }
            for bloco in blocos
        ],
    }


def _gravar_dataset(linha: AgenteBiDataset, blocos: list[_BlocoPronto], escopo: EscopoAgenteBi, ponto) -> None:
    ponto()
    try:
        db.session.add(linha)
        if blocos:
            db.session.flush()
            for bloco in blocos:
                db.session.add(
                    AgenteBiDatasetBloco(
                        dataset_id=linha.id,
                        conta_id=escopo.conta_id,
                        franquia_id=escopo.franquia_id,
                        usuario_id=escopo.usuario_id,
                        indice=bloco.indice,
                        registros=bloco.registros,
                        sha256=bloco.sha256,
                        conteudo_json=bloco.conteudo,
                        created_at=linha.created_at,
                    )
                )
        ponto()
        _commit_ou_erro()
    except Exception:
        db.session.rollback()
        raise


def _eh_manifesto_segmentado(bruto) -> bool:
    return (
        isinstance(bruto, dict)
        and bruto.get("armazenamento") == "segmentado"
        and bruto.get("contrato") == CONTRATO_DADOS_SEGMENTADOS
    )


def _dados_do_armazenamento(linha: AgenteBiDataset):
    try:
        bruto = json.loads(linha.dados_tipados_json)
    except json.JSONDecodeError as exc:
        raise AgenteBiErro("integridade_dados", 409) from exc
    blocos = (
        AgenteBiDatasetBloco.query.filter_by(
            dataset_id=linha.id,
            conta_id=linha.conta_id,
            franquia_id=linha.franquia_id,
            usuario_id=linha.usuario_id,
        )
        .order_by(AgenteBiDatasetBloco.indice.asc(), AgenteBiDatasetBloco.id.asc())
        .all()
    )
    if _eh_manifesto_segmentado(bruto):
        return _recompor_segmentado(bruto, blocos, linha)
    if blocos:
        raise AgenteBiErro("integridade_dados", 409)
    return bruto


def _recompor_segmentado(bruto: dict, blocos: list, linha: AgenteBiDataset):
    """Recompõe o dataset. Manifesto sem os blocos esperados não vira dado."""
    descritores = bruto.get("blocos")
    if not isinstance(descritores, list) or not descritores or len(descritores) != len(blocos):
        raise AgenteBiErro("integridade_dados", 409)
    ordenados = sorted(blocos, key=lambda bloco: (bloco.indice, getattr(bloco, "id", 0) or 0))
    indices = [bloco.indice for bloco in ordenados]
    if indices != list(range(len(ordenados))) or len(set(indices)) != len(indices):
        raise AgenteBiErro("integridade_dados", 409)
    dados = []
    for posicao, (descritor, bloco) in enumerate(zip(descritores, ordenados)):
        if not isinstance(descritor, dict) or descritor.get("indice") != posicao or bloco.indice != posicao:
            raise AgenteBiErro("integridade_dados", 409)
        if descritor.get("sha256") != bloco.sha256:
            raise AgenteBiErro("integridade_dados", 409)
        if hashlib.sha256(bloco.conteudo_json.encode("utf-8")).hexdigest() != bloco.sha256:
            raise AgenteBiErro("integridade_dados", 409)
        try:
            parte = json.loads(bloco.conteudo_json)
        except json.JSONDecodeError as exc:
            raise AgenteBiErro("integridade_dados", 409) from exc
        if not isinstance(parte, list) or len(parte) != bloco.registros:
            raise AgenteBiErro("integridade_dados", 409)
        dados.extend(parte)
    if len(dados) != bruto.get("registros"):
        raise AgenteBiErro("integridade_dados", 409)
    completo = hashlib.sha256(_canonico(dados).encode("utf-8")).hexdigest()
    if completo != bruto.get("sha256"):
        raise AgenteBiErro("integridade_dados", 409)
    escopo = EscopoAgenteBi(linha.conta_id, linha.franquia_id, linha.usuario_id)
    for bloco in ordenados:
        if not _linha_no_escopo(bloco, escopo):
            raise AgenteBiErro("integridade_dados", 409)
    return dados


def redigir_dados_nao_autorizados(dados):
    """Tira da resposta o que a governança não autoriza intacto. Não grava."""
    if not isinstance(dados, list):
        return dados
    saida = []
    for registro in dados:
        ajustado = _suprimir_celulas(registro)
        saida.append(ajustado if isinstance(ajustado, dict) else registro)
    return saida


def representar_dataset_autorizado(estrutura, perfil, dados, metadados):
    """Prepara a representação que pode ser selada.

    A governança oficial decide a minimização. Esta função só aceita o
    resultado quando a estrutura, as coordenadas e uma segunda passagem
    reproduzem o mesmo conteúdo. Número mascarado deixa de ser medida.
    Se a prova falha, isola a célula ou a linha; se nem isso fecha, recusa
    sem gravar. Não devolve o valor original sensível.
    """
    embrulho = {
        "estrutura": estrutura,
        "perfil": perfil,
        "dados_tipados": dados,
        "metadados_preparacao": metadados,
    }
    promovido, originais = _promover_numeros_longos(embrulho)
    autorizado = _minimizar_documento(promovido)
    if autorizado is None:
        isolado = _isolar_dados(promovido)
        if isolado is None:
            return None, "conteudo_nao_permitido"
        autorizado = _minimizar_documento(isolado)
        if autorizado is None:
            return None, "minimizacao_nao_comprovada"
    restaurado = _restaurar_numeros_inalterados(autorizado, originais)
    if restaurado is None:
        return None, "minimizacao_nao_comprovada"
    selado = _selar_protecao(embrulho, restaurado)
    if selado is None:
        return None, "minimizacao_nao_comprovada"
    return selado, None


def _governar_documento(valor):
    try:
        return govern_outbound_content(
            valor,
            purpose=PURPOSE_AUDITORIA_FRETE,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent="agente_bi",
            provider="persistencia_local",
        )
    except Exception:
        return None


def _resultado_confiavel(resultado, original) -> bool:
    return (
        resultado is not None
        and resultado.issued_by_cleiton
        and resultado.decision in {DECISION_ALLOW, DECISION_MINIMIZE, DECISION_BLOCK}
        and governance_result_is_coherent(resultado, original=original)
    )


_ISOLAR = object()


def _minimizar_documento(embrulho):
    """Prova o documento pelos textos distintos, na governança oficial.

    O walker oficial classifica cada string e, nas chaves mascaráveis, o
    valor daquela chave. Repetir a mesma prova em cada célula estoura o
    tempo das planilhas grandes sem mudar a decisão. O documento inteiro
    só é reescrito com o que essa prova autorizou.
    """
    compacto = _compactar(embrulho)
    primeiro = _governar_documento(compacto)
    if not _resultado_confiavel(primeiro, compacto) or primeiro.decision == DECISION_BLOCK:
        return None
    if primeiro.decision == DECISION_ALLOW:
        if primeiro.safe_content != compacto:
            return None
        return embrulho
    seguro = primeiro.safe_content
    if not _compativel(compacto, seguro) or not _coordenadas_preservadas(compacto, seguro):
        return None
    segundo = _governar_documento(compacto)
    if (
        not _resultado_confiavel(segundo, compacto)
        or segundo.decision != DECISION_MINIMIZE
        or segundo.safe_content != seguro
    ):
        return None
    mapa = _mapa_autorizado(compacto, seguro)
    if mapa is None:
        return None
    conferido = _governar_documento(seguro)
    if (
        _resultado_confiavel(conferido, seguro)
        and conferido.decision == DECISION_ALLOW
        and conferido.safe_content == seguro
    ):
        return _reescrever(embrulho, mapa)
    if (
        _resultado_confiavel(conferido, seguro)
        and conferido.decision == DECISION_MINIMIZE
        and _compativel(seguro, conferido.safe_content)
        and _coordenadas_preservadas(seguro, conferido.safe_content)
    ):
        instaveis = _textos_alterados(seguro, conferido.safe_content)
        ajustado = {
            origem: _ISOLAR if destino in instaveis else destino
            for origem, destino in mapa.items()
        }
        estabilizado = _reescrever(embrulho, ajustado)
        prova = _compactar(estabilizado)
        final = _governar_documento(prova)
        if (
            _resultado_confiavel(final, prova)
            and final.decision == DECISION_ALLOW
            and final.safe_content == prova
        ):
            return estabilizado
    return None


def _compactar(valor):
    campos = {}
    textos = set()
    _coletar_textos(valor, campos, textos)
    lista_campos = []
    for chave in sorted(campos):
        for item in sorted(campos[chave]):
            lista_campos.append({chave: item})
    return {"campos": lista_campos, "textos": sorted(textos)}


def _coletar_textos(valor, campos, textos):
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if (
                isinstance(chave, str)
                and chave in MASKABLE_FIELD_KEYS
                and isinstance(item, str)
                and item.strip()
            ):
                campos.setdefault(chave, set()).add(item)
            else:
                _coletar_textos(item, campos, textos)
    elif isinstance(valor, list):
        for item in valor:
            _coletar_textos(item, campos, textos)
    elif isinstance(valor, str):
        textos.add(valor)


def _mapa_autorizado(compacto, seguro):
    mapa = {}
    campos = compacto.get("campos")
    campos_seguros = seguro.get("campos")
    textos = compacto.get("textos")
    textos_seguros = seguro.get("textos")
    if not isinstance(campos, list) or not isinstance(campos_seguros, list) or len(campos) != len(campos_seguros):
        return None
    if not isinstance(textos, list) or not isinstance(textos_seguros, list) or len(textos) != len(textos_seguros):
        return None
    for original, autorizado in zip(campos, campos_seguros):
        if not isinstance(original, dict) or not isinstance(autorizado, dict) or len(original) != 1:
            return None
        chave, valor = next(iter(original.items()))
        if chave not in autorizado or not isinstance(valor, str) or not isinstance(autorizado[chave], str):
            return None
        if autorizado[chave] != valor:
            mapa[valor] = autorizado[chave]
    for original, autorizado in zip(textos, textos_seguros):
        if not isinstance(original, str) or not isinstance(autorizado, str):
            return None
        if autorizado != original:
            mapa[original] = autorizado
    return mapa


def _textos_alterados(base, alterado):
    saida = set()
    if isinstance(base, dict) and isinstance(alterado, dict):
        for chave, valor in base.items():
            saida.update(_textos_alterados(valor, alterado.get(chave)))
        return saida
    if isinstance(base, list) and isinstance(alterado, list) and len(base) == len(alterado):
        for item, outro in zip(base, alterado):
            saida.update(_textos_alterados(item, outro))
        return saida
    if isinstance(base, str) and isinstance(alterado, str) and base != alterado:
        saida.add(base)
    return saida


def _reescrever(valor, mapa):
    if isinstance(valor, dict):
        if "tipo" in valor and "valor" in valor and isinstance(valor.get("valor"), str) and valor["valor"] in mapa:
            destino = mapa[valor["valor"]]
            if destino is _ISOLAR:
                return {"tipo": "ausente"}
            copia = {chave: _reescrever(item, mapa) for chave, item in valor.items()}
            copia["valor"] = destino
            if copia.get("tipo") == "numero":
                copia["tipo"] = "texto"
            if copia.get("tipo_cache") == "numero":
                copia["tipo_cache"] = "texto"
            return copia
        return {chave: _reescrever(item, mapa) for chave, item in valor.items()}
    if isinstance(valor, list):
        return [_reescrever(item, mapa) for item in valor]
    if isinstance(valor, str) and valor in mapa:
        destino = mapa[valor]
        return "protegido" if destino is _ISOLAR else destino
    return valor


def _compativel(original, seguro) -> bool:
    if isinstance(original, bool) or isinstance(seguro, bool):
        return isinstance(original, bool) and isinstance(seguro, bool) and original is seguro
    if isinstance(original, dict):
        if not isinstance(seguro, dict) or set(original) != set(seguro):
            return False
        return all(_compativel(original[chave], seguro[chave]) for chave in original)
    if isinstance(original, list):
        if not isinstance(seguro, list) or len(original) != len(seguro):
            return False
        return all(_compativel(item, outro) for item, outro in zip(original, seguro))
    if isinstance(original, str):
        return isinstance(seguro, str)
    if isinstance(original, int):
        return isinstance(seguro, int) and not isinstance(seguro, bool) and original == seguro
    if isinstance(original, float):
        return isinstance(seguro, float) and original == seguro
    return original == seguro


def _coordenadas_preservadas(original, seguro) -> bool:
    chaves = {"arquivo", "aba", "regiao", "linha", "indice", "coluna", "coluna_inicio", "coluna_fim"}
    if isinstance(original, dict) and isinstance(seguro, dict):
        for chave, valor in original.items():
            if chave in chaves and seguro.get(chave) != valor:
                return False
            if not _coordenadas_preservadas(valor, seguro.get(chave)):
                return False
        return True
    if isinstance(original, list) and isinstance(seguro, list) and len(original) == len(seguro):
        return all(_coordenadas_preservadas(item, outro) for item, outro in zip(original, seguro))
    return True


def _intacto(valor) -> bool:
    resultado = _governar_documento(valor)
    return (
        _resultado_confiavel(resultado, valor)
        and resultado.decision == DECISION_ALLOW
        and resultado.safe_content == valor
    )


def _isolar_dados(embrulho):
    dados = embrulho.get("dados_tipados")
    if not isinstance(dados, list):
        return None
    if not _intacto(embrulho.get("estrutura")) or not _intacto(embrulho.get("perfil")):
        return None
    if not _intacto(embrulho.get("metadados_preparacao")):
        return None
    novos = []
    for registro in dados:
        if _intacto(registro):
            novos.append(registro)
            continue
        ajustado = _suprimir_celulas(registro)
        if ajustado is None or not _intacto(ajustado):
            continue
        novos.append(ajustado)
    if dados and not novos:
        return None
    candidato = dict(embrulho)
    candidato["dados_tipados"] = novos
    return candidato


def _suprimir_celulas(registro):
    if not isinstance(registro, dict) or not isinstance(registro.get("valores"), list):
        return None
    copia = dict(registro)
    valores = []
    for celula in registro["valores"]:
        if _intacto(celula):
            valores.append(celula)
        else:
            valores.append({"tipo": "ausente"})
    copia["valores"] = valores
    return copia


def _texto_de_numero_longo(valor):
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, int):
        texto = str(abs(valor))
        if len(texto) >= 10:
            return texto
        return None
    if isinstance(valor, float) and math.isfinite(valor):
        texto = format(valor, "f")
        if sum(1 for caractere in texto if caractere.isdigit()) >= 10:
            return texto
    return None


def _promover_celula(celula):
    if not isinstance(celula, dict):
        return celula, None
    if celula.get("tipo") == "numero":
        texto = _texto_de_numero_longo(celula.get("valor"))
        if texto is None:
            return celula, None
        nova = dict(celula)
        nova["tipo"] = "texto"
        nova["valor"] = texto
        return nova, texto
    if (
        celula.get("tipo") == "formula"
        and celula.get("cache_estado") == "presente"
        and celula.get("tipo_cache") == "numero"
    ):
        texto = _texto_de_numero_longo(celula.get("valor"))
        if texto is None:
            return celula, None
        nova = dict(celula)
        nova["tipo_cache"] = "texto"
        nova["valor"] = texto
        return nova, texto
    return celula, None


def _promover_numeros_longos(embrulho):
    dados = embrulho.get("dados_tipados")
    originais = {}
    if not isinstance(dados, list):
        return embrulho, originais
    copia = []
    mudou = False
    for registro in dados:
        if not isinstance(registro, dict) or not isinstance(registro.get("valores"), list):
            copia.append(registro)
            continue
        valores = []
        registro_mudou = False
        for indice, celula in enumerate(registro["valores"]):
            nova, texto = _promover_celula(celula)
            valores.append(nova)
            if texto is not None:
                originais[
                    (
                        registro.get("arquivo"),
                        registro.get("aba"),
                        registro.get("regiao"),
                        registro.get("linha"),
                        indice,
                    )
                ] = (celula, texto)
                registro_mudou = True
        if registro_mudou:
            novo = dict(registro)
            novo["valores"] = valores
            copia.append(novo)
            mudou = True
        else:
            copia.append(registro)
    if not mudou:
        return embrulho, originais
    saida = dict(embrulho)
    saida["dados_tipados"] = copia
    return saida, originais


def _restaurar_numeros_inalterados(autorizado, originais):
    if not originais:
        return autorizado
    dados = autorizado.get("dados_tipados")
    if not isinstance(dados, list):
        return None
    copia = []
    for registro in dados:
        if not isinstance(registro, dict) or not isinstance(registro.get("valores"), list):
            copia.append(registro)
            continue
        valores = list(registro["valores"])
        mudou = False
        for indice, celula in enumerate(valores):
            chave = (
                registro.get("arquivo"),
                registro.get("aba"),
                registro.get("regiao"),
                registro.get("linha"),
                indice,
            )
            par = originais.get(chave)
            if par is None or not isinstance(celula, dict):
                continue
            celula_original, texto = par
            if celula.get("valor") == texto:
                valores[indice] = celula_original
                mudou = True
        if mudou:
            novo = dict(registro)
            novo["valores"] = valores
            copia.append(novo)
        else:
            copia.append(registro)
    saida = dict(autorizado)
    saida["dados_tipados"] = copia
    return saida


def _selar_protecao(original, autorizado):
    if autorizado == original:
        return original
    dados_originais = original.get("dados_tipados")
    dados = autorizado.get("dados_tipados")
    minimizadas, suprimidas, colunas = _diferenças(dados_originais, dados)
    estrutura = autorizado.get("estrutura")
    perfil = dict(autorizado.get("perfil") or {})
    if isinstance(dados_originais, list) and isinstance(dados, list) and len(dados) != len(dados_originais):
        estrutura = _alinhar_linhas(estrutura, dados)
        if isinstance(perfil.get("linhas_elegiveis"), int):
            perfil["linhas_elegiveis"] = len(dados)
    metadados = dict(autorizado.get("metadados_preparacao") or {})
    metadados["protecao"] = {
        "decisao": "isolada" if suprimidas else "minimizada",
        "celulas_minimizadas": minimizadas,
        "celulas_suprimidas": suprimidas,
        "colunas": colunas,
    }
    selado = {
        "estrutura": estrutura,
        "perfil": perfil,
        "dados_tipados": dados,
        "metadados_preparacao": metadados,
    }
    if not _intacto(selado):
        return None
    logger.info(
        "evento=agente_bi_preparacao_protegida decisao=%s celulas_minimizadas=%s celulas_suprimidas=%s",
        selado["metadados_preparacao"]["protecao"]["decisao"],
        minimizadas,
        suprimidas,
    )
    return selado


def _diferenças(antes, depois):
    minimizadas = 0
    suprimidas = 0
    vistos = []
    chaves = set()
    if not isinstance(antes, list) or not isinstance(depois, list):
        return 0, 0, []
    if len(depois) < len(antes):
        suprimidas += len(antes) - len(depois)
    for registro_antes, registro_depois in zip(antes, depois):
        if not isinstance(registro_antes, dict) or not isinstance(registro_depois, dict):
            continue
        valores_antes = registro_antes.get("valores")
        valores_depois = registro_depois.get("valores")
        if not isinstance(valores_antes, list) or not isinstance(valores_depois, list):
            continue
        for indice, (celula_antes, celula_depois) in enumerate(zip(valores_antes, valores_depois)):
            if celula_antes == celula_depois:
                continue
            if isinstance(celula_depois, dict) and celula_depois.get("tipo") == "ausente":
                suprimidas += 1
            else:
                minimizadas += 1
            coordenada = (
                registro_depois.get("arquivo"),
                registro_depois.get("aba"),
                registro_depois.get("regiao"),
                indice,
            )
            if coordenada in chaves:
                continue
            if not all(isinstance(parte, int) and not isinstance(parte, bool) and parte >= 0 for parte in coordenada):
                continue
            chaves.add(coordenada)
            vistos.append(
                {
                    "arquivo": coordenada[0],
                    "aba": coordenada[1],
                    "regiao": coordenada[2],
                    "indice": coordenada[3],
                }
            )
    vistos.sort(key=lambda item: (item["arquivo"], item["aba"], item["regiao"], item["indice"]))
    return minimizadas, suprimidas, vistos


def _alinhar_linhas(estrutura, dados):
    if not isinstance(estrutura, dict) or not isinstance(estrutura.get("regioes"), list):
        return estrutura
    contagem = {}
    for registro in dados:
        if not isinstance(registro, dict):
            continue
        chave = (registro.get("arquivo"), registro.get("aba"), registro.get("regiao"))
        contagem[chave] = contagem.get(chave, 0) + 1
    novas = []
    mudou = False
    for regiao in estrutura["regioes"]:
        if not isinstance(regiao, dict):
            novas.append(regiao)
            continue
        chave = (regiao.get("arquivo"), regiao.get("aba"), regiao.get("indice"))
        atual = contagem.get(chave, 0)
        if isinstance(regiao.get("linhas_elegiveis"), int) and regiao["linhas_elegiveis"] != atual:
            copia = dict(regiao)
            copia["linhas_elegiveis"] = atual
            novas.append(copia)
            mudou = True
        else:
            novas.append(regiao)
    if not mudou:
        return estrutura
    saida = dict(estrutura)
    saida["regioes"] = novas
    return saida


def _autorizar_conteudo_persistivel(valor) -> None:
    """Reusa a governança existente. Não grava original nem cópia não comprovada.

    O dataset preparado chega aqui já minimizado, quando a governança autorizou
    essa forma. Este ponto continua exigindo decisão intacta: não aceita
    `safe_content` diferente do valor, não grava o original sensível e não
    registra o valor no log. A prova compacta só dispensa a varredura integral
    quando cada texto distinto já foi autorizado sem alteração.
    """
    embrulho = {"conteudo": valor}
    if _prova_compacta_intacta(embrulho):
        return
    resultado = govern_outbound_content(
        embrulho,
        purpose=PURPOSE_AUDITORIA_FRETE,
        content_type=CONTENT_TYPE_DOCUMENT,
        agent="agente_bi",
        provider="persistencia_local",
    )
    if (
        resultado.decision != DECISION_ALLOW
        or not resultado.issued_by_cleiton
        or not governance_result_is_coherent(resultado, original=embrulho)
        or resultado.safe_content != embrulho
    ):
        raise AgenteBiErro("conteudo_nao_permitido", 422)


def _prova_compacta_intacta(valor) -> bool:
    compacto = _compactar(valor)
    resultado = _governar_documento(compacto)
    return (
        _resultado_confiavel(resultado, compacto)
        and resultado.decision == DECISION_ALLOW
        and resultado.safe_content == compacto
    )


def _recusar_volume(valor) -> None:
    if len(_canonico(valor).encode("utf-8")) > MAX_JSON_BYTES:
        raise AgenteBiErro("payload_invalido", 422)


def _validar_json_controlado(valor, *, profundidade: int) -> None:
    if profundidade > MAX_PROFUNDIDADE:
        raise AgenteBiErro("payload_invalido", 422)
    if isinstance(valor, dict):
        if len(valor) > MAX_CHAVES:
            raise AgenteBiErro("payload_invalido", 422)
        for chave, item in valor.items():
            if not isinstance(chave, str) or len(chave) > 80 or _chave_proibida(chave):
                raise AgenteBiErro("conteudo_nao_permitido", 422)
            _validar_json_controlado(item, profundidade=profundidade + 1)
        return
    if isinstance(valor, list):
        if len(valor) > MAX_LISTA:
            raise AgenteBiErro("payload_invalido", 422)
        for item in valor:
            _validar_json_controlado(item, profundidade=profundidade + 1)
        return
    if isinstance(valor, bool) or valor is None:
        return
    if isinstance(valor, int):
        return
    if isinstance(valor, float):
        if not math.isfinite(valor):
            raise AgenteBiErro("payload_invalido", 422)
        return
    if isinstance(valor, str):
        if len(valor) > MAX_TEXTO:
            raise AgenteBiErro("payload_invalido", 422)
        return
    raise AgenteBiErro("payload_invalido", 422)


def _chave_proibida(chave: str) -> bool:
    if not chave.strip():
        return True
    normal = "".join(caractere.lower() if caractere.isalnum() else "_" for caractere in chave.strip())
    while "__" in normal:
        normal = normal.replace("__", "_")
    normal = normal.strip("_")
    if normal in _COMPOSTOS_PROIBIDOS or normal in _TOKENS_PROIBIDOS:
        return True
    return any(parte in _TOKENS_PROIBIDOS for parte in normal.split("_") if parte)


def _validar_definicoes(valor, *, estado_publicacao: str) -> list[dict]:
    if not isinstance(valor, list):
        raise AgenteBiErro("payload_invalido", 422)
    if len(valor) > LIMITE_GRAFICOS:
        raise AgenteBiErro("limite_graficos", 422)
    if estado_publicacao == "publicado" and len(valor) < 1:
        raise AgenteBiErro("payload_invalido", 422)
    refs = set()
    saida = []
    for item in valor:
        if not isinstance(item, dict) or set(item) != {"ref", "titulo", "tipo", "definicao"}:
            raise AgenteBiErro("payload_invalido", 422)
        ref = _texto_exato(item["ref"], padrao=_REF_GRAFICO_RE)
        if ref in refs:
            raise AgenteBiErro("payload_invalido", 422)
        refs.add(ref)
        titulo = item["titulo"]
        if not isinstance(titulo, str) or not titulo.strip() or len(titulo) > 120:
            raise AgenteBiErro("payload_invalido", 422)
        titulo = titulo.strip()
        _autorizar_conteudo_persistivel(titulo)
        tipo = _escolha(item["tipo"], TIPOS_GRAFICO)
        definicao = _json_objeto(item["definicao"])
        saida.append(
            {
                "ref": ref,
                "titulo": titulo,
                "tipo": tipo,
                "definicao": definicao,
            }
        )
    return saida


def _validar_datasets_dashboard(escopo: EscopoAgenteBi, valor) -> list[dict]:
    if not isinstance(valor, list) or not valor or len(valor) > LIMITE_DATASETS_DASHBOARD:
        raise AgenteBiErro("payload_invalido", 422)
    saida = []
    vistos = set()
    for item in valor:
        if not isinstance(item, dict) or set(item) != {"dataset_id", "versao", "impressao"}:
            raise AgenteBiErro("payload_invalido", 422)
        dataset_id = _inteiro_minimo(item["dataset_id"], minimo=1)
        versao = _inteiro_minimo(item["versao"], minimo=1)
        impressao = item["impressao"]
        if not isinstance(impressao, str) or _IMPRESSAO_RE.fullmatch(impressao) is None:
            raise AgenteBiErro("identidade_invalida", 409)
        dataset = _obter_no_escopo(AgenteBiDataset, escopo, dataset_id)
        if dataset is None:
            raise AgenteBiErro("nao_encontrado", 404)
        if dataset.versao != versao or dataset.impressao != impressao:
            raise AgenteBiErro("identidade_invalida", 409)
        if dataset.id in vistos:
            raise AgenteBiErro("payload_invalido", 422)
        vistos.add(dataset.id)
        saida.append(
            {
                "dataset_id": dataset.id,
                "versao": dataset.versao,
                "impressao": dataset.impressao,
            }
        )
    return saida


def _canonico(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
