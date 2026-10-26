"""Executor determinístico do AgenteBI (SCRUM-261).

Calcula definições já validadas em `definir.v1`. Não chama modelo, não
interpreta SQL ou Python e não grava fila, cobrança ou dashboard.
A memória de cálculo reutiliza `criar_analise` na etapa `execucao`.
"""
from __future__ import annotations

import logging
import math
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal

from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, DSL_CONTRATO
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    obter_analise,
    obter_dataset,
)

logger = logging.getLogger(__name__)

CONTRATO_CALCULO = "calculo.v1"
CONTRATO_CONTEXTO_CALCULO = "contexto.calculo.v1"
EXECUTOR = "executor.deterministico.v1"
MAX_LINHAS = 200_000
MAX_GRUPOS = 500
_PRECISAO = Context(prec=28, rounding=ROUND_HALF_EVEN)
_OPERACOES = frozenset(
    {
        "contagem",
        "contagem_distinta",
        "count_if",
        "maximo",
        "media",
        "minimo",
        "par_numerico",
        "participacao",
        "razao",
        "soma",
    }
)
_NUMERICAS = frozenset({"maximo", "media", "minimo", "participacao", "razao", "soma"})
_CONTAGENS = frozenset({"contagem", "contagem_distinta", "count_if", "par_numerico"})
_CHAVES_PEDIDO = frozenset({"analise_id", "parametros", "relacoes_autorizadas"})
_PROIBIDAS = frozenset(
    {"sql", "python", "codigo", "expressao", "formula", "script", "query", "exec", "eval", "select", "import"}
)
_FILTROS = frozenset({"diferente", "entre", "igual", "maior", "maior_ou_igual", "menor", "menor_ou_igual"})
_ORDENADOS = frozenset({"entre", "maior", "maior_ou_igual", "menor", "menor_ou_igual"})


class CalculoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def calcular_definicao(escopo, corpo: dict) -> dict:
    """Executa a definição do próprio usuário e grava a memória de cálculo."""
    pedido = _pedido(corpo)
    origem = obter_analise(escopo, pedido["analise_id"])
    if origem.get("etapa") != "definicao" or origem.get("estado") != "concluida":
        raise CalculoErro("analise_indisponivel")
    if origem.get("contrato_versao") != CONTRATO_DEFINIR:
        raise CalculoErro("analise_indisponivel")
    documento = origem.get("resultado")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR:
        raise CalculoErro("analise_indisponivel")
    _recusar_executavel(documento)
    if documento.get("dsl") != DSL_CONTRATO or documento.get("execucao") != "nao_executada":
        raise CalculoErro("analise_indisponivel")
    dataset = obter_dataset(escopo, origem["dataset_id"])
    if (
        dataset["versao"] != origem["versao"]
        or dataset["impressao"] != origem["impressao"]
        or documento.get("dataset_id") != dataset["id"]
        or documento.get("dataset_versao") != dataset["versao"]
        or documento.get("dataset_impressao") != dataset["impressao"]
    ):
        raise CalculoErro("identidade_invalida", 409)
    perfil = perfilar_dataset(escopo, dataset["id"])
    vinculo = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    if (
        vinculo.get("id") != dataset["id"]
        or vinculo.get("versao") != dataset["versao"]
        or vinculo.get("impressao") != dataset["impressao"]
        or perfil.get("vinculo_preparacao") != documento.get("vinculo_preparacao")
    ):
        raise CalculoErro("identidade_invalida", 409)
    definicoes = documento.get("definicoes")
    if not isinstance(definicoes, list) or not definicoes:
        raise CalculoErro("analise_indisponivel")
    _conferir_parametros(definicoes, pedido["parametros"])
    _conferir_relacoes(perfil.get("relacoes") or [], pedido["relacoes"])
    indicadores = [
        _calcular_um(
            item,
            perfil,
            dataset.get("dados_tipados") or [],
            dataset.get("estrutura") or {},
            pedido["parametros"],
            pedido["relacoes"],
            dataset,
        )
        for item in definicoes
        if isinstance(item, dict)
    ]
    if len(indicadores) != len(definicoes):
        raise CalculoErro("analise_indisponivel")
    memoria = {
        "contrato": CONTRATO_CALCULO,
        "executor": EXECUTOR,
        "ia_calculo": False,
        "chamada_anthropic": False,
        "origem_definicao_id": origem["id"],
        "origem_contrato": CONTRATO_DEFINIR,
        "dataset_id": dataset["id"],
        "dataset_versao": dataset["versao"],
        "dataset_impressao": dataset["impressao"],
        "indicadores": indicadores,
    }
    bloqueios = [{"id": item["id"], "motivo": item["motivo"]} for item in indicadores if item["status"] == "bloqueado"]
    gravada = _com_rodada(
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
                "etapa": "execucao",
                "estado": "concluida",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_CALCULO,
                    "definicao_id": origem["id"],
                    "dataset_id": dataset["id"],
                    "versao": dataset["versao"],
                    "impressao": dataset["impressao"],
                },
                "regras_negocio": {
                    "executor": EXECUTOR,
                    "ia_calculo": False,
                    "limite_linhas": MAX_LINHAS,
                    "limite_grupos": MAX_GRUPOS,
                },
                "esclarecimentos": {"bloqueios": bloqueios},
                "contrato_versao": CONTRATO_CALCULO,
                "resultado": memoria,
            },
        ),
    )
    logger.info(
        "evento=agente_bi_calculo usuario_id=%s definicao_id=%s calculo_id=%s dataset_id=%s versao=%s indicadores=%s bloqueados=%s",
        escopo.usuario_id,
        origem["id"],
        gravada["id"],
        dataset["id"],
        dataset["versao"],
        len(indicadores),
        len(bloqueios),
    )
    return _publicar(gravada, memoria)


