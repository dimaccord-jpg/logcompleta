"""Contratos `patch.definir.v2` e `definir.v2` do AgenteBI (SCRUM-269).

Prepara o plano conversacional e a definição evolutiva. A validação é
estrutural: não interpreta texto, não chama provedor, não executa a DSL e
não publica dashboard. Métrica, agregação, dimensão, filtro, unidade e
referência continuam nas funções de `definir.v1`.

Usuário, conta, dashboard e dataset não entram nestes documentos. A revisão
base e a definição anterior só são aceitas quando o servidor as informa no
vínculo e o documento repete os mesmos números. Documento `v1` passa por
`reconhecer_historico` e permanece como foi gravado.
"""
from __future__ import annotations

import copy

from app.services.agente_bi_analisar_service import CATALOGO_CONTRATO, CONTRATO_ANALISAR, GRAFICOS, OPERACAO_PAR
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    DSL_CONTRATO,
    EXECUCAO,
    OPERACOES_EXECUCAO,
    DefinirErro,
    _CHAVES_RASCUNHO,
    _ESCLARECIMENTOS,
    _FILTROS,
    _FILTROS_ORDENADOS,
    _JUNCAO,
    _NOMES_PROIBIDOS,
    _ORIGENS,
    _STATUS,
    _TIPOS_PARAMETRO,
    _coluna,
    _dimensoes,
    _forma,
    _grafico_compativel,
    _id,
    _inteiro,
    _leitura,
    _lista_fechada,
    _metrica,
    _motivo,
    _nome_parametro,
    _operacao_compativel,
    _parametros,
    _recusar_executavel,
    _tipo_coluna,
    _ref_coluna,
    _ref_regiao,
    _relacionamentos,
    _unidade,
    _validar_filtros,
    _varrer,
)
from app.services.agente_bi_persistencia_service import LIMITE_GRAFICOS

CONTRATO_PATCH_V1 = "patch.definir.v1"
CONTRATO_PATCH_V2 = "patch.definir.v2"
CONTRATO_DEFINIR_V2 = "definir.v2"
_VERSAO = 2
_LIMITE_OPERACOES = 24
_SITUACOES = frozenset({"esclarecimento", "proposto"})
_ACOES = frozenset(
    {
        "consultar",
        "criar_grafico",
        "alterar_indicador",
        "alterar_agrupamento",
        "alterar_apresentacao",
        "definir_filtro",
        "alterar_periodo",
    }
)
_OPERACOES_SEGUNDA = frozenset({OPERACAO_PAR, "razao"})
_CHAVES_PLANO = frozenset({"contrato", "versao", "situacao", "operacoes"})
_CHAVES_OPERACAO = frozenset(
    {
        "id",
        "acao",
        "grafico",
        "temporario",
        "dependencias",
        "coluna",
        "segunda_coluna",
        "operacao",
        "apresentacao",
        "operador",
        "parametros",
        "esclarecimento",
    }
)
_CHAVES_PARAMETRO_TIPADO = frozenset({"forma", "nome", "tipo"})
_CHAVES_PARAMETRO_OPACO = frozenset({"forma", "referencia"})
_CHAVES_ESCLARECIMENTO = frozenset({"motivo"})
_CHAVES_DEFINICAO = frozenset(
    {
        "contrato",
        "versao",
        "catalogo",
        "dsl",
        "execucao",
        "definicao_anterior_id",
        "revisao_base",
        "origem_contrato",
        "proveniencia",
        "plano",
        "definicoes",
    }
)
_CHAVES_PROVENIENCIA = frozenset(
    {
        "definicao_anterior_id",
        "revisao_base",
        "origem_contrato",
        "plano_contrato",
        "plano_versao",
        "ids_preservados",
        "graficos_novos",
    }
)
_CHAVES_GRAFICO_NOVO = frozenset({"id", "temporario"})
_CHAVES_VINCULO_PLANO = frozenset({"graficos", "colunas"})
_CHAVES_VINCULO_DEFINICAO = frozenset(
    {
        "definicao_anterior_id",
        "revisao_base",
        "origem_contrato",
        "definicoes",
        "contexto",
        "plano",
    }
)
_CHAVES_CONTEXTO = ("colunas", "regioes", "qualidade", "relacoes")
_CHAVES_IDENTIDADE = frozenset(
    {
        "usuario_id",
        "conta_id",
        "franquia_id",
        "dashboard_id",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
    }
)
_HISTORICOS = frozenset({CONTRATO_PATCH_V1, CONTRATO_DEFINIR, CONTRATO_ANALISAR})
_ORIGENS_DEFINICAO = frozenset({CONTRATO_DEFINIR, CONTRATO_DEFINIR_V2})
_CHAVES_REF = ("arquivo", "aba", "regiao", "indice")


