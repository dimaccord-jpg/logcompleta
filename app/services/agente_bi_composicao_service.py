"""Compositor determinístico `definir.v2` do AgenteBI (SCRUM-269, etapa 3.2).

Recebe plano, definição anterior, contexto, vínculos de parâmetro e
identidade já conferidos pelo servidor. Trabalha em cópia. Não interpreta
texto, não calcula, não publica, não grava e não chama provedor.

Cada gráfico anterior passa pelo validador da DSL antes de qualquer
operação, no contexto técnico autorizado. A representação validada precisa
coincidir com a entrada. Estados intermediários podem ser incompatíveis. A
região do filtro só é conferida com a medida final, sem criar junção. Só o
documento final passa de novo pela DSL e por `validar_definicao_evolutiva`.
Motivo derivado pela DSL fica no gráfico, com status bloqueada. Combinação
que não forma documento válido devolve esclarecimento e não inclui definição
parcial.
"""
from __future__ import annotations

import copy

from app.services.agente_bi_analisar_service import CATALOGO_CONTRATO, OPERACAO_PAR
from app.services.agente_bi_contrato_v2_service import (
    CONTRATO_DEFINIR_V2,
    CONTRATO_PATCH_V2,
    _validar_item,
    validar_definicao_evolutiva,
    validar_plano,
)
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    DSL_CONTRATO,
    EXECUCAO,
    DefinirErro,
    _CHAVES_RASCUNHO,
    _JUNCAO,
    _ORIGENS,
    _TIPOS_PARAMETRO,
    _coluna,
    _coord,
    _coord_regiao,
    _forma,
    _grafico_compativel,
    _id,
    _leitura,
    _motivo,
    _nome_parametro,
    _operacao_compativel,
    _ref_de_coluna,
    _tipo_coluna,
    _unidade,
)
from app.services.agente_bi_persistencia_service import LIMITE_GRAFICOS

_VERSAO = 2
_ORIGEM_GRAFICO_NOVO = "dados"
_OPERACOES_SEGUNDA = frozenset({OPERACAO_PAR, "razao"})
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
_CHAVES_IDENTIDADE_OPERACAO = frozenset(
    {
        "definicao_anterior_id",
        "revisao_base",
        "origem_contrato",
    }
)
_CHAVES_VINCULO_PARAMETRO = frozenset({"referencia", "grafico", "nome", "tipo"})
_FILTROS_ACAO = frozenset({"definir_filtro", "alterar_periodo"})


class ComposicaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


class _Falha(Exception):
    def __init__(self, codigo: str, operacao: str | None = None, itens: list | None = None):
        self.codigo = codigo
        self.operacao = operacao
        self.itens = itens
        super().__init__(codigo)


def compor_definicao(
    plano: dict,
    definicao_anterior: dict,
    contexto: dict,
    vinculos_parametros: list,
    identidade: dict,
) -> dict:
    """Compõe `definir.v2` ou devolve esclarecimento, sem definição parcial."""
    _exigir_entradas(plano, definicao_anterior, contexto, vinculos_parametros, identidade)
    try:
        return _compor(plano, definicao_anterior, contexto, vinculos_parametros, identidade)
    except _Falha as exc:
        return _esclarecimento(exc.codigo, exc.operacao, exc.itens)


def _exigir_entradas(plano, definicao_anterior, contexto, vinculos_parametros, identidade) -> None:
    if not isinstance(plano, dict) or not isinstance(definicao_anterior, dict) or not isinstance(contexto, dict):
        raise ComposicaoErro("payload_invalido")
    if not isinstance(vinculos_parametros, list):
        raise ComposicaoErro("payload_invalido")
    if not isinstance(identidade, dict) or set(identidade) != _CHAVES_IDENTIDADE_OPERACAO:
        raise ComposicaoErro("payload_invalido")
    for envelope in (plano, definicao_anterior, contexto, identidade, *vinculos_parametros):
        if isinstance(envelope, dict) and _CHAVES_IDENTIDADE & set(envelope):
            raise ComposicaoErro("autorizacao_indevida", 403)
    anterior = identidade.get("definicao_anterior_id")
    revisao = identidade.get("revisao_base")
    if isinstance(anterior, bool) or not isinstance(anterior, int) or anterior < 1:
        raise ComposicaoErro("payload_invalido")
    if isinstance(revisao, bool) or not isinstance(revisao, int) or revisao < 1:
        raise ComposicaoErro("payload_invalido")
    if identidade.get("origem_contrato") not in {CONTRATO_DEFINIR, CONTRATO_DEFINIR_V2}:
        raise ComposicaoErro("payload_invalido")