def _pedido(corpo) -> dict:
    if not isinstance(corpo, dict) or not set(corpo) <= _CHAVES_PEDIDO or "analise_id" not in corpo:
        raise CalculoErro("payload_invalido")
    analise_id = corpo.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or analise_id < 1:
        raise CalculoErro("payload_invalido")
    parametros = corpo.get("parametros", {})
    if not isinstance(parametros, dict):
        raise CalculoErro("parametro_inconsistente")
    relacoes = corpo.get("relacoes_autorizadas", [])
    if not isinstance(relacoes, list) or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in relacoes):
        raise CalculoErro("payload_invalido")
    return {"analise_id": analise_id, "parametros": parametros, "relacoes": set(relacoes)}


def _conferir_parametros(definicoes, informados) -> None:
    conhecidos = set()
    for item in definicoes:
        if not isinstance(item, dict):
            continue
        for parametro in item.get("parametros") or []:
            if isinstance(parametro, dict) and isinstance(parametro.get("nome"), str):
                conhecidos.add(parametro["nome"])
    if any(not isinstance(nome, str) or nome not in conhecidos for nome in informados):
        raise CalculoErro("parametro_inconsistente")


def _conferir_relacoes(relacoes, autorizadas) -> None:
    if any(indice >= len(relacoes) for indice in autorizadas):
        raise CalculoErro("referencia_inexistente")


def _calcular_um(definicao, perfil, dados, estrutura, parametros, autorizadas, dataset) -> dict:
    identidade = _identidade(definicao, dataset)
    if identidade is None:
        return _bloqueado("g0", None, "contrato_invalido", dataset, {})
    operacao = definicao.get("metrica", {}).get("operacao")
    if operacao not in _OPERACOES:
        return _bloqueado(identidade["id"], definicao, "operacao_incompativel", dataset, {})
    medida = _coluna(perfil, definicao["metrica"]["coluna"])
    if medida is None:
        return _bloqueado(identidade["id"], definicao, "referencia_inexistente", dataset, {})
    censo = _censo(estrutura, definicao["metrica"]["coluna"])
    motivo = _impedimento(definicao, perfil, medida)
    relacoes = definicao.get("relacionamentos") or []
    if motivo and not (motivo == "confirmar_relacao_candidata" and relacoes and _relacoes_autorizadas(relacoes, autorizadas)):
        return _bloqueado(identidade["id"], definicao, motivo, dataset, censo)
    if definicao.get("status") == "bloqueada" and motivo != "confirmar_relacao_candidata":
        return _bloqueado(identidade["id"], definicao, definicao.get("esclarecimento") or "bloqueada", dataset, censo)
    if definicao.get("status") == "bloqueada" and not _relacoes_autorizadas(relacoes, autorizadas):
        return _bloqueado(identidade["id"], definicao, "confirmar_relacao_candidata", dataset, censo)
    if definicao.get("status") not in {"definida", "bloqueada"}:
        return _bloqueado(identidade["id"], definicao, "contrato_invalido", dataset, censo)
    juncao, falha = _preparar_juncao(definicao, perfil, dados, autorizadas)
    if falha:
        return _bloqueado(identidade["id"], definicao, falha, dataset, censo, juncao="recusada")
    try:
        resolvidos = _resolver_parametros(definicao, parametros)
    except CalculoErro as exc:
        return _bloqueado(identidade["id"], definicao, exc.codigo, dataset, censo, juncao=juncao)
    linhas = _linhas(dados, definicao["metrica"]["coluna"])
    if len(linhas) > MAX_LINHAS:
        return _bloqueado(identidade["id"], definicao, "limite_volume", dataset, censo, juncao=juncao)
    return _agregar(definicao, perfil, linhas, resolvidos, censo, dataset, juncao, medida)


