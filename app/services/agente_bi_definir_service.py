"""Contrato `definir.v1` do AgenteBI (SCRUM-260).

Converte a seleção aprovada em definições declarativas. O catálogo parte de `analisar.v1` e acrescenta só os operadores que a
SCRUM-261 executa: `count_if`, razão, participação e junção
muitos-para-um já validada. A saída não é SQL nem Python, e este
módulo não executa expressão nenhuma. O cálculo fica no executor
determinístico.

O adaptador Anthropic existente entra só como preparo: instrução, schema
e contexto minimizado. Esta etapa não chama o SDK e não abre cobrança.
"""
from __future__ import annotations

import copy
import json
import logging

from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_analisar_service import (
    CATALOGO_CONTRATO,
    CONTRATO_ANALISAR,
    GRAFICOS,
    OPERACAO_PAR,
    OPERACOES,
    OPERACOES_AGREGACAO,
    PROVEDOR_ANTHROPIC,
    PROVEDOR_SIMULADO,
    AnalisarErro,
    TransferenciaRecusada,
    governar_representacao,
    montar_contexto,
    referencias_da_representacao,
    texto_de_cabecalho_transferivel,
)
from app.services.agente_bi_anthropic_service import preparar_saida_estruturada
from app.services.agente_bi_descoberta_service import CONTRATO_SELECAO
from app.services.agente_bi_perfil_semantico_service import FINALIDADE_TRANSFERENCIA, perfilar_dataset
from app.services.agente_bi_persistencia_service import (
    LIMITE_GRAFICOS,
    AgenteBiErro,
    criar_analise,
    obter_analise,
)
from app.services.cleiton_ai_data_governance import (
    DECISION_ALLOW,
    govern_outbound_content,
)
from app.services.cleiton_ai_privacy_classifier import SUPPORTED_PURPOSES

logger = logging.getLogger(__name__)

CONTRATO_DEFINIR = "definir.v1"
CONTRATO_CONTEXTO_DEFINIR = "contexto.definir.v1"
DSL_CONTRATO = "dsl.analitica.v1"
EXECUCAO = "nao_executada"
_JUNCAO = "nao_executada"
_UNIDADES = frozenset({"adimensional", "contagem", "quantidade", "valor"})
OPERACOES_EXECUCAO = OPERACOES | {"count_if", "participacao", "razao"}
JUNCOES_DSL = ("muitos_para_um",)
_GRANULARIDADES = frozenset({"categoria", "data", "par", "tempo", "total"})
_FILTROS = frozenset({"diferente", "entre", "igual", "maior", "maior_ou_igual", "menor", "menor_ou_igual"})
_FILTROS_ORDENADOS = frozenset({"entre", "maior", "maior_ou_igual", "menor", "menor_ou_igual"})
_TIPOS_PARAMETRO = frozenset({"booleano", "data", "numero", "texto"})
_PAPEIS_DIMENSAO = frozenset({"agrupamento", "segunda_medida"})
_STATUS = frozenset({"bloqueada", "definida"})
_ORIGENS = frozenset({"dados", "referencia"})
_LEITURAS = frozenset(
    {
        "serie_temporal_de_medida",
        "medida_por_categoria",
        "indicador_de_medida",
        "contagem_de_registros",
        "tabela_agregada",
        "dispersao_de_medidas",
    }
)
_ESCLARECIMENTOS = frozenset(
    {
        "confirmar_rotulos",
        "resolver_tipo_misto",
        "resolver_data_ambigua",
        "resolver_formula_sem_cache",
        "resolver_divisao_lateral",
        "confirmar_relacao_candidata",
        "informar_unidade",
        "informar_parametro",
    }
)
_MODALIDADES = ("A", "B", "C")
_LIMITACOES = frozenset(
    {
        "juncao_nao_executada",
        "sem_sql",
        "sem_python",
        "executor_ausente",
        "teto_seis_graficos",
    }
)
_CHAVES_PROIBIDAS = frozenset(
    {
        "sql",
        "python",
        "codigo",
        "expressao",
        "formula",
        "script",
        "query",
        "exec",
        "eval",
        "select",
        "import",
    }
)
_NOMES_PROIBIDOS = _CHAVES_PROIBIDAS | frozenset({"from", "where", "drop", "insert", "update", "delete", "lambda"})
_CHAVES_SELECAO = frozenset(
    {
        "id",
        "contrato",
        "origem_analise_id",
        "origem_contrato",
        "vinculo_preparacao",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
        "modalidades",
        "limite_graficos",
        "graficos",
        "perguntas_confirmadas",
        "executavel",
        "juncao",
        "chamada_anthropic",
        "apto_anthropic",
    }
)
_CHAVES_GRAFICO = frozenset(
    {
        "id",
        "status",
        "leitura",
        "grafico",
        "operacao",
        "origem",
        "regiao",
        "medida",
        "agrupamento",
        "segunda_medida",
        "evidencias",
        "juncao",
    }
)
_CHAVES_REF = ("arquivo", "aba", "regiao", "indice")
_CHAVES_REGIAO = ("arquivo", "aba", "regiao")
_CHAVES_RASCUNHO = frozenset(
    {
        "sugestao_id",
        "status",
        "grafico",
        "leitura",
        "origem",
        "metrica",
        "granularidade",
        "dimensoes",
        "filtros",
        "parametros",
        "relacionamentos",
        "juncao",
        "esclarecimento",
    }
)
_CHAVES_METRICA = frozenset({"operacao", "coluna", "unidade"})
_CHAVES_DIMENSAO = frozenset({"papel", "coluna", "granularidade", "unidade"})
_CHAVES_FILTRO = frozenset({"coluna", "operador", "parametro", "parametro_fim"})
_CHAVES_PARAMETRO = frozenset({"nome", "tipo", "obrigatorio"})
_CHAVES_RELACAO = frozenset({"indice", "juncao"})
_CHAVES_DOCUMENTO = frozenset(
    {
        "contrato",
        "catalogo",
        "dsl",
        "provedor",
        "chamada_anthropic",
        "apto_anthropic",
        "contrato_saida_validado",
        "origem_selecao_id",
        "origem_descoberta_id",
        "origem_contrato",
        "descoberta_contrato",
        "vinculo_preparacao",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
        "modalidades",
        "limite_graficos",
        "perguntas_confirmadas",
        "execucao",
        "definicoes",
    }
)
_CHAVES_TRANSFERENCIA = frozenset(
    {
        "contrato",
        "amostras",
        "valores_de_celula",
        "rotulos",
        "apto_anthropic",
        "autorizado_para_provedor",
        "transferencia",
        "modalidades",
        "limite_graficos",
        "graficos",
        "regioes",
        "colunas",
        "relacoes",
        "qualidade",
    }
)
_CHAVES_PEDIDO = frozenset({"analise_id"})
_CONFLITOS_RODADA = frozenset({"rodada_existente", "conflito_versao"})
_HEX = frozenset("0123456789abcdef")
_NOME = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


class DefinirErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def descrever_dsl() -> dict:
    """Catálogo fechado. Fora desta lista, a definição é recusada."""
    return {
        "contrato": DSL_CONTRATO,
        "catalogo": CATALOGO_CONTRATO,
        "agregacoes": sorted(OPERACOES_AGREGACAO | {"count_if", "participacao", "razao"}),
        "par_numerico": OPERACAO_PAR,
        "graficos": sorted(GRAFICOS),
        "juncoes_executaveis": list(JUNCOES_DSL),
        "unidades": sorted(_UNIDADES),
        "granularidades": sorted(_GRANULARIDADES),
        "filtros": sorted(_FILTROS),
        "parametros": sorted(_TIPOS_PARAMETRO),
        "esclarecimentos": sorted(_ESCLARECIMENTOS),
        "expressoes_executaveis": [],
        "limitacoes": sorted(_LIMITACOES),
    }