def descrever_contratos_v2() -> dict:
    """Enumera o que a etapa sabe validar. Não descreve execução."""
    return {
        "plano": {
            "contrato": CONTRATO_PATCH_V2,
            "versao": _VERSAO,
            "situacoes": sorted(_SITUACOES),
            "acoes": sorted(_ACOES),
            "chaves": sorted(_CHAVES_PLANO),
            "chaves_operacao": sorted(_CHAVES_OPERACAO),
            "operacoes_segunda_coluna": sorted(_OPERACOES_SEGUNDA),
        },
        "definicao": {
            "contrato": CONTRATO_DEFINIR_V2,
            "versao": _VERSAO,
            "dsl": DSL_CONTRATO,
            "catalogo": CATALOGO_CONTRATO,
            "execucao": EXECUCAO,
            "limite_graficos": LIMITE_GRAFICOS,
            "chaves": sorted(_CHAVES_DEFINICAO),
            "chaves_proveniencia": sorted(_CHAVES_PROVENIENCIA),
        },
    }


def reconhecer_historico(documento: dict) -> dict:
    """Reconhece um documento v1 e devolve a mesma estrutura, sem conversão."""
    contrato = documento.get("contrato") if isinstance(documento, dict) else None
    if not isinstance(documento, dict) or not _enumerado(contrato, _HISTORICOS):
        raise DefinirErro("contrato_saida_invalido")
    return copy.deepcopy(documento)


def validar_plano(documento: dict, vinculo: dict) -> dict:
    """Confere o plano fechado. O vínculo informa gráficos e colunas autorizadas."""
    if not isinstance(documento, dict):
        raise DefinirErro("contrato_saida_invalido")
    _recusar_executavel(documento)
    if documento.get("contrato") != CONTRATO_PATCH_V2 or _inteiro(documento.get("versao")) != _VERSAO:
        raise DefinirErro("contrato_saida_invalido")
    _recusar_identidades(documento)
    if set(documento) != _CHAVES_PLANO:
        raise DefinirErro("contrato_saida_invalido")
    situacao = documento.get("situacao")
    if not _enumerado(situacao, _SITUACOES):
        raise DefinirErro("contrato_saida_invalido")
    existentes, colunas = _vinculo_plano(vinculo)
    brutas = documento.get("operacoes")
    if not isinstance(brutas, list) or not brutas or len(brutas) > _LIMITE_OPERACOES:
        raise DefinirErro("contrato_saida_invalido")
    operacoes = [_operacao(item, existentes, colunas) for item in brutas]
    arestas = _dependencias_resolviveis(operacoes)
    _conferir_temporarios(operacoes, arestas)
    criacoes = sum(1 for item in operacoes if item["acao"] == "criar_grafico")
    if len(existentes) + criacoes > LIMITE_GRAFICOS:
        raise DefinirErro("limite_graficos")
    tem_esclarecimento = any(item["esclarecimento"] is not None for item in operacoes)
    if tem_esclarecimento != (situacao == "esclarecimento"):
        raise DefinirErro("contrato_saida_invalido")
    return {
        "contrato": CONTRATO_PATCH_V2,
        "versao": _VERSAO,
        "situacao": situacao,
        "operacoes": operacoes,
    }


