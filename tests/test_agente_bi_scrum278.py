"""SCRUM-278: comprovação de lacunas e preservação de sugestões viáveis.

Provedores falsos e datasets sintéticos. Não chama Claude, Gemini nem outra IA faturável.
"""
from __future__ import annotations

import copy
import json
from datetime import date

import pytest

from app.models import AgenteBiExecucao, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_analisar_service import (
    PROVEDOR_ANTHROPIC,
    AnalisarErro,
    analisar,
    lacunas_comprovadas,
    lacunas_refutadas,
    montar_contexto,
    validar_saida,
)
from app.services.agente_bi_colaboracao_service import revalidar_contrato_tecnico
from app.services.agente_bi_descoberta_service import (
    DescobertaErro,
    aprovar_selecao,
    diagnosticar_preparacao,
    diagnosticar_resultado,
    descobrir,
)
from app.services.agente_bi_execucao_service import _referencias
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario
from tests.test_agente_bi_colaboracao import (  # noqa: F401 - fixture autouse da colaboração falsa
    _Claude,
    _Gemini,
    _ate,
    _colaboracao_falsa,
    _critica,
    _descobrir,
    _eventos,
    _finais,
    _tecnicas,
    _tentativas,
)
from tests.test_agente_bi_descoberta_claude import _condicional, _pedido, _preparar
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx
from tests.test_agente_bi_resiliencia import PEDIDO as PEDIDO_A
from tests.test_agente_bi_resiliencia import _critica_de, _indicador
from tests.test_agente_bi_resiliencia import _preparar as _preparar_livro


def _perfil(ctx_usuario, conteudo, nome, chave):
    ingerido = _ingerir(ctx_usuario, conteudo, nome, chave=chave)
    escopo = escopo_do_usuario(ctx_usuario)
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    return escopo, ingerido["dataset"]["id"], perfil


def _contexto(perfil, pedido=None):
    return montar_contexto(perfil, pedido or {"modalidades": ["A"]})


def _regiao(coluna):
    return {"arquivo": coluna["arquivo"], "aba": coluna["aba"], "regiao": coluna["regiao"]}


def _coord(coluna):
    return {chave: coluna[chave] for chave in ("arquivo", "aba", "regiao", "indice")}


def _leitura(operacao, grafico):
    if operacao in {"contagem", "contagem_distinta"} and grafico == "indicador":
        return "contagem_de_registros"
    return {
        "indicador": "indicador_de_medida",
        "linha": "serie_temporal_de_medida",
        "barra": "medida_por_categoria",
        "pizza": "medida_por_categoria",
        "tabela": "tabela_agregada",
    }[grafico]


def _sugestao(coluna, operacao, *, grafico="indicador", agrupamento=None, status="viavel", duvidas=()):
    evidencias = [codigo for codigo in coluna.get("evidencias") or [] if isinstance(codigo, str) and codigo != "aba_oculta"][:1]
    return {
        "status": status,
        "leitura": _leitura(operacao, grafico),
        "grafico": grafico,
        "operacao": operacao,
        "origem": "dados",
        "regiao": _regiao(coluna),
        "medida": _coord(coluna),
        "agrupamento": None if agrupamento is None else _coord(agrupamento),
        "segunda_medida": None,
        "evidencias": evidencias,
        "juncao": "nao_executada",
        "lacuna": None,
        "duvidas": list(duvidas),
    }


def _aceita(perfil, pedido, sugestoes):
    proposta = {"descobertas": [], "sugestoes": copy.deepcopy(list(sugestoes))}
    try:
        analisar(perfil, pedido, provedor=lambda _contexto, proposta=proposta: proposta, proveniencia=PROVEDOR_ANTHROPIC)
    except AnalisarErro:
        return False
    return True


def _textual(perfil):
    return next(
        coluna
        for coluna in perfil["colunas"]
        if coluna.get("dominio_inconsistente") is False
        and coluna.get("presentes", 0) >= 1
        and coluna.get("distintos", 0) >= 2
        and "texto" in (coluna.get("tipos") or [])
        and coluna.get("dimensao_principal") not in {"valor", "quantidade"}
        and not ({"data", "tempo"} & set(coluna.get("dimensoes") or []))
    )