def instrucao_de_rascunho() -> str:
    """Instrução estática. Não inclui dado de planilha nem operador novo."""
    catalogo = descrever_dsl()
    return "\n".join(
        [
            "Defina os graficos ja selecionados somente com a DSL fechada.",
            "Responda apenas com JSON de uma chave, definicoes, sem texto ao redor e sem markdown.",
            "Nao invente coluna, metrica, unidade, relacao ou significado de negocio.",
            "Nome de coluna, quando presente, e anotacao. A identidade executavel e arquivo, aba, regiao e indice.",
            "Nao devolva SQL, Python, formula ou qualquer expressao executavel.",
            "Agregacoes: " + ", ".join(catalogo["agregacoes"]) + ".",
            "Par numerico: " + OPERACAO_PAR + ". Graficos: " + ", ".join(catalogo["graficos"]) + ".",
            "Unidades: " + ", ".join(catalogo["unidades"]) + ".",
            "Granularidades: " + ", ".join(catalogo["granularidades"]) + ".",
            "Filtros: " + ", ".join(catalogo["filtros"]) + ".",
            "Tipos de parametro: " + ", ".join(catalogo["parametros"]) + ".",
            "Juncao executavel: muitos_para_um, apenas com cardinalidade validada e sem ambiguidade.",
            "Relacao candidata sem essa validacao bloqueia a definicao.",
            "No maximo seis definicoes, uma por grafico selecionado, na mesma ordem.",
            "Se faltar dado indispensavel, status bloqueada e o esclarecimento do catalogo.",
            "Esclarecimentos: " + ", ".join(catalogo["esclarecimentos"]) + ".",
        ]
    )


def esquema_rascunho_definir() -> dict:
    """JSON Schema do rascunho. A validação local continua obrigatória."""
    ref = {
        "type": "object",
        "additionalProperties": False,
        "properties": {chave: {"type": "integer", "minimum": 0} for chave in _CHAVES_REF},
        "required": list(_CHAVES_REF),
    }
    metrica = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operacao": {"type": "string", "enum": sorted(OPERACOES_EXECUCAO)},
            "coluna": ref,
            "unidade": {"anyOf": [{"type": "string", "enum": sorted(_UNIDADES)}, {"type": "null"}]},
        },
        "required": sorted(_CHAVES_METRICA),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "definicoes": {
                "type": "array",
                "maxItems": LIMITE_GRAFICOS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "sugestao_id": {"type": "string"},
                        "status": {"type": "string", "enum": sorted(_STATUS)},
                        "grafico": {"type": "string", "enum": sorted(GRAFICOS)},
                        "leitura": {"type": "string", "enum": sorted(_LEITURAS)},
                        "origem": {"type": "string", "enum": sorted(_ORIGENS)},
                        "metrica": metrica,
                        "granularidade": {"type": "string", "enum": sorted(_GRANULARIDADES)},
                        "dimensoes": {
                            "type": "array",
                            "maxItems": 2,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "papel": {"type": "string", "enum": sorted(_PAPEIS_DIMENSAO)},
                                    "coluna": ref,
                                    "granularidade": {"type": "string", "enum": sorted(_GRANULARIDADES)},
                                    "unidade": {
                                        "anyOf": [
                                            {"type": "string", "enum": sorted(_UNIDADES)},
                                            {"type": "null"},
                                        ]
                                    },
                                },
                                "required": sorted(_CHAVES_DIMENSAO),
                            },
                        },
                        "filtros": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "coluna": ref,
                                    "operador": {"type": "string", "enum": sorted(_FILTROS)},
                                    "parametro": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                    "parametro_fim": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                                },
                                "required": sorted(_CHAVES_FILTRO),
                            },
                        },
                        "parametros": {
                            "type": "array",
                            "maxItems": 8,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "nome": {"type": "string"},
                                    "tipo": {"type": "string", "enum": sorted(_TIPOS_PARAMETRO)},
                                    "obrigatorio": {"type": "boolean"},
                                },
                                "required": sorted(_CHAVES_PARAMETRO),
                            },
                        },
                        "relacionamentos": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "indice": {"type": "integer", "minimum": 0},
                                    "juncao": {"type": "string", "enum": [_JUNCAO]},
                                },
                                "required": sorted(_CHAVES_RELACAO),
                            },
                        },
                        "juncao": {"type": "string", "enum": [_JUNCAO]},
                        "esclarecimento": {
                            "anyOf": [{"type": "string", "enum": sorted(_ESCLARECIMENTOS)}, {"type": "null"}]
                        },
                    },
                    "required": sorted(_CHAVES_RASCUNHO),
                },
            }
        },
        "required": ["definicoes"],
    }


def montar_contexto_definir(perfil: dict, selecao: dict) -> dict:
    """Contexto estrutural da seleção. Sem rótulo, célula ou texto livre."""
    ident = _identidade_selecao(selecao)
    try:
        base = montar_contexto(perfil, {"modalidades": ["A"]})
    except AnalisarErro as exc:
        raise _de_analisar(exc) from exc
    if base.get("vinculo_preparacao") != ident["vinculo_preparacao"]:
        raise DefinirErro("selecao_invalida")
    dataset = base.get("dataset") if isinstance(base.get("dataset"), dict) else {}
    if (
        dataset.get("id") != ident["dataset_id"]
        or dataset.get("versao") != ident["dataset_versao"]
        or dataset.get("impressao") != ident["dataset_impressao"]
    ):
        raise DefinirErro("selecao_invalida")
    graficos = [_grafico_selecao(item, base) for item in ident["graficos"]]
    return {
        "contrato": CONTRATO_CONTEXTO_DEFINIR,
        "vinculo_preparacao": ident["vinculo_preparacao"],
        "amostras": False,
        "valores_de_celula": False,
        "rotulos": False,
        "apto_anthropic": False,
        "autorizado_para_provedor": False,
        "transferencia": "bloqueada",
        "dataset": {
            "id": ident["dataset_id"],
            "versao": ident["dataset_versao"],
            "impressao": ident["dataset_impressao"],
        },
        "origem_selecao_id": ident["id"],
        "origem_descoberta_id": ident["origem_analise_id"],
        "modalidades": ident["modalidades"],
        "limite_graficos": LIMITE_GRAFICOS,
        "perguntas_confirmadas": ident["perguntas_confirmadas"],
        "graficos": graficos,
        "regioes": base["regioes"],
        "colunas": base["colunas"],
        "relacoes": base["relacoes"],
        "qualidade": base["qualidade"],
    }


def contexto_de_transferencia(contexto: dict) -> dict:
    """Cópia da fronteira. Sem vínculo, dataset ou identificador da conta."""
    if not isinstance(contexto, dict) or contexto.get("contrato") != CONTRATO_CONTEXTO_DEFINIR:
        raise DefinirErro("contexto_nao_autorizado")
    payload = {chave: contexto[chave] for chave in _CHAVES_TRANSFERENCIA}
    if payload.get("apto_anthropic") is not False or payload.get("transferencia") != "bloqueada":
        raise DefinirErro("autorizacao_indevida", 403)
    return payload


def provedor_simulado(contexto: dict) -> dict:
    """Traduz cada gráfico selecionado. Não vê texto, fórmula nem SQL."""
    if not isinstance(contexto, dict) or contexto.get("contrato") != CONTRATO_CONTEXTO_DEFINIR:
        raise DefinirErro("contexto_nao_autorizado")
    return {"definicoes": [_rascunho_de_grafico(contexto, grafico) for grafico in contexto.get("graficos") or []]}