def validar_definicao_evolutiva(documento: dict, vinculo: dict) -> dict:
    """Confere a definição final, a proveniência e o plano já fechado.

    `contrato_saida_validado` não integra o documento e não encerra a
    validação. Cada gráfico volta a passar pela DSL existente.
    """
    if not isinstance(documento, dict):
        raise DefinirErro("contrato_saida_invalido")
    _recusar_executavel(documento)
    if documento.get("contrato") != CONTRATO_DEFINIR_V2 or _inteiro(documento.get("versao")) != _VERSAO:
        raise DefinirErro("contrato_saida_invalido")
    if "contrato_saida_validado" in documento:
        raise DefinirErro("contrato_saida_invalido")
    _recusar_identidades(documento)
    if set(documento) != _CHAVES_DEFINICAO:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("catalogo") != CATALOGO_CONTRATO or documento.get("dsl") != DSL_CONTRATO:
        raise DefinirErro("contrato_saida_invalido")
    if documento.get("execucao") != EXECUCAO:
        raise DefinirErro("contrato_saida_invalido")
    servidor = _vinculo_definicao(vinculo)
    _igual_proveniencia(
        documento.get("definicao_anterior_id"),
        documento.get("revisao_base"),
        documento.get("origem_contrato"),
        servidor,
    )
    brutas = documento.get("definicoes")
    if not isinstance(brutas, list) or not brutas:
        raise DefinirErro("contrato_saida_invalido")
    if len(brutas) > LIMITE_GRAFICOS or len(servidor["ids"]) > LIMITE_GRAFICOS:
        raise DefinirErro("limite_graficos")
    definicoes = [_validar_item(item, servidor["contexto"]) for item in brutas]
    ids = [item["id"] for item in definicoes]
    if len(set(ids)) != len(ids) or ids[: len(servidor["ids"])] != servidor["ids"]:
        raise DefinirErro("contrato_saida_invalido")
    for item in definicoes:
        base = servidor["por_id"].get(item["id"])
        if base is None:
            if item["sugestao_id"] is not None:
                raise DefinirErro("contrato_saida_invalido")
            continue
        if item["sugestao_id"] != base["sugestao_id"] or item["origem"] != base["origem"]:
            raise DefinirErro("contrato_saida_invalido")
    novos = ids[len(servidor["ids"]) :]
    plano = validar_plano(
        documento.get("plano"),
        {"graficos": list(servidor["ids"]), "colunas": servidor["contexto"]["colunas"]},
    )
    plano_servidor = validar_plano(
        servidor["plano"],
        {"graficos": list(servidor["ids"]), "colunas": servidor["contexto"]["colunas"]},
    )
    if plano != plano_servidor:
        raise DefinirErro("proveniencia_inconsistente")
    criacoes = [item["temporario"] for item in plano["operacoes"] if item["acao"] == "criar_grafico"]
    graficos_novos = [{"id": grafico, "temporario": temp} for grafico, temp in zip(novos, criacoes)]
    if len(criacoes) != len(novos) or any(item["temporario"] is None for item in graficos_novos):
        raise DefinirErro("proveniencia_inconsistente")
    proveniencia = _proveniencia(documento.get("proveniencia"), servidor, graficos_novos)
    return {
        "contrato": CONTRATO_DEFINIR_V2,
        "versao": _VERSAO,
        "catalogo": CATALOGO_CONTRATO,
        "dsl": DSL_CONTRATO,
        "execucao": EXECUCAO,
        "definicao_anterior_id": servidor["definicao_anterior_id"],
        "revisao_base": servidor["revisao_base"],
        "origem_contrato": servidor["origem_contrato"],
        "proveniencia": proveniencia,
        "plano": plano,
        "definicoes": definicoes,
    }


def _vinculo_plano(vinculo) -> tuple[set[str], dict]:
    if not isinstance(vinculo, dict):
        raise DefinirErro("contrato_saida_invalido")
    if _CHAVES_IDENTIDADE & set(vinculo):
        raise DefinirErro("autorizacao_indevida", 403)
    if set(vinculo) != _CHAVES_VINCULO_PLANO:
        raise DefinirErro("contrato_saida_invalido")
    return _graficos_autorizados(vinculo.get("graficos")), _colunas_autorizadas(vinculo.get("colunas"))


def _vinculo_definicao(vinculo) -> dict:
    if not isinstance(vinculo, dict):
        raise DefinirErro("contrato_saida_invalido")
    if _CHAVES_IDENTIDADE & set(vinculo):
        raise DefinirErro("autorizacao_indevida", 403)
    if set(vinculo) != _CHAVES_VINCULO_DEFINICAO:
        raise DefinirErro("contrato_saida_invalido")
    anterior = _inteiro(vinculo.get("definicao_anterior_id"), minimo=1)
    revisao = _inteiro(vinculo.get("revisao_base"), minimo=1)
    origem = vinculo.get("origem_contrato")
    if anterior is None or revisao is None or not _enumerado(origem, _ORIGENS_DEFINICAO):
        raise DefinirErro("contrato_saida_invalido")
    por_id = {}
    ids = []
    for item in _lista_base(vinculo.get("definicoes")):
        por_id[item["id"]] = item
        ids.append(item["id"])
    return {
        "definicao_anterior_id": anterior,
        "revisao_base": revisao,
        "origem_contrato": origem,
        "por_id": por_id,
        "ids": ids,
        "contexto": _contexto(vinculo.get("contexto")),
        "plano": vinculo.get("plano"),
    }


