"""Persistência da aplicação v2 do AgenteBI (SCRUM-269, etapa 3.4.1).

Cria a identidade durável de um plano `patch.definir.v2` já gravado e,
depois, aponta para a definição, a memória e a revisão publicadas.
Não publica dashboard, não calcula e não confirma a transação: quem chama
decide o commit. Identificadores de dashboard, dataset e revisão saem do
plano persistido, não de um documento do modelo.

A conclusão exige que o cálculo já gravado nomeie, no contexto, a
definição final e o plano desta aplicação. Se o resultado trouxer
`origem_definicao_id`, ela tem de ser a mesma definição. A chave da
análise e a identidade do dataset não substituem essas referências. O
executor atual não persiste `calculo.v2`; o contexto é o contrato mínimo
que o orquestrador de publicação terá de preencher.

A revisão base também sai do plano persistido. Uma revisão posterior do
mesmo painel não entra no lugar dela. Inserção direta por ORM ou SQL pode
contornar essa leitura: os gatilhos não interpretam o JSON do plano nem o
contexto do cálculo. Essa brecha continua sendo limitação da integridade
no banco, não garantia já implementada.
"""
from __future__ import annotations

import logging

from sqlalchemy.exc import DataError, IntegrityError

from app.agente_bi_aplicacao_integridade import (
    CONTRATO_CALCULO_APLICACAO,
    CONTRATO_DEFINICAO_APLICACAO,
    CONTRATO_PLANO_APLICACAO,
    CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO,
)
from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiAplicacao,
    AgenteBiDashboardDataset,
    utcnow_naive,
)
from app.services.agente_bi_execucao_service import ExecucaoErro
from app.services.agente_bi_interpretacao_v2_service import (
    InterpretacaoErro,
    recuperar_contexto,
    revalidar_plano_duravel,
)
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    EscopoAgenteBi,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)

logger = logging.getLogger(__name__)


def preparar_aplicacao(escopo: EscopoAgenteBi, plano_id: int) -> dict:
    """Abre, ou reencontra, a aplicação do plano. Não grava artefato nem commit."""
    escopo = _escopo(escopo)
    plano_id = _inteiro(plano_id)
    existente = _por_plano(plano_id)
    if existente is not None:
        if not _no_escopo(existente, escopo):
            raise AgenteBiErro("nao_encontrado", 404)
        return aplicacao_para_dict(existente)

    contexto = _contexto_do_plano(escopo, plano_id)
    linha = db.session.get(AgenteBiAnalise, plano_id)
    if linha is None or not _no_escopo(linha, escopo):
        raise AgenteBiErro("nao_encontrado", 404)
    db.session.refresh(linha)
    registro = _registro(escopo, plano_id)
    if registro.get("estado") != "concluida" or registro.get("contrato_versao") != CONTRATO_PLANO_APLICACAO:
        raise AgenteBiErro("analise_indisponivel", 409)
    resultado = registro.get("resultado")
    if not isinstance(resultado, dict) or resultado.get("situacao") != "proposto":
        raise AgenteBiErro("analise_indisponivel", 409)
    try:
        revalidar_plano_duravel(linha, resultado)
    except ExecucaoErro as exc:
        raise AgenteBiErro(exc.codigo, 422) from exc

    definicao = _registro(escopo, int(contexto["definicao_anterior_id"]))
    if definicao.get("contrato_versao") not in CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO:
        raise AgenteBiErro("identidade_invalida", 409)
    if definicao.get("estado") != "concluida" or definicao.get("etapa") != "definicao":
        raise AgenteBiErro("identidade_invalida", 409)
    painel = _painel(escopo, int(contexto["dashboard_id"]))
    dataset = _dataset(escopo, int(linha.dataset_id))
    if not _dataset_confere(linha, contexto, definicao, painel, dataset):
        raise AgenteBiErro("identidade_invalida", 409)
    if int(painel["revisao"]) != int(contexto["revisao_base"]):
        raise AgenteBiErro("identidade_invalida", 409)
    if not isinstance(painel.get("chave"), str) or not painel["chave"]:
        raise AgenteBiErro("identidade_invalida", 409)

    agora = utcnow_naive()
    nova = AgenteBiAplicacao(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        plano_id=plano_id,
        plano_contrato=CONTRATO_PLANO_APLICACAO,
        dataset_id=int(linha.dataset_id),
        dataset_versao=int(linha.dataset_versao),
        dataset_impressao=linha.dataset_impressao,
        dashboard_id=int(painel["id"]),
        dashboard_chave=painel["chave"],
        revisao_base=int(painel["revisao"]),
        definicao_anterior_id=int(definicao["id"]),
        definicao_anterior_contrato=definicao["contrato_versao"],
        estado="preparada",
        preparada_em=agora,
        created_at=agora,
        updated_at=agora,
    )
    gravada = _inserir(nova, plano_id, escopo)
    logger.info(
        "evento=agente_bi_aplicacao_preparada usuario_id=%s aplicacao_id=%s plano_id=%s",
        escopo.usuario_id,
        gravada.id,
        gravada.plano_id,
    )
    return aplicacao_para_dict(gravada)