def _numerica(perfil, minimo):
    escolhida = next(
        (
            coluna
            for coluna in perfil["colunas"]
            if coluna.get("dimensao_principal") in {"valor", "quantidade"}
            and coluna.get("dominio_inconsistente") is False
            and coluna.get("presentes", 0) >= minimo
        ),
        None,
    )
    if escolhida is None:
        raise AssertionError(
            [
                {
                    "indice": coluna.get("indice"),
                    "dimensao": coluna.get("dimensao_principal"),
                    "dimensoes": coluna.get("dimensoes"),
                    "tipos": coluna.get("tipos"),
                    "presentes": coluna.get("presentes"),
                    "dominio": coluna.get("dominio_inconsistente"),
                }
                for coluna in perfil["colunas"]
            ]
        )
    return escolhida


def _temporal(perfil):
    return next(
        coluna
        for coluna in perfil["colunas"]
        if {"data", "tempo"} & set(coluna.get("dimensoes") or []) and coluna.get("dominio_inconsistente") is False
    )


def _documento(contexto, sugestoes):
    return {
        "vinculo_preparacao": contexto["vinculo_preparacao"],
        "descobertas": [],
        "sugestoes": sugestoes,
    }


def _lacuna(codigo, coluna):
    return {
        "id": "s9",
        "status": "dependente_de_novos_dados",
        "operacao": None,
        "lacuna": codigo,
        "regiao": _regiao(coluna),
    }


def _viavel_minimo():
    return {"id": "s1", "status": "viavel", "operacao": "contagem"}


def _diagnostico(perfil, codigo, *, pedido=None, com_viavel=True, contexto=None, vinculo=None):
    contexto = contexto if contexto is not None else _contexto(perfil, pedido)
    coluna = perfil["colunas"][0]
    sugestoes = [_lacuna(codigo, coluna)]
    if com_viavel:
        sugestoes.append(_viavel_minimo())
    resultado = _documento(contexto, sugestoes)
    if vinculo is not None:
        resultado["vinculo_preparacao"] = vinculo
    achados = diagnosticar_resultado(resultado, None if vinculo is False else contexto)
    return next(item for item in achados if item["codigo"] == codigo)


def _planilha_categorica():
    return _xlsx(
        {
            "rotas": [
                ["origem", "quando"],
                ["norte", date(2024, 1, 1)],
                ["sul", date(2024, 1, 2)],
                ["norte", date(2024, 1, 3)],
                ["leste", date(2024, 1, 4)],
            ]
        }
    )


def _planilha_numerica():
    return _xlsx(
        {
            "medidas": [
                ["codigo", "valor"],
                ["A", 10.5],
                ["B", 20.25],
                ["A", 10.5],
            ]
        }
    )