def _lista_base(valor) -> list:
    if not isinstance(valor, list) or not valor or len(valor) > LIMITE_GRAFICOS:
        if isinstance(valor, list) and len(valor) > LIMITE_GRAFICOS:
            raise DefinirErro("limite_graficos")
        raise DefinirErro("contrato_saida_invalido")
    vistos = set()
    for item in valor:
        if not isinstance(item, dict) or not _id(item.get("id"), "g") or item["id"] in vistos:
            raise DefinirErro("contrato_saida_invalido")
        sugestao = item.get("sugestao_id")
        if sugestao is not None and not _id(sugestao, "s"):
            raise DefinirErro("contrato_saida_invalido")
        if not _enumerado(item.get("origem"), _ORIGENS):
            raise DefinirErro("contrato_saida_invalido")
        vistos.add(item["id"])
    return valor


def _contexto(valor) -> dict:
    if not isinstance(valor, dict):
        raise DefinirErro("contrato_saida_invalido")
    saida = {}
    for chave in _CHAVES_CONTEXTO:
        item = valor.get(chave)
        if not isinstance(item, list):
            raise DefinirErro("contrato_saida_invalido")
        saida[chave] = item
    for coluna in saida["colunas"]:
        if not isinstance(coluna, dict):
            raise DefinirErro("contrato_saida_invalido")
        _blindar_coluna(coluna)
    for regiao in saida["regioes"]:
        if not isinstance(regiao, dict):
            raise DefinirErro("contrato_saida_invalido")
        _blindar_regiao(regiao)
    return saida


def _graficos_autorizados(valor) -> set[str]:
    if not isinstance(valor, list) or len(valor) > LIMITE_GRAFICOS:
        if isinstance(valor, list) and len(valor) > LIMITE_GRAFICOS:
            raise DefinirErro("limite_graficos")
        raise DefinirErro("contrato_saida_invalido")
    saida = []
    for item in valor:
        if not _id(item, "g") or item in saida:
            raise DefinirErro("contrato_saida_invalido")
        saida.append(item)
    return set(saida)


def _colunas_autorizadas(valor) -> dict:
    if not isinstance(valor, list):
        raise DefinirErro("contrato_saida_invalido")
    indice = {}
    for coluna in valor:
        if not isinstance(coluna, dict):
            raise DefinirErro("contrato_saida_invalido")
        _blindar_coluna(coluna)
        ref = _ref_coluna({chave: coluna.get(chave) for chave in _CHAVES_REF})
        if ref is None:
            raise DefinirErro("contrato_saida_invalido")
        chave = (ref["arquivo"], ref["aba"], ref["regiao"], ref["indice"])
        if chave in indice:
            raise DefinirErro("contrato_saida_invalido")
        indice[chave] = coluna
    return indice


def _operacao(item, existentes: set[str], colunas: dict) -> dict:
    if not isinstance(item, dict) or set(item) != _CHAVES_OPERACAO:
        raise DefinirErro("contrato_saida_invalido")
    acao = item.get("acao")
    if not _id(item.get("id"), "o") or not _enumerado(acao, _ACOES):
        raise DefinirErro("contrato_saida_invalido")
    grafico = _grafico_existente(item.get("grafico"), existentes)
    temporario = _temporario(item.get("temporario"))
    coluna, coluna_obj = _coluna_autorizada(item.get("coluna"), colunas)
    segunda, _segunda = _coluna_autorizada(item.get("segunda_coluna"), colunas)
    canonico = {
        "id": item["id"],
        "acao": acao,
        "grafico": grafico,
        "temporario": temporario,
        "dependencias": _dependencias(item.get("dependencias")),
        "coluna": coluna,
        "segunda_coluna": segunda,
        "operacao": _escolha(item.get("operacao"), OPERACOES_EXECUCAO),
        "apresentacao": _escolha(item.get("apresentacao"), GRAFICOS),
        "operador": _escolha(item.get("operador"), _FILTROS),
        "parametros": _parametros_plano(item.get("parametros")),
        "esclarecimento": _esclarecimento(item.get("esclarecimento")),
    }
    _conferir_acao(canonico, coluna_obj)
    return canonico


def _grafico_existente(valor, existentes: set[str]):
    if valor is None:
        return None
    if not _id(valor, "g"):
        raise DefinirErro("contrato_saida_invalido")
    if valor not in existentes:
        raise DefinirErro("referencia_inexistente")
    return valor


def _temporario(valor):
    if valor is None:
        return None
    if not _id(valor, "t"):
        raise DefinirErro("referencia_temporaria_invalida")
    return valor


