"""Dashboards do AgenteBI (SCRUM-262).

Monta, reabre e filtra painéis a partir da memória `calculo.v1`.
Não chama modelo, não recalcula na consulta e não cria tabela nova.
O filtro só repete o executor determinístico já validado.
O detalhe de um gráfico só relê a série já gravada do cálculo exibido.
"""
from __future__ import annotations

import logging

from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiDashboard
from app.services.agente_bi_calculo_service import (
    CONTRATO_CALCULO,
    EXECUTOR,
    CalculoErro,
    _OPERACOES,
    calcular_definicao,
)
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR
from app.services.agente_bi_persistencia_service import (
    LIMITE_GRAFICOS,
    TIPOS_GRAFICO,
    AgenteBiErro,
    _REF_GRAFICO_RE,
    criar_dashboard,
    obter_analise,
    obter_dashboard,
)

logger = logging.getLogger(__name__)

CONTRATO_DASHBOARD = "dashboard.v1"
_CHAVES_MONTAR = frozenset({"analise_id"})
_CHAVES_FILTRO = frozenset({"parametros", "relacoes_autorizadas", "persistir"})
_STATUS = frozenset({"bloqueado", "calculado"})
_UNIDADES = frozenset({"adimensional", "contagem", "quantidade", "valor"})
_TIPOS_PARAMETRO = frozenset({"booleano", "data", "numero", "texto"})
_CAMPOS_SERIE = ("chave", "elegiveis", "resultado")


def montar_dashboard(escopo, corpo: dict) -> dict:
    """Grava uma revisão do painel com os gráficos da memória já calculada."""
    analise, memoria = _memoria_autorizada(escopo, _analise_pedida(corpo, _CHAVES_MONTAR))
    indicadores = _indicadores_autorizados(analise, memoria)
    _definicao_compativel(escopo, analise, memoria)
    painel = _persistir(escopo, analise, indicadores)
    consulta = _montar_consulta(painel, analise, memoria, indicadores, escopo)
    logger.info(
        "evento=agente_bi_dashboard_montado usuario_id=%s dashboard_id=%s calculo_id=%s graficos=%s",
        escopo.usuario_id,
        painel["id"],
        analise["id"],
        len(consulta["graficos"]),
    )
    return {"ok": True, "persistido": True, "dashboard": painel, "consulta": consulta}


def consultar_dashboard(escopo, dashboard_id: int) -> dict:
    """Reabre o painel lendo a memória gravada. Não executa cálculo nem modelo."""
    painel, analise, memoria, indicadores = _painel_com_memoria(escopo, dashboard_id)
    consulta = _montar_consulta(painel, analise, memoria, indicadores, escopo)
    logger.info(
        "evento=agente_bi_dashboard_consultado usuario_id=%s dashboard_id=%s calculo_id=%s graficos=%s",
        escopo.usuario_id,
        painel["id"],
        analise["id"],
        len(consulta["graficos"]),
    )
    return {"ok": True, "persistido": True, "dashboard": painel, "consulta": consulta}


def filtrar_dashboard(escopo, dashboard_id: int, corpo: dict) -> dict:
    """Aplica filtros pelo executor determinístico e devolve a memória nova."""
    if not isinstance(corpo, dict) or not set(corpo) <= _CHAVES_FILTRO:
        raise AgenteBiErro("payload_invalido", 422)
    persistir = corpo.get("persistir", False)
    if not isinstance(persistir, bool):
        raise AgenteBiErro("payload_invalido", 422)
    parametros = corpo.get("parametros", {})
    if not isinstance(parametros, dict):
        raise AgenteBiErro("parametro_inconsistente", 422)
    painel, _analise, memoria, _indicadores = _painel_com_memoria(escopo, dashboard_id)
    definicao_id = memoria.get("origem_definicao_id")
    if isinstance(definicao_id, bool) or not isinstance(definicao_id, int) or definicao_id < 1:
        raise AgenteBiErro("analise_indisponivel", 422)
    pedido = {"analise_id": definicao_id, "parametros": parametros}
    if "relacoes_autorizadas" in corpo:
        pedido["relacoes_autorizadas"] = corpo.get("relacoes_autorizadas")
    try:
        calculado = calcular_definicao(escopo, pedido)
    except CalculoErro as exc:
        raise AgenteBiErro(exc.codigo, exc.status) from exc
    novo_id = calculado["calculo"]["id"]
    if persistir:
        salvo = montar_dashboard(escopo, {"analise_id": novo_id})
        salvo["persistido"] = True
        logger.info(
            "evento=agente_bi_dashboard_filtrado usuario_id=%s dashboard_id=%s calculo_id=%s persistido=1",
            escopo.usuario_id,
            salvo["dashboard"]["id"],
            novo_id,
        )
        return salvo
    novo_analise, nova_memoria = _memoria_autorizada(escopo, novo_id)
    novos = _indicadores_autorizados(novo_analise, nova_memoria)
    _refs_presentes(painel, novos)
    consulta = _montar_consulta(painel, novo_analise, nova_memoria, novos, escopo)
    logger.info(
        "evento=agente_bi_dashboard_filtrado usuario_id=%s dashboard_id=%s calculo_id=%s persistido=0",
        escopo.usuario_id,
        painel["id"],
        novo_id,
    )
    return {"ok": True, "persistido": False, "dashboard": painel, "consulta": consulta}