def _compor(plano, definicao_anterior, contexto, vinculos_parametros, identidade) -> dict:
    plano = copy.deepcopy(plano)
    definicao_anterior = copy.deepcopy(definicao_anterior)
    contexto = _contexto_fechado(contexto)
    if definicao_anterior.get("contrato") != identidade["origem_contrato"]:
        raise _Falha("proveniencia_inconsistente")
    analise = definicao_anterior.get("analise_id")
    if isinstance(analise, bool) or not isinstance(analise, int) or analise != identidade["definicao_anterior_id"]:
        raise _Falha("proveniencia_inconsistente")
    originais = _definicoes_anteriores(definicao_anterior.get("definicoes"))
    _conferir_definicoes_anteriores(originais, contexto)
    ids = [item["id"] for item in originais]
    try:
        canonico = validar_plano(plano, {"graficos": list(ids), "colunas": contexto["colunas"]})
    except DefinirErro as exc:
        if exc.codigo == "autorizacao_indevida":
            raise ComposicaoErro(exc.codigo, exc.status) from exc
        raise _Falha(exc.codigo) from exc
    if canonico["situacao"] == "esclarecimento":
        itens = [
            {"operacao": op["id"], "motivo": op["esclarecimento"]["motivo"]}
            for op in canonico["operacoes"]
            if op["esclarecimento"] is not None
        ]
        if not itens:
            raise _Falha("contrato_saida_invalido")
        raise _Falha(itens[0]["motivo"], itens[0]["operacao"], itens)
    vinculos = _indexar_vinculos(vinculos_parametros, originais)
    criacoes = [op["temporario"] for op in canonico["operacoes"] if op["acao"] == "criar_grafico"]
    if len(originais) + len(criacoes) > LIMITE_GRAFICOS:
        raise _Falha("limite_graficos")
    ordenadas = _ordenar(canonico["operacoes"])
    _recusar_conflitos(ordenadas)
    trabalho = copy.deepcopy(originais)
    estados = {item["id"]: _estado_de(item) for item in trabalho}
    usados = set(ids)
    temporarios = {}
    novos = []
    # IDs e a lista final seguem a ordem textual das criações: a proveniência
    # casa o enésimo gráfico novo com a enésima criação do plano.
    for op in canonico["operacoes"]:
        if op["acao"] != "criar_grafico":
            continue
        identificador = _novo_id(usados)
        usados.add(identificador)
        temporarios[op["temporario"]] = identificador
        item = _grafico_novo(identificador)
        estados[identificador] = _estado_novo(item)
        novos.append(identificador)
    for op in ordenadas:
        _aplicar(op, estados, temporarios, vinculos, contexto)
    for identificador in ids:
        if estados[identificador]["sujo"]:
            _materializar(estados[identificador], contexto)
    for identificador in novos:
        if not estados[identificador]["sujo"]:
            raise _Falha("contrato_saida_invalido")
        _materializar(estados[identificador], contexto)
    definicoes = [estados[identificador]["item"] for identificador in ids]
    definicoes.extend(estados[identificador]["item"] for identificador in novos)
    _auditar(originais, definicoes, ordenadas, temporarios, novos, vinculos)
    graficos_novos = [{"id": identificador, "temporario": temp} for identificador, temp in zip(novos, criacoes)]
    documento = _documento(identidade, _proveniencia(identidade, ids, graficos_novos), copy.deepcopy(canonico), definicoes)
    vinculo = {
        "definicao_anterior_id": identidade["definicao_anterior_id"],
        "revisao_base": identidade["revisao_base"],
        "origem_contrato": identidade["origem_contrato"],
        "definicoes": [
            {"id": item["id"], "sugestao_id": item["sugestao_id"], "origem": item["origem"]} for item in originais
        ],
        "contexto": contexto,
        "plano": copy.deepcopy(canonico),
    }
    try:
        validado = validar_definicao_evolutiva(documento, vinculo)
    except DefinirErro as exc:
        raise _Falha(exc.codigo) from exc
    _auditar(originais, validado["definicoes"], ordenadas, temporarios, novos, vinculos)
    return validado


