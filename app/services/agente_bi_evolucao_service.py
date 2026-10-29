"""Evolução conversacional de dashboards do AgenteBI (SCRUM-265).

O pedido em texto livre vira um patch fechado sobre o contrato `definir.v1`.
Filtro, período, quebra e indicador que já cabem no catálogo e no dataset
autorizado seguem sem modelo. Paráfrase sem coluna ou operação identificável
passa só pelo adaptador Anthropic governado. Métrica, unidade, período ou
relação ambígua pede esclarecimento e não publica número.

O patch aprovado regrava a definição, repete o executor determinístico e
abre outra revisão. A revisão anterior e o dataset original permanecem.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import unicodedata
from datetime import date

from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_analisar_service import (
    PROVEDOR_ANTHROPIC,
    PROVEDOR_SIMULADO,
    TransferenciaRecusada,
    governar_representacao,
    texto_de_cabecalho_transferivel,
)
from app.services.agente_bi_calculo_service import CalculoErro, _coagir, calcular_definicao
from app.services.agente_bi_dashboard_service import consultar_dashboard, filtrar_dashboard, montar_dashboard
from app.services.agente_bi_leitura_service import CONTRATO_DEFINIR_V2, LeituraErro, exigir_definicao_legivel
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    OPERACOES_EXECUCAO,
    DefinirErro,
    _FILTROS,
    _FILTROS_ORDENADOS,
    _forma,
    _grafico_compativel,
    _granularidade_temporal,
    _leitura,
    _operacao_compativel,
    _recusar_executavel,
    _ref_de_coluna,
    _tipo_coluna,
    _unidade,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.cleiton_ai_data_governance import govern_outbound_content
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    listar_dashboards,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)

logger = logging.getLogger(__name__)

CONTRATO_EVOLUCAO = "evolucao.v1"
CONTRATO_PATCH = "patch.definir.v1"
CONTRATO_CONTEXTO = "contexto.evolucao.v1"
_CHAVES_PEDIDO = frozenset({"texto", "parametros"})
_CHAVES_PATCH = frozenset({"acao", "coluna", "operador", "valor", "valor_fim", "operacao"})
_ACOES = frozenset({"definir_filtro", "alterar_periodo", "alterar_agrupamento", "alterar_indicador", "esclarecer"})
_OPERACOES_TEXTO = (
    ("contagem distinta", "contagem_distinta"),
    ("contagem condicional", "count_if"),
    ("count_if", "count_if"),
    ("participacao", "participacao"),
    ("contagem", "contagem"),
    ("minimo", "minimo"),
    ("maximo", "maximo"),
    ("media", "media"),
    ("razao", "razao"),
    ("soma", "soma"),
)
_OPERADORES_TEXTO = (
    ("maior ou igual", "maior_ou_igual"),
    ("menor ou igual", "menor_ou_igual"),
    ("maior que", "maior"),
    ("acima de", "maior"),
    ("menor que", "menor"),
    ("abaixo de", "menor"),
    ("diferente de", "diferente"),
    ("igual a", "igual"),
    ("igual", "igual"),
    ("entre", "entre"),
)
_VERBO_MUDANCA = (
    "troque",
    "trocar",
    "mude",
    "mudar",
    "altere",
    "alterar",
    "substitua",
    "use",
    "usar",
    "quero",
    "mostre",
    "mostrar",
)
_SINAIS_SEMANTICOS = _VERBO_MUDANCA + ("compare", "comparar", "foque", "destaque", "tendencia", "tipico", "recorte")
_PROIBIDOS = ("select", "drop", "insert", "delete", "update", "exec", "eval", "import", "python", "sql")
_IMAGEM = ("ocr", "imagem", "foto", "print", "screenshot")
_JUNCAO = ("juncao", "join", "relacione", "relacionar", "cruze", "cruzar")
_ARQUIVO = ("planilha", "xlsx", "csv", "arquivo")
_DATA_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATA_BARRA = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_PERGUNTAS = {
    "metrica_nao_identificada": "Não identifiquei a métrica no catálogo autorizado. Indique soma, média, contagem, mínimo, máximo ou participação.",
    "periodo_nao_identificado": "Não identifiquei o período com segurança. Informe as datas no formato AAAA-MM-DD.",
    "data_ambigua": "A data pode ser lida de dois jeitos. Informe no formato AAAA-MM-DD.",
    "coluna_nao_identificada": "Não identifiquei a coluna com segurança. Use o cabeçalho autorizado.",
    "agrupamento_ambiguo": "Há mais de uma coluna com esse nome. Indique o cabeçalho exato.",
    "indicador_ambiguo": "Há mais de um indicador compatível. Diga qual indicador deve mudar.",
    "filtro_incompleto": "Falta o operador do filtro. Use igual, diferente, maior, menor ou entre.",
    "informar_parametro": "Informe a coluna, o operador e o valor. Filtros e período não ficam salvos: ao recarregar a página, informe esses valores outra vez.",
    "pedido_repetido": "Esse pedido já foi interpretado. Reformule o pedido ou informe a coluna e a operação.",
    "interpretacao_indisponivel": "Esse pedido precisa de interpretação e o adaptador governado não está disponível. Cite a coluna e a operação.",
    "relacao_nao_identificada": "Não identifiquei a relação com segurança. Não vou presumir vínculo entre as bases.",
    "pedido_nao_identificado": "Não identifiquei filtro, período, quebra ou indicador nesse pedido.",
    "dataset_inalterado": "Este pedido não altera o arquivo original. A mudança fica só na revisão do dashboard.",
    "unidade_nao_identificada": "Não identifiquei a unidade dessa métrica. Não vou publicar um indicador sem unidade.",
}


class EvolucaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def evoluir_dashboard(escopo, dashboard_id: int, corpo: dict, *, transporte=None) -> dict:
    """Aplica um pedido textual ao dashboard do escopo, ou pede esclarecimento."""
    texto, informados = _pedido(corpo)
    carregado = _carregar(escopo, dashboard_id)
    if carregado["definicao"].get("contrato_versao") != CONTRATO_DEFINIR:
        raise EvolucaoErro("analise_indisponivel")
    herdados = _validar_parametros_informados(carregado["definicoes"], informados)
    classificacao = _classificar(texto, carregado)
    if classificacao["tipo"] == "invalido":
        raise EvolucaoErro("alteracao_invalida")
    if classificacao["tipo"] == "proibido":
        raise EvolucaoErro("nao_autorizada", 403)
    if classificacao["tipo"] == "explicar":
        return _responder_explicacao(carregado, texto)
    if classificacao["tipo"] == "esclarecer":
        return _responder_esclarecimento(carregado, classificacao["motivo"], chamada=False, cobranca=False)
    if classificacao["tipo"] == "semantico":
        patch, chamada, cobranca = _patch_do_modelo(escopo, carregado, texto, transporte)
        if patch == "pedido_repetido":
            return _responder_esclarecimento(carregado, "pedido_repetido", chamada=False, cobranca=False)
        if patch is None:
            return _responder_esclarecimento(carregado, "interpretacao_indisponivel", chamada=chamada, cobranca=cobranca)
        if patch.get("acao") == "esclarecer":
            return _responder_esclarecimento(carregado, "metrica_nao_identificada", chamada=chamada, cobranca=cobranca)
        via = "interpretacao"
    else:
        patch = classificacao["patch"]
        chamada = False
        cobranca = False
        via = "deterministica"
    try:
        aplicado = _aplicar(escopo, carregado, patch, herdados)
    except DefinirErro as exc:
        if exc.codigo == "autorizacao_indevida":
            raise EvolucaoErro("nao_autorizada", 403) from exc
        raise EvolucaoErro("alteracao_invalida") from exc
    if aplicado["tipo"] == "esclarecer":
        return _responder_esclarecimento(carregado, aplicado["motivo"], chamada=chamada, cobranca=cobranca)
    if aplicado["tipo"] == "filtrar":
        salvo = filtrar_dashboard(
            escopo,
            carregado["painel"]["id"],
            {"parametros": aplicado["parametros"], "persistir": True},
        )
    else:
        definicao = _persistir_definicao(escopo, carregado, aplicado["documento"], chamada=chamada)
        try:
            calculado = calcular_definicao(escopo, {"analise_id": definicao["id"], "parametros": aplicado["parametros"]})
        except CalculoErro as exc:
            raise EvolucaoErro(exc.codigo, exc.status) from exc
        if _publicacao_vazia(calculado, aplicado["alvos"]):
            motivo = _motivo_bloqueio(calculado, aplicado["alvos"])
            return _responder_esclarecimento(carregado, motivo, chamada=chamada, cobranca=cobranca)
        salvo = montar_dashboard(escopo, {"analise_id": calculado["calculo"]["id"]})
    logger.info(
        "evento=agente_bi_dashboard_evoluido usuario_id=%s dashboard_id=%s via=%s chamada_anthropic=%s revisao=%s",
        escopo.usuario_id,
        salvo["dashboard"]["id"],
        via,
        int(chamada),
        salvo["dashboard"]["revisao"],
    )
    return _responder_aplicada(
        escopo,
        carregado,
        salvo,
        patch,
        via=via,
        chamada=chamada,
        cobranca=cobranca,
        parametros=aplicado.get("parametros") or {},
    )


def _pedido(corpo) -> tuple[str, dict]:
    if not isinstance(corpo, dict) or "texto" not in corpo or not set(corpo) <= _CHAVES_PEDIDO:
        raise EvolucaoErro("payload_invalido")
    texto = corpo.get("texto")
    if not isinstance(texto, str) or not texto.strip() or len(texto) > 500 or "\x00" in texto:
        raise EvolucaoErro("payload_invalido")
    if "parametros" not in corpo:
        return texto.strip(), {}
    parametros = corpo.get("parametros")
    if not isinstance(parametros, dict):
        raise EvolucaoErro("parametro_inconsistente")
    return texto.strip(), parametros


def _carregar(escopo, dashboard_id: int) -> dict:
    painel = obter_dashboard(escopo, dashboard_id)
    consulta = consultar_dashboard(escopo, dashboard_id)
    analise_id = painel.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int):
        raise AgenteBiErro("analise_indisponivel", 422)
    calculo = obter_analise(escopo, analise_id)
    memoria = calculo.get("resultado") if isinstance(calculo.get("resultado"), dict) else {}
    definicao_id = memoria.get("origem_definicao_id")
    if isinstance(definicao_id, bool) or not isinstance(definicao_id, int) or definicao_id < 1:
        raise AgenteBiErro("analise_indisponivel", 422)
    definicao = obter_analise(escopo, definicao_id)
    try:
        exigir_definicao_legivel(escopo, definicao)
    except LeituraErro as exc:
        raise AgenteBiErro(exc.codigo, exc.status) from exc
    documento = definicao.get("resultado")
    contrato = definicao.get("contrato_versao")
    if contrato == CONTRATO_DEFINIR:
        if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR:
            raise AgenteBiErro("analise_indisponivel", 422)
    elif contrato == CONTRATO_DEFINIR_V2:
        if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR_V2:
            raise AgenteBiErro("analise_indisponivel", 422)
    else:
        raise AgenteBiErro("analise_indisponivel", 422)
    definicoes = documento.get("definicoes")
    if not isinstance(definicoes, list) or not definicoes:
        raise AgenteBiErro("analise_indisponivel", 422)
    dataset = obter_dataset(escopo, calculo["dataset_id"])
    if dataset["id"] != calculo["dataset_id"] or dataset["versao"] != calculo["versao"] or dataset["impressao"] != calculo["impressao"]:
        raise AgenteBiErro("identidade_invalida", 409)
    perfil = perfilar_dataset(escopo, dataset["id"])
    colunas = [item for item in perfil.get("colunas") or [] if isinstance(item, dict)]
    autorizadas = _colunas_da_regiao(colunas, definicoes)
    return {
        "painel": painel,
        "consulta": consulta["consulta"],
        "definicao": definicao,
        "documento": documento,
        "definicoes": definicoes,
        "dataset": dataset,
        "perfil": perfil,
        "colunas": colunas,
        "autorizadas": autorizadas,
        "rotulos": _mapa_rotulos(dataset.get("estrutura"), colunas),
    }


def _classificar(texto: str, carregado: dict) -> dict:
    normal = _norm(texto)
    if _contem_termo(normal, _PROIBIDOS) or _contem_termo(normal, _IMAGEM):
        return {"tipo": "invalido"}
    if _contem_termo(normal, _JUNCAO) or "outra aba" in normal:
        return {"tipo": "proibido"}
    if re.match(r"^(explique|explicar|por que|porque|o que significa|qual o resultado|qual o nome|quem)\b", normal):
        return {"tipo": "explicar"}
    periodo = _periodo(normal)
    if periodo is not None:
        return _classificar_periodo(periodo, carregado)
    quebra = re.search(r"\b(?:agrupe|agrupar|agrupamento|quebre|quebrar|quebra)\b(?:\s+por)?\s+(.+)$", normal)
    if quebra or re.search(r"\b(sem agrupamento|sem quebra|total geral|remova a quebra|remova o agrupamento)\b", normal):
        return _classificar_quebra(normal, quebra, carregado)
    filtro = _filtro(normal)
    if filtro is not None:
        return _classificar_filtro(filtro, carregado)
    operacao = _operacao_pedida(normal)
    if operacao and _contem_termo(normal, _VERBO_MUDANCA + ("indicador", "metrica", "agregacao")):
        return {"tipo": "aplicar", "patch": _patch("alterar_indicador", operacao=operacao)}
    if re.search(r"\bindicador de\b", normal) or re.search(r"\b(indicador|metrica|agregacao)\b", normal):
        return {"tipo": "esclarecer", "motivo": "metrica_nao_identificada"}
    if _contem_termo(normal, _ARQUIVO):
        return {"tipo": "esclarecer", "motivo": "dataset_inalterado"}
    if _contem_termo(normal, _SINAIS_SEMANTICOS):
        return {"tipo": "semantico"}
    return {"tipo": "esclarecer", "motivo": "pedido_nao_identificado"}


def _classificar_periodo(periodo, carregado) -> dict:
    if periodo == "ambigua":
        return {"tipo": "esclarecer", "motivo": "data_ambigua"}
    if periodo == "invalida":
        return {"tipo": "invalido"}
    coluna = _unica_data(carregado["autorizadas"])
    if coluna == "ambiguo":
        return {"tipo": "esclarecer", "motivo": "periodo_nao_identificado"}
    if coluna is None:
        return {"tipo": "esclarecer", "motivo": "periodo_nao_identificado"}
    inicio, fim = periodo
    if inicio and fim:
        return {"tipo": "aplicar", "patch": _patch("alterar_periodo", coluna=coluna, operador="entre", valor=inicio, valor_fim=fim)}
    if inicio:
        return {"tipo": "aplicar", "patch": _patch("alterar_periodo", coluna=coluna, operador="maior_ou_igual", valor=inicio)}
    return {"tipo": "aplicar", "patch": _patch("alterar_periodo", coluna=coluna, operador="menor_ou_igual", valor=fim)}


def _classificar_quebra(normal, quebra, carregado) -> dict:
    if quebra is None:
        return {"tipo": "aplicar", "patch": _patch("alterar_agrupamento", coluna=None)}
    achado = _casar(quebra.group(1), carregado)
    if achado == "ambiguo":
        return {"tipo": "esclarecer", "motivo": "agrupamento_ambiguo"}
    if achado == "fora":
        return {"tipo": "proibido"}
    if achado is None:
        return {"tipo": "esclarecer", "motivo": "coluna_nao_identificada"}
    return {"tipo": "aplicar", "patch": _patch("alterar_agrupamento", coluna=achado)}


def _classificar_filtro(filtro, carregado) -> dict:
    if filtro == "incompleto":
        return {"tipo": "esclarecer", "motivo": "filtro_incompleto"}
    nome, operador, valor, valor_fim = filtro
    achado = _casar(nome, carregado)
    if achado == "ambiguo":
        return {"tipo": "esclarecer", "motivo": "coluna_nao_identificada"}
    if achado == "fora":
        return {"tipo": "proibido"}
    if achado is None:
        return {"tipo": "esclarecer", "motivo": "coluna_nao_identificada"}
    if operador in _FILTROS_ORDENADOS and _tipo_coluna(achado) not in {"numero", "data"}:
        return {"tipo": "invalido"}
    if operador == "entre" and not valor_fim:
        return {"tipo": "esclarecer", "motivo": "filtro_incompleto"}
    return {
        "tipo": "aplicar",
        "patch": _patch("definir_filtro", coluna=achado, operador=operador, valor=valor, valor_fim=valor_fim),
    }


def _aplicar(escopo, carregado, patch, herdados) -> dict:
    acao = patch.get("acao")
    if acao not in _ACOES or acao == "esclarecer":
        raise EvolucaoErro("alteracao_invalida")
    _recusar_executavel(patch)
    documento = copy.deepcopy(carregado["documento"])
    definicoes = documento.get("definicoes")
    if not isinstance(definicoes, list):
        raise EvolucaoErro("alteracao_invalida")
    if acao in {"definir_filtro", "alterar_periodo"}:
        return _aplicar_filtro(carregado, documento, definicoes, patch, herdados)
    alvos = _alvos_unicos(definicoes)
    if alvos == "ambiguo":
        return {"tipo": "esclarecer", "motivo": "indicador_ambiguo"}
    if acao == "alterar_agrupamento":
        if not _reescrever_agrupamento(alvos[0], patch.get("coluna"), carregado["perfil"]):
            return {"tipo": "esclarecer", "motivo": "coluna_nao_identificada"}
    else:
        resultado = _reescrever_indicador(alvos[0], patch, carregado["perfil"])
        if resultado != "ok":
            return {"tipo": "esclarecer", "motivo": resultado}
    _recusar_executavel(documento)
    if documento.get("dataset_id") != carregado["dataset"]["id"]:
        raise EvolucaoErro("alteracao_invalida")
    parametros = _parametros_herdados(definicoes, herdados)
    if _faltam_parametros(definicoes, parametros):
        return {"tipo": "esclarecer", "motivo": "informar_parametro"}
    return {
        "tipo": "redefinir",
        "documento": documento,
        "parametros": parametros,
        "alvos": [item["id"] for item in alvos],
    }


def _aplicar_filtro(carregado, documento, definicoes, patch, herdados) -> dict:
    coluna = patch.get("coluna")
    operador = patch.get("operador")
    if not isinstance(coluna, dict) or operador not in _FILTROS:
        raise EvolucaoErro("alteracao_invalida")
    tipo = _tipo_coluna(coluna)
    if tipo is None or (operador in _FILTROS_ORDENADOS and tipo not in {"numero", "data"}):
        raise EvolucaoErro("alteracao_invalida")
    valor, valor_fim = _valores_do_patch(patch, tipo)
    if valor is None or (operador == "entre" and valor_fim is None):
        return {"tipo": "esclarecer", "motivo": "filtro_incompleto"}
    alvos = [
        item
        for item in definicoes
        if isinstance(item, dict) and item.get("status") == "definida" and _mesma_regiao(item, coluna)
    ]
    if not alvos:
        raise EvolucaoErro("nao_autorizada", 403)
    if _so_troca_valor(alvos, coluna, operador):
        parametros = _parametros_herdados(definicoes, herdados)
        for item in alvos:
            filtro = _filtro_da_coluna(item, coluna, operador)
            parametros[filtro["parametro"]] = valor
            if operador == "entre":
                parametros[filtro["parametro_fim"]] = valor_fim
        if _faltam_parametros(definicoes, parametros):
            return {"tipo": "esclarecer", "motivo": "informar_parametro"}
        return {"tipo": "filtrar", "parametros": parametros}
    for item in alvos:
        _gravar_filtro(item, coluna, operador, tipo, valor_fim is not None)
    parametros = _parametros_do_documento(definicoes, herdados)
    parametros.update(_valores_novos(alvos, coluna, valor, valor_fim))
    if _faltam_parametros(definicoes, parametros):
        return {"tipo": "esclarecer", "motivo": "informar_parametro"}
    _recusar_executavel(documento)
    return {
        "tipo": "redefinir",
        "documento": documento,
        "parametros": parametros,
        "alvos": [item["id"] for item in alvos if isinstance(item.get("id"), str)],
    }


def _reescrever_agrupamento(definicao, coluna, perfil) -> bool:
    medida = _coluna_perfil(perfil, definicao["metrica"]["coluna"])
    segunda = _segunda(definicao, perfil)
    operacao = definicao["metrica"]["operacao"]
    if medida is None or not _operacao_compativel(operacao, medida, segunda):
        return False
    if coluna is not None and _coord(coluna) == _coord(medida):
        return False
    if coluna is not None and not _mesma_regiao(definicao, coluna):
        raise EvolucaoErro("nao_autorizada", 403)
    grafico = _grafico_para(definicao.get("grafico"), operacao, medida, coluna, segunda)
    if grafico is None:
        return False
    granularidade, dimensoes = _forma(grafico, operacao, medida, coluna, segunda)
    unidade = _unidade(operacao, medida)
    if unidade is None:
        return False
    definicao["grafico"] = grafico
    definicao["leitura"] = _leitura(grafico, operacao)
    definicao["granularidade"] = granularidade
    definicao["dimensoes"] = dimensoes
    definicao["metrica"]["unidade"] = unidade
    definicao["status"] = "definida"
    definicao["esclarecimento"] = None
    return True


def _reescrever_indicador(definicao, patch, perfil) -> str:
    operacao = patch.get("operacao")
    if operacao not in OPERACOES_EXECUCAO:
        return "metrica_nao_identificada"
    coluna = patch.get("coluna")
    medida = _coluna_perfil(perfil, definicao["metrica"]["coluna"]) if coluna is None else coluna
    if medida is None:
        return "coluna_nao_identificada"
    if not _mesma_regiao(definicao, medida):
        raise EvolucaoErro("nao_autorizada", 403)
    agrupamento = _agrupamento(definicao, perfil)
    segunda = _segunda(definicao, perfil)
    if operacao in {"count_if", "razao", "par_numerico"}:
        return "informar_parametro" if operacao == "count_if" else "relacao_nao_identificada"
    if operacao == "participacao" and agrupamento is None and not definicao.get("filtros"):
        return "informar_parametro"
    if not _operacao_compativel(operacao, medida, segunda if operacao == "razao" else None):
        return "metrica_nao_identificada"
    unidade = _unidade(operacao, medida)
    if unidade is None:
        return "unidade_nao_identificada"
    grafico = _grafico_para(definicao.get("grafico"), operacao, medida, agrupamento, None)
    if grafico is None:
        return "metrica_nao_identificada"
    granularidade, dimensoes = _forma(grafico, operacao, medida, agrupamento, None)
    definicao["grafico"] = grafico
    definicao["leitura"] = _leitura(grafico, operacao)
    definicao["granularidade"] = granularidade
    definicao["dimensoes"] = dimensoes
    definicao["metrica"] = {"operacao": operacao, "coluna": _ref_de_coluna(medida), "unidade": unidade}
    definicao["status"] = "definida"
    definicao["esclarecimento"] = None
    return "ok"


def preparar_contexto_semantico(carregado, texto) -> dict | None:
    """Representação autorizada da evolução. Não chama o provedor nem altera a rota."""
    try:
        candidato = _contexto_minimizado(carregado, texto)
        autorizado = governar_representacao(
            candidato,
            governar=govern_outbound_content,
            limpeza=frozenset({"nome", "intencao", "nome_arquivo", "nome_aba", "pedido"}),
            nulas=frozenset({"descricao", "unidade"}),
        )
    except (TransferenciaRecusada, KeyError, TypeError):
        return None
    payload = autorizado["payload"]
    original = candidato.get("intencao")
    obtida = payload.get("intencao") if isinstance(payload, dict) else None
    if isinstance(original, str) and original.strip() and not (isinstance(obtida, str) and obtida.strip()):
        return None
    return payload


def _patch_do_modelo(escopo, carregado, texto, transporte):
    from app.services.agente_bi_anthropic_service import chamar_anthropic_governado, preparar_saida_estruturada

    from os import getenv

    if _vaza_rotulo(texto, carregado["rotulos"]):
        return None, False, False
    governado = preparar_contexto_semantico(carregado, texto)
    if not isinstance(governado, dict) or not (governado.get("pedido") or governado.get("intencao")):
        return None, False, False
    modelo = (getenv("AGENTE_BI_ANTHROPIC_MODEL") or "").strip()
    mensagem = json.dumps(governado, ensure_ascii=True, sort_keys=True)
    esquema = {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_CHAVES_PATCH),
        "properties": {
            "acao": {"type": "string", "enum": sorted(_ACOES)},
            "coluna": {"type": ["integer", "null"]},
            "operador": {"type": ["string", "null"]},
            "valor": {"type": ["string", "null"]},
            "valor_fim": {"type": ["string", "null"]},
            "operacao": {"type": ["string", "null"]},
        },
    }
    resultado = chamar_anthropic_governado(
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        mensagem=mensagem,
        system=_instrucao(),
        chave_idempotencia=_chave(carregado, texto),
        transporte=transporte,
        max_tokens=512,
        output_config=preparar_saida_estruturada(modelo, esquema) if modelo else None,
    )
    if resultado.get("codigo") in {"admissao_recusada", "isolamento", "identidade_incoerente"}:
        raise EvolucaoErro("nao_autorizada", 403)
    if resultado.get("incerto"):
        raise EvolucaoErro("efeito_incerto", 503)
    if resultado.get("repetida") is True:
        return "pedido_repetido", False, False
    if not resultado.get("ok"):
        return None, False, False
    cobranca = bool(resultado.get("evento_id"))
    try:
        patch = _patch_interpretado(resultado.get("texto"), carregado)
    except TypeError:
        patch = None
    if patch is None:
        raise EvolucaoErro("alteracao_invalida")
    return patch, True, cobranca


def _contexto_minimizado(carregado, texto) -> dict:
    dataset = carregado.get("dataset") if isinstance(carregado.get("dataset"), dict) else {}
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    colunas = []
    for indice, coluna in enumerate(carregado["autorizadas"]):
        colunas.append(
            {
                "indice": indice,
                "referencia": {
                    "arquivo": coluna.get("arquivo"),
                    "aba": coluna.get("aba"),
                    "regiao": coluna.get("regiao"),
                    "indice": coluna.get("indice"),
                },
                "papel": coluna.get("dimensao_principal"),
                "tipos": list(coluna.get("tipos") or []),
                "evidencias": list(coluna.get("evidencias") or []),
                "nome": texto_de_cabecalho_transferivel(),
                "nome_arquivo": None,
                "nome_aba": None,
                "descricao": None,
                "unidade": None,
            }
        )
    indicadores = []
    for item in carregado["definicoes"]:
        if not isinstance(item, dict):
            continue
        medida = item.get("metrica") if isinstance(item.get("metrica"), dict) else {}
        coluna_definida = medida.get("coluna") if isinstance(medida.get("coluna"), dict) else None
        unidade = medida.get("unidade") if isinstance(medida.get("unidade"), str) else None
        indicadores.append(
            {
                "id": item.get("id"),
                "operacao": medida.get("operacao"),
                "grafico": item.get("grafico"),
                "coluna": _indice_autorizado(carregado["autorizadas"], medida.get("coluna")),
                "referencia": None
                if coluna_definida is None
                else {
                    "arquivo": coluna_definida.get("arquivo"),
                    "aba": coluna_definida.get("aba"),
                    "regiao": coluna_definida.get("regiao"),
                    "indice": coluna_definida.get("indice"),
                },
                "unidade_registrada": unidade,
            }
        )
    return {
        "contrato": CONTRATO_CONTEXTO,
        "rotulos": False,
        "valores_de_celula": False,
        "amostras": False,
        "apto_anthropic": False,
        "autorizado_para_provedor": False,
        "transferencia": "bloqueada",
        "pedido": _norm(texto),
        "intencao": texto if isinstance(texto, str) and texto.strip() else None,
        "dataset": {
            "id": dataset.get("id"),
            "versao": dataset.get("versao"),
            "impressao": dataset.get("impressao"),
        },
        "vinculo_preparacao": perfil.get("vinculo_preparacao"),
        "operacoes": sorted(OPERACOES_EXECUCAO),
        "filtros": sorted(_FILTROS),
        "colunas": colunas,
        "indicadores": indicadores,
    }


def _patch_interpretado(texto, carregado):
    if not isinstance(texto, str):
        return None
    bruto = texto.strip()
    if not bruto.startswith("{") or not bruto.endswith("}"):
        return None
    try:
        corpo = json.loads(bruto)
    except json.JSONDecodeError:
        return None
    if not isinstance(corpo, dict) or set(corpo) != _CHAVES_PATCH:
        return None
    acao = corpo.get("acao")
    coluna_indice = corpo.get("coluna")
    operador = corpo.get("operador")
    operacao = corpo.get("operacao")
    valor = corpo.get("valor")
    valor_fim = corpo.get("valor_fim")
    if not _tipo_interpretado(acao, coluna_indice, operador, operacao, valor, valor_fim):
        return None
    if acao not in _ACOES:
        return None
    if acao == "esclarecer":
        return {"acao": "esclarecer"}
    coluna = _coluna_do_indice(carregado["autorizadas"], coluna_indice)
    if coluna_indice is not None and coluna is None:
        return None
    if operador is not None and operador not in _FILTROS:
        return None
    if operacao is not None and operacao not in OPERACOES_EXECUCAO:
        return None
    if valor is not None and not _valor_aceitavel(valor):
        return None
    if valor_fim is not None and not _valor_aceitavel(valor_fim):
        return None
    if acao in {"definir_filtro", "alterar_periodo"} and (coluna is None or operador is None or valor is None):
        return None
    if acao == "alterar_indicador" and operacao is None:
        return None
    return _patch(acao, coluna=coluna, operador=operador, valor=valor, valor_fim=valor_fim, operacao=operacao)


def _tipo_interpretado(acao, coluna_indice, operador, operacao, valor, valor_fim) -> bool:
    if not isinstance(acao, str):
        return False
    if coluna_indice is not None and (isinstance(coluna_indice, bool) or not isinstance(coluna_indice, int)):
        return False
    if operador is not None and not isinstance(operador, str):
        return False
    if operacao is not None and not isinstance(operacao, str):
        return False
    if valor is not None and not isinstance(valor, str):
        return False
    return valor_fim is None or isinstance(valor_fim, str)


def _persistir_definicao(escopo, carregado, documento, *, chamada: bool) -> dict:
    documento["provedor"] = PROVEDOR_ANTHROPIC if chamada else PROVEDOR_SIMULADO
    documento["chamada_anthropic"] = chamada
    documento["contrato_saida_validado"] = True
    documento["execucao"] = "nao_executada"
    origem = carregado["definicao"]
    dataset = carregado["dataset"]
    return _com_rodada(
        escopo,
        origem["chave"],
        lambda rodada: criar_analise(
            escopo,
            {
                "chave": origem["chave"],
                "rodada": rodada,
                "dataset_id": dataset["id"],
                "versao": dataset["versao"],
                "impressao": dataset["impressao"],
                "etapa": "definicao",
                "estado": "concluida",
                "contexto": {
                    "contrato": "contexto.definir.v1",
                    "definicao_origem_id": origem["id"],
                    "evolucao": CONTRATO_EVOLUCAO,
                    "chamada_anthropic": chamada,
                },
                "regras_negocio": {"dsl": documento.get("dsl"), "execucao": "nao_executada", "patch": CONTRATO_PATCH},
                "esclarecimentos": {"bloqueios": []},
                "contrato_versao": CONTRATO_DEFINIR,
                "resultado": documento,
            },
        ),
    )


def _responder_aplicada(escopo, carregado, salvo, patch, *, via, chamada, cobranca, parametros) -> dict:
    return {
        "ok": True,
        "contrato": CONTRATO_EVOLUCAO,
        "situacao": "aplicada",
        "via": via,
        "chamada_anthropic": chamada,
        "cobranca": cobranca,
        "patch": _patch_publico(patch),
        "parametros": _parametros_publicos(parametros),
        "esclarecimento": None,
        "explicacao": None,
        "dashboard": salvo["dashboard"],
        "consulta": salvo["consulta"],
        "revisao_anterior_id": carregado["painel"]["id"],
        "revisoes": _revisoes(escopo, salvo["dashboard"]["chave"]),
    }


def _responder_esclarecimento(carregado, motivo, *, chamada, cobranca) -> dict:
    codigo = motivo if motivo in _PERGUNTAS else "pedido_nao_identificado"
    return {
        "ok": True,
        "contrato": CONTRATO_EVOLUCAO,
        "situacao": "esclarecimento",
        "via": "nenhuma",
        "chamada_anthropic": chamada,
        "cobranca": cobranca,
        "patch": None,
        "esclarecimento": {"motivo": codigo, "pergunta": _PERGUNTAS[codigo]},
        "explicacao": None,
        "dashboard": carregado["painel"],
        "consulta": carregado["consulta"],
        "revisao_anterior_id": None,
        "revisoes": [{"id": carregado["painel"]["id"], "revisao": carregado["painel"]["revisao"]}],
    }


def _responder_explicacao(carregado, texto) -> dict:
    normal = _norm(texto)
    comprovado = []
    hipotese = []
    nao_verificavel = []
    for grafico in carregado["consulta"].get("graficos") or []:
        if not isinstance(grafico, dict):
            continue
        if grafico.get("status") == "calculado":
            comprovado.append(_frase_comprovada(grafico))
            for ponto in grafico.get("serie") or []:
                if isinstance(ponto, dict) and isinstance(ponto.get("chave"), str) and isinstance(ponto.get("resultado"), str):
                    comprovado.append(f"Grupo {ponto['chave']}: resultado {ponto['resultado']}.")
        elif grafico.get("status") == "bloqueado":
            comprovado.append(f"Indicador {grafico.get('ref')} bloqueado, sem resultado publicado.")
    if re.search(r"\b(por que|porque|causa|motivo)\b", normal):
        hipotese.append("A causa não está na memória de cálculo. O que aparece acima é só o agregado.")
    if re.search(r"\b(cliente|nome|cpf|cnpj|endereco|telefone|registro|linha)\b", normal):
        nao_verificavel.append("O detalhe pedido não está nos agregados autorizados.")
    if not comprovado and not nao_verificavel:
        nao_verificavel.append("Não há agregado publicado para sustentar essa explicação.")
    return {
        "ok": True,
        "contrato": CONTRATO_EVOLUCAO,
        "situacao": "explicacao",
        "via": "deterministica",
        "chamada_anthropic": False,
        "cobranca": False,
        "patch": None,
        "esclarecimento": None,
        "explicacao": {"comprovado": comprovado, "hipotese": hipotese, "nao_verificavel": nao_verificavel},
        "dashboard": carregado["painel"],
        "consulta": carregado["consulta"],
        "revisao_anterior_id": None,
        "revisoes": [{"id": carregado["painel"]["id"], "revisao": carregado["painel"]["revisao"]}],
    }


def _frase_comprovada(grafico) -> str:
    numero = grafico.get("resultado") if isinstance(grafico.get("resultado"), str) else grafico.get("total")
    numero = numero if isinstance(numero, str) else "sem resultado"
    unidade = grafico.get("unidade") if isinstance(grafico.get("unidade"), str) else "sem unidade"
    operacao = grafico.get("operacao") if isinstance(grafico.get("operacao"), str) else "sem operacao"
    elegiveis = grafico.get("elegiveis") if isinstance(grafico.get("elegiveis"), int) else 0
    return f"Indicador {grafico.get('ref')}, {operacao}, unidade {unidade}, resultado {numero}, elegíveis {elegiveis}."


def _instrucao() -> str:
    return "\n".join(
        [
            "Responda somente com um JSON de patch analitico, sem markdown.",
            "Acoes: definir_filtro, alterar_periodo, alterar_agrupamento, alterar_indicador, esclarecer.",
            "Nao produza SQL, Python, formula ou codigo.",
            "Use apenas o indice de coluna recebido. Nao invente coluna, metrica, unidade ou relacao.",
            "Nome e anotacao. A identidade da coluna e a referencia tecnica, nao o nome.",
            "Se a metrica, a unidade, o periodo ou a relacao nao estiver identificada, a acao e esclarecer.",
        ]
    )


def _periodo(normal):
    faixa = re.search(r"\b(?:periodo de|de|entre) (\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}) (?:a|ate|e) (\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", normal)
    if faixa:
        return _par_datas(faixa.group(1), faixa.group(2))
    inicio = re.search(r"\ba partir de (\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", normal)
    if inicio:
        data = _data_texto(inicio.group(1))
        if data in {"ambigua", "invalida"}:
            return data
        return data, None
    if re.search(r"\b(periodo|data)\b", normal):
        fim = re.search(r"\bate (\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b", normal)
        if fim:
            data = _data_texto(fim.group(1))
            if data in {"ambigua", "invalida"}:
                return data
            return (None, data)
    return None


def _par_datas(inicio, fim):
    primeira = _data_texto(inicio)
    segunda = _data_texto(fim)
    if "ambigua" in {primeira, segunda}:
        return "ambigua"
    if "invalida" in {primeira, segunda} or primeira is None or segunda is None:
        return "invalida"
    if primeira > segunda:
        return "invalida"
    return primeira, segunda


def _data_texto(bruto):
    if _DATA_ISO.fullmatch(bruto):
        try:
            if date.fromisoformat(bruto).isoformat() != bruto:
                return "invalida"
        except ValueError:
            return "invalida"
        return bruto
    barra = _DATA_BARRA.fullmatch(bruto)
    if not barra:
        return "invalida"
    dia, mes, ano = int(barra.group(1)), int(barra.group(2)), int(barra.group(3))
    if dia > 12 and 1 <= mes <= 12:
        try:
            return date(ano, mes, dia).isoformat()
        except ValueError:
            return "invalida"
    if 1 <= dia <= 12 and 1 <= mes <= 12:
        return "ambigua"
    return "invalida"


def _filtro(normal):
    operadores = "|".join(re.escape(item[0]) for item in _OPERADORES_TEXTO)
    casou = re.search(rf"\b(?:filtre|filtrar|filtro|somente|apenas|so)\b\s+(?:por\s+)?(.+?)\s+(?:{operadores})\s+(.+)$", normal)
    if not casou:
        if re.search(r"\b(?:filtre|filtrar|filtro)\b", normal):
            return "incompleto"
        return None
    operador = _operador_texto(casou.group(0))
    if operador is None:
        return "incompleto"
    nome = casou.group(1).strip()
    resto = casou.group(2).strip()
    valor_fim = None
    if operador == "entre":
        partes = re.split(r"\s+(?:e|a|ate)\s+", resto, maxsplit=1)
        if len(partes) != 2:
            return "incompleto"
        valor, valor_fim = partes[0].strip(), partes[1].strip()
    else:
        valor = resto
    if not nome or not valor:
        return "incompleto"
    return nome, operador, valor, valor_fim


def _operador_texto(trecho):
    for texto, operador in _OPERADORES_TEXTO:
        if re.search(rf"\b{re.escape(texto)}\b", trecho):
            return operador
    return None


def _operacao_pedida(normal):
    for texto, operacao in _OPERACOES_TEXTO:
        if re.search(rf"\b{re.escape(texto)}\b", normal):
            return operacao
    return None


def _casar(nome, carregado):
    consulta = _norm(nome).strip(" .,;:")
    consulta = re.sub(r"^(?:a coluna|o campo|coluna|campo|por)\s+", "", consulta).strip()
    if not consulta:
        return None
    dentro = _por_rotulo(carregado["autorizadas"], carregado["rotulos"], consulta)
    if len(dentro) > 1:
        return "ambiguo"
    if len(dentro) == 1:
        return dentro[0]
    token = consulta.split(" ")[0]
    if token != consulta:
        return _casar(token, carregado)
    if _por_rotulo(carregado["colunas"], carregado["rotulos"], consulta):
        return "fora"
    return None


def _por_rotulo(colunas, rotulos, consulta):
    achados = []
    for coluna in colunas:
        rotulo = rotulos.get(_coord(coluna))
        if isinstance(rotulo, str) and _norm(rotulo) == consulta:
            achados.append(coluna)
    return achados


def _mapa_rotulos(estrutura, colunas) -> dict:
    """Rótulo local da classificação. A fronteira governada usa outro mapa."""
    mapa = {}
    if not isinstance(estrutura, dict):
        return mapa
    regioes = {}
    for regiao in estrutura.get("regioes") or []:
        if isinstance(regiao, dict):
            regioes[(regiao.get("arquivo"), regiao.get("aba"), regiao.get("indice"))] = regiao
    for coluna in colunas:
        regiao = regioes.get((coluna.get("arquivo"), coluna.get("aba"), coluna.get("regiao")))
        if not isinstance(regiao, dict) or regiao.get("cabecalho_deteccao") != "inequivoca":
            continue
        linha = regiao.get("cabecalho_linha")
        for candidato in regiao.get("candidatos") or []:
            if not isinstance(candidato, dict) or candidato.get("linha") != linha:
                continue
            rotulos = candidato.get("rotulos")
            indice = coluna.get("indice")
            if isinstance(rotulos, list) and isinstance(indice, int) and 0 <= indice < len(rotulos) and isinstance(rotulos[indice], str):
                mapa[_coord(coluna)] = rotulos[indice]
    return mapa


def _colunas_da_regiao(colunas, definicoes):
    regioes = set()
    for item in definicoes:
        if isinstance(item, dict) and isinstance(item.get("metrica"), dict):
            coluna = item["metrica"].get("coluna")
            if isinstance(coluna, dict):
                regioes.add((coluna.get("arquivo"), coluna.get("aba"), coluna.get("regiao")))
    return [coluna for coluna in colunas if (coluna.get("arquivo"), coluna.get("aba"), coluna.get("regiao")) in regioes]


def _unica_data(colunas):
    datas = [coluna for coluna in colunas if _tipo_coluna(coluna) == "data"]
    if len(datas) == 1:
        return datas[0]
    if len(datas) > 1:
        return "ambiguo"
    return None


def _alvos_unicos(definicoes):
    definidas = [item for item in definicoes if isinstance(item, dict) and item.get("status") == "definida"]
    if len(definidas) == 1:
        return definidas
    return "ambiguo"


def _grafico_para(atual, operacao, medida, agrupamento, segunda):
    if isinstance(atual, str) and _grafico_compativel(atual, operacao, medida, agrupamento, segunda):
        return atual
    if agrupamento is None:
        candidatos = ("indicador", "tabela")
    elif _granularidade_temporal(agrupamento) is not None:
        candidatos = ("linha", "tabela")
    else:
        candidatos = ("barra", "pizza", "tabela")
    for grafico in candidatos:
        if _grafico_compativel(grafico, operacao, medida, agrupamento, segunda):
            return grafico
    return None


def _gravar_filtro(definicao, coluna, operador, tipo, tem_fim) -> None:
    nome = f"evo{coluna['indice']}"
    nome_fim = f"evo{coluna['indice']}fim" if tem_fim else None
    filtros = []
    for item in definicao.get("filtros") or []:
        if isinstance(item, dict) and _coord(item.get("coluna")) == _coord(coluna):
            continue
        filtros.append(item)
    filtros.append(
        {
            "coluna": _ref_de_coluna(coluna),
            "operador": operador,
            "parametro": nome,
            "parametro_fim": nome_fim,
        }
    )
    nomes = {nome}
    if nome_fim:
        nomes.add(nome_fim)
    parametros = [item for item in definicao.get("parametros") or [] if isinstance(item, dict) and item.get("nome") not in nomes]
    parametros.append({"nome": nome, "tipo": tipo, "obrigatorio": True})
    if nome_fim:
        parametros.append({"nome": nome_fim, "tipo": tipo, "obrigatorio": True})
    definicao["filtros"] = filtros
    definicao["parametros"] = parametros
    definicao["status"] = "definida"
    definicao["esclarecimento"] = None


def _so_troca_valor(definicoes, coluna, operador) -> bool:
    return all(_filtro_da_coluna(item, coluna, operador) is not None for item in definicoes)


def _filtro_da_coluna(definicao, coluna, operador):
    for item in definicao.get("filtros") or []:
        if isinstance(item, dict) and item.get("operador") == operador and _coord(item.get("coluna")) == _coord(coluna):
            return item
    return None


def _valores_novos(definicoes, coluna, valor, valor_fim) -> dict:
    saida = {}
    for item in definicoes:
        filtro = _filtro_da_coluna(item, coluna, next(part.get("operador") for part in item["filtros"] if _coord(part.get("coluna")) == _coord(coluna)))
        saida[filtro["parametro"]] = valor
        if filtro.get("parametro_fim"):
            saida[filtro["parametro_fim"]] = valor_fim
    return saida


def _parametros_do_documento(definicoes, herdados=None) -> dict:
    return _parametros_herdados(definicoes, herdados)


def _parametros_herdados(definicoes, herdados=None) -> dict:
    informados = herdados or {}
    declarados = _declarados(definicoes)
    return {nome: valor for nome, valor in informados.items() if nome in declarados}


def _validar_parametros_informados(definicoes, informados) -> dict:
    if not informados:
        return {}
    declarados = _declarados(definicoes)
    saida = {}
    for nome, valor in informados.items():
        if not isinstance(nome, str) or nome not in declarados:
            raise EvolucaoErro("parametro_inconsistente")
        try:
            _coagir(declarados[nome], valor)
        except (ArithmeticError, TypeError, ValueError):
            raise EvolucaoErro("parametro_inconsistente") from None
        saida[nome] = valor
    return saida


def _declarados(definicoes) -> dict:
    nomes = {}
    for item in definicoes:
        if not isinstance(item, dict):
            continue
        for parametro in item.get("parametros") or []:
            if isinstance(parametro, dict) and isinstance(parametro.get("nome"), str):
                nomes[parametro["nome"]] = parametro.get("tipo")
    return nomes


def _parametros_publicos(valores) -> dict:
    saida = {}
    for nome, valor in valores.items():
        if not isinstance(nome, str):
            raise EvolucaoErro("parametro_inconsistente")
        if isinstance(valor, bool) or isinstance(valor, str) or (isinstance(valor, int) and not isinstance(valor, bool)):
            saida[nome] = valor
            continue
        if isinstance(valor, float):
            saida[nome] = valor
            continue
        raise EvolucaoErro("parametro_inconsistente")
    return saida


def _faltam_parametros(definicoes, informados) -> bool:
    for item in definicoes:
        if not isinstance(item, dict):
            continue
        for parametro in item.get("parametros") or []:
            if isinstance(parametro, dict) and parametro.get("obrigatorio") is True and parametro.get("nome") not in informados:
                return True
    return False


def _valores_do_patch(patch, tipo):
    valor = patch.get("valor")
    valor_fim = patch.get("valor_fim")
    if not isinstance(valor, str) or not valor:
        return None, None
    if tipo == "data":
        inicio = _data_texto(valor)
        fim = _data_texto(valor_fim) if isinstance(valor_fim, str) else None
        if inicio in {None, "ambigua", "invalida"}:
            return None, None
        if valor_fim is not None and fim in {None, "ambigua", "invalida"}:
            return None, None
        return inicio, fim
    if not _valor_aceitavel(valor):
        return None, None
    if valor_fim is not None and not _valor_aceitavel(valor_fim):
        return None, None
    return valor, valor_fim


def _valor_aceitavel(valor) -> bool:
    if not isinstance(valor, str) or not 1 <= len(valor) <= 80:
        return False
    if any(caractere in valor for caractere in "\n\r;`") or "@" in valor or "://" in valor:
        return False
    normal = _norm(valor)
    return not _contem_termo(normal, _PROIBIDOS)


def _publicacao_vazia(calculado, alvos) -> bool:
    ids = set(alvos)
    indicadores = calculado["calculo"]["resultado"].get("indicadores") or []
    vistos = [item for item in indicadores if item.get("id") in ids]
    return bool(vistos) and all(item.get("status") != "calculado" for item in vistos)


def _motivo_bloqueio(calculado, alvos) -> str:
    ids = set(alvos)
    for item in calculado["calculo"]["resultado"].get("indicadores") or []:
        if item.get("id") in ids and item.get("motivo") in _PERGUNTAS:
            return item["motivo"]
    return "metrica_nao_identificada"


def _patch(acao, *, coluna=None, operador=None, valor=None, valor_fim=None, operacao=None) -> dict:
    return {
        "acao": acao,
        "coluna": coluna,
        "operador": operador,
        "valor": valor,
        "valor_fim": valor_fim,
        "operacao": operacao,
    }


def _patch_publico(patch) -> dict:
    coluna = patch.get("coluna")
    return {
        "contrato": CONTRATO_PATCH,
        "acao": patch.get("acao"),
        "operador": patch.get("operador"),
        "operacao": patch.get("operacao"),
        "coluna_indice": None if not isinstance(coluna, dict) else coluna.get("indice"),
    }


def _revisoes(escopo, chave) -> list:
    return [{"id": item["id"], "revisao": item["revisao"]} for item in listar_dashboards(escopo, chave=chave)]


def _coluna_perfil(perfil, ref):
    if not isinstance(ref, dict):
        return None
    for coluna in perfil.get("colunas") or []:
        if isinstance(coluna, dict) and _coord(coluna) == _coord(ref):
            return coluna
    return None


def _agrupamento(definicao, perfil):
    for item in definicao.get("dimensoes") or []:
        if isinstance(item, dict) and item.get("papel") == "agrupamento":
            return _coluna_perfil(perfil, item.get("coluna"))
    return None


def _segunda(definicao, perfil):
    for item in definicao.get("dimensoes") or []:
        if isinstance(item, dict) and item.get("papel") == "segunda_medida":
            return _coluna_perfil(perfil, item.get("coluna"))
    return None


def _mesma_regiao(definicao, coluna) -> bool:
    medida = definicao.get("metrica") if isinstance(definicao.get("metrica"), dict) else {}
    ref = medida.get("coluna")
    if not isinstance(ref, dict) or not isinstance(coluna, dict):
        return False
    return (ref.get("arquivo"), ref.get("aba"), ref.get("regiao")) == (coluna.get("arquivo"), coluna.get("aba"), coluna.get("regiao"))


def _indice_autorizado(colunas, ref):
    if not isinstance(ref, dict):
        return None
    for indice, coluna in enumerate(colunas):
        if _coord(coluna) == _coord(ref):
            return indice
    return None


def _coluna_do_indice(colunas, indice):
    if indice is None:
        return None
    if isinstance(indice, bool) or not isinstance(indice, int) or not 0 <= indice < len(colunas):
        return None
    return colunas[indice]


def _coord(ref):
    if not isinstance(ref, dict):
        return None
    return (ref.get("arquivo"), ref.get("aba"), ref.get("regiao"), ref.get("indice"))


def _vaza_rotulo(mensagem, rotulos) -> bool:
    normal = _norm(mensagem)
    for rotulo in rotulos.values():
        if isinstance(rotulo, str) and len(_norm(rotulo)) >= 3 and _norm(rotulo) not in {"valor", "data", "texto"} and _norm(rotulo) in normal:
            return True
    return False


def _chave(carregado, texto) -> str:
    digest = hashlib.sha256(_norm(texto).encode("utf-8")).hexdigest()[:16]
    chave = f"evolucao{int(carregado['painel']['id'])}r{int(carregado['painel']['revisao'])}{digest}"
    return chave[:80]


def _com_rodada(escopo, chave, montar):
    erro = None
    for _ in range(2):
        try:
            return montar(_proxima_rodada(escopo, chave))
        except AgenteBiErro as exc:
            erro = exc
            if exc.codigo not in {"rodada_existente", "conflito_versao"}:
                raise
    raise erro


def _proxima_rodada(escopo, chave: str) -> int:
    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter(
            AgenteBiAnalise.conta_id == escopo.conta_id,
            AgenteBiAnalise.franquia_id == escopo.franquia_id,
            AgenteBiAnalise.usuario_id == escopo.usuario_id,
            AgenteBiAnalise.chave == chave,
        )
        .scalar()
    )
    if atual is None:
        return 1
    return int(atual) + 1


def _contem_termo(texto, termos) -> bool:
    return any(re.search(rf"\b{re.escape(termo)}\b", texto) for termo in termos)


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFD", texto.strip().lower())
    sem = "".join(caractere for caractere in base if unicodedata.category(caractere) != "Mn")
    return re.sub(r"\s+", " ", sem)