def validar_saida(contexto: dict, documento: dict) -> dict:
    """Confere o JSON fechado, as referências e a DSL. Não corrige a proposta."""
    _recusar_executavel(documento)
    if not isinstance(contexto, dict) or contexto.get("contrato") != CONTRATO_CONTEXTO_DEFINIR:
        raise DefinirErro("contexto_nao_autorizado")
    if not isinstance(documento, dict) or set(documento) != _CHAVES_DOCUMENTO:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("contrato") != CONTRATO_DEFINIR or documento.get("catalogo") != CATALOGO_CONTRATO:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("dsl") != DSL_CONTRATO or documento.get("execucao") != EXECUCAO:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("apto_anthropic") is not False or documento.get("contrato_saida_validado") is not True:
        raise DefinirErro("autorizacao_indevida", 403)
    provedor, chamada = _proveniencia(documento.get("provedor"), documento.get("chamada_anthropic"))
    if documento.get("origem_contrato") != CONTRATO_SELECAO or documento.get("descoberta_contrato") != CONTRATO_ANALISAR:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("juncao", _JUNCAO) != _JUNCAO:
        raise DefinirErro("juncao_nao_confirmada")
    _igual(documento.get("origem_selecao_id"), contexto.get("origem_selecao_id"), "selecao_invalida")
    _igual(documento.get("origem_descoberta_id"), contexto.get("origem_descoberta_id"), "selecao_invalida")
    _igual(documento.get("vinculo_preparacao"), contexto.get("vinculo_preparacao"), "selecao_invalida")
    _igual(documento.get("dataset_id"), contexto["dataset"]["id"], "selecao_invalida")
    _igual(documento.get("dataset_versao"), contexto["dataset"]["versao"], "selecao_invalida")
    _igual(documento.get("dataset_impressao"), contexto["dataset"]["impressao"], "selecao_invalida")
    if documento.get("modalidades") != contexto.get("modalidades"):
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("limite_graficos") != LIMITE_GRAFICOS:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("perguntas_confirmadas") != contexto.get("perguntas_confirmadas"):
        raise DefinirErro("contrato_saida_invalido")
    brutas = documento.get("definicoes")
    graficos = contexto.get("graficos")
    if not isinstance(brutas, list) or not isinstance(graficos, list):
        raise DefinirErro("contrato_saida_invalido")
    if len(brutas) > LIMITE_GRAFICOS or len(graficos) > LIMITE_GRAFICOS:
        raise DefinirErro("limite_graficos")
    if len(brutas) != len(graficos) or not brutas:
        raise DefinirErro("contrato_saida_invalido")
    definicoes = [_validar_definicao(item, grafico, contexto, indice) for indice, (item, grafico) in enumerate(zip(brutas, graficos), start=1)]
    if len({item["id"] for item in definicoes}) != len(definicoes):
        raise DefinirErro("contrato_saida_invalido")
    saida = {
        "contrato": CONTRATO_DEFINIR,
        "catalogo": CATALOGO_CONTRATO,
        "dsl": DSL_CONTRATO,
        "provedor": provedor,
        "chamada_anthropic": chamada,
        "apto_anthropic": False,
        "contrato_saida_validado": True,
        "origem_selecao_id": contexto["origem_selecao_id"],
        "origem_descoberta_id": contexto["origem_descoberta_id"],
        "origem_contrato": CONTRATO_SELECAO,
        "descoberta_contrato": CONTRATO_ANALISAR,
        "vinculo_preparacao": contexto["vinculo_preparacao"],
        "dataset_id": contexto["dataset"]["id"],
        "dataset_versao": contexto["dataset"]["versao"],
        "dataset_impressao": contexto["dataset"]["impressao"],
        "modalidades": list(contexto["modalidades"]),
        "limite_graficos": LIMITE_GRAFICOS,
        "perguntas_confirmadas": list(contexto["perguntas_confirmadas"]),
        "execucao": EXECUCAO,
        "definicoes": definicoes,
    }
    _varrer(saida, {item["nome"] for definicao in definicoes for item in definicao["parametros"]})
    return saida


def definir(perfil: dict, selecao: dict, *, provedor=None, proveniencia: str | None = None) -> dict:
    """Valida a seleção e só então aceita o rascunho local."""
    contexto = montar_contexto_definir(perfil, selecao)
    nome, chamada = _proveniencia_declarada(proveniencia)
    funcao = provedor or provedor_simulado
    if not _provedor_local(funcao):
        raise DefinirErro("provedor_nao_autorizado", 403)
    try:
        proposta = funcao(contexto_de_transferencia(contexto))
    except DefinirErro:
        raise
    except Exception as exc:
        raise DefinirErro("provedor_indisponivel") from exc
    _recusar_executavel(proposta)
    if not isinstance(proposta, dict) or set(proposta) != {"definicoes"}:
        raise DefinirErro("contrato_saida_invalido")
    rascunhos = proposta.get("definicoes")
    if not isinstance(rascunhos, list):
        raise DefinirErro("contrato_saida_invalido")
    if len(rascunhos) > LIMITE_GRAFICOS:
        raise DefinirErro("limite_graficos")
    documento = _fechar(contexto, [_exigir_rascunho(item) for item in rascunhos], nome, chamada)
    return validar_saida(contexto, documento)


def definir_selecao(escopo, corpo: dict) -> dict:
    """Grava a definição da seleção do próprio usuário. Não chama provedor externo."""
    if not isinstance(corpo, dict) or set(corpo) != _CHAVES_PEDIDO:
        raise DefinirErro("selecao_invalida")
    analise_id = _inteiro(corpo.get("analise_id"), minimo=1)
    if analise_id is None:
        raise DefinirErro("selecao_invalida")
    origem = obter_analise(escopo, analise_id)
    if origem.get("etapa") != "selecao" or origem.get("estado") != "concluida":
        raise DefinirErro("analise_indisponivel")
    if origem.get("contrato_versao") != CONTRATO_SELECAO:
        raise DefinirErro("analise_indisponivel")
    resultado = origem.get("resultado")
    if not isinstance(resultado, dict) or resultado.get("contrato") != CONTRATO_SELECAO:
        raise DefinirErro("analise_indisponivel")
    perfil = perfilar_dataset(escopo, origem["dataset_id"])
    dataset = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    if (
        dataset.get("id") != origem["dataset_id"]
        or dataset.get("versao") != origem["versao"]
        or dataset.get("impressao") != origem["impressao"]
    ):
        raise DefinirErro("identidade_invalida", 409)
    pacote = dict(resultado)
    pacote["id"] = origem["id"]
    documento = definir(perfil, pacote)
    if documento.get("chamada_anthropic") is not False or documento.get("apto_anthropic") is not False:
        raise DefinirErro("autorizacao_indevida", 403)
    bloqueios = [
        {"id": item["id"], "esclarecimento": item["esclarecimento"]}
        for item in documento["definicoes"]
        if item["status"] == "bloqueada"
    ]
    gravada = _com_rodada(
        escopo,
        origem["chave"],
        lambda rodada: criar_analise(
            escopo,
            {
                "chave": origem["chave"],
                "rodada": rodada,
                "dataset_id": origem["dataset_id"],
                "versao": origem["versao"],
                "impressao": origem["impressao"],
                "etapa": "definicao",
                "estado": "concluida",
                "contexto": contexto_de_transferencia(montar_contexto_definir(perfil, pacote)),
                "regras_negocio": {
                    "catalogo": CATALOGO_CONTRATO,
                    "dsl": DSL_CONTRATO,
                    "limite_graficos": LIMITE_GRAFICOS,
                    "execucao": EXECUCAO,
                },
                "esclarecimentos": {"bloqueios": bloqueios},
                "contrato_versao": CONTRATO_DEFINIR,
                "resultado": documento,
            },
        ),
    )
    logger.info(
        "evento=agente_bi_definicao usuario_id=%s analise_id=%s origem=%s definicoes=%s bloqueios=%s versao=%s",
        escopo.usuario_id,
        gravada["id"],
        origem["id"],
        len(documento["definicoes"]),
        len(bloqueios),
        gravada["versao"],
    )
    return _publicar(gravada, documento)