def _esclarecimento(motivo: str, operacao: str | None = None, itens: list | None = None) -> dict:
    blocos = itens if itens is not None else [{"operacao": operacao, "motivo": motivo}]
    return {
        "situacao": "esclarecimento",
        "motivo": motivo,
        "operacao": operacao,
        "esclarecimentos": blocos,
    }


def _contexto_fechado(contexto: dict) -> dict:
    saida = {}
    for chave in _CHAVES_CONTEXTO:
        item = contexto.get(chave)
        if not isinstance(item, list):
            raise _Falha("contrato_saida_invalido")
        saida[chave] = copy.deepcopy(item)
    return saida


def _definicoes_anteriores(valor) -> list:
    if not isinstance(valor, list) or not valor or len(valor) > LIMITE_GRAFICOS:
        if isinstance(valor, list) and len(valor) > LIMITE_GRAFICOS:
            raise _Falha("limite_graficos")
        raise _Falha("contrato_saida_invalido")
    vistos = set()
    saida = []
    for item in valor:
        if not isinstance(item, dict) or set(item) != _CHAVES_RASCUNHO | {"id"}:
            raise _Falha("contrato_saida_invalido")
        if not _id(item.get("id"), "g") or item["id"] in vistos:
            raise _Falha("contrato_saida_invalido")
        sugestao = item.get("sugestao_id")
        if sugestao is not None and not _id(sugestao, "s"):
            raise _Falha("contrato_saida_invalido")
        if item.get("origem") not in _ORIGENS:
            raise _Falha("contrato_saida_invalido")
        vistos.add(item["id"])
        saida.append(copy.deepcopy(item))
    return saida


def _conferir_definicoes_anteriores(definicoes: list, contexto: dict) -> None:
    """Recusa gráfico anterior que a DSL não reconheceria intacto."""
    for item in definicoes:
        try:
            validado = _validar_item(copy.deepcopy(item), contexto)
        except DefinirErro as exc:
            raise _Falha("proveniencia_inconsistente") from exc
        if validado != item:
            raise _Falha("proveniencia_inconsistente")


def _indexar_vinculos(brutos: list, definicoes: list) -> dict:
    por_grafico = {item["id"]: item for item in definicoes}
    indice = {}
    for bruto in brutos:
        if not isinstance(bruto, dict) or set(bruto) != _CHAVES_VINCULO_PARAMETRO:
            raise _Falha("parametro_inconsistente")
        referencia = bruto.get("referencia")
        grafico = bruto.get("grafico")
        nome = bruto.get("nome")
        tipo = bruto.get("tipo")
        if not _id(referencia, "p") or not _id(grafico, "g"):
            raise _Falha("parametro_inconsistente")
        if tipo not in _TIPOS_PARAMETRO or not _nome_parametro(nome) or referencia in indice:
            raise _Falha("parametro_inconsistente")
        item = por_grafico.get(grafico)
        if item is None:
            raise _Falha("referencia_inexistente")
        declarado = next((parametro for parametro in item["parametros"] if parametro.get("nome") == nome), None)
        if declarado is None or declarado.get("tipo") != tipo or not isinstance(declarado.get("obrigatorio"), bool):
            raise _Falha("parametro_inconsistente")
        indice[referencia] = {
            "referencia": referencia,
            "grafico": grafico,
            "nome": nome,
            "tipo": tipo,
            "obrigatorio": declarado["obrigatorio"],
        }
    return indice