def consultar_detalhe_grafico(escopo, dashboard_id: int, revisao: int, calculo_id: int, ref: str) -> dict:
    """Relê um gráfico da memória já gravada. Não recalcula, não filtra e não grava revisão."""
    if isinstance(revisao, bool) or not isinstance(revisao, int) or revisao < 1:
        raise AgenteBiErro("nao_encontrado", 404)
    if isinstance(calculo_id, bool) or not isinstance(calculo_id, int) or calculo_id < 1:
        raise AgenteBiErro("nao_encontrado", 404)
    if not isinstance(ref, str) or _REF_GRAFICO_RE.fullmatch(ref) is None:
        raise AgenteBiErro("nao_encontrado", 404)
    painel, ancora, ancora_memoria, indicadores_ancora = _painel_com_memoria(escopo, dashboard_id)
    if painel.get("revisao") != revisao:
        raise AgenteBiErro("identidade_invalida", 409)
    if calculo_id == ancora["id"]:
        analise, memoria, indicadores = ancora, ancora_memoria, indicadores_ancora
    else:
        analise, memoria = _memoria_autorizada(escopo, calculo_id)
        _definicao_compativel(escopo, analise, memoria)
        _calculo_corresponde(painel, ancora, ancora_memoria, analise, memoria)
        indicadores = _indicadores_autorizados(analise, memoria)
        _refs_presentes(painel, indicadores)
    grafico = _grafico_selecionado(painel, indicadores, ref)
    logger.info(
        "evento=agente_bi_dashboard_detalhe_consultado usuario_id=%s dashboard_id=%s revisao=%s calculo_id=%s",
        escopo.usuario_id,
        painel["id"],
        painel["revisao"],
        analise["id"],
    )
    return {
        "ok": True,
        "detalhe": {
            "contrato": CONTRATO_DASHBOARD,
            "chamada_anthropic": False,
            "ia_calculo": False,
            "executor": EXECUTOR,
            "dashboard_id": painel["id"],
            "revisao": painel["revisao"],
            "chave": painel["chave"],
            "calculo_id": analise["id"],
            "definicao_id": memoria.get("origem_definicao_id"),
            "dataset_id": analise["dataset_id"],
            "versao": analise["versao"],
            "impressao": analise["impressao"],
            "grafico": grafico,
        },
    }


def _calculo_corresponde(painel, ancora, ancora_memoria, analise, memoria) -> None:
    if memoria.get("origem_definicao_id") != ancora_memoria.get("origem_definicao_id"):
        raise AgenteBiErro("identidade_invalida", 409)
    if memoria.get("origem_contrato") != ancora_memoria.get("origem_contrato"):
        raise AgenteBiErro("identidade_invalida", 409)
    if (
        analise["dataset_id"] != ancora["dataset_id"]
        or analise["versao"] != ancora["versao"]
        or analise["impressao"] != ancora["impressao"]
    ):
        raise AgenteBiErro("identidade_invalida", 409)
    _vinculo_dataset(painel, analise)