def concluir_aplicacao(
    escopo: EscopoAgenteBi,
    aplicacao_id: int,
    *,
    definicao_id: int,
    calculo_id: int,
    dashboard_publicado_id: int,
) -> dict:
    """Liga artefatos já persistidos e marca a aplicação concluída. Não cria revisão."""
    escopo = _escopo(escopo)
    aplicacao_id = _inteiro(aplicacao_id)
    definicao_id = _inteiro(definicao_id)
    calculo_id = _inteiro(calculo_id)
    dashboard_publicado_id = _inteiro(dashboard_publicado_id)
    linha = _por_id(escopo, aplicacao_id)
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    db.session.refresh(linha)
    if linha.estado == "concluida":
        if _mesmos_artefatos(linha, definicao_id, calculo_id, dashboard_publicado_id):
            return aplicacao_para_dict(linha)
        raise AgenteBiErro("conflito_versao", 409)
    if definicao_id in {int(linha.plano_id), int(linha.definicao_anterior_id)}:
        raise AgenteBiErro("identidade_invalida", 409)
    if calculo_id in {int(linha.plano_id), int(linha.definicao_anterior_id), definicao_id}:
        raise AgenteBiErro("identidade_invalida", 409)
    if dashboard_publicado_id == int(linha.dashboard_id):
        raise AgenteBiErro("identidade_invalida", 409)

    definicao = _analise_do_artefato(escopo, definicao_id, CONTRATO_DEFINICAO_APLICACAO, "definicao")
    calculo = _analise_do_artefato(escopo, calculo_id, CONTRATO_CALCULO_APLICACAO, "execucao")
    painel = _painel_publicado(escopo, linha, dashboard_publicado_id, calculo_id)
    if not _mesmo_dataset(definicao, linha) or not _mesmo_dataset(calculo, linha):
        raise AgenteBiErro("identidade_invalida", 409)
    if not _vinculo_contem_dataset(painel, linha):
        raise AgenteBiErro("identidade_invalida", 409)
    _conferir_calculo(calculo, definicao_id, int(linha.plano_id))

    agora = utcnow_naive()
    linha.estado = "concluida"
    linha.definicao_id = definicao_id
    linha.definicao_contrato = CONTRATO_DEFINICAO_APLICACAO
    linha.calculo_id = calculo_id
    linha.calculo_contrato = CONTRATO_CALCULO_APLICACAO
    linha.dashboard_publicado_id = dashboard_publicado_id
    linha.revisao_publicada = int(painel["revisao"])
    linha.concluida_em = agora
    linha.updated_at = agora
    _flush()
    logger.info(
        "evento=agente_bi_aplicacao_concluida usuario_id=%s aplicacao_id=%s plano_id=%s dashboard_id=%s",
        escopo.usuario_id,
        linha.id,
        linha.plano_id,
        linha.dashboard_publicado_id,
    )
    return aplicacao_para_dict(linha)


def obter_aplicacao(escopo: EscopoAgenteBi, aplicacao_id: int) -> dict:
    """Relê a aplicação e os vínculos publicados. Não depende da resposta anterior."""
    escopo = _escopo(escopo)
    linha = _por_id(escopo, _inteiro(aplicacao_id))
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    db.session.refresh(linha)
    return aplicacao_para_dict(linha)


def obter_aplicacao_do_plano(escopo: EscopoAgenteBi, plano_id: int) -> dict:
    """Recupera a aplicação pela identidade do plano, dentro do escopo."""
    escopo = _escopo(escopo)
    linha = _por_plano(_inteiro(plano_id))
    if linha is None or not _no_escopo(linha, escopo):
        raise AgenteBiErro("nao_encontrado", 404)
    db.session.refresh(linha)
    return aplicacao_para_dict(linha)