def preparar_fronteira(perfil: dict, selecao: dict, *, modelo: str = "", estrutura=None) -> dict:
    """Prepara instrução, schema e contexto para o adaptador já existente.

    Não cria tentativa, não reserva franquia e não chama o SDK. A mensagem
    é a representação aprovada pela governança, não o rascunho anterior a ela.
    """
    contexto = montar_contexto_definir(perfil, selecao)
    _governanca_do_perfil(perfil.get("governanca"))
    estrutural = contexto_de_transferencia(contexto)
    _recusar_sujo(estrutural)
    representacao = _representacao_semantica(contexto, estrutura)
    if referencias_da_representacao(estrutural) != referencias_da_representacao(representacao):
        raise DefinirErro("contexto_nao_autorizado")
    if FINALIDADE_TRANSFERENCIA not in SUPPORTED_PURPOSES:
        raise DefinirErro("contexto_nao_autorizado")
    try:
        autorizado = governar_representacao(representacao, governar=govern_outbound_content)
    except TransferenciaRecusada as exc:
        raise DefinirErro("contexto_nao_autorizado") from exc
    esquema = esquema_rascunho_definir()
    return {
        "liberado": True,
        "chamada_anthropic": False,
        "instrucao": instrucao_de_rascunho(),
        "esquema": esquema,
        "saida_estruturada": preparar_saida_estruturada(modelo, esquema),
        "mensagem": _canonico(autorizado["payload"]),
    }


def _representacao_semantica(contexto: dict, estrutura) -> dict:
    """Acrescenta vínculo e anotação sem mudar a cópia persistida da definição.

    Nome de arquivo e de aba permanecem na estrutura local. A representação
    de transporte conserva as chaves com valor nulo.
    """
    del estrutura
    payload = copy.deepcopy(contexto_de_transferencia(contexto))
    for coluna in payload["colunas"]:
        coluna["nome"] = texto_de_cabecalho_transferivel()
        coluna["descricao"] = None
        coluna["unidade"] = None
    for regiao in payload["regioes"]:
        regiao["nome_arquivo"] = None
        regiao["nome_aba"] = None
    dataset = contexto.get("dataset") if isinstance(contexto.get("dataset"), dict) else {}
    payload["dataset"] = {
        "id": dataset.get("id"),
        "versao": dataset.get("versao"),
        "impressao": dataset.get("impressao"),
    }
    payload["vinculo_preparacao"] = contexto.get("vinculo_preparacao")
    payload["intencao"] = None
    return payload


def argumentos_do_adaptador(fronteira: dict, escopo, *, chave_idempotencia: str) -> dict:
    """Argumentos do adaptador governado. A chamada em si não acontece aqui."""
    if not isinstance(fronteira, dict) or fronteira.get("liberado") is not True:
        raise DefinirErro("contexto_nao_autorizado")
    if fronteira.get("chamada_anthropic") is not False:
        raise DefinirErro("autorizacao_indevida", 403)
    if not _chave_idempotencia(chave_idempotencia):
        raise DefinirErro("contexto_nao_autorizado")
    mensagem = fronteira.get("mensagem")
    instrucao = fronteira.get("instrucao")
    if not isinstance(mensagem, str) or not mensagem or not isinstance(instrucao, str):
        raise DefinirErro("contexto_nao_autorizado")
    saida = fronteira.get("saida_estruturada")
    if saida is not None and not isinstance(saida, dict):
        raise DefinirErro("contexto_nao_autorizado")
    return {
        "usuario_id": escopo.usuario_id,
        "conta_id": escopo.conta_id,
        "franquia_id": escopo.franquia_id,
        "mensagem": mensagem,
        "chave_idempotencia": chave_idempotencia,
        "system": instrucao,
        "output_config": saida,
        "transporte": None,
        "execucao_id": None,
        "worker_id": None,
        "fencing": None,
        "model": None,
        "max_tokens": None,
        "invocar": False,
    }


def _de_analisar(exc: AnalisarErro) -> DefinirErro:
    if exc.codigo == "autorizacao_indevida":
        return DefinirErro("autorizacao_indevida", 403)
    return DefinirErro("contexto_nao_autorizado")


def _governanca_do_perfil(governanca) -> None:
    if not isinstance(governanca, dict):
        raise DefinirErro("contexto_nao_autorizado")
    if governanca.get("finalidade_aprovada") is not True or governanca.get("varredura") != "comprovada":
        raise DefinirErro("contexto_nao_autorizado")
    if governanca.get("montagem_contexto") != "local_nao_autorizada" or governanca.get("decisao") != DECISION_ALLOW:
        raise DefinirErro("contexto_nao_autorizado")
    if governanca.get("apto_anthropic") is not False or governanca.get("autorizado_para_provedor") is not False:
        raise DefinirErro("autorizacao_indevida", 403)
    if governanca.get("transferencia") != "bloqueada":
        raise DefinirErro("autorizacao_indevida", 403)
    if governanca.get("valores_de_celula") is not False or governanca.get("texto_de_celula_excluido") is not True:
        raise DefinirErro("contexto_nao_autorizado")


def _identidade_selecao(selecao) -> dict:
    if not isinstance(selecao, dict) or set(selecao) != _CHAVES_SELECAO:
        raise DefinirErro("selecao_invalida")
    if selecao.get("contrato") != CONTRATO_SELECAO or selecao.get("origem_contrato") != CONTRATO_ANALISAR:
        raise DefinirErro("selecao_invalida")
    if selecao.get("executavel") != "somente_viavel" or selecao.get("juncao") != _JUNCAO:
        raise DefinirErro("selecao_invalida")
    if selecao.get("chamada_anthropic") is not False or selecao.get("apto_anthropic") is not False:
        raise DefinirErro("autorizacao_indevida", 403)
    identificador = _inteiro(selecao.get("id"), minimo=1)
    descoberta = _inteiro(selecao.get("origem_analise_id"), minimo=1)
    dataset_id = _inteiro(selecao.get("dataset_id"), minimo=1)
    versao = _inteiro(selecao.get("dataset_versao"), minimo=1)
    if None in {identificador, descoberta, dataset_id, versao}:
        raise DefinirErro("selecao_invalida")
    if not _hex64(selecao.get("vinculo_preparacao")) or not _hex64(selecao.get("dataset_impressao")):
        raise DefinirErro("selecao_invalida")
    graficos = selecao.get("graficos")
    if not isinstance(graficos, list) or not graficos:
        raise DefinirErro("selecao_invalida")
    if len(graficos) > LIMITE_GRAFICOS:
        raise DefinirErro("limite_graficos")
    if selecao.get("limite_graficos") != LIMITE_GRAFICOS:
        raise DefinirErro("selecao_invalida")
    modalidades = selecao.get("modalidades")
    if not isinstance(modalidades, list) or not modalidades or len(set(modalidades)) != len(modalidades):
        raise DefinirErro("selecao_invalida")
    if any(item not in _MODALIDADES for item in modalidades):
        raise DefinirErro("selecao_invalida")
    perguntas = selecao.get("perguntas_confirmadas")
    if not isinstance(perguntas, list) or any(not _id(item, "p") for item in perguntas):
        raise DefinirErro("selecao_invalida")
    if len(set(perguntas)) != len(perguntas):
        raise DefinirErro("selecao_invalida")
    return {
        "id": identificador,
        "origem_analise_id": descoberta,
        "vinculo_preparacao": selecao["vinculo_preparacao"],
        "dataset_id": dataset_id,
        "dataset_versao": versao,
        "dataset_impressao": selecao["dataset_impressao"],
        "modalidades": [item for item in _MODALIDADES if item in modalidades],
        "graficos": list(graficos),
        "perguntas_confirmadas": list(perguntas),
    }


