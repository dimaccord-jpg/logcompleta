"""Blueprint da persistência do AgenteBI.

Endpoints autenticados e isolados por conta, franquia e usuário dono.
A flag desligada responde como recurso inexistente. A ingestão de XLSX/CSV
exige uma segunda flag, desligada por padrão, e não chama IA.
A descoberta visível reutiliza o contrato analisar.v1. Sem execução real
habilitada, o provedor continua simulado. Com ela, a operação durável
publica só o resultado persistido.
A definição analítica reutiliza a seleção aprovada, sem chamada faturável.
O cálculo dos indicadores é local, determinístico e também sem chamada faturável.
A consulta e o filtro do dashboard releem essa memória, ou repetem o mesmo executor,
sem chamada faturável. O detalhe de um gráfico relê só a série já gravada do cálculo
exibido, também sem chamada faturável. A atualização compatível também repete esse executor
sobre a versão nova do dataset e não chama modelo. A evolução por texto reutiliza
`definir.v1`: o que já é determinístico não chama modelo; paráfrase usa o adaptador
Anthropic governado.
"""
from __future__ import annotations

import logging
import re
from functools import wraps

from flask import Blueprint, jsonify, request
from flask_login import current_user

from app.agente_bi_flags import agente_bi_api_habilitada, agente_bi_ingestao_habilitada
from app.services.agente_bi_persistencia_service import (
    MAX_JSON_BYTES,
    AgenteBiErro,
    EscopoAgenteBi,
    criar_analise,
    criar_dashboard,
    criar_dataset,
    escopo_do_usuario,
    listar_analises,
    listar_dashboards,
    listar_datasets,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)

agente_bi_api_bp = Blueprint("agente_bi_api", __name__, url_prefix="/api/agente-bi")
logger = logging.getLogger(__name__)
_MAX_CORPO_BYTES = MAX_JSON_BYTES * 3
_CODIGO_PUBLICO_ERRO = re.compile(r"^[a-z0-9_]{1,40}$")
_BLOCOS_CONTRATO = ("contexto", "resultado", "regras_negocio", "esclarecimentos")


def _erro(codigo: str, status: int):
    return jsonify({"ok": False, "codigo": codigo}), status


def _erro_descoberta(exc):
    corpo = {"ok": False, "codigo": exc.codigo}
    evidencias = getattr(exc, "evidencias", None)
    if (
        isinstance(evidencias, list)
        and evidencias
        and all(isinstance(item, str) and _CODIGO_PUBLICO_ERRO.fullmatch(item) for item in evidencias)
    ):
        corpo["verificacoes"] = list(evidencias)
    return jsonify(corpo), exc.status


def _abrir():
    if not agente_bi_api_habilitada():
        return None, _erro("nao_encontrado", 404)
    if not getattr(current_user, "is_authenticated", False):
        return None, _erro("autenticacao_necessaria", 401)
    autorizacao = avaliar_autorizacao_operacao_por_franquia(current_user)
    if not autorizacao.get("permitido"):
        return None, _erro("operacao_nao_permitida", 403)
    try:
        return escopo_do_usuario(current_user), None
    except AgenteBiErro as exc:
        return None, _erro(exc.codigo, exc.status)


def _endpoint(funcao):
    @wraps(funcao)
    def envelope(*args, **kwargs):
        escopo, erro = _abrir()
        if erro is not None:
            return erro
        try:
            return funcao(escopo, *args, **kwargs)
        except AgenteBiErro as exc:
            return _erro(exc.codigo, exc.status)

    return envelope


def _corpo_json() -> dict:
    if request.files:
        raise AgenteBiErro("payload_invalido", 422)
    if request.content_length is not None and request.content_length > _MAX_CORPO_BYTES:
        raise AgenteBiErro("payload_invalido", 422)
    if not request.is_json:
        raise AgenteBiErro("payload_invalido", 422)
    corpo = request.get_json(silent=True)
    if not isinstance(corpo, dict):
        raise AgenteBiErro("payload_invalido", 422)
    return corpo