def _agregar(definicao, perfil, linhas, parametros, censo, dataset, juncao, medida) -> dict:
    operacao = definicao["metrica"]["operacao"]
    agrupamento = _papel(definicao, "agrupamento")
    segunda = _papel(definicao, "segunda_medida")
    exclusoes: dict[str, int] = {}
    grupos: dict[str, list] = {}
    distintos: dict[str, set] = {}
    total_num = Decimal(0)
    total_den = Decimal(0)
    total_pares = 0
    base_num = Decimal(0)
    base_presente = False
    participacao_parte = operacao == "participacao" and agrupamento is None
    for linha in linhas:
        falha = _falha_filtro(linha, definicao, parametros) if not participacao_parte else None
        contribuicao = _contribuicao(linha, definicao, medida, segunda)
        if participacao_parte and contribuicao[0] is None and falha is None:
            falha = contribuicao[1]
        if participacao_parte and falha is None and contribuicao[0] == "numero":
            base_num += contribuicao[1]
            base_presente = True
        if not participacao_parte and falha:
            _contar(exclusoes, falha)
            continue
        if participacao_parte:
            falha = _falha_filtro(linha, definicao, parametros)
            if falha == "filtro":
                _contar(exclusoes, falha)
                continue
            if falha:
                _contar(exclusoes, falha)
                continue
        if contribuicao[0] is None:
            _contar(exclusoes, contribuicao[1])
            continue
        chave = _chave_grupo(linha, agrupamento, definicao.get("granularidade"))
        if agrupamento is not None and chave is None:
            if _celula(linha, agrupamento["coluna"]["indice"]).get("tipo") == "ausente":
                _contar(exclusoes, "sem_valor")
                continue
            return _bloqueado(definicao["id"], definicao, "agrupamento_nao_publicavel", dataset, censo, juncao=juncao)
        chave = chave or ""
        if contribuicao[0] == "numero":
            grupos.setdefault(chave, []).append(contribuicao[1])
            if operacao == "razao":
                grupos.setdefault(chave + "\x1fden", []).append(contribuicao[2])
                total_den += contribuicao[2]
            total_num += contribuicao[1]
        elif contribuicao[0] == "contagem":
            grupos.setdefault(chave, []).append(Decimal(1))
            distintos.setdefault(chave, set()).add(contribuicao[1])
        elif contribuicao[0] == "par":
            total_pares += 1
            grupos.setdefault(chave, []).append(Decimal(1))
    if len(grupos) > MAX_GRUPOS:
        return _bloqueado(definicao["id"], definicao, "limite_agrupamento", dataset, censo, juncao=juncao)
    if participacao_parte:
        if not base_presente or base_num == 0:
            return _bloqueado(definicao["id"], definicao, "sem_registros_elegiveis", dataset, censo, juncao=juncao)
        parte = total_num
        resultado = _dividir(parte, base_num)
        if resultado is None:
            return _bloqueado(definicao["id"], definicao, "divisao_por_zero", dataset, censo, juncao=juncao)
        elegiveis = sum(len(valores) for chave, valores in grupos.items() if "\x1f" not in chave)
        return _memoria(
            definicao,
            dataset,
            censo,
            exclusoes,
            juncao,
            elegiveis,
            _texto(resultado),
            _texto(resultado),
            [],
            _avisos(censo, exclusoes, juncao),
        )
    if operacao == "par_numerico":
        return _memoria(
            definicao,
            dataset,
            censo,
            exclusoes,
            juncao,
            total_pares,
            _texto(Decimal(total_pares)),
            _texto(Decimal(total_pares)),
            [],
            _avisos(censo, exclusoes, juncao, extra=["pares_completos"]),
        )
    if agrupamento is None:
        valores = grupos.get("", [])
        conjunto = distintos.get("", set())
        if operacao == "razao":
            if not valores or total_den == 0:
                motivo = "divisao_por_zero" if valores and total_den == 0 else "sem_registros_elegiveis"
                return _bloqueado(definicao["id"], definicao, motivo, dataset, censo, juncao=juncao)
            resultado = _dividir(total_num, total_den)
        else:
            resultado = _reduzir(operacao, valores, conjunto)
        if resultado is None:
            return _bloqueado(definicao["id"], definicao, "sem_registros_elegiveis", dataset, censo, juncao=juncao)
        return _memoria(
            definicao,
            dataset,
            censo,
            exclusoes,
            juncao,
            len(valores),
            _texto(resultado),
            _texto(resultado),
            [],
            _avisos(censo, exclusoes, juncao),
        )
    serie = []
    total = Decimal(0)
    elegiveis = 0
    for chave in sorted(grupos):
        if "\x1f" in chave:
            continue
        valores = grupos[chave]
        elegiveis += len(valores)
        if operacao == "razao":
            denominador = sum(grupos.get(chave + "\x1fden", []), Decimal(0))
            if denominador == 0:
                return _bloqueado(definicao["id"], definicao, "divisao_por_zero", dataset, censo, juncao=juncao)
            valor = _dividir(sum(valores, Decimal(0)), denominador)
        elif operacao == "participacao":
            if total_num == 0:
                return _bloqueado(definicao["id"], definicao, "divisao_por_zero", dataset, censo, juncao=juncao)
            valor = _dividir(sum(valores, Decimal(0)), total_num)
        else:
            valor = _reduzir(operacao, valores, distintos.get(chave, set()))
        if valor is None:
            continue
        if operacao in {"contagem", "count_if", "soma"}:
            total += valor
        serie.append({"chave": chave, "resultado": _texto(valor), "elegiveis": len(valores)})
    if not serie:
        return _bloqueado(definicao["id"], definicao, "sem_registros_elegiveis", dataset, censo, juncao=juncao)
    total_texto = _total_agrupado(operacao, total, total_num, total_den, elegiveis, grupos, distintos)
    if total_texto is None:
        motivo = "divisao_por_zero" if operacao == "razao" else "sem_registros_elegiveis"
        return _bloqueado(definicao["id"], definicao, motivo, dataset, censo, juncao=juncao)
    return _memoria(
        definicao,
        dataset,
        censo,
        exclusoes,
        juncao,
        elegiveis,
        None,
        total_texto,
        serie,
        _avisos(censo, exclusoes, juncao),
    )