def _grafico_selecao(item, contexto) -> dict:
    if not isinstance(item, dict) or set(item) != _CHAVES_GRAFICO:
        raise DefinirErro("selecao_invalida")
    if item.get("status") != "viavel" or item.get("origem") not in _ORIGENS or not _id(item.get("id"), "s"):
        raise DefinirErro("selecao_invalida")
    if item.get("juncao") != _JUNCAO:
        raise DefinirErro("juncao_nao_confirmada")
    grafico = item.get("grafico")
    operacao = item.get("operacao")
    if grafico not in GRAFICOS or operacao not in OPERACOES_EXECUCAO:
        raise DefinirErro("selecao_invalida")
    if item.get("leitura") != _leitura(grafico, operacao):
        raise DefinirErro("selecao_invalida")
    regiao = _ref_regiao(item.get("regiao"))
    medida = _ref_coluna(item.get("medida"))
    agrupamento = _ref_coluna(item.get("agrupamento")) if item.get("agrupamento") is not None else None
    segunda = _ref_coluna(item.get("segunda_medida")) if item.get("segunda_medida") is not None else None
    if regiao is None or medida is None:
        raise DefinirErro("selecao_invalida")
    if item.get("agrupamento") is not None and agrupamento is None:
        raise DefinirErro("selecao_invalida")
    if item.get("segunda_medida") is not None and segunda is None:
        raise DefinirErro("selecao_invalida")
    if _achar_regiao(contexto, regiao) is None or _coluna(contexto, medida) is None:
        raise DefinirErro("referencia_inexistente")
    if agrupamento is not None and _coluna(contexto, agrupamento) is None:
        raise DefinirErro("referencia_inexistente")
    if segunda is not None and _coluna(contexto, segunda) is None:
        raise DefinirErro("referencia_inexistente")
    _mesma_regiao(regiao, medida, agrupamento, segunda)
    evidencias = item.get("evidencias")
    if not isinstance(evidencias, list) or not evidencias or len(evidencias) > 8:
        raise DefinirErro("selecao_invalida")
    if any(not _codigo(valor) for valor in evidencias):
        raise DefinirErro("selecao_invalida")
    return {
        "id": item["id"],
        "leitura": item["leitura"],
        "grafico": grafico,
        "operacao": operacao,
        "origem": item["origem"],
        "regiao": regiao,
        "medida": medida,
        "agrupamento": agrupamento,
        "segunda_medida": segunda,
        "juncao": _JUNCAO,
    }


def _rascunho_de_grafico(contexto, grafico) -> dict:
    medida = _coluna(contexto, grafico["medida"])
    agrupamento = _coluna(contexto, grafico["agrupamento"])
    segunda = _coluna(contexto, grafico["segunda_medida"])
    unidade = _unidade(grafico["operacao"], medida)
    granularidade, dimensoes = _forma(grafico["grafico"], grafico["operacao"], medida, agrupamento, segunda)
    motivo = _motivo(contexto, grafico, medida, agrupamento, segunda, unidade, [], [])
    return {
        "sugestao_id": grafico["id"],
        "status": "bloqueada" if motivo else "definida",
        "grafico": grafico["grafico"],
        "leitura": grafico["leitura"],
        "origem": grafico["origem"],
        "metrica": {"operacao": grafico["operacao"], "coluna": dict(grafico["medida"]), "unidade": unidade},
        "granularidade": granularidade,
        "dimensoes": dimensoes,
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": _JUNCAO,
        "esclarecimento": motivo,
    }


def _fechar(contexto, rascunhos, provedor, chamada) -> dict:
    if len(rascunhos) != len(contexto["graficos"]):
        raise DefinirErro("contrato_saida_invalido")
    definicoes = []
    for indice, (rascunho, grafico) in enumerate(zip(rascunhos, contexto["graficos"]), start=1):
        if rascunho.get("sugestao_id") != grafico["id"]:
            raise DefinirErro("contrato_saida_invalido")
        item = dict(rascunho)
        item["id"] = f"g{indice}"
        definicoes.append(item)
    dataset = contexto["dataset"]
    return {
        "contrato": CONTRATO_DEFINIR,
        "catalogo": CATALOGO_CONTRATO,
        "dsl": DSL_CONTRATO,
        "provedor": provedor,
        "chamada_anthropic": chamada,
        "apto_anthropic": False,
        "contrato_saida_validado": True,
        "origem_selecao_id": contexto["origem_selecao_id"],
        "origem_descoberta_id": contexto["origem_descoberta_id"],
        "origem_contrato": CONTRATO_SELECAO,
        "descoberta_contrato": CONTRATO_ANALISAR,
        "vinculo_preparacao": contexto["vinculo_preparacao"],
        "dataset_id": dataset["id"],
        "dataset_versao": dataset["versao"],
        "dataset_impressao": dataset["impressao"],
        "modalidades": list(contexto["modalidades"]),
        "limite_graficos": LIMITE_GRAFICOS,
        "perguntas_confirmadas": list(contexto["perguntas_confirmadas"]),
        "execucao": EXECUCAO,
        "definicoes": definicoes,
    }


def _exigir_rascunho(item) -> dict:
    if not isinstance(item, dict) or set(item) != _CHAVES_RASCUNHO:
        raise DefinirErro("contrato_saida_invalido")
    return dict(item)


def _validar_definicao(item, grafico, contexto, indice) -> dict:
    if not isinstance(item, dict) or set(item) != _CHAVES_RASCUNHO | {"id"}:
        raise DefinirErro("contrato_saida_invalido")
    if item.get("id") != f"g{indice}" or item.get("sugestao_id") != grafico["id"]:
        raise DefinirErro("contrato_saida_invalido")
    if item.get("status") not in _STATUS or item.get("origem") != grafico["origem"]:
        raise DefinirErro("contrato_saida_invalido")
    if item.get("grafico") != grafico["grafico"] or item.get("leitura") != grafico["leitura"]:
        raise DefinirErro("contrato_saida_invalido")
    if item.get("juncao") != _JUNCAO:
        raise DefinirErro("juncao_nao_confirmada")
    metrica = _metrica(item.get("metrica"))
    if metrica["operacao"] != grafico["operacao"] or metrica["coluna"] != grafico["medida"]:
        raise DefinirErro("contrato_saida_invalido")
    medida = _coluna(contexto, metrica["coluna"])
    if medida is None:
        raise DefinirErro("referencia_inexistente")
    dimensoes = _dimensoes(item.get("dimensoes"), contexto, grafico)
    filtros = _lista_fechada(item.get("filtros"), 8)
    parametros = _parametros(item.get("parametros"))
    relacionamentos = _relacionamentos(item.get("relacionamentos"), contexto)
    agrupamento = _coluna(contexto, grafico["agrupamento"])
    segunda = _coluna(contexto, grafico["segunda_medida"])
    if not _operacao_compativel(metrica["operacao"], medida, segunda):
        raise DefinirErro("operacao_incompativel")
    if not _grafico_compativel(grafico["grafico"], metrica["operacao"], medida, agrupamento, segunda):
        raise DefinirErro("grafico_incompativel")
    esperada, esperadas_dimensoes = _forma(grafico["grafico"], metrica["operacao"], medida, agrupamento, segunda)
    if item.get("granularidade") != esperada or dimensoes != esperadas_dimensoes:
        raise DefinirErro("granularidade_incompativel")
    unidade = _unidade(metrica["operacao"], medida)
    if metrica["unidade"] != unidade:
        raise DefinirErro("unidade_incompativel")
    motivo = _motivo(
        contexto,
        grafico,
        medida,
        agrupamento,
        segunda,
        unidade,
        filtros,
        relacionamentos,
    )
    esclarecimento = item.get("esclarecimento")
    if motivo:
        if item["status"] != "bloqueada" or esclarecimento != motivo:
            raise DefinirErro("ambiguidade_nao_resolvida")
        if motivo != "confirmar_relacao_candidata" and relacionamentos:
            raise DefinirErro("contrato_saida_invalido")
        if motivo != "informar_parametro" and (filtros or parametros):
            raise DefinirErro("contrato_saida_invalido")
    else:
        if item["status"] != "definida" or esclarecimento is not None or relacionamentos:
            raise DefinirErro("contrato_saida_invalido")
    _validar_filtros(
        filtros,
        parametros,
        contexto,
        grafico["regiao"],
        incompleto=motivo == "informar_parametro",
        condicao_ausente=motivo == "informar_parametro" and not filtros and grafico["operacao"] in {"count_if", "participacao"},
    )
    return {
        "id": item["id"],
        "sugestao_id": grafico["id"],
        "status": item["status"],
        "grafico": grafico["grafico"],
        "leitura": grafico["leitura"],
        "origem": grafico["origem"],
        "metrica": metrica,
        "granularidade": esperada,
        "dimensoes": dimensoes,
        "filtros": filtros,
        "parametros": parametros,
        "relacionamentos": relacionamentos,
        "juncao": _JUNCAO,
        "esclarecimento": esclarecimento,
    }