def _abrir_ingestao():
    if not agente_bi_ingestao_habilitada():
        return None, _erro("nao_encontrado", 404)
    return _abrir()


def _endpoint_ingestao(funcao):
    @wraps(funcao)
    def envelope(*args, **kwargs):
        escopo, erro = _abrir_ingestao()
        if erro is not None:
            return erro
        try:
            return funcao(escopo, *args, **kwargs)
        except AgenteBiErro as exc:
            return _erro(exc.codigo, exc.status)

    return envelope


def _chave_consulta() -> str | None:
    if "chave" not in request.args:
        return None
    return request.args.get("chave")


@agente_bi_api_bp.post("/datasets")
@_endpoint
def post_dataset(escopo: EscopoAgenteBi):
    return jsonify({"ok": True, "dataset": criar_dataset(escopo, _corpo_json())}), 201


@agente_bi_api_bp.get("/datasets")
@_endpoint
def get_datasets(escopo: EscopoAgenteBi):
    return jsonify({"ok": True, "datasets": listar_datasets(escopo, chave=_chave_consulta())})


@agente_bi_api_bp.get("/datasets/<int:dataset_id>")
@_endpoint
def get_dataset(escopo: EscopoAgenteBi, dataset_id: int):
    return jsonify({"ok": True, "dataset": obter_dataset(escopo, dataset_id)})


def _contratos_reservados() -> frozenset[str]:
    """Identidades que só os fluxos do servidor podem gravar."""
    from app.services.agente_bi_analisar_service import CONTRATO_ANALISAR, CONTRATO_CONTEXTO_ANALISAR
    from app.services.agente_bi_descoberta_service import CONTRATO_CONTEXTO_SELECAO, CONTRATO_SELECAO
    from app.services.agente_bi_calculo_service import CONTRATO_CALCULO, CONTRATO_CONTEXTO_CALCULO
    from app.services.agente_bi_definir_service import CONTRATO_CONTEXTO_DEFINIR, CONTRATO_DEFINIR

    return frozenset({
        CONTRATO_ANALISAR,
        CONTRATO_CONTEXTO_ANALISAR,
        CONTRATO_SELECAO,
        CONTRATO_CONTEXTO_SELECAO,
        CONTRATO_DEFINIR,
        CONTRATO_CONTEXTO_DEFINIR,
        CONTRATO_CALCULO,
        CONTRATO_CONTEXTO_CALCULO,
    })


def _identidade_reservada(corpo: dict) -> bool:
    reservados = _contratos_reservados()
    if corpo.get("contrato_versao") in reservados:
        return True
    for chave in _BLOCOS_CONTRATO:
        bloco = corpo.get(chave)
        if isinstance(bloco, dict) and bloco.get("contrato") in reservados:
            return True
    return False


@agente_bi_api_bp.post("/analises")
@_endpoint
def post_analise(escopo: EscopoAgenteBi):
    corpo = _corpo_json()
    if _identidade_reservada(corpo):
        logger.info(
            "evento=agente_bi_contrato_reservado_recusado usuario_id=%s",
            escopo.usuario_id,
        )
        return _erro("contrato_reservado", 422)
    return jsonify({"ok": True, "analise": criar_analise(escopo, corpo)}), 201


@agente_bi_api_bp.get("/analises")
@_endpoint
def get_analises(escopo: EscopoAgenteBi):
    return jsonify({"ok": True, "analises": listar_analises(escopo, chave=_chave_consulta())})


@agente_bi_api_bp.get("/analises/<int:analise_id>")
@_endpoint
def get_analise(escopo: EscopoAgenteBi, analise_id: int):
    return jsonify({"ok": True, "analise": obter_analise(escopo, analise_id)})


@agente_bi_api_bp.post("/dashboards")
@_endpoint
def post_dashboard(escopo: EscopoAgenteBi):
    return jsonify({"ok": True, "dashboard": criar_dashboard(escopo, _corpo_json())}), 201


@agente_bi_api_bp.get("/dashboards")
@_endpoint
def get_dashboards(escopo: EscopoAgenteBi):
    return jsonify({"ok": True, "dashboards": listar_dashboards(escopo, chave=_chave_consulta())})