def _contribuicao(linha, definicao, medida, segunda):
    operacao = definicao["metrica"]["operacao"]
    celula = _celula(linha, definicao["metrica"]["coluna"]["indice"])
    if operacao == "par_numerico":
        if segunda is None:
            return None, "operacao_incompativel"
        primeiro = _numero(celula)
        segundo = _numero(_celula(linha, segunda["coluna"]["indice"]))
        if primeiro is None or segundo is None:
            return None, "sem_valor"
        return "par", None
    if operacao == "razao":
        if segunda is None:
            return None, "operacao_incompativel"
        numerador = _numero(celula)
        denominador = _numero(_celula(linha, segunda["coluna"]["indice"]))
        if numerador is None or denominador is None:
            return None, "sem_valor"
        return "numero", numerador, denominador
    if operacao in _NUMERICAS:
        numero = _numero(celula)
        if numero is None:
            return None, "sem_valor" if not _presente(celula) else "tipo_incompativel"
        return "numero", numero
    if not _presente(celula):
        return None, "sem_valor"
    if medida.get("dimensao_principal") in {"valor", "quantidade"} and _numero(celula) is None:
        return None, "tipo_incompativel"
    return "contagem", _identidade_valor(celula)


def _reduzir(operacao, valores, distintos):
    if operacao in {"contagem", "count_if"}:
        return Decimal(len(valores))
    if operacao == "contagem_distinta":
        return Decimal(len(distintos))
    if not valores and operacao in _NUMERICAS:
        return None
    if operacao == "soma":
        return sum(valores, Decimal(0))
    if operacao == "media":
        if len(valores) < 1:
            return None
        return _dividir(sum(valores, Decimal(0)), Decimal(len(valores)))
    if operacao == "minimo":
        return min(valores) if valores else None
    if operacao == "maximo":
        return max(valores) if valores else None
    return None


def _total_agrupado(operacao, total, total_num, total_den, elegiveis, grupos, distintos):
    if operacao == "participacao":
        return "1"
    if operacao == "razao":
        quociente = _dividir(total_num, total_den)
        return None if quociente is None else _texto(quociente)
    if operacao == "media":
        quociente = _dividir(total_num, Decimal(elegiveis))
        return None if quociente is None else _texto(quociente)
    if operacao == "contagem_distinta":
        conjunto = set()
        for item in distintos.values():
            conjunto.update(item)
        return _texto(Decimal(len(conjunto)))
    if operacao in {"minimo", "maximo"}:
        valores = [valor for chave, itens in grupos.items() if "\x1f" not in chave for valor in itens]
        if not valores:
            return None
        return _texto(min(valores) if operacao == "minimo" else max(valores))
    return _texto(total)


def _falha_filtro(linha, definicao, parametros):
    for filtro in definicao.get("filtros") or []:
        nome = filtro.get("parametro")
        if nome not in parametros:
            return "informar_parametro"
        tipo, valor = parametros[nome]
        fim = None
        if filtro.get("operador") == "entre":
            nome_fim = filtro.get("parametro_fim")
            if nome_fim not in parametros:
                return "informar_parametro"
            tipo_fim, fim = parametros[nome_fim]
            if tipo_fim != tipo:
                return "parametro_inconsistente"
        celula = _celula(linha, filtro["coluna"]["indice"])
        comparado = _comparar(filtro.get("operador"), celula, tipo, valor, fim)
        if comparado is None:
            return "sem_valor" if not _presente(celula) else "tipo_incompativel"
        if comparado is False:
            return "filtro"
    return None