def _metrica(valor) -> dict:
    if not isinstance(valor, dict) or set(valor) != _CHAVES_METRICA:
        raise DefinirErro("contrato_saida_invalido")
    if valor.get("operacao") not in OPERACOES_EXECUCAO:
        raise DefinirErro("contrato_saida_invalido")
    coluna = _ref_coluna(valor.get("coluna"))
    if coluna is None:
        raise DefinirErro("contrato_saida_invalido")
    unidade = valor.get("unidade")
    if unidade is not None and unidade not in _UNIDADES:
        raise DefinirErro("unidade_incompativel")
    return {"operacao": valor["operacao"], "coluna": coluna, "unidade": unidade}


def _dimensoes(valor, contexto, grafico) -> list:
    brutas = _lista_fechada(valor, 2)
    saida = []
    for item in brutas:
        if not isinstance(item, dict) or set(item) != _CHAVES_DIMENSAO:
            raise DefinirErro("contrato_saida_invalido")
        if item.get("papel") not in _PAPEIS_DIMENSAO or item.get("granularidade") not in _GRANULARIDADES:
            raise DefinirErro("granularidade_incompativel")
        coluna = _ref_coluna(item.get("coluna"))
        if coluna is None or _coluna(contexto, coluna) is None:
            raise DefinirErro("referencia_inexistente")
        unidade = item.get("unidade")
        if unidade is not None and unidade not in _UNIDADES:
            raise DefinirErro("unidade_incompativel")
        saida.append(
            {
                "papel": item["papel"],
                "coluna": coluna,
                "granularidade": item["granularidade"],
                "unidade": unidade,
            }
        )
    esperadas = []
    if grafico["agrupamento"] is not None:
        esperadas.append(grafico["agrupamento"])
    if grafico["segunda_medida"] is not None:
        esperadas.append(grafico["segunda_medida"])
    obtidas = [item["coluna"] for item in saida]
    if obtidas != esperadas:
        raise DefinirErro("contrato_saida_invalido")
    return saida


def _parametros(valor) -> list:
    brutos = _lista_fechada(valor, 8)
    saida = []
    nomes = set()
    for item in brutos:
        if not isinstance(item, dict) or set(item) != _CHAVES_PARAMETRO:
            raise DefinirErro("contrato_saida_invalido")
        if not _nome_parametro(item.get("nome")):
            if isinstance(item.get("nome"), str) and item["nome"].lower() in _NOMES_PROIBIDOS:
                raise DefinirErro("expressao_nao_permitida")
            raise DefinirErro("parametro_inconsistente")
        if item.get("tipo") not in _TIPOS_PARAMETRO or not isinstance(item.get("obrigatorio"), bool):
            raise DefinirErro("parametro_inconsistente")
        if item["nome"] in nomes:
            raise DefinirErro("parametro_inconsistente")
        nomes.add(item["nome"])
        saida.append({"nome": item["nome"], "tipo": item["tipo"], "obrigatorio": item["obrigatorio"]})
    return saida


def _relacionamentos(valor, contexto) -> list:
    brutos = _lista_fechada(valor, 4)
    saida = []
    vistos = set()
    relacoes = contexto.get("relacoes") if isinstance(contexto.get("relacoes"), list) else []
    for item in brutos:
        if not isinstance(item, dict) or set(item) != _CHAVES_RELACAO:
            raise DefinirErro("contrato_saida_invalido")
        if item.get("juncao") != _JUNCAO:
            raise DefinirErro("juncao_nao_confirmada")
        indice = _inteiro(item.get("indice"))
        if indice is None or indice >= len(relacoes) or indice in vistos:
            raise DefinirErro("referencia_inexistente")
        if relacoes[indice].get("juncao") != _JUNCAO:
            raise DefinirErro("juncao_nao_confirmada")
        vistos.add(indice)
        saida.append({"indice": indice, "juncao": _JUNCAO})
    return saida


def _validar_filtros(filtros, parametros, contexto, regiao, *, incompleto: bool, condicao_ausente: bool = False) -> None:
    if any(not isinstance(item, dict) or set(item) != _CHAVES_FILTRO for item in filtros):
        raise DefinirErro("contrato_saida_invalido")
    nomes = {item["nome"]: item for item in parametros}
    usados = set()
    houve_incompleto = False
    for item in filtros:
        if item.get("operador") not in _FILTROS:
            raise DefinirErro("filtro_incompativel")
        coluna_ref = _ref_coluna(item.get("coluna"))
        coluna = None if coluna_ref is None else _coluna(contexto, coluna_ref)
        if coluna is None:
            raise DefinirErro("referencia_inexistente")
        if _coord_regiao(coluna) != _coord_regiao(regiao):
            raise DefinirErro("juncao_nao_confirmada")
        faltando = item.get("parametro") is None or (item["operador"] == "entre" and item.get("parametro_fim") is None)
        if faltando:
            houve_incompleto = True
            if not incompleto:
                raise DefinirErro("ambiguidade_nao_resolvida")
        _vincular_parametro(item.get("parametro"), coluna, nomes, usados, item["operador"], obrigatorio=not faltando)
        if item["operador"] == "entre":
            _vincular_parametro(item.get("parametro_fim"), coluna, nomes, usados, item["operador"], obrigatorio=not faltando)
        elif item.get("parametro_fim") is not None:
            raise DefinirErro("parametro_inconsistente")
    if incompleto and not houve_incompleto and not condicao_ausente:
        raise DefinirErro("ambiguidade_nao_resolvida")
    if any(nome not in usados for nome in nomes):
        raise DefinirErro("parametro_inconsistente")


def _vincular_parametro(nome, coluna, nomes, usados, operador, *, obrigatorio: bool) -> None:
    if nome is None:
        if obrigatorio:
            raise DefinirErro("parametro_inconsistente")
        return
    if not _nome_parametro(nome) or nome not in nomes:
        raise DefinirErro("parametro_inconsistente")
    tipo = _tipo_coluna(coluna)
    if tipo is None or nomes[nome]["tipo"] != tipo:
        raise DefinirErro("filtro_incompativel")
    if operador in _FILTROS_ORDENADOS and tipo not in {"numero", "data"}:
        raise DefinirErro("filtro_incompativel")
    usados.add(nome)


def _motivo(contexto, grafico, medida, agrupamento, segunda, unidade, filtros, relacionamentos):
    regiao = _achar_regiao(contexto, grafico["regiao"])
    qualidade = _achar_qualidade(contexto, grafico["regiao"])
    if regiao is None or qualidade is None:
        raise DefinirErro("referencia_inexistente")
    ambiguidades = set(regiao.get("ambiguidades") or [])
    if regiao.get("cabecalho") != "inequivoca" or qualidade.get("cabecalho_ambiguo") or "cabecalho_ambiguo" in ambiguidades:
        return "confirmar_rotulos"
    if "divisao_lateral_ambigua" in ambiguidades:
        return "resolver_divisao_lateral"
    if qualidade.get("formulas_sem_cache", 0) > 0:
        return "resolver_formula_sem_cache"
    if any(coluna is not None and coluna.get("dominio_inconsistente") for coluna in (medida, agrupamento, segunda)):
        return "resolver_tipo_misto"
    if grafico["grafico"] == "linha" and (qualidade.get("datas_ambiguas", 0) > 0 or "data_ambigua" in ambiguidades):
        return "resolver_data_ambigua"
    if unidade is None:
        return "informar_unidade"
    if relacionamentos:
        return "confirmar_relacao_candidata"
    if any(_filtro_incompleto(item) for item in filtros):
        return "informar_parametro"
    if grafico["operacao"] == "count_if" and not filtros:
        return "informar_parametro"
    if grafico["operacao"] == "participacao" and grafico["grafico"] == "indicador" and not filtros:
        return "informar_parametro"
    return None


def _filtro_incompleto(item) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("parametro") is None:
        return True
    return item.get("operador") == "entre" and item.get("parametro_fim") is None