@agente_bi_api_bp.get("/dashboards/<int:dashboard_id>")
@_endpoint
def get_dashboard(escopo: EscopoAgenteBi, dashboard_id: int):
    return jsonify({"ok": True, "dashboard": obter_dashboard(escopo, dashboard_id)})


@agente_bi_api_bp.post("/dashboards/montar")
@_endpoint
def post_dashboard_montar(escopo: EscopoAgenteBi):
    from app.services.agente_bi_dashboard_service import montar_dashboard

    return jsonify(montar_dashboard(escopo, _corpo_json())), 201


@agente_bi_api_bp.get("/dashboards/<int:dashboard_id>/consulta")
@_endpoint
def get_dashboard_consulta(escopo: EscopoAgenteBi, dashboard_id: int):
    from app.services.agente_bi_dashboard_service import consultar_dashboard

    return jsonify(consultar_dashboard(escopo, dashboard_id))


def _sem_cache(resposta):
    if isinstance(resposta, tuple):
        corpo = resposta[0]
        corpo.headers["Cache-Control"] = "no-store"
        return corpo, resposta[1]
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@agente_bi_api_bp.get(
    "/dashboards/<int:dashboard_id>/revisoes/<int:revisao>/calculos/<int:calculo_id>/graficos/<ref>"
)
def get_dashboard_grafico_detalhe(dashboard_id: int, revisao: int, calculo_id: int, ref: str):
    from app.services.agente_bi_dashboard_service import consultar_detalhe_grafico

    escopo, erro = _abrir()
    if erro is not None:
        return _sem_cache(erro)
    try:
        corpo = consultar_detalhe_grafico(escopo, dashboard_id, revisao, calculo_id, ref)
    except AgenteBiErro as exc:
        return _sem_cache(_erro(exc.codigo, exc.status))
    return _sem_cache(jsonify(corpo))


@agente_bi_api_bp.post("/dashboards/<int:dashboard_id>/filtros")
@_endpoint
def post_dashboard_filtros(escopo: EscopoAgenteBi, dashboard_id: int):
    from app.services.agente_bi_dashboard_service import filtrar_dashboard

    resultado = filtrar_dashboard(escopo, dashboard_id, _corpo_json())
    status = 201 if resultado.get("persistido") else 200
    return jsonify(resultado), status


@agente_bi_api_bp.post("/dashboards/<int:dashboard_id>/evolucao")
@_endpoint
def post_dashboard_evolucao(escopo: EscopoAgenteBi, dashboard_id: int):
    from app.services.agente_bi_calculo_service import CalculoErro
    from app.services.agente_bi_definir_service import DefinirErro
    from app.services.agente_bi_evolucao_service import EvolucaoErro, evoluir_dashboard

    try:
        resultado = evoluir_dashboard(escopo, dashboard_id, _corpo_json())
    except EvolucaoErro as exc:
        return _erro(exc.codigo, exc.status)
    except CalculoErro as exc:
        return _erro(exc.codigo, exc.status)
    except DefinirErro as exc:
        return _erro(exc.codigo, exc.status)
    status = 201 if resultado.get("situacao") == "aplicada" else 200
    return jsonify(resultado), status


_BLOQUEIOS_ATUALIZACAO = frozenset(
    {"atualizacao_incompativel", "mudanca_significado", "ambiguidade_relevante"}
)


@agente_bi_api_bp.post("/dashboards/<int:dashboard_id>/atualizacao")
@_endpoint_ingestao
def post_dashboard_atualizacao(escopo: EscopoAgenteBi, dashboard_id: int):
    from app.services.agente_bi_atualizacao_service import (
        ENCAMINHAMENTO,
        AtualizacaoErro,
        atualizar_requisicao,
    )
    from app.services.agente_bi_calculo_service import CalculoErro
    from app.services.agente_bi_ingestao_service import IngestaoErro
    from app.services.agente_bi_perfil_semantico_service import PerfilErro

    try:
        resultado = atualizar_requisicao(escopo, dashboard_id)
    except IngestaoErro as exc:
        return _erro(exc.codigo, exc.status)
    except AtualizacaoErro as exc:
        if exc.codigo in _BLOQUEIOS_ATUALIZACAO:
            return jsonify({"ok": False, "codigo": exc.codigo, "encaminhamento": ENCAMINHAMENTO}), exc.status
        return _erro(exc.codigo, exc.status)
    except CalculoErro as exc:
        return _erro(exc.codigo, exc.status)
    except PerfilErro as exc:
        return _erro(exc.codigo, 422)
    status = 201 if resultado.get("recalculado") else 200
    return jsonify(resultado), status


