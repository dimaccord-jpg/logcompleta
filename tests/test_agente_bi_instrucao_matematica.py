"""SCRUM-277: as IAs recebem as regras matemáticas que os validadores já aplicam.

Provedores falsos e planilhas sintéticas. Não chama Claude nem Gemini faturáveis.
Não altera predicado, catálogo, schema nem quantidade de chamadas.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.services.agente_bi_analisar_service import (
    OPERACAO_PAR,
    AnalisarErro,
    _NUMERICAS,
    _TEMPORAIS,
    _grafico_cabe,
    _mesma_regiao,
    _operacao_cabe,
    analisar,
    instrucao_de_compatibilidade,
    instrucao_de_rascunho,
)
from app.services.agente_bi_colaboracao_service import (
    _CHAVES_CRITICA,
    esquema_critica,
    instrucao_de_critica,
    instrucao_de_revisao,
)
from tests.test_agente_bi_analisar import _movimento, _pedido_a, _perfil
from tests.test_agente_bi_colaboracao import (
    _Claude,
    _Gemini,
    _ate,
    _critica,
    _descobrir,
    _eventos,
    _proposta_viavel,
    _tentativas,
)
from tests.test_agente_bi_compatibilidade_analitica import _base, _julgar, _por_papel, _rascunho
from tests.test_agente_bi_descoberta_claude import _condicional, _pedido, _preparar
from tests.test_agente_bi_ingestao import _par

NUMERICAS = ", ".join(sorted(_NUMERICAS))
TEMPORAIS = ", ".join(sorted(_TEMPORAIS))
SINAIS_TEMPORAIS = ", ".join(sorted({"componente_horario", "tipo_data", "tipo_hora"}))
SINAIS_NUMERICOS = ", ".join(sorted({"tipo_numero_fracionado", "tipo_numero_inteiro"}))

R_CONTAGEM = (
    "contagem aceita medida categorica, textual ou de outra dimensao "
    "quando o dominio e consistente e existe ao menos um valor presente."
)
R_DISTINTA = (
    "contagem_distinta exige ao menos um valor presente e ao menos um valor distinto, "
    "com dominio consistente, em medida categorica, textual ou de outra dimensao."
)
R_SOMA = f"soma, minimo e maximo exigem dimensao_principal em {NUMERICAS} e ao menos um valor presente."
R_MEDIA = f"media exige dimensao_principal em {NUMERICAS} e ao menos dois valores presentes."
R_PAR = (
    f"{OPERACAO_PAR} exige duas medidas com dimensao_principal em {NUMERICAS} "
    "e dominio consistente, sem minimo de presentes."
)
R_PAR_GRAFICO = f"{OPERACAO_PAR} cabe somente em dispersao, sem agrupamento e com segunda_medida."
R_DISPERSAO = f"dispersao exige {OPERACAO_PAR}."
R_SEGUNDA = f"Fora de {OPERACAO_PAR}, segunda_medida deve ser nula."
R_MESMA_COLUNA = "Medida e agrupamento nao podem referenciar a mesma coluna."
R_INDICADOR = "indicador nao admite agrupamento."
R_LINHA = f"linha exige agrupamento com dimensao em {TEMPORAIS}; evidencia temporal sem essa dimensao nao basta."
R_TEXTO = (
    "barra, pizza e tabela com agrupamento exigem o tipo texto, "
    f"sem dimensao em {TEMPORAIS}, sem evidencia em {SINAIS_TEMPORAIS}, "
    f"sem dimensao_principal em {NUMERICAS}, sem o tipo numero e sem evidencia em {SINAIS_NUMERICOS}."
)
R_TABELA_SOLTA = "tabela sem agrupamento nao exige essas restricoes textuais."
R_BARRA = "barra exige ao menos dois valores distintos no agrupamento e nao tem teto."
R_PIZZA = "pizza exige de dois a oito valores distintos no agrupamento, inclusive."
R_TABELA_LIMITE = "tabela com agrupamento textual valido nao tem limite de valores distintos."
R_EXIGE_GRUPO = "linha, barra e pizza exigem agrupamento."
R_REGIAO = (
    "Medida, agrupamento e segunda_medida, quando presentes, "
    "devem compartilhar arquivo, aba e regiao com a regiao da sugestao."
)
R_DOMINIO = "dominio_inconsistente na medida impede a operacao."
R_DOMINIO_SEGUNDA = f"dominio_inconsistente na segunda_medida impede {OPERACAO_PAR}."

REGRAS = [
    R_CONTAGEM,
    R_DISTINTA,
    R_SOMA,
    R_MEDIA,
    R_PAR,
    R_PAR_GRAFICO,
    R_DISPERSAO,
    R_SEGUNDA,
    R_MESMA_COLUNA,
    R_INDICADOR,
    R_LINHA,
    R_TEXTO,
    R_TABELA_SOLTA,
    R_BARRA,
    R_PIZZA,
    R_TABELA_LIMITE,
    R_EXIGE_GRUPO,
    R_REGIAO,
    R_DOMINIO,
    R_DOMINIO_SEGUNDA,
]


@pytest.fixture(autouse=True)
def _provedores_falsos(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("AGENTE_BI_COLABORACAO_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)
    monkeypatch.setattr("app.services.agente_bi_gemini_service.montar_cliente_gemini", proibido)


def _col(**mudancas):
    base = {
        "arquivo": 0,
        "aba": 0,
        "regiao": 0,
        "indice": 1,
        "dimensoes": ["indefinida"],
        "dimensao_principal": "indefinida",
        "evidencias": ["tipo_texto"],
        "tipos": ["texto"],
        "dominio_inconsistente": False,
        "presentes": 4,
        "distintos": 3,
    }
    base.update(mudancas)
    return base


def _num(**mudancas):
    base = _col(
        indice=2,
        dimensoes=["valor"],
        dimensao_principal="valor",
        evidencias=["tipo_numero_fracionado"],
        tipos=["numero"],
        presentes=4,
        distintos=4,
    )
    base.update(mudancas)
    return base


def _texto(**mudancas):
    base = _col(indice=3)
    base.update(mudancas)
    return base


def _quantidade(**mudancas):
    return _num(
        indice=4,
        dimensoes=["quantidade"],
        dimensao_principal="quantidade",
        evidencias=["tipo_numero_inteiro"],
        tipos=["numero"],
        **mudancas,
    )


def _zona(regiao, medida, agrupamento=None, segunda=None):
    try:
        _mesma_regiao(regiao, medida, agrupamento, segunda)
    except AnalisarErro as exc:
        return exc.codigo
    return True


def _caso(regra, esperado, funcao, argumentos, identificador):
    return pytest.param(
        regra,
        esperado,
        lambda funcao=funcao, argumentos=argumentos: funcao(*argumentos),
        id=identificador,
    )


def _grupo(**mudancas):
    return _texto(distintos=3, **mudancas)


_REGIAO = {"arquivo": 0, "aba": 0, "regiao": 0}
_DATA = _texto(dimensoes=["data"], dimensao_principal="data", evidencias=["tipo_data"], tipos=["data"])
_TEMPO = _texto(dimensoes=["tempo"], dimensao_principal="tempo", evidencias=["tipo_hora"], tipos=["hora"])

CASOS = [
    _caso(R_CONTAGEM, True, _operacao_cabe, ("contagem", _texto(presentes=1, distintos=1)), "contagem-texto"),
    _caso(
        R_CONTAGEM,
        True,
        _operacao_cabe,
        ("contagem", _col(dimensao_principal="identificador", dimensoes=["identificador"], presentes=1)),
        "contagem-identificador",
    ),
    _caso(R_CONTAGEM, True, _operacao_cabe, ("contagem", _num(presentes=1)), "contagem-valor"),
    _caso(R_CONTAGEM, True, _operacao_cabe, ("contagem", _DATA | {"presentes": 1}), "contagem-data"),
    _caso(R_CONTAGEM, False, _operacao_cabe, ("contagem", _texto(presentes=0)), "contagem-sem-presente"),
    _caso(
        R_CONTAGEM,
        False,
        _operacao_cabe,
        ("contagem", _texto(presentes=4, dominio_inconsistente=True)),
        "contagem-dominio",
    ),
    _caso(R_DISTINTA, True, _operacao_cabe, ("contagem_distinta", _texto(presentes=1, distintos=1)), "distinta-texto"),
    _caso(R_DISTINTA, True, _operacao_cabe, ("contagem_distinta", _num(presentes=1, distintos=1)), "distinta-valor"),
    _caso(
        R_DISTINTA,
        True,
        _operacao_cabe,
        ("contagem_distinta", _DATA | {"presentes": 2, "distintos": 2}),
        "distinta-data",
    ),
    _caso(
        R_DISTINTA,
        False,
        _operacao_cabe,
        ("contagem_distinta", _texto(presentes=1, distintos=0)),
        "distinta-sem-distinto",
    ),
    _caso(
        R_DISTINTA,
        False,
        _operacao_cabe,
        ("contagem_distinta", _texto(presentes=0, distintos=1)),
        "distinta-sem-presente",
    ),
    _caso(R_SOMA, True, _operacao_cabe, ("soma", _num(presentes=1)), "soma-um-presente"),
    _caso(R_SOMA, True, _operacao_cabe, ("minimo", _quantidade(presentes=1)), "minimo-quantidade"),
    _caso(R_SOMA, True, _operacao_cabe, ("maximo", _num(presentes=1)), "maximo-um-presente"),
    _caso(R_SOMA, False, _operacao_cabe, ("soma", _num(presentes=0)), "soma-zero"),
    _caso(R_SOMA, False, _operacao_cabe, ("minimo", _num(presentes=0)), "minimo-zero"),
    _caso(R_SOMA, False, _operacao_cabe, ("soma", _texto(presentes=4)), "soma-texto"),
    _caso(R_SOMA, False, _operacao_cabe, ("maximo", _texto(presentes=4)), "maximo-texto"),
    _caso(R_MEDIA, True, _operacao_cabe, ("media", _num(presentes=2)), "media-dois"),
    _caso(R_MEDIA, True, _operacao_cabe, ("media", _quantidade(presentes=2)), "media-quantidade"),
    _caso(R_MEDIA, False, _operacao_cabe, ("media", _num(presentes=1)), "media-um"),
    _caso(R_MEDIA, False, _operacao_cabe, ("media", _texto(presentes=4)), "media-texto"),
    _caso(R_PAR, True, _operacao_cabe, (OPERACAO_PAR, _num(presentes=0), _quantidade(presentes=0)), "par-sem-presentes"),
    _caso(R_PAR, True, _operacao_cabe, (OPERACAO_PAR, _num(), _quantidade()), "par-duas-numericas"),
    _caso(R_PAR, False, _operacao_cabe, (OPERACAO_PAR, _num(), _texto()), "par-segunda-texto"),
    _caso(R_PAR, False, _operacao_cabe, (OPERACAO_PAR, _num(), None), "par-sem-segunda"),
    _caso(R_PAR, False, _operacao_cabe, (OPERACAO_PAR, None, _quantidade()), "par-sem-medida"),
    _caso(
        R_PAR,
        False,
        _operacao_cabe,
        (OPERACAO_PAR, _num(), _quantidade(dominio_inconsistente=True)),
        "par-segunda-dominio",
    ),
    _caso(R_PAR_GRAFICO, True, _grafico_cabe, ("dispersao", OPERACAO_PAR, _num(), None, _quantidade()), "par-dispersao"),
    _caso(R_PAR_GRAFICO, False, _grafico_cabe, ("barra", OPERACAO_PAR, _num(), None, _quantidade()), "par-barra"),
    _caso(
        R_PAR_GRAFICO,
        False,
        _grafico_cabe,
        ("dispersao", OPERACAO_PAR, _num(), _texto(), _quantidade()),
        "par-com-grupo",
    ),
    _caso(R_PAR_GRAFICO, False, _grafico_cabe, ("dispersao", OPERACAO_PAR, _num(), None, None), "par-dispersao-sem-segunda"),
    _caso(R_DISPERSAO, True, _grafico_cabe, ("dispersao", OPERACAO_PAR, _num(), None, _quantidade()), "dispersao-par"),
    _caso(R_DISPERSAO, False, _grafico_cabe, ("dispersao", "soma", _num(), None, None), "dispersao-soma"),
    _caso(R_DISPERSAO, False, _grafico_cabe, ("dispersao", "contagem", _texto(), None, None), "dispersao-contagem"),
    _caso(R_SEGUNDA, True, _grafico_cabe, ("indicador", "soma", _num(), None, None), "segunda-nula"),
    _caso(R_SEGUNDA, False, _grafico_cabe, ("indicador", "soma", _num(), None, _quantidade()), "segunda-no-indicador"),
    _caso(R_SEGUNDA, False, _grafico_cabe, ("barra", "soma", _num(), _texto(distintos=3), _quantidade()), "segunda-na-barra"),
    _caso(
        R_MESMA_COLUNA,
        True,
        _grafico_cabe,
        ("barra", "contagem", _texto(indice=3, distintos=3), _texto(indice=5, distintos=3), None),
        "colunas-distintas",
    ),
    _caso(
        R_MESMA_COLUNA,
        False,
        _grafico_cabe,
        ("barra", "contagem", _texto(indice=3, distintos=4), _texto(indice=3, distintos=4), None),
        "mesma-coluna",
    ),
    _caso(R_INDICADOR, True, _grafico_cabe, ("indicador", "soma", _num(), None, None), "indicador-sem-grupo"),
    _caso(R_INDICADOR, False, _grafico_cabe, ("indicador", "soma", _num(), _texto(distintos=3), None), "indicador-com-grupo"),
    _caso(R_LINHA, True, _grafico_cabe, ("linha", "soma", _num(), _DATA, None), "linha-data"),
    _caso(R_LINHA, True, _grafico_cabe, ("linha", "soma", _num(), _TEMPO, None), "linha-tempo"),
    _caso(
        R_LINHA,
        True,
        _grafico_cabe,
        (
            "linha",
            "soma",
            _num(),
            _texto(dimensoes=["data"], dimensao_principal="data", tipos=["numero"], evidencias=["tipo_numero_inteiro"]),
            None,
        ),
        "linha-data-com-sinal-numerico",
    ),
    _caso(
        R_LINHA,
        False,
        _grafico_cabe,
        ("linha", "soma", _num(), _texto(dimensoes=["indefinida"], evidencias=["tipo_data"]), None),
        "linha-so-evidencia-data",
    ),
    _caso(
        R_LINHA,
        False,
        _grafico_cabe,
        ("linha", "soma", _num(), _texto(dimensoes=["indefinida"], evidencias=["tipo_hora"]), None),
        "linha-so-evidencia-hora",
    ),
    _caso(R_LINHA, False, _grafico_cabe, ("linha", "soma", _num(), _texto(), None), "linha-texto"),
    _caso(R_TEXTO, True, _grafico_cabe, ("barra", "soma", _num(), _grupo(), None), "barra-texto"),
    _caso(R_TEXTO, True, _grafico_cabe, ("pizza", "soma", _num(), _grupo(), None), "pizza-texto"),
    _caso(R_TEXTO, True, _grafico_cabe, ("tabela", "soma", _num(), _grupo(), None), "tabela-texto"),
    _caso(
        R_TEXTO,
        True,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(tipos=["texto", "numero_pendente"]), None),
        "barra-numero-pendente",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(dimensoes=["data"], dimensao_principal="data"), None),
        "barra-dimensao-data",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(dimensoes=["tempo"], dimensao_principal="tempo"), None),
        "barra-dimensao-tempo",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(evidencias=["tipo_texto", "tipo_data"]), None),
        "barra-evidencia-data",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(evidencias=["tipo_texto", "tipo_hora"]), None),
        "barra-evidencia-hora",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(evidencias=["tipo_texto", "componente_horario"]), None),
        "barra-componente-horario",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(dimensao_principal="valor", dimensoes=["valor"]), None),
        "barra-dimensao-valor",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(dimensao_principal="quantidade", dimensoes=["quantidade"]), None),
        "barra-dimensao-quantidade",
    ),
    _caso(R_TEXTO, False, _grafico_cabe, ("barra", "soma", _num(), _grupo(tipos=["texto", "numero"]), None), "barra-tipo-numero"),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(evidencias=["tipo_texto", "tipo_numero_inteiro"]), None),
        "barra-evidencia-inteiro",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(evidencias=["tipo_texto", "tipo_numero_fracionado"]), None),
        "barra-evidencia-fracionado",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("barra", "soma", _num(), _grupo(tipos=["booleano"]), None),
        "barra-sem-texto",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("pizza", "soma", _num(), _grupo(dimensoes=["data"], dimensao_principal="data"), None),
        "pizza-data",
    ),
    _caso(
        R_TEXTO,
        False,
        _grafico_cabe,
        ("tabela", "soma", _num(), _grupo(tipos=["texto", "numero"]), None),
        "tabela-numero",
    ),
    _caso(R_TABELA_SOLTA, True, _grafico_cabe, ("tabela", "soma", _num(), None, None), "tabela-sem-grupo"),
    _caso(
        R_TABELA_SOLTA,
        False,
        _grafico_cabe,
        ("tabela", "soma", _num(), _grupo(dimensao_principal="valor", dimensoes=["valor"], tipos=["numero"]), None),
        "tabela-grupo-numerico",
    ),
    _caso(R_BARRA, False, _grafico_cabe, ("barra", "soma", _num(), _texto(distintos=1), None), "barra-distintos-1"),
    _caso(R_BARRA, True, _grafico_cabe, ("barra", "soma", _num(), _texto(distintos=2), None), "barra-distintos-2"),
    _caso(R_BARRA, True, _grafico_cabe, ("barra", "soma", _num(), _texto(distintos=8), None), "barra-distintos-8"),
    _caso(R_BARRA, True, _grafico_cabe, ("barra", "soma", _num(), _texto(distintos=9), None), "barra-distintos-9"),
    _caso(R_PIZZA, False, _grafico_cabe, ("pizza", "soma", _num(), _texto(distintos=1), None), "pizza-distintos-1"),
    _caso(R_PIZZA, True, _grafico_cabe, ("pizza", "soma", _num(), _texto(distintos=2), None), "pizza-distintos-2"),
    _caso(R_PIZZA, True, _grafico_cabe, ("pizza", "soma", _num(), _texto(distintos=8), None), "pizza-distintos-8"),
    _caso(R_PIZZA, False, _grafico_cabe, ("pizza", "soma", _num(), _texto(distintos=9), None), "pizza-distintos-9"),
    _caso(R_TABELA_LIMITE, True, _grafico_cabe, ("tabela", "contagem", _texto(indice=1), _texto(distintos=1), None), "tabela-distintos-1"),
    _caso(R_TABELA_LIMITE, True, _grafico_cabe, ("tabela", "contagem", _texto(indice=1), _texto(distintos=9), None), "tabela-distintos-9"),
    _caso(
        R_TABELA_LIMITE,
        False,
        _grafico_cabe,
        ("tabela", "contagem", _texto(indice=1), _grupo(dimensoes=["data"], dimensao_principal="data"), None),
        "tabela-data-nao-e-textual-valida",
    ),
    _caso(R_EXIGE_GRUPO, True, _grafico_cabe, ("linha", "soma", _num(), _DATA, None), "linha-com-grupo"),
    _caso(R_EXIGE_GRUPO, True, _grafico_cabe, ("barra", "soma", _num(), _grupo(), None), "barra-com-grupo"),
    _caso(R_EXIGE_GRUPO, True, _grafico_cabe, ("pizza", "soma", _num(), _grupo(), None), "pizza-com-grupo"),
    _caso(R_EXIGE_GRUPO, False, _grafico_cabe, ("linha", "soma", _num(), None, None), "linha-sem-grupo"),
    _caso(R_EXIGE_GRUPO, False, _grafico_cabe, ("barra", "soma", _num(), None, None), "barra-sem-grupo"),
    _caso(R_EXIGE_GRUPO, False, _grafico_cabe, ("pizza", "soma", _num(), None, None), "pizza-sem-grupo"),
    _caso(R_REGIAO, True, _zona, (_REGIAO, _num(), _texto(), _quantidade()), "regiao-comum"),
    _caso(R_REGIAO, True, _zona, (_REGIAO, _num(), None, None), "regiao-sem-opcionais"),
    _caso(R_REGIAO, "juncao_nao_confirmada", _zona, (_REGIAO, _num(), _texto(aba=1), None), "grupo-outra-aba"),
    _caso(R_REGIAO, "juncao_nao_confirmada", _zona, (_REGIAO, _num(), None, _quantidade(regiao=1)), "segunda-outra-regiao"),
    _caso(R_REGIAO, "juncao_nao_confirmada", _zona, (_REGIAO, _num(arquivo=1), None, None), "medida-outro-arquivo"),
    _caso(R_DOMINIO, True, _operacao_cabe, ("soma", _num(dominio_inconsistente=False)), "dominio-consistente"),
    _caso(R_DOMINIO, False, _operacao_cabe, ("soma", _num(dominio_inconsistente=True)), "soma-dominio"),
    _caso(R_DOMINIO, False, _operacao_cabe, ("contagem", _texto(dominio_inconsistente=True)), "contagem-dominio-medida"),
    _caso(R_DOMINIO, False, _operacao_cabe, (OPERACAO_PAR, _num(dominio_inconsistente=True), _quantidade()), "par-medida-dominio"),
    _caso(R_DOMINIO_SEGUNDA, True, _operacao_cabe, (OPERACAO_PAR, _num(), _quantidade(dominio_inconsistente=False)), "segunda-consistente"),
    _caso(
        R_DOMINIO_SEGUNDA,
        True,
        _operacao_cabe,
        ("soma", _num(), _quantidade(dominio_inconsistente=True)),
        "soma-ignora-dominio-da-segunda",
    ),
    _caso(
        R_DOMINIO_SEGUNDA,
        False,
        _operacao_cabe,
        (OPERACAO_PAR, _num(), _quantidade(dominio_inconsistente=True)),
        "par-segunda-inconsistente",
    ),
]


def _com_presentes(perfil, coluna, presentes):
    copia = copy.deepcopy(perfil)
    for item in copia["colunas"]:
        if (item["arquivo"], item["aba"], item["regiao"], item["indice"]) == (
            coluna["arquivo"],
            coluna["aba"],
            coluna["regiao"],
            coluna["indice"],
        ):
            item["presentes"] = presentes
            return copia
    raise AssertionError("coluna ausente")


def _codigo(perfil, rascunho):
    codigo, saida = _julgar(perfil, rascunho)
    return codigo, saida


def test_claude_recebe_o_texto_matematico_completo_no_system(app):
    matematica = instrucao_de_compatibilidade()
    rascunho = instrucao_de_rascunho()
    assert rascunho.endswith(matematica)
    assert rascunho.count(matematica) == 1
    assert "Analise somente a estrutura recebida na mensagem." in rascunho
    assert "Responda apenas com JSON de duas chaves, descobertas e sugestoes, sem texto ao redor e sem markdown." in rascunho
    assert "viavel exige duvidas vazias, operacao, grafico, medida e evidencias, com lacuna nula." in rascunho
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum277.claude@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini("{}")
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["continuar"] is True
        assert claude.pedidos[0]["system"] == rascunho
        assert matematica in claude.pedidos[0]["system"]


def test_gemini_recebe_exatamente_o_mesmo_texto_matematico(app):
    matematica = instrucao_de_compatibilidade()
    critica = instrucao_de_critica()
    assert critica.endswith(matematica)
    assert critica.count(matematica) == 1
    assert "Voce e o revisor critico da proposta recebida." in critica
    assert "As chaves sao exatamente referencias, medidas, operacoes, evidencias e objecoes." in critica
    assert set(esquema_critica()["required"]) == _CHAVES_CRITICA
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum277.gemini@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        system = claude.pedidos[0]["system"]
        instrucao = gemini.geracoes[0]["config"]["system_instruction"]
        assert instrucao == critica
        assert system[system.index(matematica) : system.index(matematica) + len(matematica)] == matematica
        assert instrucao[instrucao.index(matematica) : instrucao.index(matematica) + len(matematica)] == matematica
        assert "response_schema" not in gemini.geracoes[0]["config"]
        assert gemini.geracoes[0]["config"]["response_json_schema"] == esquema_critica()


def test_revisao_herda_o_texto_matematico_sem_duplicar(app):
    matematica = instrucao_de_compatibilidade()
    assert matematica not in instrucao_de_revisao()
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum277.revisao@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.pedidos[1]["system"] == instrucao_de_rascunho() + "\n" + instrucao_de_revisao()
        assert claude.pedidos[1]["system"].count(matematica) == 1
        assert instrucao_de_revisao() in claude.pedidos[1]["system"]


def test_contagens_categoricas_e_agrupadas_sao_aceitas(ctx):
    _usuario, perfil = _base("scrum277.contagem@test.com")
    colunas = _por_papel(perfil)
    categoria = colunas["categoria"]
    ampla = colunas["ampla"]
    assert "texto" in categoria["tipos"]
    assert "texto" in ampla["tipos"]
    assert categoria["presentes"] >= 1
    assert categoria["distintos"] >= 1
    assert ampla["distintos"] >= 2
    assert categoria["dominio_inconsistente"] is False
    assert categoria["indice"] != ampla["indice"]
    for operacao in ("contagem", "contagem_distinta"):
        codigo, saida = _codigo(perfil, _rascunho("indicador", operacao, categoria))
        assert codigo is None, operacao
        assert saida["sugestoes"][0]["operacao"] == operacao
        assert saida["sugestoes"][0]["medida"]["indice"] == categoria["indice"]
        assert saida["sugestoes"][0]["agrupamento"] is None
        assert saida["sugestoes"][0]["segunda_medida"] is None
    codigo, saida = _codigo(perfil, _rascunho("barra", "contagem", categoria, ampla))
    assert codigo is None
    assert saida["sugestoes"][0]["operacao"] == "contagem"
    assert saida["sugestoes"][0]["agrupamento"]["indice"] == ampla["indice"]
    codigo, saida = _codigo(perfil, _rascunho("tabela", "contagem_distinta", ampla, categoria))
    assert codigo is None
    assert saida["sugestoes"][0]["operacao"] == "contagem_distinta"
    assert saida["sugestoes"][0]["agrupamento"]["indice"] == categoria["indice"]


def test_rejeicoes_matematicas_permanecem(ctx):
    _usuario, perfil = _base("scrum277.rejeicao@test.com")
    colunas = _por_papel(perfil)
    medida = colunas["medida"]
    categoria = colunas["categoria"]
    quando = colunas["quando"]
    quantidade = colunas["quantidade"]
    unico = colunas["unico"]
    ampla = colunas["ampla"]
    assert medida["presentes"] >= 2
    assert medida["dimensao_principal"] == "valor"
    assert "texto" in categoria["tipos"]
    assert "data" in quando["dimensoes"]
    assert quantidade["dimensao_principal"] == "quantidade"
    assert unico["distintos"] == 1
    assert ampla["distintos"] == 9
    assert 2 <= categoria["distintos"] <= 8
    casos = [
        ("soma-textual", perfil, _rascunho("indicador", "soma", categoria), "operacao_incompativel"),
        ("media-um-presente", _com_presentes(perfil, medida, 1), _rascunho("indicador", "media", medida), "operacao_incompativel"),
        ("par-incompativel", perfil, _rascunho("dispersao", "par_numerico", medida, segunda=categoria), "operacao_incompativel"),
        ("barra-data", perfil, _rascunho("barra", "soma", medida, quando), "grafico_incompativel"),
        ("barra-numero", perfil, _rascunho("barra", "soma", medida, quantidade), "grafico_incompativel"),
        ("pizza-um", perfil, _rascunho("pizza", "soma", medida, unico), "grafico_incompativel"),
        ("pizza-nove", perfil, _rascunho("pizza", "soma", medida, ampla), "grafico_incompativel"),
        ("linha-sem-temporal", perfil, _rascunho("linha", "soma", medida, categoria), "grafico_incompativel"),
        ("indicador-com-grupo", perfil, _rascunho("indicador", "soma", medida, categoria), "grafico_incompativel"),
        ("dispersao-sem-par", perfil, _rascunho("dispersao", "soma", medida), "grafico_incompativel"),
        ("segunda-indevida", perfil, _rascunho("indicador", "soma", medida, segunda=quantidade), "grafico_incompativel"),
        ("medida-igual-agrupamento", perfil, _rascunho("barra", "contagem", categoria, categoria), "grafico_incompativel"),
    ]
    falhas = []
    for nome, fonte, rascunho, esperado in casos:
        codigo, saida = _codigo(fonte, rascunho)
        if codigo != esperado or saida is not None:
            falhas.append((nome, codigo, esperado))
    assert falhas == []

    _conta, _franquia, usuario = _par("scrum277.regioes", "scrum277.regioes@test.com")
    _resultado, duas = _perfil(usuario, _movimento(), "regioes.xlsx", "scrum277.regioes")
    por_regiao = {}
    for coluna in duas["colunas"]:
        por_regiao.setdefault((coluna["arquivo"], coluna["aba"], coluna["regiao"]), []).append(coluna)
    assert len(por_regiao) >= 2
    origem, destino = list(por_regiao.values())[:2]
    codigo, saida = _codigo(duas, _rascunho("tabela", "contagem", origem[0], destino[0]))
    assert codigo == "juncao_nao_confirmada"
    assert saida is None


def test_colaboracao_preserva_chamadas_e_identidade_da_operacao(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum277.chamadas@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = _proposta_viavel(perfil, pedido)
        operacao = proposta["sugestoes"][0]["operacao"]
        assert operacao in {"soma", "contagem"}
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        publicada = corpo["resultado"]["sugestoes"][0]
        assert len(corpo["resultado"]["sugestoes"]) == 1
        for campo in ("operacao", "grafico", "medida", "agrupamento", "segunda_medida"):
            assert publicada[campo] == proposta["sugestoes"][0][campo]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_eventos(usuario.id)) == len(_tentativas(usuario.id)) == 3
        repetido = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert repetido["analise_id"] == corpo["analise_id"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_tentativas(usuario.id)) == 3


def test_provedor_vazio_nao_acrescenta_sugestao(ctx):
    _usuario, perfil = _base("scrum277.vazio@test.com")
    pedido = _pedido_a()
    chamadas = {"n": 0}

    def provedor(contexto):
        chamadas["n"] += 1
        assert contexto["valores_de_celula"] is False
        return {"descobertas": [], "sugestoes": []}

    saida = analisar(perfil, pedido, provedor=provedor)
    assert chamadas["n"] == 1
    assert saida["sugestoes"] == []
    assert saida["descobertas"] == []
    assert saida["provedor"] == "simulado"
    assert saida["chamada_anthropic"] is False
    simulado = analisar(perfil, pedido)
    assert simulado["sugestoes"]


def test_instrucao_matematica_permanece_estatica_e_fechada():
    texto = instrucao_de_compatibilidade()
    assert texto == instrucao_de_compatibilidade()
    assert texto.splitlines() == REGRAS
    for termo in ("count_if", "participacao", "razao", "OPERGEL", "regex", "NF-88421", "norte", "K200"):
        assert termo not in texto
    positivas = {caso.values[0] for caso in CASOS if caso.values[1] is True}
    negativas = {caso.values[0] for caso in CASOS if caso.values[1] is not True}
    assert set(REGRAS) == positivas == negativas


@pytest.mark.parametrize("regra,esperado,avaliar", CASOS)
def test_regra_descrita_equivale_ao_predicado(regra, esperado, avaliar):
    assert regra in instrucao_de_compatibilidade().splitlines()
    assert avaliar() == esperado