def aplicacao_para_dict(linha: AgenteBiAplicacao) -> dict:
    return {
        "id": int(linha.id),
        "conta_id": int(linha.conta_id),
        "franquia_id": int(linha.franquia_id),
        "usuario_id": int(linha.usuario_id),
        "plano_id": int(linha.plano_id),
        "plano_contrato": linha.plano_contrato,
        "dataset_id": int(linha.dataset_id),
        "versao": int(linha.dataset_versao),
        "impressao": linha.dataset_impressao,
        "dashboard_id": int(linha.dashboard_id),
        "dashboard_chave": linha.dashboard_chave,
        "revisao_base": int(linha.revisao_base),
        "definicao_anterior_id": int(linha.definicao_anterior_id),
        "definicao_anterior_contrato": linha.definicao_anterior_contrato,
        "estado": linha.estado,
        "definicao_id": None if linha.definicao_id is None else int(linha.definicao_id),
        "definicao_contrato": linha.definicao_contrato,
        "calculo_id": None if linha.calculo_id is None else int(linha.calculo_id),
        "calculo_contrato": linha.calculo_contrato,
        "dashboard_publicado_id": (
            None if linha.dashboard_publicado_id is None else int(linha.dashboard_publicado_id)
        ),
        "revisao_publicada": None if linha.revisao_publicada is None else int(linha.revisao_publicada),
        "vinculos": _vinculos(linha),
        "preparada_em": linha.preparada_em.isoformat(),
        "concluida_em": None if linha.concluida_em is None else linha.concluida_em.isoformat(),
    }


def _contexto_do_plano(escopo: EscopoAgenteBi, plano_id: int) -> dict:
    try:
        return recuperar_contexto(escopo, plano_id)
    except InterpretacaoErro as exc:
        raise AgenteBiErro(exc.codigo, exc.status) from exc


def _registro(escopo: EscopoAgenteBi, analise_id: int) -> dict:
    try:
        return obter_analise(escopo, analise_id)
    except AgenteBiErro as exc:
        raise AgenteBiErro("identidade_invalida", 409) from exc


def _dataset(escopo: EscopoAgenteBi, dataset_id: int) -> dict:
    try:
        return obter_dataset(escopo, dataset_id)
    except AgenteBiErro as exc:
        raise AgenteBiErro("identidade_invalida", 409) from exc