def _grafico_selecionado(painel, indicadores, ref: str) -> dict:
    definicoes = painel.get("definicoes_graficos")
    if not isinstance(definicoes, list):
        raise AgenteBiErro("analise_indisponivel", 422)
    definicao = next(
        (item for item in definicoes if isinstance(item, dict) and item.get("ref") == ref),
        None,
    )
    if definicao is None:
        raise AgenteBiErro("nao_encontrado", 404)
    indicador = {item["id"]: item for item in indicadores}.get(ref)
    if indicador is None or definicao.get("tipo") != indicador.get("grafico"):
        raise AgenteBiErro("analise_indisponivel", 422)
    return _grafico(definicao, indicador)


def _painel_com_memoria(escopo, dashboard_id: int):
    painel = obter_dashboard(escopo, dashboard_id)
    analise_id = painel.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int):
        raise AgenteBiErro("analise_indisponivel", 422)
    analise, memoria = _memoria_autorizada(escopo, analise_id)
    _vinculo_dataset(painel, analise)
    indicadores = _indicadores_autorizados(analise, memoria)
    _refs_presentes(painel, indicadores)
    _definicao_compativel(escopo, analise, memoria)
    return painel, analise, memoria, indicadores


def _analise_pedida(corpo, chaves) -> int:
    if not isinstance(corpo, dict) or set(corpo) != chaves or "analise_id" not in corpo:
        raise AgenteBiErro("payload_invalido", 422)
    analise_id = corpo.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or analise_id < 1:
        raise AgenteBiErro("payload_invalido", 422)
    return analise_id


def _memoria_autorizada(escopo, analise_id: int):
    analise = obter_analise(escopo, analise_id)
    if analise.get("etapa") != "execucao" or analise.get("estado") != "concluida":
        raise AgenteBiErro("analise_indisponivel", 422)
    if analise.get("contrato_versao") != CONTRATO_CALCULO:
        raise AgenteBiErro("analise_indisponivel", 422)
    memoria = analise.get("resultado")
    if not isinstance(memoria, dict) or memoria.get("contrato") != CONTRATO_CALCULO:
        raise AgenteBiErro("analise_indisponivel", 422)
    if memoria.get("executor") != EXECUTOR or memoria.get("ia_calculo") is not False:
        raise AgenteBiErro("analise_indisponivel", 422)
    if memoria.get("chamada_anthropic") is not False:
        raise AgenteBiErro("analise_indisponivel", 422)
    if (
        memoria.get("dataset_id") != analise["dataset_id"]
        or memoria.get("dataset_versao") != analise["versao"]
        or memoria.get("dataset_impressao") != analise["impressao"]
    ):
        raise AgenteBiErro("identidade_invalida", 409)
    indicadores = memoria.get("indicadores")
    if not isinstance(indicadores, list) or not indicadores:
        raise AgenteBiErro("payload_invalido", 422)
    if len(indicadores) > LIMITE_GRAFICOS:
        raise AgenteBiErro("limite_graficos", 422)
    return analise, memoria


def _definicao_compativel(escopo, analise, memoria) -> None:
    definicao_id = memoria.get("origem_definicao_id")
    if isinstance(definicao_id, bool) or not isinstance(definicao_id, int) or definicao_id < 1:
        raise AgenteBiErro("analise_indisponivel", 422)
    if memoria.get("origem_contrato") != CONTRATO_DEFINIR:
        raise AgenteBiErro("analise_indisponivel", 422)
    origem = obter_analise(escopo, definicao_id)
    if origem.get("etapa") != "definicao" or origem.get("estado") != "concluida":
        raise AgenteBiErro("analise_indisponivel", 422)
    if origem.get("contrato_versao") != CONTRATO_DEFINIR:
        raise AgenteBiErro("analise_indisponivel", 422)
    if (
        origem["dataset_id"] != analise["dataset_id"]
        or origem["versao"] != analise["versao"]
        or origem["impressao"] != analise["impressao"]
    ):
        raise AgenteBiErro("identidade_invalida", 409)
    documento = origem.get("resultado")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR:
        raise AgenteBiErro("analise_indisponivel", 422)


def _indicadores_autorizados(analise, memoria) -> list[dict]:
    bruto = memoria.get("indicadores")
    if not isinstance(bruto, list):
        raise AgenteBiErro("analise_indisponivel", 422)
    vistos = set()
    saida = []
    for item in bruto:
        indicador = _indicador(item, analise)
        if indicador["id"] in vistos:
            raise AgenteBiErro("payload_invalido", 422)
        vistos.add(indicador["id"])
        saida.append(indicador)
    return saida