def test_dataset_categorico_aceita_contagem_e_contagem_distinta(ctx):
    _conta, _franquia, usuario = _par("scrum278-cat", "scrum278.cat@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, _planilha_categorica(), "rotas.xlsx", "lote-278-cat")
    assert perfil["regioes"][0]["cabecalho"]["deteccao"] == "inequivoca"
    coluna = _textual(perfil)
    pedido = {"modalidades": ["A"]}
    sugestoes = [_sugestao(coluna, "contagem"), _sugestao(coluna, "contagem_distinta")]
    assert _aceita(perfil, pedido, sugestoes)
    contexto = _contexto(perfil, pedido)
    assert "medida_numerica_ausente" not in lacunas_refutadas(contexto)
    assert "sem_registros" in lacunas_refutadas(contexto)
    saida = analisar(perfil, pedido, provedor=lambda _contexto: {"descobertas": [], "sugestoes": sugestoes})
    assert sorted(item["operacao"] for item in saida["sugestoes"] if item["status"] == "viavel") == [
        "contagem",
        "contagem_distinta",
    ]


def test_dataset_numerico_aceita_soma_e_media(ctx):
    _conta, _franquia, usuario = _par("scrum278-num", "scrum278.num@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, _planilha_numerica(), "medidas.xlsx", "lote-278-num")
    coluna = _numerica(perfil, 2)
    pedido = {"modalidades": ["A"]}
    sugestoes = [_sugestao(coluna, "soma"), _sugestao(coluna, "media")]
    assert _aceita(perfil, pedido, sugestoes)
    contexto = _contexto(perfil, pedido)
    assert "medida_numerica_ausente" in lacunas_refutadas(contexto)
    saida = analisar(perfil, pedido, provedor=lambda _contexto: {"descobertas": [], "sugestoes": sugestoes})
    assert {item["operacao"] for item in saida["sugestoes"] if item["status"] == "viavel"} >= {"soma", "media"}


def test_dataset_parcial_mantem_contagem_e_comprova_lacuna_de_soma(ctx):
    _conta, _franquia, usuario = _par("scrum278-parcial", "scrum278.parcial@test.com")
    escopo, dataset_id, perfil = _perfil(usuario, _planilha_categorica(), "parcial.xlsx", "lote-278-parcial")
    pedido = {"modalidades": ["A", "C"], "texto": "soma"}
    saida = analisar(perfil, pedido)
    operacoes = {item["operacao"] for item in saida["sugestoes"] if item["status"] == "viavel"}
    assert "contagem" in operacoes
    lacunas = [item for item in saida["sugestoes"] if item.get("lacuna") == "medida_numerica_ausente"]
    assert len(lacunas) == 1
    contexto = _contexto(perfil, pedido)
    diagnostico = next(
        item
        for item in diagnosticar_resultado(saida, contexto)
        if item["codigo"] == "medida_numerica_ausente"
    )
    fato, separador, hipotese = diagnostico["mensagem"].partition("Hipótese analítica:")
    assert separador
    assert "não há uma medida numérica" in fato
    assert "indicador" not in fato
    assert "pode depender desse dado" in hipotese
    assert diagnostico["continuidade_independente"] is True
    assert diagnostico["nivel"] == "correcao"
    assert "contagem" in {item["operacao"] for item in saida["sugestoes"] if item["status"] == "viavel"}
    assert escopo.usuario_id and dataset_id


def test_lacuna_numerica_refutada_nao_bloqueia(ctx):
    _conta, _franquia, usuario = _par("scrum278-ref-num", "scrum278.refnum@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, _planilha_numerica(), "valor.xlsx", "lote-278-ref-num")
    com_viavel = _diagnostico(perfil, "medida_numerica_ausente")
    assert com_viavel["nivel"] == "informacao"
    assert com_viavel["acao"] == "nenhuma"
    assert com_viavel["continuidade_independente"] is True
    assert "não foi comprovada" in com_viavel["mensagem"]
    assert "Fato: não há" not in com_viavel["mensagem"]
    assert "Fato: falta" not in com_viavel["mensagem"]
    sem_viavel = _diagnostico(perfil, "medida_numerica_ausente", com_viavel=False)
    assert sem_viavel["nivel"] == "informacao"
    assert sem_viavel["continuidade_independente"] is False


def test_lacuna_temporal_refutada_por_dimensao_existente(ctx):
    _conta, _franquia, usuario = _par("scrum278-tempo", "scrum278.tempo@test.com")
    planilha = _xlsx(
        {
            "folha": [
                ["quando", "classe"],
                [date(2024, 1, 1), "a"],
                [date(2024, 1, 2), "b"],
                [date(2024, 1, 3), "a"],
            ]
        }
    )
    _escopo, _dataset_id, perfil = _perfil(usuario, planilha, "tempo.xlsx", "lote-278-tempo")
    assert _temporal(perfil)
    contexto = _contexto(perfil)
    assert "dimensao_temporal_ausente" in lacunas_refutadas(contexto)
    diagnostico = _diagnostico(perfil, "dimensao_temporal_ausente", contexto=contexto)
    assert diagnostico["nivel"] == "informacao"
    assert "uma data ou período" in diagnostico["mensagem"]
    assert "não foi comprovada" in diagnostico["mensagem"]
    assert "Fato: não há" not in diagnostico["mensagem"]


def test_ausencia_comprovada_separa_fato_e_hipotese(ctx):
    _conta, _franquia, usuario = _par("scrum278-prova", "scrum278.prova@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, b"classe\nA\nB\nA\n", "classe.csv", "lote-278-prova")
    contexto = _contexto(perfil)
    assert "medida_numerica_ausente" not in lacunas_refutadas(contexto)
    comprovada = _diagnostico(perfil, "medida_numerica_ausente", contexto=contexto)
    fato, separador, hipotese = comprovada["mensagem"].partition("Hipótese analítica:")
    assert separador
    assert fato.startswith("Fato: não há uma medida numérica.")
    assert "indicador" not in fato
    assert "pode depender desse dado" in hipotese
    assert comprovada["nivel"] == "correcao"
    assert comprovada["continuidade_independente"] is True
    isolada = _diagnostico(perfil, "medida_numerica_ausente", contexto=contexto, com_viavel=False)
    assert isolada["nivel"] == "bloqueio"
    assert isolada["continuidade_independente"] is False
    assert isolada["mensagem"].startswith("Fato: não há uma medida numérica.")


def test_diagnostico_sem_contexto_nao_afirma_ausencia(ctx):
    _conta, _franquia, usuario = _par("scrum278-sem", "scrum278.sem@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, b"valor\n10\n20\n", "valor.csv", "lote-278-sem")
    contexto = _contexto(perfil)
    coluna = perfil["colunas"][0]
    resultado = _documento(contexto, [_lacuna("medida_numerica_ausente", coluna), _viavel_minimo()])
    sem_contexto = diagnosticar_resultado(resultado)[0]
    assert sem_contexto["categoria"] == "dados_adicionais"
    assert "medida numérica" in sem_contexto["mensagem"]
    assert "Fato: não há" not in sem_contexto["mensagem"]
    assert "Fato: falta" not in sem_contexto["mensagem"]
    assert "não está comprovada" in sem_contexto["mensagem"]
    assert "O perfil disponível não comprova essa ausência" in sem_contexto["mensagem"]
    assert "Hipótese analítica" in sem_contexto["mensagem"]
    divergente = diagnosticar_resultado(
        dict(resultado, vinculo_preparacao="0" * 64),
        contexto,
    )[0]
    assert "Fato: não há" not in divergente["mensagem"]
    assert "O perfil disponível mostra que esse dado existe" not in divergente["mensagem"]
    assert "O perfil disponível não comprova essa ausência" in divergente["mensagem"]
    preparacao = diagnosticar_preparacao(
        {"regioes": [{"arquivo": 0, "aba": 0, "regiao": 0, "linhas_observadas": 0}], "colunas": [], "qualidade": {}}
    )
    assert preparacao[0]["nivel"] == "bloqueio"
    assert "para produzir o indicador pedido" in preparacao[0]["mensagem"]


def test_data_ambigua_nao_comprova_lacuna_temporal(ctx):
    _conta, _franquia, usuario = _par("scrum278-amb-data", "scrum278.ambdata@test.com")
    _escopo, _dataset_id, perfil = _perfil(
        usuario,
        b"dia\n01/02/2024\n13/02/2024\n",
        "datas.csv",
        "lote-278-amb-data",
    )
    contexto = _contexto(perfil)
    assert any(item["datas_ambiguas"] > 0 for item in contexto["qualidade"])
    assert "dimensao_temporal_ausente" not in lacunas_comprovadas(contexto)
    diagnostico = _diagnostico(perfil, "dimensao_temporal_ausente", contexto=contexto)
    assert "Fato: não há" not in diagnostico["mensagem"]
    assert "O perfil disponível não comprova essa ausência" in diagnostico["mensagem"]
    assert diagnostico["nivel"] == "correcao"
    assert diagnostico["continuidade_independente"] is True
    isolada = _diagnostico(perfil, "dimensao_temporal_ausente", contexto=contexto, com_viavel=False)
    assert isolada["nivel"] == "bloqueio"
    assert isolada["continuidade_independente"] is False
    assert "Fato: não há" not in isolada["mensagem"]


def test_relacao_parcial_nao_comprova_ausencia(ctx, monkeypatch):
    _conta, _franquia, usuario = _par("scrum278-rel", "scrum278.rel@test.com")
    planilha = _xlsx(
        {
            "origem": [
                ["outro", "codigo"],
                ["X1", "K1"],
                ["X2", "K2"],
                ["X3", "K3"],
            ],
            "destino": [
                ["outro", "codigo"],
                ["Y1", "K1"],
                ["Y2", "K2"],
                ["Y3", "K3"],
            ],
        }
    )
    ingerido = _ingerir(usuario, planilha, "relacao.xlsx", chave="lote-278-rel")
    escopo = escopo_do_usuario(usuario)
    dataset_id = ingerido["dataset"]["id"]
    monkeypatch.setenv("AGENTE_BI_RELACOES_MAX_PARES", "1")
    perfil_parcial = perfilar_dataset(escopo, dataset_id)
    contexto_parcial = _contexto(perfil_parcial)
    assert contexto_parcial["orcamento_relacoes"]["analise_relacoes_parcial"] is True
    assert "relacao_ausente" not in lacunas_comprovadas(contexto_parcial)
    assert "relacao_ausente" not in lacunas_refutadas(contexto_parcial)
    diagnostico = _diagnostico(perfil_parcial, "relacao_ausente", contexto=contexto_parcial)
    assert "Fato: não há" not in diagnostico["mensagem"]
    assert "O perfil disponível não comprova essa ausência" in diagnostico["mensagem"]
    monkeypatch.delenv("AGENTE_BI_RELACOES_MAX_PARES", raising=False)
    perfil_cheio = perfilar_dataset(escopo, dataset_id)
    contexto_cheio = _contexto(perfil_cheio)
    assert "relacao_ausente" in lacunas_refutadas(contexto_cheio)


def test_numero_ambiguo_nao_comprova_medida_ausente(ctx):
    _conta, _franquia, usuario = _par("scrum278-amb-num", "scrum278.ambnum@test.com")
    _escopo, _dataset_id, perfil = _perfil(
        usuario,
        b'medida\n1.234\n"1,5"\n',
        "medida.csv",
        "lote-278-amb-num",
    )
    assert any(coluna["dominio_inconsistente"] for coluna in perfil["colunas"])
    contexto = _contexto(perfil)
    assert "medida_numerica_ausente" not in lacunas_comprovadas(contexto)
    diagnostico = _diagnostico(perfil, "medida_numerica_ausente", contexto=contexto)
    assert "Fato: não há" not in diagnostico["mensagem"]
    assert "O perfil disponível não comprova essa ausência" in diagnostico["mensagem"]


def test_regiao_unica_limpa_comprova_relacao_ausente(ctx):
    _conta, _franquia, usuario = _par("scrum278-uma", "scrum278.uma@test.com")
    _escopo, _dataset_id, perfil = _perfil(usuario, b"classe\nA\nB\nA\n", "classe.csv", "lote-278-uma")
    contexto = _contexto(perfil)
    assert len(contexto["regioes"]) == 1
    assert contexto["orcamento_relacoes"]["analise_relacoes_parcial"] is False
    assert "relacao_ausente" in lacunas_comprovadas(contexto)
    diagnostico = _diagnostico(perfil, "relacao_ausente", contexto=contexto)
    assert diagnostico["mensagem"].startswith("Fato: não há a relação entre as regiões.")
    assert diagnostico["nivel"] == "correcao"
    assert diagnostico["continuidade_independente"] is True


def test_regioes_sem_linhas_comprovam_sem_registros(ctx):
    _conta, _franquia, usuario = _par("scrum278-zero", "scrum278.zero@test.com")
    planilha = _xlsx({"origem": [["origem", "destino"]], "destino": [["codigo", "nome"]]})
    _escopo, _dataset_id, perfil = _perfil(usuario, planilha, "vazia.xlsx", "lote-278-zero")
    contexto = _contexto(perfil)
    assert contexto["regioes"]
    assert all(regiao["linhas"] == 0 for regiao in contexto["regioes"])
    assert "sem_registros" in lacunas_comprovadas(contexto)
    diagnostico = _diagnostico(perfil, "sem_registros", contexto=contexto)
    assert diagnostico["mensagem"].startswith("Fato: não há pelo menos um registro.")
    assert diagnostico["nivel"] == "correcao"
    assert diagnostico["continuidade_independente"] is True


def test_descoberta_simulada_propaga_contexto_comprovado(ctx, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_COLABORACAO_ENABLED", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
    _conta, _franquia, usuario = _par("scrum278-sim", "scrum278.sim@test.com")
    escopo, dataset_id, _perfil_local = _perfil(usuario, b"classe\nA\nB\nA\n", "classe.csv", "lote-278-sim")
    antes_tentativas = IaChamadaTentativa.query.filter_by(usuario_id=usuario.id).count()
    antes_eventos = IaConsumoEvento.query.filter_by(usuario_id=usuario.id).count()
    corpo = descobrir(
        escopo,
        {"dataset_id": dataset_id, "pedido": {"modalidades": ["A", "C"], "texto": "soma"}},
    )
    assert corpo["resultado"]["chamada_anthropic"] is False
    diagnostico = next(item for item in corpo["diagnosticos"] if item["codigo"] == "medida_numerica_ausente")
    assert diagnostico["mensagem"].startswith("Fato: não há uma medida numérica.")
    assert "Hipótese analítica" in diagnostico["mensagem"]
    assert IaChamadaTentativa.query.filter_by(usuario_id=usuario.id).count() == antes_tentativas
    assert IaConsumoEvento.query.filter_by(usuario_id=usuario.id).count() == antes_eventos


def test_revisao_que_omite_contagem_preserva_a_versao_validada(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum278.preserva@test.com")
        pedido = _pedido(perfil, ["A"])
        texto = _textual(perfil)
        numero = _numerica(perfil, 1)
        contagem = _sugestao(texto, "contagem")
        soma = _sugestao(numero, "soma")
        assert _aceita(perfil, pedido, [contagem, soma])
        claude = _Claude([{"descobertas": [], "sugestoes": [contagem, soma]}, {"descobertas": [], "sugestoes": [soma]}])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        operacoes = [item["operacao"] for item in corpo["resultado"]["sugestoes"]]
        assert operacoes.count("contagem") == 1
        assert operacoes.count("soma") == 1
        assert all(item["status"] == "viavel" for item in corpo["resultado"]["sugestoes"])
        assert validar_saida(montar_contexto(perfil, pedido), corpo["resultado"])["contrato_saida_validado"] is True
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_tentativas(usuario.id)) == len(_eventos(usuario.id)) == 3
        repetido = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert repetido["analise_id"] == corpo["analise_id"]
        assert [item["operacao"] for item in repetido["resultado"]["sugestoes"]] == operacoes
        assert claude.chamadas == 2
        assert len(_tentativas(usuario.id)) == len(_eventos(usuario.id)) == 3
        assert len(_finais(dataset_id)) == 1
        tecnica = _tecnicas(dataset_id)[0]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        documento = json.loads(tecnica.resultado_json)
        assert revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), documento) == documento


def test_revisao_reclassifica_e_prevalece_sobre_a_proposta_anterior(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum278.reclass@test.com")
        pedido = _pedido(perfil, ["A"])
        texto = _textual(perfil)
        contagem = _sugestao(texto, "contagem")
        reclassificada = _sugestao(texto, "contagem", status="condicional", duvidas=["escolher_medida"])
        assert _aceita(perfil, pedido, [contagem])
        assert _aceita(perfil, pedido, [reclassificada])
        claude = _Claude(
            [
                {"descobertas": [], "sugestoes": [contagem]},
                {"descobertas": [], "sugestoes": [reclassificada]},
            ]
        )
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert [item["status"] for item in corpo["resultado"]["sugestoes"]] == ["condicional"]
        assert corpo["resultado"]["sugestoes"][0]["operacao"] == "contagem"
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_preservacao_respeita_o_teto_de_sugestoes(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum278.teto@test.com")
        pedido = _pedido(perfil, ["A"])
        validas = _combinacoes_validas(perfil, pedido)
        assert len(validas) >= 9
        anterior = validas[:8]
        revisao = validas[1:9]
        extra = anterior[0]
        assert _combinacao(extra) not in {_combinacao(item) for item in revisao}
        claude = _Claude(
            [
                {"descobertas": [], "sugestoes": anterior},
                {"descobertas": [], "sugestoes": revisao},
            ]
        )
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        publicadas = [_combinacao(item) for item in corpo["resultado"]["sugestoes"]]
        assert len(publicadas) == 8
        assert _combinacao(extra) not in publicadas
        assert publicadas == [_combinacao(item) for item in revisao] or set(publicadas) == {
            _combinacao(item) for item in revisao
        }
        assert claude.chamadas == 2


def test_uniao_invalida_volta_somente_a_revisao(app, monkeypatch):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum278.uniao@test.com")
        pedido = _pedido(perfil, ["A"])
        texto = _textual(perfil)
        numero = _numerica(perfil, 1)
        contagem = _sugestao(texto, "contagem")
        soma = _sugestao(numero, "soma")
        claude = _Claude(
            [
                {"descobertas": [], "sugestoes": [contagem, soma]},
                {"descobertas": [], "sugestoes": [soma]},
            ]
        )
        gemini = _Gemini(json.dumps(_critica(perfil)))
        from app.services import agente_bi_colaboracao_service as servico_colaboracao
        from app.services.agente_bi_analisar_service import analisar as analisar_real

        def antes():
            def falso(perfil_chamada, pedido_chamada, *, provedor=None, proveniencia=None):
                proposta = provedor({})
                if len(proposta.get("sugestoes") or []) > 1:
                    raise AnalisarErro("contrato_saida_invalido")
                return analisar_real(
                    perfil_chamada,
                    pedido_chamada,
                    provedor=provedor,
                    proveniencia=proveniencia,
                )

            monkeypatch.setattr(servico_colaboracao, "analisar", falso)

        corpo = None
        for _ in range(6):
            corpo = _descobrir(
                escopo,
                dataset_id,
                pedido,
                claude,
                gemini,
                {"antes_de_materializar": antes},
            )
            estado = corpo.get("colaboracao") or {}
            if corpo.get("resultado") or estado.get("continuar") is not True:
                break
        else:
            raise AssertionError(corpo)
        assert [item["operacao"] for item in corpo["resultado"]["sugestoes"]] == ["soma"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_tentativas(usuario.id)) == len(_eventos(usuario.id)) == 3


def test_resposta_de_pergunta_nao_promove_condicional(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("scrum278.condicional@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        sugestao = corpo["resultado"]["sugestoes"][0]
        assert sugestao["status"] == "condicional"
        with pytest.raises(DescobertaErro) as erro:
            aprovar_selecao(
                escopo,
                {
                    "analise_id": corpo["analise_id"],
                    "sugestoes": [sugestao["id"]],
                    "perguntas": [{"id": item["id"], "confirmada": True} for item in corpo["resultado"]["perguntas"]],
                    "confirmacao": True,
                },
            )
        assert erro.value.codigo == "sugestao_nao_executavel"
        assert sugestao["status"] == "condicional"


def test_reclassificacao_semantica_e_bloqueio_estrutural(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar_livro("scrum278.estrutura@test.com")
        valida = _indicador(perfil, 0)
        bloqueada = _indicador(perfil, 1)
        condicional = _indicador(perfil, 1, status="condicional", duvidas=["resolver_formula_sem_cache"])
        with pytest.raises(AnalisarErro) as erro:
            analisar(
                perfil,
                PEDIDO_A,
                provedor=lambda _contexto: {"descobertas": [], "sugestoes": [bloqueada]},
                proveniencia=PROVEDOR_ANTHROPIC,
            )
        assert erro.value.codigo == "contrato_saida_invalido"
        claude = _Claude(
            [
                {"descobertas": [], "sugestoes": [valida, bloqueada]},
                {"descobertas": [], "sugestoes": [condicional]},
            ]
        )
        gemini = _Gemini(json.dumps(_critica_de(valida)))
        corpo = _ate(escopo, dataset_id, PEDIDO_A, claude, gemini)
        viaveis = [item for item in corpo["resultado"]["sugestoes"] if item["status"] == "viavel"]
        assert viaveis
        assert all(item["regiao"]["aba"] != 1 for item in viaveis)
        assert any(item["status"] == "condicional" and item["regiao"]["aba"] == 1 for item in corpo["resultado"]["sugestoes"])
        assert validar_saida(montar_contexto(perfil, PEDIDO_A), corpo["resultado"])["contrato_saida_validado"] is True
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def _combinacao(item):
    def coordenada(valor):
        if not isinstance(valor, dict):
            return None
        return tuple(valor.get(chave) for chave in ("arquivo", "aba", "regiao", "indice"))

    return (
        item.get("operacao"),
        item.get("grafico"),
        coordenada(item.get("medida")),
        coordenada(item.get("agrupamento")),
        coordenada(item.get("segunda_medida")),
    )


def _combinacoes_validas(perfil, pedido):
    candidatas = []
    vistas = set()
    colunas = [
        coluna
        for coluna in perfil["colunas"]
        if coluna.get("dominio_inconsistente") is False and coluna.get("presentes", 0) >= 1
    ]
    for coluna in colunas:
        temporal = bool({"data", "tempo"} & set(coluna.get("dimensoes") or []))
        numerica = coluna.get("dimensao_principal") in {"valor", "quantidade"}
        textual = "texto" in (coluna.get("tipos") or []) and not temporal and not numerica
        if textual or numerica or temporal:
            for operacao in ("contagem", "contagem_distinta"):
                candidatas.append(_sugestao(coluna, operacao))
        if numerica and coluna.get("presentes", 0) >= 1:
            for operacao in ("soma", "minimo", "maximo"):
                candidatas.append(_sugestao(coluna, operacao))
        if numerica and coluna.get("presentes", 0) >= 2:
            candidatas.append(_sugestao(coluna, "media"))
        if numerica:
            for grupo in colunas:
                if _regiao(grupo) != _regiao(coluna) or grupo.get("indice") == coluna.get("indice"):
                    continue
                grupo_temporal = bool({"data", "tempo"} & set(grupo.get("dimensoes") or []))
                grupo_numerico = grupo.get("dimensao_principal") in {"valor", "quantidade"}
                if grupo_temporal:
                    candidatas.append(_sugestao(coluna, "soma", grafico="linha", agrupamento=grupo))
                if (
                    not grupo_temporal
                    and not grupo_numerico
                    and "texto" in (grupo.get("tipos") or [])
                    and grupo.get("distintos", 0) >= 2
                ):
                    candidatas.append(_sugestao(coluna, "soma", grafico="barra", agrupamento=grupo))
                    candidatas.append(_sugestao(coluna, "soma", grafico="tabela", agrupamento=grupo))
    validas = []
    for item in candidatas:
        chave = _combinacao(item)
        if chave in vistas or not item["evidencias"]:
            continue
        if not _aceita(perfil, pedido, [item]):
            continue
        vistas.add(chave)
        validas.append(item)
        if len(validas) >= 9 and _aceita(perfil, pedido, validas[:8]) and _aceita(perfil, pedido, validas[1:9]):
            return validas
    return validas