def _dependencias(valor) -> list:
    if not isinstance(valor, list) or len(valor) > _LIMITE_OPERACOES:
        raise DefinirErro("contrato_saida_invalido")
    saida = []
    for item in valor:
        if not _id(item, "o"):
            raise DefinirErro("dependencia_invalida")
        saida.append(item)
    return saida


def _enumerado(valor, permitidos) -> bool:
    return isinstance(valor, str) and valor in permitidos


def _escolha(valor, permitidos):
    if valor is None:
        return None
    if not _enumerado(valor, permitidos):
        raise DefinirErro("contrato_saida_invalido")
    return valor


def _blindar_coluna(coluna: dict) -> None:
    for chave in ("tipos", "dimensoes", "evidencias"):
        if chave not in coluna:
            continue
        _lista_de_textos(coluna.get(chave))
    if "dimensao_principal" not in coluna:
        return
    principal = coluna.get("dimensao_principal")
    if principal is not None and not isinstance(principal, str):
        raise DefinirErro("contrato_saida_invalido")


def _blindar_regiao(regiao: dict) -> None:
    if "ambiguidades" not in regiao:
        return
    _lista_de_textos(regiao.get("ambiguidades"))


def _lista_de_textos(valor) -> None:
    if valor is None:
        return
    if not isinstance(valor, list) or any(not isinstance(item, str) for item in valor):
        raise DefinirErro("contrato_saida_invalido")


def _coluna_autorizada(valor, colunas: dict):
    if valor is None:
        return None, None
    ref = _ref_coluna(valor)
    if ref is None:
        raise DefinirErro("contrato_saida_invalido")
    chave = (ref["arquivo"], ref["aba"], ref["regiao"], ref["indice"])
    coluna = colunas.get(chave)
    if coluna is None:
        raise DefinirErro("referencia_inexistente")
    return ref, coluna


def _parametros_plano(valor) -> list:
    if not isinstance(valor, list) or len(valor) > 2:
        raise DefinirErro("contrato_saida_invalido")
    nomes = set()
    referencias = set()
    saida = []
    for item in valor:
        if not isinstance(item, dict):
            raise DefinirErro("contrato_saida_invalido")
        forma = item.get("forma")
        if forma == "tipado" and set(item) == _CHAVES_PARAMETRO_TIPADO:
            nome = item.get("nome")
            if not _nome_parametro(nome):
                if isinstance(nome, str) and nome.lower() in _NOMES_PROIBIDOS:
                    raise DefinirErro("expressao_nao_permitida")
                raise DefinirErro("parametro_inconsistente")
            tipo = item.get("tipo")
            if not _enumerado(tipo, _TIPOS_PARAMETRO) or nome in nomes:
                raise DefinirErro("parametro_inconsistente")
            nomes.add(nome)
            saida.append({"forma": "tipado", "nome": nome, "tipo": tipo})
            continue
        if forma == "opaca" and set(item) == _CHAVES_PARAMETRO_OPACO and _id(item.get("referencia"), "p"):
            if item["referencia"] in referencias:
                raise DefinirErro("parametro_inconsistente")
            referencias.add(item["referencia"])
            saida.append({"forma": "opaca", "referencia": item["referencia"]})
            continue
        raise DefinirErro("contrato_saida_invalido")
    return saida


def _esclarecimento(valor):
    if valor is None:
        return None
    if not isinstance(valor, dict) or set(valor) != _CHAVES_ESCLARECIMENTO:
        raise DefinirErro("contrato_saida_invalido")
    motivo = valor.get("motivo")
    if not _enumerado(motivo, _ESCLARECIMENTOS):
        raise DefinirErro("contrato_saida_invalido")
    return {"motivo": motivo}


