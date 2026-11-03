"""SCRUM-279: diagnóstico técnico dos descartes, sem dado de planilha no log.

Provedores falsos e datasets sintéticos. Não chama Claude, Gemini nem outra IA faturável.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agente_bi_analisar_service import (
    GRAFICOS,
    MOTIVO_VIABILIDADE,
    OPERACOES,
    _CODIGOS_ANALISAR,
    _ETAPAS_DESCARTE,
    _FORA_DO_CATALOGO,
    _MOTIVOS_DIAGNOSTICO,
    _STATUS,
    _grafico_cabe,
    _motivo_grafico,
    _motivo_operacao,
    _motivo_regiao,
    _operacao_cabe,
    _perguntas,
    _regiao_viavel,
    _registrar_descarte,
    _validar_sugestao,
    descrever_catalogo,
    instrucao_de_compatibilidade,
    instrucao_de_rascunho,
    logger as logger_analisar,
)
from app.services.agente_bi_analisar_service import (
    AnalisarErro,
)
from app.services.agente_bi_colaboracao_service import (
    _MOTIVOS_DESCARTE,
    _analisar_aceita,
    _codigo_analise,
    _materializar,
    _preservar_viaveis,
)
from app.services.agente_bi_persistencia_service import escopo_do_usuario
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx

ROOT = Path(__file__).resolve().parents[1]
SENTINELA = "NF-88421"
HASH_TABELA_REDUZIDA = "999cf57f98f243a867cdd7cd0c6c4a658573ce054275ce680d7b6b8f299d27c8"
BITS_TABELA_REDUZIDA = 70658
HASH_CATALOGO_INSTRUCAO = "1ce05b3a8f436eb2b4a47131d3bfc95b79324584f0fabc285cf4aa79a8bbbbc1"
_LINHA_DESCARTE = re.compile(
    r"^evento=agente_bi_descarte"
    r" etapa=(?P<etapa>[a-z_]+)"
    r" motivo=(?P<motivo>[a-z_]+)"
    r"(?: codigo=(?P<codigo>[a-z_]+))?"
    r"(?: status=(?P<status>[a-z_]+))?"
    r"(?: operacao=(?P<operacao>[a-z0-9_]+))?"
    r"(?: grafico=(?P<grafico>[a-z_]+))?"
    r"(?: quantidade=(?P<quantidade>-?[0-9]+|fora_do_catalogo))?"
    r"(?: analise_id=(?P<analise_id>-?[0-9]+|fora_do_catalogo))?"
    r"(?: pai_id=(?P<pai_id>-?[0-9]+|fora_do_catalogo))?$"
)
_OPS = [None, "contagem", "contagem_distinta", "soma", "media", "minimo", "maximo", "par_numerico", "fora"]
_GRAFICOS = [None, "barra", "linha", "pizza", "tabela", "indicador", "dispersao", "outro"]
_DOMINIOS = [False, True, 0, 1, None]
_DIMENSOES = ["valor", "quantidade", "identificador", "indefinida", None]
_PRESENTES = [0, 1, 2]
_DISTINTOS = [0, 1, 2]
_MOTIVOS_NOVOS = (
    "agrupamento_ausente",
    "agrupamento_igual_medida",
    "agrupamento_nao_temporal",
    "agrupamento_numerico",
    "agrupamento_sem_texto",
    "agrupamento_temporal",
    "cabecalho_nao_inequivoco",
    "cardinalidade_barra",
    "cardinalidade_pizza",
    "contrato_divergente",
    "dispersao_sem_par",
    "distintos_insuficientes",
    "grafico_nao_catalogado",
    "indicador_com_agrupamento",
    "medida_ausente",
    "medida_nao_numerica",
    "operacao_nao_catalogada",
    "par_com_agrupamento",
    "par_sem_dispersao",
    "par_sem_segunda",
    "presentes_insuficientes",
    "regiao_ausente",
    "relacao_sem_ancora",
    "segunda_ausente",
    "segunda_nao_numerica",
    "segunda_presente",
    "teto_perguntas",
    "agente_bi_descarte",
    "fora_do_catalogo",
)


def _forma(dominio, dimensao, presentes, distintos, indice=0):
    return {
        "dominio_inconsistente": dominio,
        "dimensao_principal": dimensao,
        "presentes": presentes,
        "distintos": distintos,
        "dimensoes": ["data"] if dimensao in {"data", None} else [dimensao],
        "evidencias": ["tipo_data"] if dimensao in {"data", None} else ["tipo_texto"],
        "tipos": ["texto"] if dimensao not in {"valor", "quantidade"} else ["numero"],
        "arquivo": 0,
        "aba": 0,
        "regiao": 0,
        "indice": indice,
    }


def _formas_reduzidas():
    formas = [None]
    for dominio in _DOMINIOS:
        for dimensao in _DIMENSOES:
            for presentes in _PRESENTES:
                for distintos in _DISTINTOS:
                    formas.append(_forma(dominio, dimensao, presentes, distintos))
    return formas


def _bit(valor, destino):
    destino.append(49 if valor else 48)


def _tabela_reduzida():
    """Mesma matriz congelada em f22d0f44b7af1c9873fe6c92bf129a9caa8cbb06."""
    formas = _formas_reduzidas()
    medidas = formas
    segundas = [None, formas[1], formas[3], formas[10], formas[40], formas[-1]]
    agrupamentos = [None, formas[1], formas[4], formas[8], formas[15], formas[30], formas[-2]]
    buf = bytearray()
    for operacao in _OPS:
        for medida in medidas:
            for segunda in segundas:
                _bit(_operacao_cabe(operacao, medida, segunda), buf)
    incompleta = {"dominio_inconsistente": True}
    for operacao in _OPS:
        for segunda in (None, incompleta, formas[1]):
            _bit(_operacao_cabe(operacao, incompleta, segunda), buf)
    for operacao in _OPS:
        if operacao == "par_numerico":
            continue
        _bit(_operacao_cabe(operacao, formas[1], incompleta), buf)
    for grafico in _GRAFICOS:
        for operacao in _OPS:
            for medida in medidas[::12]:
                for agrupamento in agrupamentos:
                    for segunda in segundas:
                        _bit(_grafico_cabe(grafico, operacao, medida, agrupamento, segunda), buf)
    ref = {"arquivo": 0, "aba": 0, "regiao": 0}
    for cabecalho in ("inequivoca", "ambigua", "ausente", "opaco", None, ""):
        for ambiguidades in (
            [],
            ["cabecalho_ambiguo"],
            ["divisao_lateral_ambigua"],
            ["cabecalho_ambiguo", "divisao_lateral_ambigua"],
            ["outra"],
        ):
            for formulas in (0, 1, -1, 2):
                regiao = {
                    "arquivo": 0,
                    "aba": 0,
                    "regiao": 0,
                    "cabecalho": cabecalho,
                    "ambiguidades": ambiguidades,
                }
                qualidade = {
                    "arquivo": 0,
                    "aba": 0,
                    "regiao": 0,
                    "formulas_sem_cache": formulas,
                }
                contexto = {"regioes": [regiao], "qualidade": [qualidade]}
                for colunas in (
                    [],
                    [None],
                    [{"dominio_inconsistente": False}],
                    [{"dominio_inconsistente": True}],
                    [{"dominio_inconsistente": 0}],
                    [{"dominio_inconsistente": None}],
                    [None, {"dominio_inconsistente": False}],
                    [{"dominio_inconsistente": True}, {}],
                ):
                    _bit(_regiao_viavel(contexto, ref, colunas), buf)
    sem_formula = {"arquivo": 0, "aba": 0, "regiao": 0}
    contexto = {
        "regioes": [
            {"arquivo": 0, "aba": 0, "regiao": 0, "cabecalho": "ambigua", "ambiguidades": []}
        ],
        "qualidade": [sem_formula],
    }
    _bit(_regiao_viavel(contexto, ref, []), buf)

    class _UmaVez(list):
        def __contains__(self, item):
            if getattr(self, "n", 0):
                raise RuntimeError("segundo acesso")
            self.n = 1
            return list.__contains__(self, item)

    contexto = {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "cabecalho": "inequivoca",
                "ambiguidades": _UmaVez(["cabecalho_ambiguo"]),
            }
        ],
        "qualidade": [{"arquivo": 0, "aba": 0, "regiao": 0, "formulas_sem_cache": 0}],
    }
    _bit(_regiao_viavel(contexto, ref, []), buf)
    contexto = {"regioes": [], "qualidade": []}
    _bit(_regiao_viavel(contexto, None, []), buf)
    return buf


def _blob_catalogo():
    return json.dumps(
        {"catalogo": descrever_catalogo(), "instrucao": instrucao_de_compatibilidade()},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _eventos(caplog):
    return [registro for registro in caplog.records if registro.getMessage().startswith("evento=agente_bi_descarte")]


def _conferir_eventos(caplog):
    assert SENTINELA not in caplog.text
    mensagens = []
    for registro in _eventos(caplog):
        assert registro.levelno == logging.INFO
        assert "%s" in registro.msg
        assert SENTINELA not in registro.msg
        assert SENTINELA not in repr(registro.args)
        mensagem = registro.getMessage()
        combinacao = _LINHA_DESCARTE.fullmatch(mensagem)
        assert combinacao, mensagem
        grupos = combinacao.groupdict()
        assert grupos["etapa"] in _ETAPAS_DESCARTE or grupos["etapa"] == _FORA_DO_CATALOGO
        assert grupos["motivo"] in _MOTIVOS_DIAGNOSTICO or grupos["motivo"] == _FORA_DO_CATALOGO
        if grupos["codigo"] is not None:
            assert grupos["codigo"] in _CODIGOS_ANALISAR or grupos["codigo"] == _FORA_DO_CATALOGO
        if grupos["status"] is not None:
            assert grupos["status"] in _STATUS or grupos["status"] == _FORA_DO_CATALOGO
        if grupos["operacao"] is not None:
            assert grupos["operacao"] in OPERACOES or grupos["operacao"] == _FORA_DO_CATALOGO
        if grupos["grafico"] is not None:
            assert grupos["grafico"] in GRAFICOS or grupos["grafico"] == _FORA_DO_CATALOGO
        mensagens.append(mensagem)
    return mensagens


def _desfecho(funcao):
    try:
        return ("ok", funcao())
    except AnalisarErro as exc:
        return ("erro", exc.codigo, exc.motivo, exc.args)


def _igual_com_e_sem_log(funcao):
    logging.disable(logging.CRITICAL)
    try:
        sem_log = _desfecho(funcao)
    finally:
        logging.disable(logging.NOTSET)
    com_log = _desfecho(funcao)
    assert sem_log == com_log
    return com_log


def _num(presentes=2, distintos=2, dominio=False, dimensao="valor", indice=0):
    return {
        "dominio_inconsistente": dominio,
        "dimensao_principal": dimensao,
        "presentes": presentes,
        "distintos": distintos,
        "dimensoes": ["data"] if dimensao == "data" else ["valor"] if dimensao in {"valor", "quantidade"} else ["identificador"],
        "evidencias": ["tipo_texto"],
        "tipos": ["texto"],
        "arquivo": 0,
        "aba": 0,
        "regiao": 0,
        "indice": indice,
        "celula": SENTINELA,
        "cabecalho": SENTINELA,
    }


def _coluna(indice=0, **extra):
    item = _num(presentes=3, distintos=3, dimensao="identificador", indice=indice)
    item.update(extra)
    return item


def _contexto(colunas, *, cabecalho="inequivoca", ambiguidades=None, formulas=0, qualidade=True):
    return {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "cabecalho": cabecalho,
                "ambiguidades": [] if ambiguidades is None else ambiguidades,
                "evidencias": ["tipo_texto"],
                "nome_aba": SENTINELA,
                "rotulos": [SENTINELA],
            }
        ],
        "colunas": colunas,
        "qualidade": []
        if not qualidade
        else [{"arquivo": 0, "aba": 0, "regiao": 0, "formulas_sem_cache": formulas}],
        "relacoes": [],
    }


def _ref(indice=None):
    base = {"arquivo": 0, "aba": 0, "regiao": 0}
    if indice is not None:
        base["indice"] = indice
    return base


def _item(operacao, grafico, *, indice=0, agrupamento=None, status="viavel", perguntas=None, leitura=None):
    if leitura is None:
        if operacao in {"contagem", "contagem_distinta"} and grafico == "indicador":
            leitura = "contagem_de_registros"
        else:
            leitura = {
                "linha": "serie_temporal_de_medida",
                "barra": "medida_por_categoria",
                "pizza": "medida_por_categoria",
                "indicador": "indicador_de_medida",
                "tabela": "tabela_agregada",
                "dispersao": "dispersao_de_medidas",
            }[grafico]
    return {
        "id": "s1",
        "status": status,
        "leitura": leitura,
        "grafico": grafico,
        "operacao": operacao,
        "origem": "dados",
        "regiao": _ref(),
        "medida": _ref(indice),
        "agrupamento": None if agrupamento is None else _ref(agrupamento),
        "segunda_medida": None,
        "evidencias": ["tipo_texto"],
        "juncao": "nao_executada",
        "perguntas": [] if perguntas is None else perguntas,
        "lacuna": None,
    }


def _regiao_minima(cabecalho="inequivoca", ambiguidades=None, formulas=0):
    return {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "cabecalho": cabecalho,
                "ambiguidades": [] if ambiguidades is None else ambiguidades,
            }
        ],
        "qualidade": [{"arquivo": 0, "aba": 0, "regiao": 0, "formulas_sem_cache": formulas}],
    }


def _contexto_perguntas():
    return {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "cabecalho": "inequivoca",
                "ambiguidades": [],
                "nome_aba": SENTINELA,
                "rotulos": [SENTINELA],
            }
        ],
        "colunas": [_coluna()],
        "qualidade": [],
        "relacoes": [],
        "modalidades": ["A"],
        "intencoes": [],
    }


def _rascunho(duvidas):
    return {
        "duvidas": list(duvidas),
        "regiao": _ref(),
        "texto": SENTINELA,
        "celula": SENTINELA,
    }


def _perfil(marca):
    from app.services.agente_bi_perfil_semantico_service import perfilar_dataset as perfilar

    _conta, _franquia, usuario = _par(f"scrum279-{marca}", f"scrum279.{marca}@test.com")
    planilha = _xlsx(
        {
            SENTINELA: [
                [SENTINELA, "valor"],
                [SENTINELA, 1],
                ["B", 2],
                [SENTINELA, 3],
            ]
        }
    )
    ingerido = _ingerir(usuario, planilha, "aba.xlsx", chave=f"lote-279-{marca}")
    escopo = escopo_do_usuario(usuario)
    perfil = perfilar(escopo, ingerido["dataset"]["id"])
    return escopo, perfil


def _textual(perfil):
    return next(
        coluna
        for coluna in perfil["colunas"]
        if coluna.get("dominio_inconsistente") is False
        and coluna.get("presentes", 0) >= 1
        and "texto" in (coluna.get("tipos") or [])
        and coluna.get("dimensao_principal") not in {"valor", "quantidade"}
    )


def _sugestao(coluna, operacao):
    from tests.test_agente_bi_scrum278 import _sugestao as montar

    return montar(coluna, operacao)


def test_tabela_verdade_reduzida_reproduz_f22d0f4():
    bits = _tabela_reduzida()
    assert len(bits) == BITS_TABELA_REDUZIDA
    assert hashlib.sha256(bits).hexdigest() == HASH_TABELA_REDUZIDA


def test_catalogo_e_instrucao_matematica_inalterados():
    assert hashlib.sha256(_blob_catalogo().encode("utf-8")).hexdigest() == HASH_CATALOGO_INSTRUCAO
    texto = instrucao_de_compatibilidade() + "\n" + instrucao_de_rascunho()
    for motivo in _MOTIVOS_NOVOS:
        assert motivo not in texto
    assert "relacao_sem_ancora" not in _MOTIVOS_DESCARTE
    assert "teto_perguntas" not in _MOTIVOS_DESCARTE
    assert "contrato_divergente" not in _MOTIVOS_DESCARTE
    assert "medida_nao_numerica" not in _MOTIVOS_DESCARTE


def test_codigos_literais_de_analisar_erro_formam_a_allowlist():
    fonte = (ROOT / "app" / "services" / "agente_bi_analisar_service.py").read_text(encoding="utf-8")
    achados = set(re.findall(r'AnalisarErro\(\s*"([a-z_]+)"', fonte))
    assert achados == set(_CODIGOS_ANALISAR)
    assert len(_CODIGOS_ANALISAR) == 13
    assert MOTIVO_VIABILIDADE in _MOTIVOS_DIAGNOSTICO


def test_helper_rejeita_sentinela_e_nao_levanta(caplog, monkeypatch):
    caplog.set_level(logging.INFO)

    def estoura(*_args, **_kwargs):
        raise RuntimeError(SENTINELA)

    monkeypatch.setattr(logger_analisar, "info", estoura)
    _registrar_descarte("perguntas", "teto_perguntas", quantidade=1)
    monkeypatch.undo()
    caplog.set_level(logging.INFO)
    _registrar_descarte(
        SENTINELA,
        SENTINELA,
        codigo=SENTINELA,
        status=SENTINELA,
        operacao=SENTINELA,
        grafico=SENTINELA,
        quantidade=True,
        analise_id=SENTINELA,
        pai_id=1.5,
    )
    mensagens = _conferir_eventos(caplog)
    assert mensagens == [
        "evento=agente_bi_descarte etapa=fora_do_catalogo motivo=fora_do_catalogo "
        "codigo=fora_do_catalogo status=fora_do_catalogo operacao=fora_do_catalogo "
        "grafico=fora_do_catalogo quantidade=fora_do_catalogo analise_id=fora_do_catalogo "
        "pai_id=fora_do_catalogo"
    ]


def test_gates_nao_registram_log(caplog):
    caplog.set_level(logging.INFO)
    assert _operacao_cabe("soma", _num(dimensao="identificador")) is False
    assert _grafico_cabe("dispersao", "soma", _num(), None, None) is False
    assert _regiao_viavel({"regioes": [], "qualidade": []}, None, []) is False
    assert _eventos(caplog) == []


@pytest.mark.parametrize(
    ("operacao", "medida", "segunda", "esperado"),
    [
        ("soma", None, None, "medida_ausente"),
        ("soma", _num(dominio=True), None, "dominio_inconsistente"),
        ("par_numerico", _num(), None, "segunda_ausente"),
        ("par_numerico", _num(dimensao="identificador"), _num(), "medida_nao_numerica"),
        ("par_numerico", _num(), _num(dimensao="identificador"), "segunda_nao_numerica"),
        ("par_numerico", _num(), _num(dominio=True), "dominio_inconsistente"),
        ("par_numerico", _num(), _num(), None),
        ("soma", _num(dimensao="identificador"), None, "medida_nao_numerica"),
        ("soma", _num(presentes=0), None, "presentes_insuficientes"),
        ("minimo", _num(presentes=1), None, None),
        ("media", _num(presentes=1), None, "presentes_insuficientes"),
        ("media", _num(presentes=2), None, None),
        ("contagem", _num(dimensao="identificador", presentes=0), None, "presentes_insuficientes"),
        ("contagem", _num(dimensao="identificador", presentes=1), None, None),
        ("contagem_distinta", _num(dimensao="identificador", presentes=1, distintos=0), None, "distintos_insuficientes"),
        ("contagem_distinta", _num(dimensao="identificador", presentes=1, distintos=1), None, None),
        ("fora", _num(), None, "operacao_nao_catalogada"),
    ],
)
def test_motivo_operacao(operacao, medida, segunda, esperado):
    assert _motivo_operacao(operacao, medida, segunda) == esperado
    assert _operacao_cabe(operacao, medida, segunda) is (esperado is None)


def test_motivo_operacao_nao_le_segunda_quando_medida_ja_falha():
    class Bomba(dict):
        def __getitem__(self, chave):
            raise AssertionError(chave)

    assert _motivo_operacao("soma", {"dominio_inconsistente": True}, Bomba()) == "dominio_inconsistente"


@pytest.mark.parametrize(
    ("grafico", "operacao", "medida", "agrupamento", "segunda", "esperado"),
    [
        ("barra", "par_numerico", _num(), None, _num(indice=1), "par_sem_dispersao"),
        ("dispersao", "par_numerico", _num(), _num(indice=1), _num(indice=2), "par_com_agrupamento"),
        ("dispersao", "par_numerico", _num(), None, None, "par_sem_segunda"),
        ("dispersao", "par_numerico", _num(), None, _num(indice=1), None),
        ("indicador", "soma", _num(), None, _num(indice=1), "segunda_presente"),
        ("dispersao", "soma", _num(), None, None, "dispersao_sem_par"),
        ("barra", "soma", _num(), _num(indice=0), None, "agrupamento_igual_medida"),
        ("indicador", "soma", _num(), _num(indice=1), None, "indicador_com_agrupamento"),
        ("indicador", "soma", _num(), None, None, None),
        ("tabela", "soma", _num(), None, None, None),
        ("barra", "soma", _num(), None, None, "agrupamento_ausente"),
        ("linha", "soma", _num(), _num(dimensao="data", indice=1), None, None),
        ("linha", "soma", _num(), _num(dimensao="identificador", indice=1), None, "agrupamento_nao_temporal"),
        ("barra", "soma", _num(), _num(dimensao="data", indice=1), None, "agrupamento_temporal"),
        ("pizza", "contagem", _num(dimensao="identificador"), _num(dimensao="valor", indice=1), None, "agrupamento_numerico"),
        (
            "barra",
            "contagem",
            _num(dimensao="identificador"),
            {
                **_forma(False, "identificador", 2, 2, 1),
                "tipos": ["data"],
                "evidencias": [],
                "dimensoes": ["identificador"],
            },
            None,
            "agrupamento_sem_texto",
        ),
        ("pizza", "contagem", _num(dimensao="identificador"), _num(dimensao="identificador", distintos=1, indice=1), None, "cardinalidade_pizza"),
        ("pizza", "contagem", _num(dimensao="identificador"), _num(dimensao="identificador", distintos=2, indice=1), None, None),
        ("barra", "contagem", _num(dimensao="identificador"), _num(dimensao="identificador", distintos=1, indice=1), None, "cardinalidade_barra"),
        ("barra", "contagem", _num(dimensao="identificador"), _num(dimensao="identificador", distintos=2, indice=1), None, None),
        ("outro", "contagem", _num(dimensao="identificador"), _num(dimensao="identificador", indice=1), None, "grafico_nao_catalogado"),
    ],
)
def test_motivo_grafico(grafico, operacao, medida, agrupamento, segunda, esperado):
    assert _motivo_grafico(grafico, operacao, medida, agrupamento, segunda) == esperado
    assert _grafico_cabe(grafico, operacao, medida, agrupamento, segunda) is (esperado is None)


@pytest.mark.parametrize(
    ("contexto", "colunas", "esperado"),
    [
        ({"regioes": [], "qualidade": []}, [], "regiao_ausente"),
        (_regiao_minima("ambigua"), [], "cabecalho_nao_inequivoco"),
        (_regiao_minima(formulas=2), [], "formula_sem_cache"),
        (_regiao_minima(ambiguidades=["cabecalho_ambiguo"]), [], "cabecalho_ambiguo"),
        (_regiao_minima(ambiguidades=["divisao_lateral_ambigua"]), [], "divisao_lateral_ambigua"),
        (_regiao_minima(), [_num(dominio=True)], "dominio_inconsistente"),
        (_regiao_minima(), [_num(dominio=0)], "dominio_inconsistente"),
        (_regiao_minima(), [None, _num()], None),
    ],
)
def test_motivo_regiao(contexto, colunas, esperado):
    assert _motivo_regiao(contexto, {"arquivo": 0, "aba": 0, "regiao": 0}, colunas) == esperado
    assert _regiao_viavel(contexto, {"arquivo": 0, "aba": 0, "regiao": 0}, colunas) is (esperado is None)


def test_validar_sugestao_registra_operacao_grafico_e_viabilidade(caplog):
    caplog.set_level(logging.INFO)
    textual = _coluna()
    assert SENTINELA in json.dumps(_contexto([textual]))

    def operacao():
        return _validar_sugestao(_item("soma", "indicador"), _contexto([textual]), [])

    def grafico():
        grupo = _coluna(1, dimensoes=["identificador"])
        return _validar_sugestao(_item("contagem", "linha", agrupamento=1), _contexto([textual, grupo]), [])

    def viabilidade():
        return _validar_sugestao(_item("contagem", "indicador"), _contexto([textual], cabecalho="ambigua"), [])

    def regiao_ausente():
        return _validar_sugestao(_item("contagem", "indicador"), _contexto([textual], qualidade=False), [])

    def contrato_anterior():
        return _validar_sugestao(_item("contagem", "indicador", perguntas=["p1"]), _contexto([textual]), [{"id": "p1"}])

    assert _igual_com_e_sem_log(operacao) == ("erro", "operacao_incompativel", None, ("operacao_incompativel",))
    assert _igual_com_e_sem_log(grafico) == ("erro", "grafico_incompativel", None, ("grafico_incompativel",))
    assert _igual_com_e_sem_log(viabilidade) == (
        "erro",
        "contrato_saida_invalido",
        MOTIVO_VIABILIDADE,
        ("contrato_saida_invalido",),
    )
    assert _igual_com_e_sem_log(regiao_ausente)[0] == "erro"
    antes = len(_eventos(caplog))
    with pytest.raises(AnalisarErro) as contrato:
        contrato_anterior()
    assert contrato.value.codigo == "contrato_saida_invalido"
    assert contrato.value.motivo == "viavel_com_pergunta"
    novos = _eventos(caplog)[antes:]
    assert len(novos) == 1
    combinacao = _LINHA_DESCARTE.fullmatch(novos[0].getMessage())
    assert combinacao, novos[0].getMessage()
    grupos = combinacao.groupdict()
    assert grupos["motivo"] == "viavel_com_pergunta"
    assert grupos["etapa"] in _ETAPAS_DESCARTE or grupos["etapa"] == _FORA_DO_CATALOGO
    assert grupos["motivo"] in _MOTIVOS_DIAGNOSTICO or grupos["motivo"] == _FORA_DO_CATALOGO
    if grupos["codigo"] is not None:
        assert grupos["codigo"] in _CODIGOS_ANALISAR or grupos["codigo"] == _FORA_DO_CATALOGO
    if grupos["status"] is not None:
        assert grupos["status"] in _STATUS or grupos["status"] == _FORA_DO_CATALOGO
    if grupos["operacao"] is not None:
        assert grupos["operacao"] in OPERACOES or grupos["operacao"] == _FORA_DO_CATALOGO
    if grupos["grafico"] is not None:
        assert grupos["grafico"] in GRAFICOS or grupos["grafico"] == _FORA_DO_CATALOGO
    mensagens = _conferir_eventos(caplog)
    assert "motivo=medida_nao_numerica" in mensagens[0]
    assert "codigo=operacao_incompativel" in mensagens[0]
    assert "etapa=validar_sugestao" in mensagens[0]
    assert "motivo=agrupamento_nao_temporal" in mensagens[1]
    assert "codigo=grafico_incompativel" in mensagens[1]
    assert "motivo=cabecalho_nao_inequivoco" in mensagens[2]
    assert "codigo=contrato_saida_invalido" in mensagens[2]
    assert "motivo=regiao_ausente" in mensagens[3]


def _estouro_de_perguntas():
    rascunhos = []
    for indice in range(13):
        item = _rascunho(["escolher_medida"])
        item["regiao"] = {"arquivo": indice, "aba": 0, "regiao": 0}
        rascunhos.append(item)
    return rascunhos


def test_perguntas_conta_relacao_e_teto(caplog):
    caplog.set_level(logging.INFO)
    contexto = _contexto_perguntas()

    def executar(rascunhos):
        copia = copy.deepcopy(rascunhos)
        saida = _perguntas(contexto, copia, lexical=False)
        return saida, copia

    relacao = _igual_com_e_sem_log(
        lambda: executar([_rascunho(["confirmar_relacao_candidata"]), _rascunho(["confirmar_relacao_candidata"])])
    )
    teto = _igual_com_e_sem_log(lambda: executar(_estouro_de_perguntas()))
    misto = _igual_com_e_sem_log(
        lambda: executar([_rascunho(["confirmar_relacao_candidata"]), *_estouro_de_perguntas()])
    )
    assert relacao[0] == teto[0] == misto[0] == "ok"
    assert relacao[1][1] == []
    assert len(teto[1][1]) == 12
    assert [item["regiao"]["arquivo"] for item in teto[1][1]] == list(range(12))
    assert len(misto[1][1]) == 12
    assert SENTINELA in json.dumps(_rascunho(["escolher_medida"]))
    mensagens = _conferir_eventos(caplog)
    assert mensagens == [
        "evento=agente_bi_descarte etapa=perguntas motivo=relacao_sem_ancora quantidade=2",
        "evento=agente_bi_descarte etapa=perguntas motivo=teto_perguntas quantidade=1",
        "evento=agente_bi_descarte etapa=perguntas motivo=relacao_sem_ancora quantidade=1",
        "evento=agente_bi_descarte etapa=perguntas motivo=teto_perguntas quantidade=1",
    ]


def test_preservar_viaveis_registra_a_uniao_reprovada(ctx, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _escopo, perfil = _perfil("preservar")
    coluna = _textual(perfil)
    boa = _sugestao(coluna, "contagem")
    ruim = _sugestao(coluna, "soma")
    perfil["colunas"][0]["rotulo"] = SENTINELA
    perfil["regioes"][0]["nome_aba"] = SENTINELA
    tecnica = SimpleNamespace(
        id=1,
        estado="concluida",
        resultado_json={
            "papel": "claude",
            "texto": SENTINELA,
            "proposta": {"descobertas": [], "sugestoes": [ruim, boa]},
        },
    )
    monkeypatch.setattr("app.services.agente_bi_colaboracao_service._tecnicas", lambda *_args, **_kwargs: [tecnica])
    monkeypatch.setattr("app.services.agente_bi_colaboracao_service._tecnica_integra", lambda *_args, **_kwargs: True)
    proposta = {
        "descobertas": [{"codigo": "regiao_com_medida", "regiao": None, "relacao": None, "texto": SENTINELA}],
        "sugestoes": [],
    }
    pedido = {"modalidades": ["A"]}
    revisao = SimpleNamespace(id=9)

    def executar():
        return _preservar_viaveis(
            SimpleNamespace(usuario_id=1, conta_id=1, franquia_id=1),
            SimpleNamespace(id=1, versao=1, impressao="imp"),
            pedido,
            perfil,
            "cb",
            revisao,
            copy.deepcopy(proposta),
        )

    resultado = _igual_com_e_sem_log(executar)
    assert resultado[0] == "ok"
    assert resultado[1]["descobertas"] == proposta["descobertas"]
    assert resultado[1]["sugestoes"] == []
    recusa = _codigo_analise(perfil, pedido, {"descobertas": [], "sugestoes": [ruim]})
    assert recusa[0] == "operacao_incompativel"
    assert recusa[1] is None
    assert _analisar_aceita(perfil, pedido, {"descobertas": [], "sugestoes": [boa]}) is True
    mensagens = _conferir_eventos(caplog)
    preservar = [item for item in mensagens if "etapa=preservar_viaveis" in item]
    assert preservar == [
        "evento=agente_bi_descarte etapa=preservar_viaveis motivo=fora_do_catalogo "
        "codigo=contrato_saida_invalido quantidade=1"
    ]


def test_materializar_registra_erro_e_contrato(ctx, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    escopo, perfil = _perfil("materializar")
    perfil["colunas"][0]["celula"] = SENTINELA
    perfil["regioes"][0]["nome_aba"] = SENTINELA
    dataset = SimpleNamespace(id=1, versao=1, impressao="imp", chave="k")
    revisao = SimpleNamespace(id=17)
    pedido = {"modalidades": ["A"]}
    proposta = {"descobertas": [], "sugestoes": [{"texto": SENTINELA, "celula": [SENTINELA]}]}
    documento = {"proposta": proposta, "hash": "abc", "texto": SENTINELA}
    encerrada = {"ok": True, "colaboracao": {"estado": "encerrada", "fase": None, "continuar": False}}

    def rejeitada():
        return _materializar(escopo, dataset, pedido, perfil, "cb", revisao, documento, {})

    assert _igual_com_e_sem_log(rejeitada) == ("ok", encerrada)
    monkeypatch.setattr(
        "app.services.agente_bi_colaboracao_service.analisar",
        lambda *_args, **_kwargs: {
            "contrato": "outro",
            "contrato_saida_validado": False,
            "texto": SENTINELA,
        },
    )

    def divergente():
        return _materializar(escopo, dataset, pedido, perfil, "cb", revisao, documento, {})

    assert _igual_com_e_sem_log(divergente) == ("ok", encerrada)
    mensagens = _conferir_eventos(caplog)
    assert mensagens == [
        "evento=agente_bi_descarte etapa=materializar motivo=fora_do_catalogo "
        "codigo=contrato_saida_invalido pai_id=17",
        "evento=agente_bi_descarte etapa=materializar motivo=contrato_divergente pai_id=17",
    ]