def _resolver_parametros(definicao, informados) -> dict:
    resolvidos = {}
    declarados = {item["nome"]: item for item in definicao.get("parametros") or [] if isinstance(item, dict)}
    for filtro in definicao.get("filtros") or []:
        for nome in (filtro.get("parametro"), filtro.get("parametro_fim")):
            if not nome:
                continue
            if nome not in declarados or nome not in informados:
                raise CalculoErro("informar_parametro")
            bruto = informados[nome]
            tipo = declarados[nome].get("tipo")
            try:
                resolvidos[nome] = (tipo, _coagir(tipo, bruto))
            except (ArithmeticError, ValueError, TypeError):
                raise CalculoErro("parametro_inconsistente") from None
    return resolvidos


def _coagir(tipo, bruto):
    if tipo == "numero":
        if isinstance(bruto, bool) or bruto is None:
            raise ValueError
        if isinstance(bruto, int):
            return Decimal(bruto)
        if isinstance(bruto, float):
            if not math.isfinite(bruto):
                raise ValueError
            return Decimal(str(bruto))
        if isinstance(bruto, str) and bruto and len(bruto) <= 40:
            return Decimal(bruto)
        raise ValueError
    if tipo == "data":
        if not isinstance(bruto, str):
            raise ValueError
        return date.fromisoformat(bruto)
    if tipo == "texto":
        if not isinstance(bruto, str) or not bruto or len(bruto) > 80 or not _publicavel(bruto):
            raise ValueError
        return bruto
    if tipo == "booleano":
        if not isinstance(bruto, bool):
            raise ValueError
        return bruto
    raise ValueError


def _comparar(operador, celula, tipo, valor, fim):
    if operador not in _FILTROS:
        return None
    if operador in _ORDENADOS and tipo not in {"numero", "data"}:
        return None
    atual = _comparavel(celula, tipo)
    if atual is None:
        return None
    if operador == "igual":
        return atual == valor
    if operador == "diferente":
        return atual != valor
    if operador == "entre":
        return valor <= atual <= fim
    if operador == "maior":
        return atual > valor
    if operador == "maior_ou_igual":
        return atual >= valor
    if operador == "menor":
        return atual < valor
    if operador == "menor_ou_igual":
        return atual <= valor
    return None


def _comparavel(celula, tipo):
    if tipo == "numero":
        return _numero(celula)
    if tipo == "data":
        return _data_celula(celula)
    if tipo == "booleano":
        valor = celula.get("valor") if isinstance(celula, dict) else None
        return valor if isinstance(valor, bool) else None
    if tipo == "texto":
        valor = celula.get("valor") if isinstance(celula, dict) else None
        if celula.get("tipo") != "texto" or not isinstance(valor, str):
            return None
        return valor
    return None


def _preparar_juncao(definicao, perfil, dados, autorizadas):
    relacoes_def = definicao.get("relacionamentos") or []
    if not relacoes_def:
        return "nao_aplicada", None
    if not _relacoes_autorizadas(relacoes_def, autorizadas):
        return "recusada", "confirmar_relacao_candidata"
    relacoes = perfil.get("relacoes") or []
    medida = definicao["metrica"]["coluna"]
    for item in relacoes_def:
        indice = item.get("indice")
        if not isinstance(indice, int) or indice >= len(relacoes):
            return "recusada", "referencia_inexistente"
        motivo = _validar_relacao(relacoes[indice], medida, dados)
        if motivo:
            return "recusada", motivo
    return "muitos_para_um", None


def _validar_relacao(relacao, medida, dados):
    if not isinstance(relacao, dict):
        return "juncao_ambigua"
    if relacao.get("ambiguidades"):
        return "juncao_ambigua"
    cardinalidade = relacao.get("cardinalidade")
    if cardinalidade not in {"1:1", "1:N"}:
        return "juncao_ambigua"
    unico = _lado(relacao.get("lado_unico"))
    associado = _lado(relacao.get("lado_associado"))
    if unico is None or associado is None:
        return "juncao_ambigua"
    if _regiao(unico) == _regiao(associado):
        return "juncao_ambigua"
    if cardinalidade == "1:N" and _regiao(medida) != _regiao(associado):
        return "juncao_incompativel"
    if cardinalidade == "1:1" and _regiao(medida) not in {_regiao(unico), _regiao(associado)}:
        return "juncao_incompativel"
    if not _lado_unico(dados, unico):
        return "juncao_ambigua"
    if _multiplica(dados, unico, associado):
        return "juncao_ambigua"
    return None


def _lado_unico(dados, ref) -> bool:
    vistos = set()
    for linha in _linhas(dados, ref):
        chave = _identidade_valor(_celula(linha, ref["indice"]))
        if chave is None:
            continue
        if chave in vistos:
            return False
        vistos.add(chave)
    return len(vistos) >= 1


def _multiplica(dados, unico, associado) -> bool:
    indice = {}
    for linha in _linhas(dados, unico):
        chave = _identidade_valor(_celula(linha, unico["indice"]))
        if chave is None:
            continue
        indice[chave] = indice.get(chave, 0) + 1
        if indice[chave] > 1:
            return True
    for linha in _linhas(dados, associado):
        chave = _identidade_valor(_celula(linha, associado["indice"]))
        if chave is not None and indice.get(chave, 0) > 1:
            return True
    return False