def _ordenar(operacoes: list) -> list:
    """Dependências primeiro. No empate, vale a ordem textual."""
    indice = {item["id"]: posicao for posicao, item in enumerate(operacoes)}
    por_id = {item["id"]: item for item in operacoes}
    restantes = {item["id"]: set(item["dependencias"]) for item in operacoes}
    prontos = [item["id"] for item in operacoes if not item["dependencias"]]
    ordem = []
    while prontos:
        prontos.sort(key=lambda identificador: indice[identificador])
        atual = prontos.pop(0)
        ordem.append(por_id[atual])
        for item in operacoes:
            deps = restantes[item["id"]]
            if atual in deps:
                deps.remove(atual)
                if not deps:
                    prontos.append(item["id"])
    if len(ordem) != len(operacoes):
        raise _Falha("dependencia_invalida")
    return ordem


def _recusar_conflitos(operacoes: list) -> None:
    vistos = {}
    for op in operacoes:
        campo = _campo_escrita(op)
        if campo is None:
            continue
        if campo in vistos:
            raise _Falha(
                "conflito_escrita",
                op["id"],
                [
                    {"operacao": vistos[campo], "motivo": "conflito_escrita"},
                    {"operacao": op["id"], "motivo": "conflito_escrita"},
                ],
            )
        vistos[campo] = op["id"]


def _campo_escrita(op: dict):
    if op["acao"] == "consultar":
        return None
    if op["acao"] == "criar_grafico":
        return ("criar", op["temporario"])
    alvo = op["grafico"] if op["grafico"] is not None else op["temporario"]
    if op["acao"] == "alterar_indicador":
        return ("indicador", alvo)
    if op["acao"] == "alterar_agrupamento":
        return ("agrupamento", alvo)
    if op["acao"] == "alterar_apresentacao":
        return ("apresentacao", alvo)
    if op["acao"] in _FILTROS_ACAO:
        return ("filtro", alvo, _coord(op["coluna"]))
    return ("desconhecida", op["id"])


def _estado_de(item: dict) -> dict:
    return {
        "item": item,
        "medida": copy.deepcopy(item["metrica"]["coluna"]),
        "agrupamento": _papel(item, "agrupamento"),
        "segunda": _papel(item, "segunda_medida"),
        "operacao": item["metrica"]["operacao"],
        "apresentacao": item["grafico"],
        "sujo": False,
        "ultima": None,
    }


def _estado_novo(item: dict) -> dict:
    return {
        "item": item,
        "medida": None,
        "agrupamento": None,
        "segunda": None,
        "operacao": None,
        "apresentacao": None,
        "sujo": False,
        "ultima": None,
    }


def _grafico_novo(identificador: str) -> dict:
    return {
        "id": identificador,
        "sugestao_id": None,
        "status": "definida",
        "grafico": "indicador",
        "leitura": "indicador_de_medida",
        "origem": _ORIGEM_GRAFICO_NOVO,
        "metrica": {
            "operacao": "contagem",
            "coluna": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1},
            "unidade": "contagem",
        },
        "granularidade": "total",
        "dimensoes": [],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": _JUNCAO,
        "esclarecimento": None,
    }


def _novo_id(usados: set[str]) -> str:
    numero = 1
    while numero < 1000:
        candidato = f"g{numero}"
        if candidato not in usados:
            return candidato
        numero += 1
    raise _Falha("limite_graficos")


def _aplicar(op: dict, estados: dict, temporarios: dict, vinculos: dict, contexto: dict) -> None:
    if op["acao"] == "criar_grafico":
        _aplicar_criacao(estados[temporarios[op["temporario"]]], op)
        return
    alvo = _resolver(op, temporarios, estados)
    if op["acao"] == "consultar":
        return
    estado = estados[alvo]
    if op["acao"] == "alterar_indicador":
        _aplicar_indicador(estado, op)
        return
    if op["acao"] == "alterar_agrupamento":
        estado["agrupamento"] = copy.deepcopy(op["coluna"])
        _marcar(estado, op)
        return
    if op["acao"] == "alterar_apresentacao":
        estado["apresentacao"] = op["apresentacao"]
        _marcar(estado, op)
        return
    if op["acao"] in _FILTROS_ACAO:
        _aplicar_filtro(estado, op, alvo, vinculos, contexto)
        return
    raise _Falha("contrato_saida_invalido", op["id"])


def _marcar(estado: dict, op: dict) -> None:
    estado["sujo"] = True
    estado["ultima"] = op["id"]