def _forma(grafico, operacao, medida, agrupamento, segunda):
    if operacao == "razao":
        if segunda is None:
            raise DefinirErro("grafico_incompativel")
        denominador = {
            "papel": "segunda_medida",
            "coluna": _ref_de_coluna(segunda),
            "granularidade": "total",
            "unidade": "adimensional",
        }
        if grafico == "indicador":
            if agrupamento is not None:
                raise DefinirErro("grafico_incompativel")
            return "total", [denominador]
        if agrupamento is None:
            raise DefinirErro("grafico_incompativel")
        if grafico == "linha":
            grano = _granularidade_temporal(agrupamento)
            if grano is None:
                raise DefinirErro("granularidade_incompativel")
        elif grafico in {"barra", "pizza", "tabela"}:
            if _granularidade_temporal(agrupamento) is not None or _numerica_estrutural(agrupamento):
                raise DefinirErro("granularidade_incompativel")
            if "texto" not in agrupamento.get("tipos", []):
                raise DefinirErro("granularidade_incompativel")
            grano = "categoria"
        else:
            raise DefinirErro("grafico_incompativel")
        return grano, [_dimensao_agrupamento(agrupamento, grano), denominador]
    if operacao == OPERACAO_PAR:
        if segunda is None or agrupamento is not None:
            raise DefinirErro("grafico_incompativel")
        return "par", [
            {
                "papel": "segunda_medida",
                "coluna": _ref_de_coluna(segunda),
                "granularidade": "par",
                "unidade": _unidade(operacao, segunda),
            }
        ]
    if grafico == "indicador":
        if agrupamento is not None or segunda is not None:
            raise DefinirErro("grafico_incompativel")
        return "total", []
    if agrupamento is None:
        if grafico == "tabela":
            return "total", []
        raise DefinirErro("grafico_incompativel")
    if grafico == "linha":
        grano = _granularidade_temporal(agrupamento)
        if grano is None:
            raise DefinirErro("granularidade_incompativel")
        return grano, [_dimensao_agrupamento(agrupamento, grano)]
    if grafico in {"barra", "pizza", "tabela"}:
        if _granularidade_temporal(agrupamento) is not None or _numerica_estrutural(agrupamento):
            raise DefinirErro("granularidade_incompativel")
        if "texto" not in agrupamento.get("tipos", []):
            raise DefinirErro("granularidade_incompativel")
        return "categoria", [_dimensao_agrupamento(agrupamento, "categoria")]
    raise DefinirErro("grafico_incompativel")


def _dimensao_agrupamento(coluna, granularidade) -> dict:
    return {
        "papel": "agrupamento",
        "coluna": _ref_de_coluna(coluna),
        "granularidade": granularidade,
        "unidade": None,
    }


def _operacao_compativel(operacao, medida, segunda) -> bool:
    if medida is None:
        return False
    if operacao == OPERACAO_PAR:
        return segunda is not None and _numerica_estrutural(medida) and _numerica_estrutural(segunda) and medida["presentes"] >= 1 and segunda["presentes"] >= 1
    if operacao in {"soma", "minimo", "maximo"}:
        return _numerica_estrutural(medida) and medida["presentes"] >= 1
    if operacao == "media":
        return _numerica_estrutural(medida) and medida["presentes"] >= 2
    if operacao in {"contagem", "count_if"}:
        return medida["presentes"] >= 1
    if operacao == "contagem_distinta":
        return medida["presentes"] >= 1 and medida["distintos"] >= 1
    if operacao == "participacao":
        return _numerica_estrutural(medida) and medida["presentes"] >= 1
    if operacao == "razao":
        return (
            segunda is not None
            and _numerica_estrutural(medida)
            and _numerica_estrutural(segunda)
            and medida["presentes"] >= 1
            and segunda["presentes"] >= 1
            and medida.get("dimensao_principal") in {"valor", "quantidade"}
            and medida.get("dimensao_principal") == segunda.get("dimensao_principal")
        )
    return False


def _grafico_compativel(grafico, operacao, medida, agrupamento, segunda) -> bool:
    if operacao == OPERACAO_PAR:
        return grafico == "dispersao" and agrupamento is None and segunda is not None
    if operacao == "razao":
        if segunda is None or _coord(segunda) == _coord(medida):
            return False
        if agrupamento is not None and _coord(agrupamento) == _coord(medida):
            return False
        if grafico == "indicador":
            return agrupamento is None
        if grafico == "dispersao" or agrupamento is None:
            return False
        if grafico == "linha":
            return _granularidade_temporal(agrupamento) is not None
        if grafico == "pizza":
            return 2 <= agrupamento["distintos"] <= 8
        if grafico == "barra":
            return agrupamento["distintos"] >= 2
        return grafico == "tabela"
    if grafico == "dispersao":
        return False
    if agrupamento is not None and _coord(agrupamento) == _coord(medida):
        return False
    if grafico == "indicador":
        return agrupamento is None and segunda is None
    if grafico == "tabela":
        return segunda is None
    if agrupamento is None or segunda is not None:
        return False
    if grafico == "linha":
        return _granularidade_temporal(agrupamento) is not None
    if grafico == "pizza":
        return 2 <= agrupamento["distintos"] <= 8
    if grafico == "barra":
        return agrupamento["distintos"] >= 2
    return False


def _unidade(operacao, coluna):
    if coluna is None:
        return None
    if operacao in {"contagem", "contagem_distinta", "count_if"}:
        return "contagem"
    if operacao in {"razao", "participacao"}:
        return "adimensional"
    if coluna.get("dimensao_principal") == "valor":
        return "valor"
    if coluna.get("dimensao_principal") == "quantidade":
        return "quantidade"
    return None


def _granularidade_temporal(coluna):
    if coluna is None:
        return None
    dimensoes = set(coluna.get("dimensoes") or [])
    evidencias = set(coluna.get("evidencias") or [])
    if "tempo" in dimensoes or "tipo_hora" in evidencias or "componente_horario" in evidencias:
        return "tempo"
    if "data" in dimensoes or "tipo_data" in evidencias:
        return "data"
    return None


def _numerica_estrutural(coluna) -> bool:
    if coluna is None:
        return False
    if coluna.get("dimensao_principal") in {"valor", "quantidade"}:
        return True
    tipos = set(coluna.get("tipos") or [])
    evidencias = set(coluna.get("evidencias") or [])
    return "numero" in tipos or "tipo_numero_inteiro" in evidencias or "tipo_numero_fracionado" in evidencias


def _tipo_coluna(coluna):
    if _numerica_estrutural(coluna):
        return "numero"
    dimensoes = set(coluna.get("dimensoes") or [])
    tipos = set(coluna.get("tipos") or [])
    if "data" in dimensoes or "data" in tipos:
        return "data"
    if "booleano" in tipos:
        return "booleano"
    if "texto" in tipos:
        return "texto"
    return None


def _leitura(grafico, operacao) -> str:
    if operacao in {"contagem", "contagem_distinta", "count_if"} and grafico == "indicador":
        return "contagem_de_registros"
    return {
        "linha": "serie_temporal_de_medida",
        "barra": "medida_por_categoria",
        "pizza": "medida_por_categoria",
        "indicador": "indicador_de_medida",
        "tabela": "tabela_agregada",
        "dispersao": "dispersao_de_medidas",
    }[grafico]


def _coluna(contexto, ref):
    if ref is None:
        return None
    for coluna in contexto.get("colunas") or []:
        if _coord(coluna) == _coord(ref):
            return coluna
    return None


def _achar_regiao(contexto, ref):
    if ref is None:
        return None
    chave = _coord_regiao(ref)
    for regiao in contexto.get("regioes") or []:
        if _coord_regiao(regiao) == chave:
            return regiao
    return None


def _achar_qualidade(contexto, ref):
    if ref is None:
        return None
    chave = _coord_regiao(ref)
    for item in contexto.get("qualidade") or []:
        if _coord_regiao(item) == chave:
            return item
    return None