def _impedimento(definicao, perfil, medida):
    regiao = _regiao_perfil(perfil, definicao["metrica"]["coluna"])
    qualidade = _qualidade(perfil, definicao["metrica"]["coluna"])
    if regiao is None or qualidade is None:
        return "referencia_inexistente"
    ambiguidades = set(regiao.get("ambiguidades_estruturais") or [])
    cabecalho = regiao.get("cabecalho") if isinstance(regiao.get("cabecalho"), dict) else {}
    if cabecalho.get("deteccao") != "inequivoca" or qualidade.get("cabecalho_ambiguo") or "cabecalho_ambiguo" in ambiguidades:
        return "confirmar_rotulos"
    if "divisao_lateral_ambigua" in ambiguidades:
        return "resolver_divisao_lateral"
    formulas = qualidade.get("formulas_sem_cache")
    observadas = formulas.get("observadas") if isinstance(formulas, dict) else formulas
    if isinstance(observadas, int) and observadas > 0:
        return "resolver_formula_sem_cache"
    usadas = [medida]
    for item in definicao.get("dimensoes") or []:
        coluna = _coluna(perfil, item.get("coluna"))
        if coluna is not None:
            usadas.append(coluna)
    for filtro in definicao.get("filtros") or []:
        coluna = _coluna(perfil, filtro.get("coluna"))
        if coluna is not None:
            usadas.append(coluna)
    if any(coluna.get("dominio_inconsistente") for coluna in usadas):
        return "resolver_tipo_misto"
    datas = qualidade.get("datas") if isinstance(qualidade.get("datas"), dict) else {}
    colunas_data = set(datas.get("colunas") or [])
    if any(coluna.get("indice") in colunas_data for coluna in usadas):
        return "resolver_data_ambigua"
    esperada = _unidade_esperada(definicao["metrica"]["operacao"], medida)
    if esperada is None:
        return "informar_unidade"
    if definicao["metrica"].get("unidade") != esperada:
        return "unidade_incompativel"
    if definicao.get("relacionamentos"):
        return "confirmar_relacao_candidata"
    if definicao["metrica"]["operacao"] == "count_if" and not definicao.get("filtros"):
        return "informar_parametro"
    if (
        definicao["metrica"]["operacao"] == "participacao"
        and definicao.get("grafico") == "indicador"
        and not definicao.get("filtros")
    ):
        return "informar_parametro"
    if any(_filtro_incompleto(item) for item in definicao.get("filtros") or []):
        return "informar_parametro"
    return None


def _unidade_esperada(operacao, coluna):
    if operacao in {"contagem", "contagem_distinta", "count_if", "par_numerico"}:
        return "contagem" if operacao != "par_numerico" else coluna.get("dimensao_principal") if coluna.get("dimensao_principal") in {"valor", "quantidade"} else None
    if operacao in {"razao", "participacao"}:
        return "adimensional"
    if coluna.get("dimensao_principal") == "valor":
        return "valor"
    if coluna.get("dimensao_principal") == "quantidade":
        return "quantidade"
    return None


def _memoria(definicao, dataset, censo, exclusoes, juncao, elegiveis, resultado, total, serie, avisos) -> dict:
    excluidos = [{"motivo": motivo, "quantidade": quantidade} for motivo, quantidade in sorted(censo.items())]
    for motivo, quantidade in sorted(exclusoes.items()):
        excluidos.append({"motivo": motivo, "quantidade": quantidade})
    return {
        "id": definicao["id"],
        "status": "calculado",
        "motivo": None,
        "operacao": definicao["metrica"]["operacao"],
        "agregacao": definicao["metrica"]["operacao"],
        "grafico": definicao.get("grafico"),
        "unidade": definicao["metrica"].get("unidade"),
        "granularidade": definicao.get("granularidade"),
        "dataset_id": dataset["id"],
        "dataset_versao": dataset["versao"],
        "dataset_impressao": dataset["impressao"],
        "definicao_id": definicao["id"],
        "filtros": [_filtro_publico(item) for item in definicao.get("filtros") or []],
        "juncao": juncao,
        "elegiveis": elegiveis,
        "excluidos": excluidos,
        "grupos": len(serie),
        "resultado": resultado,
        "total": total,
        "serie": serie,
        "avisos": avisos,
    }