def _indicador(item, analise) -> dict:
    if not isinstance(item, dict):
        raise AgenteBiErro("analise_indisponivel", 422)
    identificador = item.get("id")
    status = item.get("status")
    grafico = item.get("grafico")
    if not isinstance(identificador, str) or not identificador or len(identificador) > 40:
        raise AgenteBiErro("analise_indisponivel", 422)
    if status not in _STATUS or grafico not in TIPOS_GRAFICO:
        raise AgenteBiErro("analise_indisponivel", 422)
    operacao = item.get("operacao")
    if status == "calculado" and operacao not in _OPERACOES:
        raise AgenteBiErro("analise_indisponivel", 422)
    if operacao is not None and operacao not in _OPERACOES:
        raise AgenteBiErro("analise_indisponivel", 422)
    if (
        item.get("dataset_id") != analise["dataset_id"]
        or item.get("dataset_versao") != analise["versao"]
        or item.get("dataset_impressao") != analise["impressao"]
    ):
        raise AgenteBiErro("identidade_invalida", 409)
    _texto_ou_nulo(item.get("resultado"))
    _texto_ou_nulo(item.get("total"))
    serie = item.get("serie")
    avisos = item.get("avisos")
    if not isinstance(serie, list) or not isinstance(avisos, list):
        raise AgenteBiErro("analise_indisponivel", 422)
    if any(not isinstance(aviso, str) for aviso in avisos):
        raise AgenteBiErro("analise_indisponivel", 422)
    for ponto in serie:
        if not isinstance(ponto, dict):
            raise AgenteBiErro("analise_indisponivel", 422)
        if "resultado" in ponto:
            _texto_ou_nulo(ponto.get("resultado"))
    return item


def _persistir(escopo, analise, indicadores) -> dict:
    return criar_dashboard(
        escopo,
        {
            "chave": analise["chave"],
            "revisao": _proxima_revisao(escopo, analise["chave"]),
            "estado_publicacao": "publicado",
            "contrato_versao": CONTRATO_DASHBOARD,
            "executor_versao": EXECUTOR,
            "definicoes_graficos": [_definicao_persistida(item) for item in indicadores],
            "datasets": [
                {
                    "dataset_id": analise["dataset_id"],
                    "versao": analise["versao"],
                    "impressao": analise["impressao"],
                }
            ],
            "analise_id": analise["id"],
        },
    )


def _definicao_persistida(indicador) -> dict:
    tipo = indicador["grafico"]
    operacao = indicador.get("operacao") or "indefinida"
    corpo = {
        "indicador": indicador["id"],
        "operacao": operacao,
        "estado": indicador["status"],
    }
    unidade = indicador.get("unidade")
    if unidade in _UNIDADES:
        corpo["unidade"] = unidade
    return {
        "ref": indicador["id"],
        "titulo": _titulo(tipo, operacao),
        "tipo": tipo,
        "definicao": corpo,
    }


def _titulo(tipo: str, operacao: str) -> str:
    return f"{tipo}-{operacao}"[:120]


def _proxima_revisao(escopo, chave: str) -> int:
    atual = (
        db.session.query(func.max(AgenteBiDashboard.revisao))
        .filter(
            AgenteBiDashboard.conta_id == escopo.conta_id,
            AgenteBiDashboard.franquia_id == escopo.franquia_id,
            AgenteBiDashboard.usuario_id == escopo.usuario_id,
            AgenteBiDashboard.chave == chave,
        )
        .scalar()
    )
    if atual is None:
        return 1
    return int(atual) + 1


def _vinculo_dataset(painel, analise) -> None:
    datasets = painel.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise AgenteBiErro("identidade_invalida", 409)
    compativel = any(
        isinstance(item, dict)
        and item.get("dataset_id") == analise["dataset_id"]
        and item.get("versao") == analise["versao"]
        and item.get("impressao") == analise["impressao"]
        for item in datasets
    )
    if not compativel:
        raise AgenteBiErro("identidade_invalida", 409)


def _refs_presentes(painel, indicadores) -> None:
    definicoes = painel.get("definicoes_graficos")
    if not isinstance(definicoes, list) or not definicoes:
        raise AgenteBiErro("analise_indisponivel", 422)
    if len(definicoes) > LIMITE_GRAFICOS:
        raise AgenteBiErro("limite_graficos", 422)
    por_id = {item["id"]: item for item in indicadores}
    for definicao in definicoes:
        if not isinstance(definicao, dict):
            raise AgenteBiErro("analise_indisponivel", 422)
        indicador = por_id.get(definicao.get("ref"))
        if indicador is None:
            raise AgenteBiErro("analise_indisponivel", 422)
        if definicao.get("tipo") != indicador.get("grafico"):
            raise AgenteBiErro("analise_indisponivel", 422)