@agente_bi_api_bp.post("/ingestao")
@_endpoint_ingestao
def post_ingestao(escopo: EscopoAgenteBi):
    from app.services.agente_bi_ingestao_service import IngestaoErro, ingerir_requisicao

    try:
        resultado = ingerir_requisicao(escopo)
    except IngestaoErro as exc:
        return _erro(exc.codigo, exc.status)
    status = 201 if resultado["persistido"] and not resultado["reutilizado"] else 200
    return jsonify(resultado), status


def _status_analisar(codigo: str) -> int:
    if codigo in {"autorizacao_indevida", "provedor_nao_autorizado"}:
        return 403
    return 422


@agente_bi_api_bp.post("/descoberta/preparacao")
@_endpoint_ingestao
def post_descoberta_preparacao(escopo: EscopoAgenteBi):
    from app.services.agente_bi_descoberta_service import DescobertaErro, preparar_requisicao
    from app.services.agente_bi_ingestao_service import IngestaoErro

    try:
        resultado = preparar_requisicao(escopo)
    except IngestaoErro as exc:
        return _erro(exc.codigo, exc.status)
    except DescobertaErro as exc:
        return _erro(exc.codigo, exc.status)
    status = 201 if resultado["persistido"] and not resultado["reutilizado"] else 200
    return jsonify(resultado), status


@agente_bi_api_bp.post("/descoberta")
@_endpoint
def post_descoberta(escopo: EscopoAgenteBi):
    from app.services.agente_bi_analisar_service import AnalisarErro
    from app.services.agente_bi_descoberta_service import DescobertaErro, descobrir
    from app.services.agente_bi_perfil_semantico_service import PerfilErro

    try:
        return jsonify(descobrir(escopo, _corpo_json())), 201
    except DescobertaErro as exc:
        return _erro_descoberta(exc)
    except AnalisarErro as exc:
        return _erro(exc.codigo, _status_analisar(exc.codigo))
    except PerfilErro as exc:
        return _erro(exc.codigo, 422)


@agente_bi_api_bp.post("/descoberta/selecao")
@_endpoint
def post_descoberta_selecao(escopo: EscopoAgenteBi):
    from app.services.agente_bi_descoberta_service import DescobertaErro, aprovar_selecao

    try:
        return jsonify(aprovar_selecao(escopo, _corpo_json())), 201
    except DescobertaErro as exc:
        return _erro(exc.codigo, exc.status)


@agente_bi_api_bp.post("/definicao")
@_endpoint
def post_definicao(escopo: EscopoAgenteBi):
    from app.services.agente_bi_definir_service import DefinirErro, definir_selecao
    from app.services.agente_bi_perfil_semantico_service import PerfilErro

    try:
        return jsonify(definir_selecao(escopo, _corpo_json())), 201
    except DefinirErro as exc:
        return _erro(exc.codigo, exc.status)
    except PerfilErro as exc:
        return _erro(exc.codigo, 422)


@agente_bi_api_bp.post("/calculo")
@_endpoint
def post_calculo(escopo: EscopoAgenteBi):
    from app.services.agente_bi_calculo_service import CalculoErro, calcular_definicao
    from app.services.agente_bi_perfil_semantico_service import PerfilErro

    try:
        return jsonify(calcular_definicao(escopo, _corpo_json())), 201
    except CalculoErro as exc:
        return _erro(exc.codigo, exc.status)
    except PerfilErro as exc:
        return _erro(exc.codigo, 422)