def _bloqueado(identificador, definicao, motivo, dataset, censo, *, juncao="nao_aplicada") -> dict:
    metrica = definicao.get("metrica") if isinstance(definicao, dict) else {}
    if not isinstance(metrica, dict):
        metrica = {}
    return {
        "id": identificador,
        "status": "bloqueado",
        "motivo": motivo,
        "operacao": metrica.get("operacao"),
        "agregacao": metrica.get("operacao"),
        "grafico": None if not isinstance(definicao, dict) else definicao.get("grafico"),
        "unidade": metrica.get("unidade"),
        "granularidade": None if not isinstance(definicao, dict) else definicao.get("granularidade"),
        "dataset_id": dataset["id"],
        "dataset_versao": dataset["versao"],
        "dataset_impressao": dataset["impressao"],
        "definicao_id": identificador,
        "filtros": [] if not isinstance(definicao, dict) else [_filtro_publico(item) for item in definicao.get("filtros") or [] if isinstance(item, dict)],
        "juncao": juncao,
        "elegiveis": 0,
        "excluidos": [{"motivo": motivo_censo, "quantidade": quantidade} for motivo_censo, quantidade in sorted(censo.items())],
        "grupos": 0,
        "resultado": None,
        "total": None,
        "serie": [],
        "avisos": _avisos(censo, {}, juncao),
    }


def _filtro_publico(item) -> dict:
    return {
        "operador": item.get("operador"),
        "parametro": item.get("parametro"),
        "parametro_fim": item.get("parametro_fim"),
        "coluna": {
            "arquivo": item["coluna"]["arquivo"],
            "aba": item["coluna"]["aba"],
            "regiao": item["coluna"]["regiao"],
            "indice": item["coluna"]["indice"],
        },
    }


def _avisos(censo, exclusoes, juncao, extra=()):
    avisos = []
    if censo.get("pre_preenchida"):
        avisos.append("linhas_pre_preenchidas_excluidas")
    if censo.get("vazia"):
        avisos.append("linhas_vazias_excluidas")
    if any(quantidade > 0 for quantidade in exclusoes.values()):
        avisos.append("ha_exclusoes")
    if juncao == "muitos_para_um":
        avisos.append("juncao_sem_duplicacao")
    avisos.extend(extra)
    return sorted(set(avisos))


def _censo(estrutura, ref) -> dict:
    regioes = estrutura.get("regioes") if isinstance(estrutura, dict) else None
    if not isinstance(regioes, list):
        return {}
    regiao = next(
        (
            item
            for item in regioes
            if isinstance(item, dict)
            and item.get("arquivo") == ref["arquivo"]
            and item.get("aba") == ref["aba"]
            and item.get("indice") == ref["regiao"]
        ),
        None,
    )
    if regiao is None:
        return {}
    censo = {}
    for motivo, campo in (
        ("pre_preenchida", "linhas_pre_preenchidas"),
        ("vazia", "linhas_vazias"),
        ("invalida", "linhas_invalidas"),
        ("cabecalho_repetido", "linhas_cabecalho_repetido"),
    ):
        quantidade = regiao.get(campo)
        if isinstance(quantidade, int) and not isinstance(quantidade, bool) and quantidade > 0:
            censo[motivo] = quantidade
    return censo


def _chave_grupo(linha, agrupamento, granularidade):
    if agrupamento is None:
        return ""
    celula = _celula(linha, agrupamento["coluna"]["indice"])
    if not _presente(celula):
        return None
    if granularidade in {"data", "tempo"}:
        data = _data_celula(celula)
        if data is None:
            return None
        if granularidade == "data":
            return data.isoformat()
        valor = celula.get("valor")
        return valor if isinstance(valor, str) and _publicavel(valor) else data.isoformat()
    if celula.get("tipo") == "numero":
        numero = _numero(celula)
        return None if numero is None else _texto(numero)
    valor = celula.get("valor")
    if isinstance(valor, str) and _publicavel(valor):
        return valor
    return None


def _publicavel(texto: str) -> bool:
    if not texto or len(texto) > 80:
        return False
    if "@" in texto or "://" in texto or any(ord(caractere) < 32 for caractere in texto):
        return False
    digitos = sum(caractere.isdigit() for caractere in texto)
    if digitos >= 11:
        return False
    return True


def _numero(celula):
    if not isinstance(celula, dict):
        return None
    tipo = celula.get("tipo")
    if tipo == "formula":
        if celula.get("cache_estado") != "presente" or celula.get("tipo_cache") != "numero":
            return None
        valor = celula.get("valor")
    elif tipo == "numero":
        valor = celula.get("valor")
    else:
        return None
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, int):
        return Decimal(valor)
    if isinstance(valor, float):
        if not math.isfinite(valor):
            return None
        return Decimal(str(valor))
    if isinstance(valor, str):
        try:
            return Decimal(valor)
        except ArithmeticError:
            return None
    return None


def _data_celula(celula):
    if not isinstance(celula, dict) or celula.get("tipo") != "data" or not isinstance(celula.get("valor"), str):
        return None
    try:
        return date.fromisoformat(celula["valor"][:10])
    except ValueError:
        return None


def _presente(celula) -> bool:
    if not isinstance(celula, dict):
        return False
    tipo = celula.get("tipo")
    if tipo in {"ausente", "erro", "nao_suportado"}:
        return False
    if tipo == "formula":
        return celula.get("cache_estado") == "presente" and celula.get("valor") is not None
    return celula.get("valor") is not None