def _mesma_regiao(regiao, *referencias) -> None:
    chave = _coord_regiao(regiao)
    for ref in referencias:
        if ref is not None and _coord_regiao(ref) != chave:
            raise DefinirErro("juncao_nao_confirmada")


def _ref_coluna(valor):
    if not isinstance(valor, dict) or set(valor) != set(_CHAVES_REF):
        return None
    saida = {}
    for chave in _CHAVES_REF:
        numero = _inteiro(valor.get(chave))
        if numero is None:
            return None
        saida[chave] = numero
    return saida


def _ref_regiao(valor):
    if not isinstance(valor, dict) or set(valor) != set(_CHAVES_REGIAO):
        return None
    saida = {}
    for chave in _CHAVES_REGIAO:
        numero = _inteiro(valor.get(chave))
        if numero is None:
            return None
        saida[chave] = numero
    return saida


def _ref_de_coluna(coluna) -> dict:
    return {chave: coluna[chave] for chave in _CHAVES_REF}


def _coord(ref):
    return (ref["arquivo"], ref["aba"], ref["regiao"], ref["indice"])


def _coord_regiao(ref):
    return (ref["arquivo"], ref["aba"], ref["regiao"])


def _lista_fechada(valor, teto) -> list:
    if not isinstance(valor, list) or len(valor) > teto:
        raise DefinirErro("contrato_saida_invalido")
    return list(valor)


def _proveniencia(provedor, chamada) -> tuple[str, bool]:
    if provedor == PROVEDOR_SIMULADO and chamada is False:
        return PROVEDOR_SIMULADO, False
    if provedor == PROVEDOR_ANTHROPIC and chamada is True:
        return PROVEDOR_ANTHROPIC, True
    if chamada is not False:
        raise DefinirErro("autorizacao_indevida", 403)
    raise DefinirErro("contrato_saida_invalido")


def _proveniencia_declarada(proveniencia) -> tuple[str, bool]:
    nome = proveniencia or PROVEDOR_SIMULADO
    if nome == PROVEDOR_SIMULADO:
        return PROVEDOR_SIMULADO, False
    if nome == PROVEDOR_ANTHROPIC:
        return PROVEDOR_ANTHROPIC, True
    raise DefinirErro("contrato_saida_invalido")


def _provedor_local(funcao) -> bool:
    if not callable(funcao):
        return False
    modulo = getattr(funcao, "__module__", "") or ""
    nome = getattr(funcao, "__name__", "") or ""
    if "anthropic" in modulo.lower() or "anthropic" in nome.lower():
        return False
    return True


def _igual(obtido, esperado, codigo) -> None:
    if obtido != esperado:
        raise DefinirErro(codigo)


def _inteiro(valor, *, minimo=0, maximo=1_000_000):
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    if valor < minimo or valor > maximo:
        return None
    return valor


def _hex64(valor) -> bool:
    return isinstance(valor, str) and len(valor) == 64 and all(caractere in _HEX for caractere in valor)


def _id(valor, prefixo) -> bool:
    if not isinstance(valor, str) or len(valor) < 2 or len(valor) > 4 or valor[0] != prefixo:
        return False
    corpo = valor[1:]
    return corpo.isdigit() and corpo[0] != "0"


def _codigo(valor) -> bool:
    return isinstance(valor, str) and 1 <= len(valor) <= 40 and all(caractere in _NOME for caractere in valor)


def _nome_parametro(valor) -> bool:
    if not isinstance(valor, str) or not 1 <= len(valor) <= 31:
        return False
    if valor[0] not in "abcdefghijklmnopqrstuvwxyz" or valor.lower() in _NOMES_PROIBIDOS:
        return False
    return all(caractere in _NOME for caractere in valor)


def _chave_idempotencia(valor) -> bool:
    if not isinstance(valor, str) or not 8 <= len(valor) <= 80:
        return False
    permitidos = _NOME | frozenset(".:-")
    if valor[0] not in _NOME:
        return False
    return all(caractere in permitidos for caractere in valor)


def _recusar_executavel(valor, profundidade: int = 0) -> None:
    if profundidade > 12:
        raise DefinirErro("contrato_saida_invalido")
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if isinstance(chave, str) and chave.lower() in _CHAVES_PROIBIDAS:
                raise DefinirErro("expressao_nao_permitida")
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, list):
        for item in valor:
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, str):
        normal = valor.strip().lower()
        if "\n" in valor or normal.startswith(("select ", "import ", "def ", "exec(", "eval(")):
            raise DefinirErro("expressao_nao_permitida")


def _recusar_sujo(valor, profundidade: int = 0) -> None:
    if profundidade > 8:
        raise DefinirErro("contexto_nao_autorizado")
    if valor is None or isinstance(valor, bool):
        return
    if isinstance(valor, int):
        if valor < 0 or valor > 1_000_000:
            raise DefinirErro("contexto_nao_autorizado")
        return
    if isinstance(valor, str):
        if len(valor) > 64 or any(ord(caractere) < 32 for caractere in valor):
            raise DefinirErro("contexto_nao_autorizado")
        if any(marca in valor for marca in ("=", "@", "/", "\\")):
            raise DefinirErro("contexto_nao_autorizado")
        if sum(caractere.isdigit() for caractere in valor) >= 6:
            raise DefinirErro("contexto_nao_autorizado")
        return
    if isinstance(valor, list):
        for item in valor:
            _recusar_sujo(item, profundidade + 1)
        return
    if isinstance(valor, dict):
        for item in valor.values():
            _recusar_sujo(item, profundidade + 1)
        return
    raise DefinirErro("contexto_nao_autorizado")


def _varrer(valor, nomes: set[str]) -> None:
    if valor is None or isinstance(valor, bool) or isinstance(valor, int):
        return
    if isinstance(valor, str):
        if valor in _TOKENS or valor in nomes or _id(valor, "g") or _id(valor, "s") or _id(valor, "p") or _hex64(valor):
            return
        raise DefinirErro("contrato_saida_invalido")
    if isinstance(valor, list):
        for item in valor:
            _varrer(item, nomes)
        return
    if isinstance(valor, dict):
        for item in valor.values():
            _varrer(item, nomes)
        return
    raise DefinirErro("contrato_saida_invalido")


def _canonico(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _publicar(analise: dict, documento: dict) -> dict:
    return {
        "ok": True,
        "definicao": {
            "id": analise["id"],
            "usuario_id": analise["usuario_id"],
            "dataset_id": analise["dataset_id"],
            "versao": analise["versao"],
            "impressao": analise["impressao"],
            "rodada": analise["rodada"],
            "etapa": analise["etapa"],
            "estado": analise["estado"],
            "contrato_versao": analise["contrato_versao"],
            "definicoes": len(documento["definicoes"]),
            "bloqueios": sum(1 for item in documento["definicoes"] if item["status"] == "bloqueada"),
            "resultado": documento,
        },
    }


def _com_rodada(escopo, chave: str, montar):
    erro = None
    for _ in range(2):
        try:
            return montar(_proxima_rodada(escopo, chave))
        except AgenteBiErro as exc:
            erro = exc
            if exc.codigo not in _CONFLITOS_RODADA:
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


_TOKENS = frozenset(
    {
        CONTRATO_DEFINIR,
        CONTRATO_CONTEXTO_DEFINIR,
        DSL_CONTRATO,
        CATALOGO_CONTRATO,
        CONTRATO_ANALISAR,
        CONTRATO_SELECAO,
        PROVEDOR_SIMULADO,
        PROVEDOR_ANTHROPIC,
        EXECUCAO,
        _JUNCAO,
        OPERACAO_PAR,
        *_UNIDADES,
        *_GRANULARIDADES,
        *_FILTROS,
        *_TIPOS_PARAMETRO,
        *_PAPEIS_DIMENSAO,
        *_STATUS,
        *_ORIGENS,
        *_LEITURAS,
        *_ESCLARECIMENTOS,
        *_MODALIDADES,
        *OPERACOES_EXECUCAO,
        *GRAFICOS,
    }
)