def _resolver(op: dict, temporarios: dict, estados: dict) -> str:
    if op["grafico"] is not None:
        if op["grafico"] not in estados:
            raise _Falha("referencia_inexistente", op["id"])
        return op["grafico"]
    destino = temporarios.get(op["temporario"])
    if destino is None:
        raise _Falha("referencia_temporaria_invalida", op["id"])
    return destino


def _aplicar_criacao(estado: dict, op: dict) -> None:
    item = estado["item"]
    estado["medida"] = copy.deepcopy(op["coluna"])
    estado["agrupamento"] = None
    estado["segunda"] = None if op["segunda_coluna"] is None else copy.deepcopy(op["segunda_coluna"])
    estado["operacao"] = op["operacao"]
    estado["apresentacao"] = op["apresentacao"]
    item["filtros"] = []
    item["parametros"] = []
    item["relacionamentos"] = []
    item["sugestao_id"] = None
    item["origem"] = _ORIGEM_GRAFICO_NOVO
    _marcar(estado, op)


def _aplicar_indicador(estado: dict, op: dict) -> None:
    estado["operacao"] = op["operacao"]
    if op["coluna"] is not None:
        estado["medida"] = copy.deepcopy(op["coluna"])
    if op["operacao"] in _OPERACOES_SEGUNDA:
        estado["segunda"] = copy.deepcopy(op["segunda_coluna"])
    else:
        estado["segunda"] = None
    _marcar(estado, op)


def _aplicar_filtro(estado: dict, op: dict, alvo: str, vinculos: dict, contexto: dict) -> None:
    coluna = _coluna(contexto, op["coluna"])
    if coluna is None:
        raise _Falha("referencia_inexistente", op["id"])
    tipo = _tipo_coluna(coluna)
    if tipo is None:
        raise _Falha("filtro_incompativel", op["id"])
    nomes = _nomes_do_filtro(op, alvo, vinculos, tipo)
    if len(nomes) != len({item["nome"] for item in nomes}):
        raise _Falha("parametro_inconsistente", op["id"])
    item = estado["item"]
    coord = _coord(op["coluna"])
    filtros = [copy.deepcopy(atual) for atual in item["filtros"] if _coord(atual["coluna"]) != coord]
    if len(filtros) >= 8:
        raise _Falha("contrato_saida_invalido", op["id"])
    filtros.append(
        {
            "coluna": copy.deepcopy(op["coluna"]),
            "operador": op["operador"],
            "parametro": nomes[0]["nome"],
            "parametro_fim": nomes[1]["nome"] if len(nomes) == 2 else None,
        }
    )
    usados = set()
    for filtro in filtros:
        usados.add(filtro["parametro"])
        if filtro.get("parametro_fim"):
            usados.add(filtro["parametro_fim"])
    mantidos = [copy.deepcopy(atual) for atual in item["parametros"] if atual.get("nome") in usados]
    conhecidos = {atual["nome"]: atual for atual in mantidos}
    for pedido in nomes:
        atual = conhecidos.get(pedido["nome"])
        if atual is None:
            if len(mantidos) >= 8:
                raise _Falha("contrato_saida_invalido", op["id"])
            novo = {"nome": pedido["nome"], "tipo": pedido["tipo"], "obrigatorio": pedido["obrigatorio"]}
            mantidos.append(novo)
            conhecidos[pedido["nome"]] = novo
            continue
        if atual["tipo"] != pedido["tipo"]:
            raise _Falha("parametro_inconsistente", op["id"])
    item["filtros"] = filtros
    item["parametros"] = mantidos
    _marcar(estado, op)


def _nomes_do_filtro(op: dict, alvo: str, vinculos: dict, tipo: str) -> list:
    saida = []
    for parametro in op["parametros"]:
        if parametro["forma"] == "tipado":
            if parametro["tipo"] != tipo:
                raise _Falha("filtro_incompativel", op["id"])
            saida.append({"nome": parametro["nome"], "tipo": parametro["tipo"], "obrigatorio": True})
            continue
        vinculo = vinculos.get(parametro["referencia"])
        if vinculo is None:
            raise _Falha("referencia_inexistente", op["id"])
        if vinculo["grafico"] != alvo:
            raise _Falha("parametro_inconsistente", op["id"])
        if vinculo["tipo"] != tipo:
            raise _Falha("parametro_inconsistente", op["id"])
        saida.append(
            {"nome": vinculo["nome"], "tipo": vinculo["tipo"], "obrigatorio": vinculo["obrigatorio"]}
        )
    return saida