def _montar_consulta(painel, analise, memoria, indicadores, escopo) -> dict:
    por_id = {item["id"]: item for item in indicadores}
    graficos = []
    for definicao in painel["definicoes_graficos"]:
        graficos.append(_grafico(definicao, por_id[definicao["ref"]]))
    return {
        "contrato": CONTRATO_DASHBOARD,
        "chamada_anthropic": False,
        "ia_calculo": False,
        "executor": EXECUTOR,
        "dashboard_id": painel["id"],
        "revisao": painel["revisao"],
        "chave": painel["chave"],
        "calculo_id": analise["id"],
        "definicao_id": memoria.get("origem_definicao_id"),
        "dataset_id": analise["dataset_id"],
        "versao": analise["versao"],
        "impressao": analise["impressao"],
        "graficos": graficos,
        "filtros": _filtros_disponiveis(escopo, memoria),
    }


def _grafico(definicao, indicador) -> dict:
    serie = indicador.get("serie")
    avisos = indicador.get("avisos")
    if not isinstance(serie, list) or not isinstance(avisos, list):
        raise AgenteBiErro("analise_indisponivel", 422)
    return {
        "ref": definicao["ref"],
        "titulo": definicao["titulo"],
        "tipo": indicador["grafico"],
        "status": indicador["status"],
        "motivo": indicador.get("motivo"),
        "operacao": indicador.get("operacao"),
        "unidade": indicador.get("unidade"),
        "granularidade": indicador.get("granularidade"),
        "resultado": _texto_ou_nulo(indicador.get("resultado")),
        "total": _texto_ou_nulo(indicador.get("total")),
        "serie": [_ponto(item) for item in serie],
        "avisos": [item for item in avisos if isinstance(item, str)],
        "elegiveis": indicador.get("elegiveis") if isinstance(indicador.get("elegiveis"), int) else None,
        "grupos": indicador.get("grupos") if isinstance(indicador.get("grupos"), int) else None,
        "filtros": indicador.get("filtros") if isinstance(indicador.get("filtros"), list) else [],
        "juncao": indicador.get("juncao") if isinstance(indicador.get("juncao"), str) else None,
    }


def _ponto(item) -> dict:
    if not isinstance(item, dict):
        raise AgenteBiErro("analise_indisponivel", 422)
    ponto = {chave: item.get(chave) for chave in _CAMPOS_SERIE if chave in item}
    if "resultado" in ponto:
        ponto["resultado"] = _texto_ou_nulo(ponto["resultado"])
    return ponto


def _texto_ou_nulo(valor):
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor
    raise AgenteBiErro("analise_indisponivel", 422)


def _filtros_disponiveis(escopo, memoria) -> list[dict]:
    origem = obter_analise(escopo, memoria["origem_definicao_id"])
    documento = origem.get("resultado")
    if not isinstance(documento, dict):
        return []
    vistos = set()
    saida = []
    for definicao in documento.get("definicoes") or []:
        if not isinstance(definicao, dict):
            continue
        filtros = definicao.get("filtros") if isinstance(definicao.get("filtros"), list) else []
        for parametro in definicao.get("parametros") or []:
            if not isinstance(parametro, dict):
                continue
            nome = parametro.get("nome")
            tipo = parametro.get("tipo")
            if not isinstance(nome, str) or nome in vistos or tipo not in _TIPOS_PARAMETRO:
                continue
            vistos.add(nome)
            operador = None
            for filtro in filtros:
                if not isinstance(filtro, dict):
                    continue
                if filtro.get("parametro") == nome or filtro.get("parametro_fim") == nome:
                    operador = filtro.get("operador") if isinstance(filtro.get("operador"), str) else None
                    break
            saida.append(
                {
                    "nome": nome,
                    "tipo": tipo,
                    "obrigatorio": parametro.get("obrigatorio") is True,
                    "operador": operador,
                    "indicador": definicao.get("id") if isinstance(definicao.get("id"), str) else None,
                }
            )
    return saida