def _identidade_valor(celula):
    if not _presente(celula):
        return None
    numero = _numero(celula)
    if numero is not None:
        return "n:" + _texto(numero)
    data = _data_celula(celula)
    if data is not None:
        return "d:" + data.isoformat()
    valor = celula.get("valor")
    if isinstance(valor, str):
        return "t:" + valor
    if isinstance(valor, bool):
        return "b:" + ("1" if valor else "0")
    return None


def _texto(valor: Decimal) -> str:
    if not valor.is_finite():
        raise CalculoErro("resultado_instavel")
    texto = format(valor, "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def _dividir(numerador: Decimal, denominador: Decimal):
    if denominador == 0:
        return None
    return _PRECISAO.divide(numerador, denominador)


def _celula(linha, indice):
    valores = linha.get("valores") if isinstance(linha, dict) else None
    if not isinstance(valores, list) or not isinstance(indice, int) or indice < 0 or indice >= len(valores):
        return {"tipo": "ausente"}
    celula = valores[indice]
    return celula if isinstance(celula, dict) else {"tipo": "ausente"}


def _linhas(dados, ref):
    return [
        item
        for item in dados
        if isinstance(item, dict)
        and item.get("arquivo") == ref["arquivo"]
        and item.get("aba") == ref["aba"]
        and item.get("regiao") == ref["regiao"]
    ]


def _coluna(perfil, ref):
    if not isinstance(ref, dict):
        return None
    for coluna in perfil.get("colunas") or []:
        if (
            coluna.get("arquivo") == ref.get("arquivo")
            and coluna.get("aba") == ref.get("aba")
            and coluna.get("regiao") == ref.get("regiao")
            and coluna.get("indice") == ref.get("indice")
        ):
            return coluna
    return None


def _regiao_perfil(perfil, ref):
    for regiao in perfil.get("regioes") or []:
        if regiao.get("arquivo") == ref["arquivo"] and regiao.get("aba") == ref["aba"] and regiao.get("regiao") == ref["regiao"]:
            return regiao
    return None


def _qualidade(perfil, ref):
    por_regiao = (perfil.get("qualidade") or {}).get("por_regiao") if isinstance(perfil.get("qualidade"), dict) else None
    if not isinstance(por_regiao, list):
        return None
    for item in por_regiao:
        if item.get("arquivo") == ref["arquivo"] and item.get("aba") == ref["aba"] and item.get("regiao") == ref["regiao"]:
            return item
    return None


def _papel(definicao, papel):
    for item in definicao.get("dimensoes") or []:
        if isinstance(item, dict) and item.get("papel") == papel:
            return item
    return None


def _lado(lado):
    if not isinstance(lado, dict):
        return None
    if any(not isinstance(lado.get(chave), int) for chave in ("arquivo", "aba", "regiao", "coluna")):
        return None
    return {
        "arquivo": lado["arquivo"],
        "aba": lado["aba"],
        "regiao": lado["regiao"],
        "indice": lado["coluna"],
    }


def _regiao(ref) -> tuple:
    return (ref["arquivo"], ref["aba"], ref["regiao"])


def _relacoes_autorizadas(relacoes, autorizadas) -> bool:
    return bool(relacoes) and all(isinstance(item, dict) and item.get("indice") in autorizadas for item in relacoes)


def _filtro_incompleto(item) -> bool:
    if not isinstance(item, dict):
        return True
    if item.get("parametro") is None:
        return True
    return item.get("operador") == "entre" and item.get("parametro_fim") is None


def _identidade(definicao, dataset):
    identificador = definicao.get("id")
    metrica = definicao.get("metrica")
    if not isinstance(identificador, str) or not isinstance(metrica, dict) or not isinstance(metrica.get("coluna"), dict):
        return None
    return {"id": identificador, "dataset": dataset["id"]}


def _contar(mapa, motivo) -> None:
    mapa[motivo] = mapa.get(motivo, 0) + 1


def _recusar_executavel(valor, profundidade: int = 0) -> None:
    if profundidade > 12:
        raise CalculoErro("expressao_nao_permitida")
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if isinstance(chave, str) and chave.lower() in _PROIBIDAS:
                raise CalculoErro("expressao_nao_permitida")
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, list):
        for item in valor:
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, str) and (valor.strip().lower().startswith(("select ", "import ", "def ", "exec(", "eval("))):
        raise CalculoErro("expressao_nao_permitida")


def _publicar(analise: dict, memoria: dict) -> dict:
    return {
        "ok": True,
        "calculo": {
            "id": analise["id"],
            "usuario_id": analise["usuario_id"],
            "dataset_id": analise["dataset_id"],
            "versao": analise["versao"],
            "impressao": analise["impressao"],
            "rodada": analise["rodada"],
            "etapa": analise["etapa"],
            "estado": analise["estado"],
            "contrato_versao": analise["contrato_versao"],
            "resultado": memoria,
        },
    }


def _com_rodada(escopo, chave: str, montar):
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