def _materializar(estado: dict, contexto: dict) -> None:
    """Recalcula leitura, unidade, granularidade, dimensões e status."""
    item = estado["item"]
    operacao = estado["ultima"]
    medida = _coluna(contexto, estado["medida"])
    agrupamento = None if estado["agrupamento"] is None else _coluna(contexto, estado["agrupamento"])
    segunda = None if estado["segunda"] is None else _coluna(contexto, estado["segunda"])
    if medida is None or (estado["agrupamento"] is not None and agrupamento is None):
        raise _Falha("referencia_inexistente", operacao)
    if estado["segunda"] is not None and segunda is None:
        raise _Falha("referencia_inexistente", operacao)
    _conferir_regiao_dos_filtros(item, medida, contexto, operacao)
    grafico = estado["apresentacao"]
    unidade = _unidade(estado["operacao"], medida)
    if not _operacao_compativel(estado["operacao"], medida, segunda):
        raise _Falha("operacao_incompativel", operacao)
    if not _grafico_compativel(grafico, estado["operacao"], medida, agrupamento, segunda):
        raise _Falha("grafico_incompativel", operacao)
    try:
        granularidade, dimensoes = _forma(grafico, estado["operacao"], medida, agrupamento, segunda)
        regiao = {"arquivo": medida["arquivo"], "aba": medida["aba"], "regiao": medida["regiao"]}
        motivo = _motivo(
            contexto,
            {"grafico": grafico, "operacao": estado["operacao"], "regiao": regiao},
            medida,
            agrupamento,
            segunda,
            unidade,
            item["filtros"],
            item["relacionamentos"],
        )
    except DefinirErro as exc:
        raise _Falha(exc.codigo, operacao) from exc
    item["grafico"] = grafico
    item["leitura"] = _leitura(grafico, estado["operacao"])
    item["metrica"] = {
        "operacao": estado["operacao"],
        "coluna": _ref_de_coluna(medida),
        "unidade": unidade,
    }
    item["granularidade"] = granularidade
    item["dimensoes"] = dimensoes
    item["status"] = "bloqueada" if motivo else "definida"
    item["esclarecimento"] = motivo
    item["juncao"] = _JUNCAO


def _conferir_regiao_dos_filtros(item: dict, medida: dict, contexto: dict, operacao: str | None) -> None:
    """Compara cada filtro com a medida já final. Não cria relacionamento."""
    regiao_medida = _coord_regiao(medida)
    for filtro in item["filtros"]:
        coluna = _coluna(contexto, filtro.get("coluna") if isinstance(filtro, dict) else None)
        if coluna is None:
            raise _Falha("referencia_inexistente", operacao)
        regiao_filtro = _coord_regiao(coluna)
        if regiao_filtro == regiao_medida:
            continue
        if _relacionamento_autorizado(item, contexto, regiao_medida, regiao_filtro):
            continue
        raise _Falha("juncao_nao_confirmada", operacao)


def _relacionamento_autorizado(item: dict, contexto: dict, origem: tuple, destino: tuple) -> bool:
    relacoes = contexto.get("relacoes")
    declarados = item.get("relacionamentos")
    if not isinstance(relacoes, list) or not isinstance(declarados, list) or not declarados:
        return False
    for declarado in declarados:
        if not isinstance(declarado, dict) or declarado.get("juncao") != _JUNCAO:
            continue
        indice = declarado.get("indice")
        if isinstance(indice, bool) or not isinstance(indice, int) or not 0 <= indice < len(relacoes):
            continue
        relacao = relacoes[indice]
        if not isinstance(relacao, dict) or relacao.get("juncao") != _JUNCAO:
            continue
        lados = {_regiao_de(relacao.get("lado_unico")), _regiao_de(relacao.get("lado_associado"))}
        if origem in lados and destino in lados:
            return True
    return False