def _conferir_acao(op: dict, coluna) -> None:
    if op["grafico"] is not None and op["temporario"] is not None:
        raise DefinirErro("contrato_saida_invalido")
    if op["acao"] == "criar_grafico" and op["grafico"] is not None:
        raise DefinirErro("contrato_saida_invalido")
    _conferir_segunda(op)
    if op["esclarecimento"] is not None:
        return
    acao = op["acao"]
    if acao == "consultar":
        um_alvo = (op["grafico"] is None) != (op["temporario"] is None)
        if not um_alvo or not _sem_modificador(op):
            raise DefinirErro("contrato_saida_invalido")
        return
    if acao == "criar_grafico":
        if op["temporario"] is None or op["coluna"] is None or op["operacao"] is None or op["apresentacao"] is None:
            raise DefinirErro("contrato_saida_invalido")
        if op["operador"] is not None:
            raise DefinirErro("contrato_saida_invalido")
        return
    if (op["grafico"] is None) == (op["temporario"] is None):
        raise DefinirErro("contrato_saida_invalido")
    if acao == "alterar_indicador":
        if op["operacao"] is None or op["apresentacao"] is not None or op["operador"] is not None or op["parametros"]:
            raise DefinirErro("contrato_saida_invalido")
        return
    if acao == "alterar_agrupamento":
        if op["coluna"] is None or op["apresentacao"] is not None or not _sem_metrica(op) or op["parametros"]:
            raise DefinirErro("contrato_saida_invalido")
        return
    if acao == "alterar_apresentacao":
        if op["apresentacao"] is None or op["coluna"] is not None or not _sem_metrica(op) or op["parametros"]:
            raise DefinirErro("contrato_saida_invalido")
        return
    if acao == "definir_filtro":
        _conferir_filtro(op, coluna, periodo=False)
        return
    if acao == "alterar_periodo":
        _conferir_filtro(op, coluna, periodo=True)
        return
    raise DefinirErro("contrato_saida_invalido")


def _conferir_segunda(op: dict) -> None:
    segunda = op["segunda_coluna"]
    if not _enumerado(op["operacao"], _OPERACOES_SEGUNDA):
        if segunda is not None:
            raise DefinirErro("contrato_saida_invalido")
        return
    if segunda is not None and op["coluna"] is not None and segunda == op["coluna"]:
        raise DefinirErro("contrato_saida_invalido")
    if op["esclarecimento"] is None and segunda is None:
        raise DefinirErro("contrato_saida_invalido")


def _sem_modificador(op: dict) -> bool:
    return (
        op["coluna"] is None
        and op["segunda_coluna"] is None
        and op["operacao"] is None
        and op["apresentacao"] is None
        and op["operador"] is None
        and not op["parametros"]
    )


def _sem_metrica(op: dict) -> bool:
    return op["operacao"] is None and op["operador"] is None


def _conferir_filtro(op: dict, coluna, *, periodo: bool) -> None:
    if op["coluna"] is None or op["operacao"] is not None or op["apresentacao"] is not None:
        raise DefinirErro("contrato_saida_invalido")
    operador = op["operador"]
    if not _enumerado(operador, _FILTROS):
        raise DefinirErro("contrato_saida_invalido")
    tipo = _tipo_coluna(coluna)
    if tipo is None:
        raise DefinirErro("filtro_incompativel")
    if periodo:
        if operador != "entre" or tipo != "data":
            raise DefinirErro("filtro_incompativel")
        esperado = 2
    else:
        if operador in _FILTROS_ORDENADOS and tipo not in {"numero", "data"}:
            raise DefinirErro("filtro_incompativel")
        esperado = 2 if operador == "entre" else 1
    if len(op["parametros"]) != esperado:
        raise DefinirErro("parametro_inconsistente")
    for item in op["parametros"]:
        if item["forma"] == "tipado" and item["tipo"] != tipo:
            raise DefinirErro("filtro_incompativel")


def _dependencias_resolviveis(operacoes) -> dict:
    ids = [item["id"] for item in operacoes]
    if len(set(ids)) != len(ids):
        raise DefinirErro("contrato_saida_invalido")
    conhecidos = set(ids)
    arestas = {}
    for item in operacoes:
        deps = item["dependencias"]
        if len(set(deps)) != len(deps) or any(dep not in conhecidos for dep in deps):
            raise DefinirErro("dependencia_invalida")
        arestas[item["id"]] = list(deps)
    if not _aciclico(ids, arestas):
        raise DefinirErro("dependencia_invalida")
    return arestas


def _aciclico(ids, arestas) -> bool:
    visitando = set()
    visitado = set()

    def visitar(no) -> bool:
        if no in visitando:
            return False
        if no in visitado:
            return True
        visitando.add(no)
        for destino in arestas.get(no, ()):
            if not visitar(destino):
                return False
        visitando.remove(no)
        visitado.add(no)
        return True

    return all(visitar(no) for no in ids)


def _conferir_temporarios(operacoes, arestas) -> None:
    criadores = {}
    for item in operacoes:
        if item["acao"] != "criar_grafico":
            continue
        temp = item["temporario"]
        if temp is None:
            if item["esclarecimento"] is None:
                raise DefinirErro("referencia_temporaria_invalida")
            continue
        if temp in criadores:
            raise DefinirErro("referencia_temporaria_invalida")
        criadores[temp] = item["id"]
    for item in operacoes:
        if item["acao"] == "criar_grafico" or item["temporario"] is None:
            continue
        criador = criadores.get(item["temporario"])
        if criador is None or criador not in _alcance(item["id"], arestas):
            raise DefinirErro("referencia_temporaria_invalida")