def _painel(escopo: EscopoAgenteBi, dashboard_id: int) -> dict:
    try:
        painel = obter_dashboard(escopo, dashboard_id)
    except AgenteBiErro as exc:
        raise AgenteBiErro("identidade_invalida", 409) from exc
    if (
        int(painel["usuario_id"]) != int(escopo.usuario_id)
        or int(painel["conta_id"]) != int(escopo.conta_id)
        or int(painel["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise AgenteBiErro("autorizacao_indevida", 403)
    return painel


def _dataset_confere(linha, contexto: dict, definicao: dict, painel: dict, dataset: dict) -> bool:
    esperado = contexto.get("dataset") if isinstance(contexto.get("dataset"), dict) else {}
    return (
        int(linha.dataset_id) == int(dataset["id"])
        and int(linha.dataset_versao) == int(dataset["versao"])
        and linha.dataset_impressao == dataset["impressao"]
        and int(esperado.get("id") or 0) == int(linha.dataset_id)
        and int(esperado.get("versao") or 0) == int(linha.dataset_versao)
        and esperado.get("impressao") == linha.dataset_impressao
        and _mesmo_dataset(definicao, linha)
        and int(painel["id"]) == int(contexto["dashboard_id"])
        and int(definicao["id"]) == int(contexto["definicao_anterior_id"])
    )


def _analise_do_artefato(escopo, analise_id: int, contrato: str, etapa: str) -> dict:
    registro = _registro(escopo, analise_id)
    if registro.get("contrato_versao") != contrato or registro.get("estado") != "concluida":
        raise AgenteBiErro("identidade_invalida", 409)
    if registro.get("etapa") != etapa:
        raise AgenteBiErro("identidade_invalida", 409)
    if (
        int(registro["usuario_id"]) != int(escopo.usuario_id)
        or int(registro["conta_id"]) != int(escopo.conta_id)
        or int(registro["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise AgenteBiErro("autorizacao_indevida", 403)
    return registro


def _painel_publicado(escopo, linha: AgenteBiAplicacao, dashboard_id: int, calculo_id: int) -> dict:
    painel = _painel(escopo, dashboard_id)
    if painel.get("estado_publicacao") != "publicado":
        raise AgenteBiErro("identidade_invalida", 409)
    if painel.get("chave") != linha.dashboard_chave:
        raise AgenteBiErro("identidade_invalida", 409)
    if int(painel["revisao"]) <= int(linha.revisao_base):
        raise AgenteBiErro("identidade_invalida", 409)
    analise_id = painel.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or int(analise_id) != calculo_id:
        raise AgenteBiErro("identidade_invalida", 409)
    return painel


def _vinculo_contem_dataset(painel: dict, linha: AgenteBiAplicacao) -> bool:
    refs = painel.get("datasets")
    if not isinstance(refs, list):
        return False
    return any(_ref_igual(item, linha) for item in refs)


def _ref_igual(item, linha: AgenteBiAplicacao) -> bool:
    if not isinstance(item, dict):
        return False
    try:
        return (
            int(item["dataset_id"]) == int(linha.dataset_id)
            and int(item["versao"]) == int(linha.dataset_versao)
            and item["impressao"] == linha.dataset_impressao
        )
    except (KeyError, TypeError, ValueError):
        return False


def _conferir_calculo(calculo: dict, definicao_id: int, plano_id: int) -> None:
    """Recusa conclusão se o cálculo não for desta definição e deste plano."""
    contexto = calculo.get("contexto")
    if not isinstance(contexto, dict):
        raise AgenteBiErro("identidade_invalida", 409)
    if not _mesmo_id(contexto.get("definicao_id"), definicao_id):
        raise AgenteBiErro("identidade_invalida", 409)
    if not _mesmo_id(contexto.get("plano_id"), plano_id):
        raise AgenteBiErro("identidade_invalida", 409)
    resultado = calculo.get("resultado")
    if isinstance(resultado, dict) and "origem_definicao_id" in resultado:
        if not _mesmo_id(resultado.get("origem_definicao_id"), definicao_id):
            raise AgenteBiErro("identidade_invalida", 409)


def _mesmo_id(valor, esperado: int) -> bool:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return False
    return valor == esperado


def _mesmo_dataset(registro: dict, linha: AgenteBiAplicacao) -> bool:
    try:
        return (
            int(registro["dataset_id"]) == int(linha.dataset_id)
            and int(registro["versao"]) == int(linha.dataset_versao)
            and registro["impressao"] == linha.dataset_impressao
        )
    except (KeyError, TypeError, ValueError):
        return False


def _mesmos_artefatos(linha, definicao_id: int, calculo_id: int, dashboard_id: int) -> bool:
    return (
        linha.definicao_id is not None
        and linha.calculo_id is not None
        and linha.dashboard_publicado_id is not None
        and int(linha.definicao_id) == definicao_id
        and int(linha.calculo_id) == calculo_id
        and int(linha.dashboard_publicado_id) == dashboard_id
    )


def _vinculos(linha: AgenteBiAplicacao) -> list[dict]:
    if linha.dashboard_publicado_id is None:
        return []
    itens = (
        AgenteBiDashboardDataset.query.filter_by(
            dashboard_id=int(linha.dashboard_publicado_id),
            conta_id=int(linha.conta_id),
            franquia_id=int(linha.franquia_id),
            usuario_id=int(linha.usuario_id),
        )
        .order_by(AgenteBiDashboardDataset.id.asc())
        .all()
    )
    return [
        {
            "dataset_id": int(item.dataset_id),
            "versao": int(item.dataset_versao),
            "impressao": item.dataset_impressao,
        }
        for item in itens
    ]


def _inserir(linha: AgenteBiAplicacao, plano_id: int, escopo: EscopoAgenteBi) -> AgenteBiAplicacao:
    db.session.add(linha)
    try:
        _flush()
    except AgenteBiErro:
        achada = _por_plano(plano_id)
        if achada is not None and _no_escopo(achada, escopo):
            return achada
        raise
    return linha


def _flush() -> None:
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        raise AgenteBiErro("conflito_versao", 409) from exc
    except DataError as exc:
        db.session.rollback()
        raise AgenteBiErro("payload_invalido", 422) from exc


def _por_plano(plano_id: int):
    return AgenteBiAplicacao.query.filter_by(plano_id=int(plano_id)).one_or_none()


def _por_id(escopo: EscopoAgenteBi, aplicacao_id: int):
    return (
        AgenteBiAplicacao.query.filter_by(
            id=int(aplicacao_id),
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
        ).one_or_none()
    )


def _no_escopo(linha, escopo: EscopoAgenteBi) -> bool:
    return (
        int(linha.usuario_id) == int(escopo.usuario_id)
        and int(linha.conta_id) == int(escopo.conta_id)
        and int(linha.franquia_id) == int(escopo.franquia_id)
    )


def _escopo(escopo) -> EscopoAgenteBi:
    if not isinstance(escopo, EscopoAgenteBi):
        raise AgenteBiErro("identidade_incompleta", 403)
    return escopo


def _inteiro(valor) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        raise AgenteBiErro("payload_invalido", 422)
    return int(valor)