def _regiao_de(ref):
    if not isinstance(ref, dict):
        return None
    coordenadas = []
    for chave in ("arquivo", "aba", "regiao"):
        numero = ref.get(chave)
        if isinstance(numero, bool) or not isinstance(numero, int):
            return None
        coordenadas.append(numero)
    return tuple(coordenadas)


def _documento(identidade: dict, proveniencia: dict, plano: dict, definicoes: list) -> dict:
    return {
        "contrato": CONTRATO_DEFINIR_V2,
        "versao": _VERSAO,
        "catalogo": CATALOGO_CONTRATO,
        "dsl": DSL_CONTRATO,
        "execucao": EXECUCAO,
        "definicao_anterior_id": identidade["definicao_anterior_id"],
        "revisao_base": identidade["revisao_base"],
        "origem_contrato": identidade["origem_contrato"],
        "proveniencia": proveniencia,
        "plano": plano,
        "definicoes": definicoes,
    }


def _proveniencia(identidade: dict, ids: list, graficos_novos: list) -> dict:
    return {
        "definicao_anterior_id": identidade["definicao_anterior_id"],
        "revisao_base": identidade["revisao_base"],
        "origem_contrato": identidade["origem_contrato"],
        "plano_contrato": CONTRATO_PATCH_V2,
        "plano_versao": _VERSAO,
        "ids_preservados": list(ids),
        "graficos_novos": graficos_novos,
    }


def _auditar(originais, definicoes, ordenadas, temporarios, novos, vinculos) -> None:
    if [item["id"] for item in definicoes[: len(originais)]] != [item["id"] for item in originais]:
        raise _Falha("proveniencia_inconsistente")
    if [item["id"] for item in definicoes[len(originais) :]] != list(novos):
        raise _Falha("proveniencia_inconsistente")
    por_id = {item["id"]: item for item in definicoes}
    if len(por_id) != len(definicoes):
        raise _Falha("proveniencia_inconsistente")
    efeitos = {}
    for op in ordenadas:
        if op["acao"] == "criar_grafico":
            efeitos.setdefault(temporarios[op["temporario"]], {})["criar"] = op
            continue
        alvo = _resolver(op, temporarios, por_id)
        if op["acao"] == "consultar":
            continue
        efeito = efeitos.setdefault(alvo, {})
        if op["acao"] in _FILTROS_ACAO:
            efeito.setdefault("filtros", []).append(op)
            continue
        nome = {
            "alterar_indicador": "indicador",
            "alterar_agrupamento": "agrupamento",
            "alterar_apresentacao": "apresentacao",
        }[op["acao"]]
        efeito[nome] = op
    for original in originais:
        final = por_id[original["id"]]
        efeito = efeitos.get(original["id"])
        if not efeito:
            if final != original:
                raise _Falha("proveniencia_inconsistente")
            continue
        _auditar_preservado(original, final, efeito, vinculos)
    for identificador in novos:
        _auditar_novo(por_id[identificador], efeitos[identificador], vinculos)


def _auditar_preservado(original: dict, final: dict, efeito: dict, vinculos: dict) -> None:
    if final["sugestao_id"] != original["sugestao_id"] or final["origem"] != original["origem"]:
        raise _Falha("proveniencia_inconsistente")
    if final["relacionamentos"] != original["relacionamentos"] or final["juncao"] != original["juncao"]:
        raise _Falha("proveniencia_inconsistente")
    if "apresentacao" in efeito:
        if final["grafico"] != efeito["apresentacao"]["apresentacao"]:
            raise _Falha("proveniencia_inconsistente")
    elif final["grafico"] != original["grafico"]:
        raise _Falha("proveniencia_inconsistente")
    if "indicador" in efeito:
        op = efeito["indicador"]
        coluna = original["metrica"]["coluna"] if op["coluna"] is None else op["coluna"]
        if final["metrica"]["operacao"] != op["operacao"] or final["metrica"]["coluna"] != coluna:
            raise _Falha("proveniencia_inconsistente")
        esperada = op["segunda_coluna"] if op["operacao"] in _OPERACOES_SEGUNDA else None
        if _papel(final, "segunda_medida") != esperada:
            raise _Falha("proveniencia_inconsistente")
    else:
        if final["metrica"]["operacao"] != original["metrica"]["operacao"]:
            raise _Falha("proveniencia_inconsistente")
        if final["metrica"]["coluna"] != original["metrica"]["coluna"]:
            raise _Falha("proveniencia_inconsistente")
        if _papel(final, "segunda_medida") != _papel(original, "segunda_medida"):
            raise _Falha("proveniencia_inconsistente")
    if "agrupamento" in efeito:
        if _papel(final, "agrupamento") != efeito["agrupamento"]["coluna"]:
            raise _Falha("proveniencia_inconsistente")
    elif _papel(final, "agrupamento") != _papel(original, "agrupamento"):
        raise _Falha("proveniencia_inconsistente")
    if "filtros" in efeito:
        _auditar_filtros(original, final, efeito["filtros"], vinculos)
    elif final["filtros"] != original["filtros"] or final["parametros"] != original["parametros"]:
        raise _Falha("proveniencia_inconsistente")