def _alcance(origem, arestas) -> set[str]:
    vistos = set()
    pilha = list(arestas.get(origem, ()))
    while pilha:
        atual = pilha.pop()
        if atual in vistos:
            continue
        vistos.add(atual)
        pilha.extend(arestas.get(atual, ()))
    return vistos


def _igual_proveniencia(anterior, revisao, origem, servidor) -> None:
    if _inteiro(anterior, minimo=1) != servidor["definicao_anterior_id"]:
        raise DefinirErro("proveniencia_inconsistente")
    if _inteiro(revisao, minimo=1) != servidor["revisao_base"]:
        raise DefinirErro("proveniencia_inconsistente")
    if origem != servidor["origem_contrato"]:
        raise DefinirErro("proveniencia_inconsistente")


def _proveniencia(valor, servidor, graficos_novos) -> dict:
    if not isinstance(valor, dict) or set(valor) != _CHAVES_PROVENIENCIA:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("definicao_anterior_id") != servidor["definicao_anterior_id"]:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("revisao_base") != servidor["revisao_base"]:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("origem_contrato") != servidor["origem_contrato"]:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("plano_contrato") != CONTRATO_PATCH_V2 or valor.get("plano_versao") != _VERSAO:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("ids_preservados") != servidor["ids"]:
        raise DefinirErro("proveniencia_inconsistente")
    if valor.get("graficos_novos") != graficos_novos:
        raise DefinirErro("proveniencia_inconsistente")
    for item in graficos_novos:
        if set(item) != _CHAVES_GRAFICO_NOVO:
            raise DefinirErro("proveniencia_inconsistente")
    return {
        "definicao_anterior_id": servidor["definicao_anterior_id"],
        "revisao_base": servidor["revisao_base"],
        "origem_contrato": servidor["origem_contrato"],
        "plano_contrato": CONTRATO_PATCH_V2,
        "plano_versao": _VERSAO,
        "ids_preservados": list(servidor["ids"]),
        "graficos_novos": graficos_novos,
    }


def _recusar_enumerados_da_definicao(item: dict) -> None:
    """Confere tipos dos enums antes de reutilizar as checagens do v1."""
    metrica = item.get("metrica")
    if isinstance(metrica, dict):
        if not isinstance(metrica.get("operacao"), str):
            raise DefinirErro("contrato_saida_invalido")
        unidade = metrica.get("unidade")
        if unidade is not None and not isinstance(unidade, str):
            raise DefinirErro("contrato_saida_invalido")
    dimensoes = item.get("dimensoes")
    if isinstance(dimensoes, list):
        for bruto in dimensoes:
            if not isinstance(bruto, dict):
                continue
            for campo in ("papel", "granularidade"):
                if bruto.get(campo) is not None and not isinstance(bruto.get(campo), str):
                    raise DefinirErro("contrato_saida_invalido")
            unidade = bruto.get("unidade")
            if unidade is not None and not isinstance(unidade, str):
                raise DefinirErro("contrato_saida_invalido")
    filtros = item.get("filtros")
    if isinstance(filtros, list):
        for bruto in filtros:
            if isinstance(bruto, dict) and bruto.get("operador") is not None and not isinstance(bruto.get("operador"), str):
                raise DefinirErro("contrato_saida_invalido")
    parametros = item.get("parametros")
    if isinstance(parametros, list):
        for bruto in parametros:
            if isinstance(bruto, dict) and bruto.get("tipo") is not None and not isinstance(bruto.get("tipo"), str):
                raise DefinirErro("parametro_inconsistente")