def _auditar_novo(final: dict, efeito: dict, vinculos: dict) -> None:
    if final["sugestao_id"] is not None or final["origem"] != _ORIGEM_GRAFICO_NOVO:
        raise _Falha("proveniencia_inconsistente")
    if final["relacionamentos"] or final["juncao"] != _JUNCAO:
        raise _Falha("proveniencia_inconsistente")
    base = efeito["criar"]
    if "indicador" in efeito:
        op = efeito["indicador"]
        operacao = op["operacao"]
        coluna = base["coluna"] if op["coluna"] is None else op["coluna"]
        segunda = op["segunda_coluna"] if operacao in _OPERACOES_SEGUNDA else None
    else:
        operacao = base["operacao"]
        coluna = base["coluna"]
        segunda = base["segunda_coluna"] if operacao in _OPERACOES_SEGUNDA else None
    apresentacao = efeito["apresentacao"]["apresentacao"] if "apresentacao" in efeito else base["apresentacao"]
    if final["grafico"] != apresentacao or final["metrica"]["operacao"] != operacao or final["metrica"]["coluna"] != coluna:
        raise _Falha("proveniencia_inconsistente")
    if _papel(final, "segunda_medida") != segunda:
        raise _Falha("proveniencia_inconsistente")
    if "agrupamento" in efeito:
        if _papel(final, "agrupamento") != efeito["agrupamento"]["coluna"]:
            raise _Falha("proveniencia_inconsistente")
    elif _papel(final, "agrupamento") is not None:
        raise _Falha("proveniencia_inconsistente")
    if "filtros" in efeito:
        _auditar_filtros({"filtros": []}, final, efeito["filtros"], vinculos)
    elif final["filtros"] or final["parametros"]:
        raise _Falha("proveniencia_inconsistente")


def _auditar_filtros(original: dict, final: dict, operacoes: list, vinculos: dict) -> None:
    substituidos = {_coord(op["coluna"]) for op in operacoes}
    for filtro in original["filtros"]:
        if _coord(filtro["coluna"]) in substituidos:
            continue
        if filtro not in final["filtros"]:
            raise _Falha("proveniencia_inconsistente")
    for op in operacoes:
        nomes = _nomes_esperados(op, vinculos)
        esperado = {
            "coluna": op["coluna"],
            "operador": op["operador"],
            "parametro": nomes[0],
            "parametro_fim": nomes[1] if len(nomes) == 2 else None,
        }
        if esperado not in final["filtros"]:
            raise _Falha("proveniencia_inconsistente")


def _nomes_esperados(op: dict, vinculos: dict) -> list:
    nomes = []
    for parametro in op["parametros"]:
        if parametro["forma"] == "tipado":
            nomes.append(parametro["nome"])
            continue
        nomes.append(vinculos[parametro["referencia"]]["nome"])
    return nomes


def _papel(item: dict, papel: str):
    colunas = [dimensao["coluna"] for dimensao in item["dimensoes"] if dimensao.get("papel") == papel]
    if not colunas:
        return None
    if len(colunas) > 1:
        raise _Falha("proveniencia_inconsistente")
    return colunas[0]