def _validar_item(item, contexto) -> dict:
    if not isinstance(item, dict) or set(item) != _CHAVES_RASCUNHO | {"id"}:
        raise DefinirErro("contrato_saida_invalido")
    if not _id(item.get("id"), "g"):
        raise DefinirErro("contrato_saida_invalido")
    sugestao = item.get("sugestao_id")
    if sugestao is not None and not _id(sugestao, "s"):
        raise DefinirErro("contrato_saida_invalido")
    if not _enumerado(item.get("status"), _STATUS) or not _enumerado(item.get("origem"), _ORIGENS):
        raise DefinirErro("contrato_saida_invalido")
    if not _enumerado(item.get("grafico"), GRAFICOS):
        raise DefinirErro("contrato_saida_invalido")
    if item.get("juncao") != _JUNCAO:
        raise DefinirErro("juncao_nao_confirmada")
    _recusar_enumerados_da_definicao(item)
    metrica = _metrica(item.get("metrica"))
    medida = _coluna(contexto, metrica["coluna"])
    if medida is None:
        raise DefinirErro("referencia_inexistente")
    agrupamento, segunda = _refs_de_dimensoes(item.get("dimensoes"))
    dimensoes = _dimensoes(item.get("dimensoes"), contexto, {"agrupamento": agrupamento, "segunda_medida": segunda})
    agrup_col = None if agrupamento is None else _coluna(contexto, agrupamento)
    segunda_col = None if segunda is None else _coluna(contexto, segunda)
    if not _operacao_compativel(metrica["operacao"], medida, segunda_col):
        raise DefinirErro("operacao_incompativel")
    if not _grafico_compativel(item["grafico"], metrica["operacao"], medida, agrup_col, segunda_col):
        raise DefinirErro("grafico_incompativel")
    esperada, esperadas = _forma(item["grafico"], metrica["operacao"], medida, agrup_col, segunda_col)
    if item.get("granularidade") != esperada or dimensoes != esperadas:
        raise DefinirErro("granularidade_incompativel")
    unidade = _unidade(metrica["operacao"], medida)
    if metrica["unidade"] != unidade:
        raise DefinirErro("unidade_incompativel")
    if item.get("leitura") != _leitura(item["grafico"], metrica["operacao"]):
        raise DefinirErro("contrato_saida_invalido")
    filtros = _lista_fechada(item.get("filtros"), 8)
    parametros = _parametros(item.get("parametros"))
    relacionamentos = _relacionamentos(item.get("relacionamentos"), contexto)
    regiao = _ref_regiao(
        {
            "arquivo": metrica["coluna"]["arquivo"],
            "aba": metrica["coluna"]["aba"],
            "regiao": metrica["coluna"]["regiao"],
        }
    )
    if regiao is None:
        raise DefinirErro("referencia_inexistente")
    motivo = _motivo(
        contexto,
        {"grafico": item["grafico"], "operacao": metrica["operacao"], "regiao": regiao},
        medida,
        agrup_col,
        segunda_col,
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
    elif item["status"] != "definida" or esclarecimento is not None or relacionamentos:
        raise DefinirErro("contrato_saida_invalido")
    _validar_filtros(
        filtros,
        parametros,
        contexto,
        regiao,
        incompleto=motivo == "informar_parametro",
        condicao_ausente=motivo == "informar_parametro" and not filtros and metrica["operacao"] in {"count_if", "participacao"},
    )
    saida = {
        "id": item["id"],
        "sugestao_id": sugestao,
        "status": item["status"],
        "grafico": item["grafico"],
        "leitura": item["leitura"],
        "origem": item["origem"],
        "metrica": metrica,
        "granularidade": esperada,
        "dimensoes": dimensoes,
        "filtros": filtros,
        "parametros": parametros,
        "relacionamentos": relacionamentos,
        "juncao": _JUNCAO,
        "esclarecimento": esclarecimento,
    }
    _varrer(saida, {parametro["nome"] for parametro in parametros})
    return saida


def _refs_de_dimensoes(valor):
    if not isinstance(valor, list):
        raise DefinirErro("contrato_saida_invalido")
    agrupamento = None
    segunda = None
    for item in valor:
        if not isinstance(item, dict):
            raise DefinirErro("contrato_saida_invalido")
        papel = item.get("papel")
        coluna = _ref_coluna(item.get("coluna"))
        if papel == "agrupamento":
            if agrupamento is not None or coluna is None:
                raise DefinirErro("contrato_saida_invalido")
            agrupamento = coluna
            continue
        if papel == "segunda_medida":
            if segunda is not None or coluna is None:
                raise DefinirErro("contrato_saida_invalido")
            segunda = coluna
            continue
        raise DefinirErro("granularidade_incompativel")
    return agrupamento, segunda


def _recusar_identidades(valor, profundidade: int = 0) -> None:
    if profundidade > 12:
        raise DefinirErro("contrato_saida_invalido")
    if isinstance(valor, dict):
        if _CHAVES_IDENTIDADE & set(valor):
            raise DefinirErro("autorizacao_indevida", 403)
        for item in valor.values():
            _recusar_identidades(item, profundidade + 1)
        return
    if isinstance(valor, list):
        for item in valor:
            _recusar_identidades(item, profundidade + 1)
