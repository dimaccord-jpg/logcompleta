"""SCRUM-144: resiliência analítica do AgenteBI (D1–D7).

Provedores simulados, sem SDK e sem chamada faturável. O livro sintético
segue o formato OP.KIP'S: disponibilidade diária com linhas pré-preenchidas
por fórmula, KPIs com fórmula sem cache nos registros e cadastro sem título
claro.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiExecucao
from app.services import agente_bi_colaboracao_service as colaboracao
from app.services.agente_bi_analisar_service import (
    JUNCOES_EXECUTAVEIS,
    MOTIVO_VIABILIDADE,
    AnalisarErro,
    _formulas_bloqueantes,
    analisar,
    instrucao_de_rascunho,
    montar_contexto,
    validar_saida,
)
from app.services.agente_bi_anthropic_service import ErroTransporte
from app.services.agente_bi_calculo_service import _impedimento
from app.services.agente_bi_colaboracao_service import (
    _CHECKPOINT,
    _MOTIVOS_DESCARTE,
    _parcial,
    esquema_critica,
    instrucao_de_critica,
    instrucao_de_revisao,
    revalidar_contrato_tecnico,
)
from app.services.agente_bi_descoberta_service import (
    achados_estruturais,
    diagnosticar_perfil,
    diagnosticar_preparacao,
    diagnosticar_resultado,
    diagnostico_de_cobertura,
    resumir_preparacao,
)
from app.services.agente_bi_execucao_service import ExecucaoErro, _referencias
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from tests.test_agente_bi_colaboracao import (  # noqa: F401 - fixture autouse da colaboração falsa
    _Claude,
    _Gemini,
    _colaboracao_falsa,
    _conteudo_claude,
    _descobrir,
    _finais,
    _tecnicas,
)
from tests.test_agente_bi_execucao import _seed_financeiro
from tests.test_agente_bi_ingestao import _ingerir, _xlsx

PEDIDO = {"modalidades": ["A"]}


def _opkips():
    return _xlsx(
        {
            "Disponibilidade_Diaria": [
                ["data", "motorista", "turno", "horas_disponiveis"],
                [date(2026, 10, 1), "Ana", "manha", 8.5],
                [date(2026, 10, 1), "Bruno", "tarde", 6.25],
                [date(2026, 10, 2), "Ana", "manha", 7.5],
                [date(2026, 10, 2), "Bruno", "tarde", None],
                [None, None, None, "=D2*0"],
                [None, None, None, "=D3*0"],
            ],
            "KPIs_Motoristas": [
                ["motorista", "km_rodados", "pontuacao", "indice"],
                ["Ana", 120.5, 8.5, "=B2/2"],
                ["Bruno", 98.25, "=C2*1", "=B3/2"],
                ["Carla", 120.5, 7.25, "=B4/2"],
            ],
            "Cadastro_Motoristas": [["motorista", "base"], ["Ana", "Norte"], ["Bruno", "Sul"], ["Carla", "Norte"]],
        }
    )


def _preparar(email):
    usuario, franquia, escopo, _analise = _seed_financeiro(email=email)
    ingerido = _ingerir(usuario, _opkips(), "opkips.xlsx", chave=f"lote-{email.split('@')[0]}")
    dataset_id = ingerido["dataset"]["id"]
    return escopo, dataset_id, perfilar_dataset(escopo, dataset_id), ingerido


def _regiao_de(perfil, aba):
    item = next(regiao for regiao in perfil["regioes"] if regiao["aba"] == aba)
    return {"arquivo": item["arquivo"], "aba": item["aba"], "regiao": item["regiao"]}


def _coord(coluna):
    return {chave: coluna[chave] for chave in ("arquivo", "aba", "regiao", "indice")}


def _medida(perfil, aba):
    return next(
        coluna
        for coluna in perfil["colunas"]
        if coluna["aba"] == aba
        and coluna["dimensao_principal"] in {"valor", "quantidade"}
        and coluna["dominio_inconsistente"] is False
        and coluna["presentes"] >= 1
    )


def _indicador(perfil, aba, *, status="viavel", duvidas=()):
    coluna = _medida(perfil, aba)
    return {
        "status": status,
        "leitura": "indicador_de_medida",
        "grafico": "indicador",
        "operacao": "soma",
        "origem": "dados",
        "regiao": _regiao_de(perfil, aba),
        "medida": _coord(coluna),
        "agrupamento": None,
        "segunda_medida": None,
        "evidencias": [codigo for codigo in coluna["evidencias"] if codigo.startswith("tipo_numero")][:1],
        "juncao": "nao_executada",
        "lacuna": None,
        "duvidas": list(duvidas),
    }


def _proposta(*sugestoes):
    return {"descobertas": [], "sugestoes": list(sugestoes)}


def _critica_de(item):
    return {
        "referencias": [item["regiao"]],
        "medidas": [item["medida"]],
        "operacoes": [item["operacao"]],
        "evidencias": [],
        "objecoes": [{"codigo": "evidencia_ausente", "referencia": item["medida"]}],
    }


def _documento(analise):
    return json.loads(analise.resultado_json)


# --- D4: fórmula fora dos registros elegíveis ---------------------------------


def test_formula_de_linha_pre_preenchida_nao_bloqueia_e_a_de_registro_bloqueia(app):
    with app.app_context():
        _escopo, _dataset_id, perfil, ingerido = _preparar("res-formula@test.com")
        qualidade = {item["aba"]: item for item in perfil["qualidade"]["por_regiao"]}
        assert qualidade[0]["formulas_sem_cache"]["observadas"] == 2
        assert qualidade[0]["formulas_sem_cache"]["bloqueantes"] == 0
        assert qualidade[0]["linhas_pre_preenchidas"] == 2
        assert qualidade[1]["formulas_sem_cache"]["bloqueantes"] == qualidade[1]["formulas_sem_cache"]["observadas"] > 0
        assert perfil["qualidade"]["totais"]["formulas_sem_cache"] == 2 + qualidade[1]["formulas_sem_cache"]["observadas"]
        contexto = {item["aba"]: item for item in montar_contexto(perfil, PEDIDO)["qualidade"]}
        assert contexto[0]["formulas_sem_cache"] == 0
        assert contexto[1]["formulas_sem_cache"] > 0

        valida = _indicador(perfil, 0)
        saida = analisar(perfil, PEDIDO, provedor=lambda _contexto: _proposta(valida))
        assert [item["status"] for item in saida["sugestoes"]] == ["viavel"]
        with pytest.raises(AnalisarErro) as erro:
            analisar(perfil, PEDIDO, provedor=lambda _contexto: _proposta(_indicador(perfil, 1)))
        assert erro.value.codigo == "contrato_saida_invalido"
        assert erro.value.motivo == MOTIVO_VIABILIDADE

        regioes = [item.get("local", {}).get("aba") for item in diagnosticar_perfil(perfil) if item["codigo"] == "formula_sem_cache"]
        assert regioes == [1]
        assert all(
            coluna.get("registro") is False
            for regiao in ingerido["preparacao"]["estrutura"]["regioes"]
            if regiao["aba"] == 0
            for coluna in regiao["formulas_observadas"]
        )


def test_perfil_historico_sem_bloqueantes_mantem_a_contagem_antiga():
    assert _formulas_bloqueantes({"observadas": 2, "publicadas": 2, "divergencia": False}) == 2
    assert _formulas_bloqueantes({"observadas": 2, "bloqueantes": 0}) == 0
    ref = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1}
    medida = {**ref, "dominio_inconsistente": False}
    definicao = {"metrica": {"coluna": ref, "operacao": "soma"}}

    def perfil(formulas):
        return {
            "regioes": [{"arquivo": 0, "aba": 0, "regiao": 0, "cabecalho": {"deteccao": "inequivoca"}, "ambiguidades_estruturais": []}],
            "qualidade": {"por_regiao": [{"arquivo": 0, "aba": 0, "regiao": 0, "cabecalho_ambiguo": False, "formulas_sem_cache": formulas}]},
        }

    assert _impedimento(definicao, perfil({"observadas": 2}), medida) == "resolver_formula_sem_cache"
    assert _impedimento(definicao, perfil({"observadas": 2, "bloqueantes": 1}), medida) == "resolver_formula_sem_cache"
    assert _impedimento(definicao, perfil({"observadas": 2, "bloqueantes": 0}), medida) != "resolver_formula_sem_cache"


# --- D5: instrução de viabilidade ------------------------------------------------


def test_instrucao_explica_a_viabilidade_que_o_validador_exige():
    instrucao = instrucao_de_rascunho()
    for trecho in (
        "cabecalho inequivoca",
        "divisao_lateral_ambigua",
        "formulas_sem_cache igual a zero",
        "dominio_inconsistente falso",
        "Valores ausentes nao impedem viavel",
        "use condicional com a mesma operacao",
        "resolver_formula_sem_cache",
    ):
        assert trecho in instrucao
    assert MOTIVO_VIABILIDADE in instrucao_de_revisao()


def test_condicional_com_operacao_e_aceita_pelo_validador_na_regiao_bloqueada(app):
    with app.app_context():
        _escopo, _dataset_id, perfil, _ingerido = _preparar("res-condicional@test.com")
        condicional = _indicador(perfil, 1, status="condicional", duvidas=["resolver_formula_sem_cache"])
        saida = analisar(perfil, PEDIDO, provedor=lambda _contexto: _proposta(condicional))
        assert saida["sugestoes"][0]["status"] == "condicional"
        assert saida["sugestoes"][0]["operacao"] == "soma"
        assert [item["codigo"] for item in saida["perguntas"] if item["id"] in saida["sugestoes"][0]["perguntas"]] == [
            "resolver_formula_sem_cache"
        ]


# --- D6 e D7: inconsistência recuperável e preservação parcial ---------------------


def test_motivo_de_viabilidade_e_interno_e_nao_amplia_o_checkpoint():
    assert MOTIVO_VIABILIDADE in _MOTIVOS_DESCARTE
    assert MOTIVO_VIABILIDADE not in _CHECKPOINT
    enum = esquema_critica()["properties"]["objecoes"]["items"]["properties"]["codigo"]["enum"]
    assert enum == sorted(_CHECKPOINT)


def test_sugestao_valida_e_preservada_e_a_invalida_vira_descarte_rastreavel(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-parcial@test.com")
        valida = _indicador(perfil, 0)
        invalida = _indicador(perfil, 1)
        claude = _Claude([_proposta(valida, invalida), _proposta(valida, invalida)])
        gemini = _Gemini(json.dumps(_critica_de(valida)))

        primeira = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        tecnica = _tecnicas(dataset_id)[0]
        documento = _documento(tecnica)
        descarte = {"lista": "sugestoes", "posicao": 1, "campo": "status", "motivo": MOTIVO_VIABILIDADE}
        assert documento["descartes"] == [descarte]
        assert documento["verificacao"] == {"aprovado": False, "codigo": None}
        assert documento["proposta"]["sugestoes"] == [{chave: valida[chave] for chave in valida}]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        assert revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), documento) == documento

        segunda = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"
        enviado = json.loads(gemini.geracoes[0]["contents"])
        assert enviado["descartes"] == [descarte]
        assert enviado["proposta"]["sugestoes"] == documento["proposta"]["sugestoes"]

        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert _conteudo_claude(claude.pedidos[1])["verificacao_anterior"]["descartes"] == [descarte]
        assert corpo["colaboracao"]["estado"] == "concluida"
        assert corpo["colaboracao"]["cobertura"] == "parcial"
        assert corpo["colaboracao"]["descartes"] == [descarte]
        resultado = corpo["resultado"]
        assert validar_saida(montar_contexto(perfil, PEDIDO), resultado)["contrato_saida_validado"] is True
        assert [(item["status"], item["regiao"]) for item in resultado["sugestoes"]] == [("viavel", valida["regiao"])]
        aviso = corpo["diagnosticos"][0]
        assert aviso["codigo"] == "cobertura_parcial"
        assert aviso["nivel"] == "informacao"
        assert "cobre só parte da proposta" in aviso["mensagem"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        finais = _finais(dataset_id)
        assert len(finais) == 1
        contexto_final = json.loads(finais[0].contexto_json)
        origem = _tecnicas(dataset_id)[-1]
        assert contexto_final["pai_id"] == origem.id
        assert contexto_final["pai_hash"] == _documento(origem)["hash"]

        repetido = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert repetido["analise_id"] == corpo["analise_id"]
        assert repetido["colaboracao"]["cobertura"] == "parcial"
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_finais(dataset_id)) == 1


def test_sugestao_condicional_recuperavel_consolida_cobertura_integral(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-integral@test.com")
        valida = _indicador(perfil, 0)
        revisada = _indicador(perfil, 1, status="condicional", duvidas=["resolver_formula_sem_cache"])
        claude = _Claude([_proposta(valida, _indicador(perfil, 1)), _proposta(valida, revisada)])
        gemini = _Gemini(json.dumps(_critica_de(valida)))
        for _ in range(2):
            _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"] == {"estado": "concluida", "fase": "pronto", "continuar": False}
        assert sorted(item["status"] for item in corpo["resultado"]["sugestoes"]) == ["condicional", "viavel"]
        assert all(item["codigo"] != "cobertura_parcial" for item in corpo["diagnosticos"])
        assert validar_saida(montar_contexto(perfil, PEDIDO), corpo["resultado"])["contrato_saida_validado"] is True
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_todas_as_sugestoes_invalidas_encerram_sem_publicar(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-todas@test.com")
        invalida = _indicador(perfil, 1)
        claude = _Claude([_proposta(invalida), _proposta(invalida)])
        gemini = _Gemini(json.dumps(_critica_de(invalida)))
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        documento = _documento(_tecnicas(dataset_id)[0])
        assert documento["proposta"] == {"descobertas": [], "sugestoes": []}
        assert documento["descartes"][0]["motivo"] == MOTIVO_VIABILIDADE
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "encerrada"
        assert corpo.get("resultado") is None
        assert _finais(dataset_id) == []
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        descartes = _documento(_tecnicas(dataset_id)[-1])["descartes"]
        assert corpo["colaboracao"]["descartes"] == descartes
        assert set(descartes[0]) == {"lista", "posicao", "campo", "motivo"}
        aviso = corpo["diagnosticos"][0]
        assert aviso["codigo"] == "proposta_descartada"
        assert aviso["motivos"] == [MOTIVO_VIABILIDADE]
        assert aviso["continuidade_independente"] is False
        assert "nenhuma sugestão da proposta passou na validação oficial" in aviso["mensagem"]
        assert "a região não cumpre as condições de uma sugestão viável" in aviso["mensagem"]
        assert "Nenhum indicador foi publicado" in aviso["mensagem"]
        publico = json.dumps(corpo, ensure_ascii=False)
        for protegido in ("horas_disponiveis", "pontuacao", "Ana", "Bruno", "medida", "evidencias"):
            assert protegido not in publico
        repetido = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert repetido == corpo
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_colaboracao_historica_encerrada_continua_terminal_apos_o_deploy(app):
    """Descarte de formato era gravado antes de D6/D7 e terminava encerrado."""
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-historica@test.com")
        valida = _indicador(perfil, 0)
        formato = dict(_indicador(perfil, 0))
        formato["evidencias"] = ["evidencia_inventada"]
        proposta = _proposta(valida, formato)
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(_critica_de(valida)))
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        documento = _documento(_tecnicas(dataset_id)[0])
        assert documento["descartes"] == [
            {"lista": "sugestoes", "posicao": 1, "campo": "evidencias", "motivo": "evidencia_ausente"}
        ]
        assert documento["proposta"]["sugestoes"]
        assert _parcial(documento) is False
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        terminal = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert terminal["colaboracao"] == {"estado": "encerrada", "fase": None, "continuar": False}
        gravados = [(item.id, item.estado, item.resultado_json, item.chave_recuperacao) for item in _tecnicas(dataset_id)]
        for _ in range(2):
            corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
            assert corpo == terminal
            assert corpo.get("resultado") is None
            assert "diagnosticos" not in corpo
        assert [(item.id, item.estado, item.resultado_json, item.chave_recuperacao) for item in _tecnicas(dataset_id)] == gravados
        assert _finais(dataset_id) == []
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_so_descarte_do_isolamento_habilita_a_recuperacao_parcial():
    base = {
        "papel": "claude",
        "acao": "revisar",
        "proposta": {"descobertas": [], "sugestoes": [{"id": "s1"}]},
        "verificacao": {"aprovado": False, "codigo": None},
    }
    viabilidade = {"lista": "sugestoes", "posicao": 1, "campo": "status", "motivo": MOTIVO_VIABILIDADE}
    isolado = {"lista": "sugestoes", "posicao": 2, "campo": "item", "motivo": "referencia_inexistente"}
    formato = {"lista": "sugestoes", "posicao": 3, "campo": "evidencias", "motivo": "evidencia_ausente"}
    catalogo = {"lista": "sugestoes", "posicao": 4, "campo": "item", "motivo": "valor_fora_do_catalogo"}
    assert _parcial({**base, "descartes": [viabilidade]}) is True
    assert _parcial({**base, "descartes": [isolado]}) is True
    assert _parcial({**base, "descartes": [formato, isolado]}) is True
    assert _parcial({**base, "descartes": [formato]}) is False
    assert _parcial({**base, "descartes": [catalogo]}) is False


def test_referencia_inexistente_e_juncao_nao_autorizada_viram_descarte(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-referencia@test.com")
        valida = _indicador(perfil, 0)
        inexistente = dict(_indicador(perfil, 0))
        inexistente["medida"] = {**valida["medida"], "indice": 29}
        juncao = _indicador(perfil, 1)
        juncao["leitura"] = "tabela_agregada"
        juncao["grafico"] = "tabela"
        juncao["agrupamento"] = _coord(next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 0 and coluna["indice"] == 1))
        proposta = _proposta(valida, inexistente, juncao)
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(_critica_de(valida)))
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        documento = _documento(tecnica)
        assert documento["descartes"] == [
            {"lista": "sugestoes", "posicao": 1, "campo": "item", "motivo": "referencia_inexistente"},
            {"lista": "sugestoes", "posicao": 2, "campo": "item", "motivo": "juncao_nao_confirmada"},
        ]
        assert documento["verificacao"]["codigo"] in {"referencia_inexistente", "juncao_nao_confirmada"}
        assert '"indice":29' not in tecnica.resultado_json.replace(" ", "")
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["cobertura"] == "parcial"
        assert JUNCOES_EXECUTAVEIS == frozenset()
        for item in corpo["resultado"]["sugestoes"]:
            assert item["agrupamento"] is None
            assert item["juncao"] == "nao_executada"
            assert item["regiao"] == valida["regiao"]


def test_erro_de_contrato_nao_recuperavel_continua_terminal(app, caplog):
    caplog.set_level("INFO")
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-contrato@test.com")
        quebrada = _indicador(perfil, 0, status="condicional", duvidas=["escolher_medida"])
        quebrada["operacao"] = None
        quebrada["medida"] = None
        quebrada["evidencias"] = []
        quebrada["leitura"] = "esclarecimento_necessario"
        claude = _Claude([_proposta(quebrada)])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert len(gemini.geracoes) == 0
        assert _tecnicas(dataset_id)[0].resultado_json is None
        assert _finais(dataset_id) == []
        assert any(
            "evento=agente_bi_descarte" in registro.getMessage() and "motivo=esclarecimento_incoerente" in registro.getMessage()
            for registro in caplog.records
        )


def test_governanca_bloqueante_durante_o_isolamento_nao_vira_descarte(app, monkeypatch):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-governanca@test.com")
        chamadas = {"n": 0}

        def analisar_falso(*_args, **_kwargs):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                raise AnalisarErro("evidencia_ausente")
            raise AnalisarErro("autorizacao_indevida")

        monkeypatch.setattr(colaboracao, "analisar", analisar_falso)
        claude = _Claude([_proposta(_indicador(perfil, 0), _indicador(perfil, 1))])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert chamadas["n"] >= 2
        assert corpo["colaboracao"]["estado"] == "recusada"
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        assert execucao.ultimo_erro == "autorizacao_indevida"
        assert execucao.estado == "falha_definitiva"
        assert len(gemini.geracoes) == 0
        assert _finais(dataset_id) == []


def test_efeito_financeiro_incerto_impede_a_preservacao(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-incerto@test.com")
        mista = _proposta(_indicador(perfil, 0), _indicador(perfil, 1))
        claude = _Claude([mista, mista])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        execucao.efeito_externo = "incerto"
        db.session.commit()
        corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "incerta"
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert _finais(dataset_id) == []


def test_falha_de_provedor_na_revisao_nao_publica_resultado_parcial(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-provedor@test.com")
        mista = _proposta(_indicador(perfil, 0), _indicador(perfil, 1))
        claude = _Claude([mista, ErroTransporte("timeout", incerto=True)])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        for _ in range(3):
            corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "incerta"
        assert corpo.get("resultado") is None
        assert _finais(dataset_id) == []
        chamadas = claude.chamadas
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert claude.chamadas == chamadas


def test_execucao_terminal_persistida_nao_e_reaberta(app, monkeypatch):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-terminal@test.com")
        claude = _Claude([_proposta(_indicador(perfil, 0), _indicador(perfil, 1))])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        with monkeypatch.context() as antigo:
            antigo.setattr(colaboracao, "_isolar", lambda *_args, **_kwargs: None)
            corpo = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        tecnica = _tecnicas(dataset_id)[0]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        assert execucao.estado == "falha_definitiva"
        assert execucao.ultimo_erro == "contrato_saida_invalido"
        novamente = _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        assert novamente["colaboracao"]["estado"] == "recusada"
        db.session.refresh(execucao)
        assert execucao.estado == "falha_definitiva"
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert db.session.get(AgenteBiAnalise, tecnica.id).resultado_json is None


def test_envelope_com_descarte_adulterado_e_recusado(app):
    with app.app_context():
        escopo, dataset_id, perfil, _ingerido = _preparar("res-adulterado@test.com")
        claude = _Claude([_proposta(_indicador(perfil, 0), _indicador(perfil, 1))])
        gemini = _Gemini(json.dumps(_critica_de(_indicador(perfil, 0))))
        _descobrir(escopo, dataset_id, PEDIDO, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        documento = _documento(tecnica)
        adulterado = json.loads(json.dumps(documento))
        adulterado["descartes"][0]["motivo"] = "motivo_inventado"
        with pytest.raises(ExecucaoErro):
            revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), adulterado)
        sem_descarte = {chave: valor for chave, valor in documento.items() if chave != "descartes"}
        with pytest.raises(ExecucaoErro):
            revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), sem_descarte)


# --- D1, D2 e D3: diagnósticos proporcionais ------------------------------------


def test_preparacao_mostra_so_impedimento_global_e_guarda_os_achados(app):
    with app.app_context():
        escopo, _dataset_id, perfil, ingerido = _preparar("res-preparacao@test.com")
        corpo = resumir_preparacao(escopo, ingerido)
        assert corpo["diagnosticos"] == []
        codigos = {item["codigo"] for item in corpo["achados"]}
        assert {"coluna_parcialmente_vazia", "cabecalho_ambiguo", "formula_sem_cache"} <= codigos
        for item in corpo["achados"]:
            assert item["nivel"] == "informacao"
            assert item["acao"] == "nenhuma"
            assert "O impacto será avaliado quando houver um pedido de análise" in item["mensagem"]
            assert "Complete" not in item["mensagem"]
            assert "Revise" not in item["mensagem"]
        assert corpo["qualidade"]["valores_ausentes"] >= 1
        assert corpo["qualidade"]["formulas_sem_cache"] >= 2
        vazia = next(item for item in corpo["achados"] if item["codigo"] == "coluna_parcialmente_vazia")
        assert vazia["local"]["linha"] == 5
        assert len(corpo["colunas"]) == len(perfil["colunas"])
        assert corpo["regioes"]


def test_preparacao_sem_registro_bloqueia_e_protecao_informa():
    vazio = {"regioes": [{"arquivo": 0, "aba": 0, "regiao": 0, "linhas_observadas": 0}], "colunas": [], "qualidade": {}}
    bloqueio = diagnosticar_preparacao(vazio)
    assert [(item["codigo"], item["nivel"]) for item in bloqueio] == [("sem_registros", "bloqueio")]
    perfil = {"regioes": [], "colunas": [], "relacoes": [], "qualidade": {}, "governanca": {"categorias_detectadas": {"cpf": 1}}}
    metadados = {"protecao": {"decisao": "minimizada", "colunas": [{"arquivo": 0, "aba": 0, "regiao": 1, "indice": 2}]}}
    protecao = diagnosticar_preparacao(perfil, metadados)
    assert [(item["codigo"], item["nivel"]) for item in protecao] == [("dados_pessoais_protegidos", "informacao")]
    assert "O valor não é mostrado" in protecao[0]["mensagem"]


def test_formula_sem_cache_consolida_ausencia_e_tipo_misto_so_com_prova(app):
    with app.app_context():
        _escopo, _dataset_id, perfil, ingerido = _preparar("res-redundante@test.com")
        estrutura = ingerido["preparacao"]["estrutura"]
        pontuacao = next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 1 and coluna["indice"] == 2)
        assert pontuacao["dominio_inconsistente"] is True
        assert pontuacao["ausentes"] == 1

        def da_aba(itens, aba):
            return [item for item in itens if (item.get("local") or {}).get("aba") == aba]

        sem_prova = da_aba(diagnosticar_perfil(perfil), 1)
        assert {"coluna_parcialmente_vazia", "dominio_inconsistente"} <= {item["codigo"] for item in sem_prova}
        consolidados = da_aba(diagnosticar_perfil(perfil, None, estrutura), 1)
        assert [item["codigo"] for item in consolidados] == ["formula_sem_cache"]
        assert consolidados[0]["consolidados"] == 2
        assert "2 achado(s) de coluna vêm dessas fórmulas (valores ausentes em 1 e mistura de tipos em 1)." in consolidados[0]["mensagem"]
        independente = da_aba(diagnosticar_perfil(perfil, None, estrutura), 0)
        assert [item["codigo"] for item in independente] == ["coluna_parcialmente_vazia"]


def test_resultado_pede_intervencao_so_para_o_que_bloqueia_a_intencao():
    regiao_a = {"arquivo": 0, "aba": 0, "regiao": 0}
    regiao_b = {"arquivo": 0, "aba": 1, "regiao": 0}
    regiao_c = {"arquivo": 0, "aba": 2, "regiao": 0}
    resultado = {
        "descobertas": [
            {"id": "d1", "codigo": "cabecalho_ambiguo", "regiao": regiao_a, "relacao": None},
            {"id": "d2", "codigo": "cabecalho_ambiguo", "regiao": regiao_a, "relacao": None},
            {"id": "d3", "codigo": "dados_incompletos", "regiao": None, "relacao": None},
            {"id": "d4", "codigo": "formula_sem_cache", "regiao": regiao_b, "relacao": None},
        ],
        "perguntas": [
            {"id": "p1", "codigo": "confirmar_rotulos", "sobre": "ambiguidade", "regiao": regiao_a, "coluna": None, "relacao": None},
            {"id": "p2", "codigo": "escolher_medida", "sobre": "metrica", "regiao": regiao_c, "coluna": None, "relacao": None},
        ],
        "sugestoes": [
            {"id": "s1", "status": "viavel", "operacao": "soma", "regiao": regiao_c, "perguntas": []},
            {"id": "s2", "status": "condicional", "operacao": "soma", "regiao": regiao_a, "perguntas": ["p1"]},
            {"id": "s3", "status": "condicional", "operacao": None, "regiao": regiao_c, "perguntas": ["p2"]},
        ],
    }
    diagnosticos = diagnosticar_resultado(resultado)
    cabecalhos = [item for item in diagnosticos if item["codigo"] == "cabecalho_ambiguo"]
    assert len(cabecalhos) == 1
    assert cabecalhos[0]["nivel"] == "correcao"
    assert cabecalhos[0]["indicadores"] == [{"id": "s2", "status": "condicional", "operacao": "soma"}]
    incompleto = next(item for item in diagnosticos if item["codigo"] == "dados_incompletos")
    assert incompleto["nivel"] == "informacao"
    assert "Não há coluna, linha ou região comprovada" in incompleto["mensagem"]
    assert "Nenhuma ação é necessária agora" in incompleto["mensagem"]
    formula = next(item for item in diagnosticos if item["codigo"] == "formula_sem_cache")
    assert formula["nivel"] == "informacao"
    pendentes = [item for item in diagnosticos if item["codigo"] == "sugestao_nao_executavel"]
    assert [item["indicadores"][0]["id"] for item in pendentes] == ["s3"]
    assert pendentes[0]["nivel"] == "esclarecimento"


def test_divisao_lateral_orienta_pela_divisao_e_nao_pelo_cabecalho():
    regiao = {"arquivo": 0, "aba": 0, "regiao": 0}
    resultado = {
        "descobertas": [{"id": "d1", "codigo": "cabecalho_ambiguo", "regiao": regiao, "relacao": None}],
        "perguntas": [
            {"id": "p1", "codigo": "resolver_divisao_lateral", "sobre": "ambiguidade", "regiao": regiao, "coluna": None, "relacao": None},
            {"id": "p2", "codigo": "confirmar_rotulos", "sobre": "ambiguidade", "regiao": regiao, "coluna": None, "relacao": None},
        ],
        "sugestoes": [
            {"id": "s1", "status": "condicional", "operacao": "soma", "regiao": regiao, "perguntas": ["p1"]},
            {"id": "s2", "status": "condicional", "operacao": "media", "regiao": regiao, "perguntas": ["p2"]},
        ],
    }
    diagnosticos = {item["codigo"]: item for item in diagnosticar_resultado(resultado)}
    divisao = diagnosticos["divisao_lateral_ambigua"]
    assert divisao["acao"] == "revisar_regiao"
    assert divisao["indicadores"] == [{"id": "s1", "status": "condicional", "operacao": "soma"}]
    assert "a divisão da planilha está ambígua" in divisao["mensagem"]
    assert "títulos" not in divisao["mensagem"]
    cabecalho = diagnosticos["cabecalho_ambiguo"]
    assert cabecalho["acao"] == "revisar_titulos"
    assert cabecalho["indicadores"] == [{"id": "s2", "status": "condicional", "operacao": "media"}]
    assert "sugestao_nao_executavel" not in diagnosticos

    so_divisao = dict(resultado, perguntas=resultado["perguntas"][:1], sugestoes=resultado["sugestoes"][:1])
    codigos = {item["codigo"]: item for item in diagnosticar_resultado(so_divisao)}
    assert codigos["cabecalho_ambiguo"]["acao"] == "nenhuma"
    assert codigos["cabecalho_ambiguo"]["nivel"] == "informacao"
    assert codigos["divisao_lateral_ambigua"]["nivel"] == "bloqueio"


def _perfil_com_formula(coluna):
    perfil = {
        "regioes": [],
        "colunas": [{"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1, "coluna_planilha": 2, **coluna}],
        "qualidade": {"por_regiao": [{"arquivo": 0, "aba": 0, "regiao": 0, "formulas_sem_cache": {"bloqueantes": 1}}]},
    }
    estrutura = {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "indice": 0,
                "formulas_observadas": [{"linha": 3, "coluna": 2, "registro": True, "cache_estado": "ausente"}],
            }
        ]
    }
    return perfil, estrutura


def test_consolidacao_cita_so_o_tipo_de_alerta_comprovado():
    perfil, estrutura = _perfil_com_formula(
        {"presentes": 2, "ausentes": 1, "linhas_ausentes": [3], "dominio_inconsistente": False, "tipos": ["numero"]}
    )
    [ausente] = diagnosticar_perfil(perfil, None, estrutura)
    assert ausente["codigo"] == "formula_sem_cache"
    assert (ausente["consolidados"], ausente["consolidados_ausentes"]) == (1, 1)
    assert "consolidados_dominio" not in ausente
    assert "1 achado(s) de coluna vêm dessas fórmulas (valores ausentes em 1)." in ausente["mensagem"]
    assert "mistura" not in ausente["mensagem"]

    perfil, estrutura = _perfil_com_formula(
        {"presentes": 3, "ausentes": 0, "dominio_inconsistente": True, "tipos": ["numero", "formula"]}
    )
    [dominio] = diagnosticar_perfil(perfil, None, estrutura)
    assert (dominio["consolidados"], dominio["consolidados_dominio"]) == (1, 1)
    assert "consolidados_ausentes" not in dominio
    assert "1 achado(s) de coluna vêm dessas fórmulas (mistura de tipos em 1)." in dominio["mensagem"]
    assert "valores ausentes" not in dominio["mensagem"]


def test_instrucao_gemini_separa_descarte_interno_de_objecao():
    instrucao = instrucao_de_critica()
    assert MOTIVO_VIABILIDADE + " e um descarte interno decidido pelo servidor" in instrucao
    assert "Nao e codigo de objecao e nao deve aparecer em objecoes." in instrucao
    linha_enum = next(linha for linha in instrucao.splitlines() if linha.startswith("codigo da objecao:"))
    assert MOTIVO_VIABILIDADE not in linha_enum
    enum = esquema_critica()["properties"]["objecoes"]["items"]["properties"]["codigo"]["enum"]
    assert MOTIVO_VIABILIDADE not in enum
    assert enum == sorted(_CHECKPOINT)


def test_cobertura_parcial_sem_descarte_nao_gera_aviso():
    assert diagnostico_de_cobertura([]) is None
    aviso = diagnostico_de_cobertura([{"lista": "sugestoes", "posicao": 1, "campo": "status", "motivo": MOTIVO_VIABILIDADE}])
    assert aviso["quantidade"] == 1
    assert "1 item da proposta foi descartado" in aviso["mensagem"]
    assert achados_estruturais({}) == []


def test_interface_mostra_um_unico_botao_de_continuidade_por_secao(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_jornada import _descoberta, _html, _instalar, _preparacao, _scripts, _sugestao
    from tests.test_agente_bi_page import _browser_document, _launch_browser

    correcoes = [
        {
            "codigo": codigo,
            "nivel": "correcao",
            "mensagem": f"Fato: achado {codigo} na região 0.",
            "local": {"arquivo": 0, "aba": 0, "regiao": indice},
            "acao": "revisar_regiao",
            "continuidade_independente": True,
        }
        for indice, codigo in enumerate(("cabecalho_ambiguo", "formula_sem_cache", "dominio_inconsistente"))
    ]
    preparacao = _preparacao()
    preparacao["diagnosticos"] = correcoes
    preparacao["achados"] = [
        {
            "codigo": "coluna_parcialmente_vazia",
            "nivel": "informacao",
            "mensagem": "Fato: há 1 valor ausente em coluna 4, região 0, aba 0, arquivo 0. O impacto será avaliado quando houver um pedido de análise. Nenhuma ação é necessária agora.",
            "acao": "nenhuma",
            "continuidade_independente": True,
        }
    ]
    descoberta = _descoberta([_sugestao(1), _sugestao(2, status="condicional")])
    descoberta["diagnosticos"] = correcoes[:2]
    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 390, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate("(corpo) => { window.__preparacao = corpo; }", preparacao)
        page.evaluate("(corpo) => { window.__descoberta = corpo; }", descoberta)
        _scripts(page, "descoberta", "dashboard")
        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "opkips.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
        )
        page.wait_for_function("() => document.getElementById('agente-bi-shell').getAttribute('data-jornada-fase') === 'preparados'")
        preparados = page.locator("#agente-bi-diagnosticos-preparados")
        assert preparados.locator("button").count() == 1
        assert preparados.locator("button[data-continuidade='preparados']").count() == 1
        assert preparados.locator(".agente-bi-diagnostico[data-nivel='correcao']").count() == 3
        assert "Correção necessária" in preparados.inner_text()
        assert preparados.locator("details.agente-bi-achados .agente-bi-diagnostico[data-nivel='informacao']").count() == 1
        assert "Corrija na planilha de origem" not in preparados.inner_text()
        preparados.locator("button[data-continuidade='preparados']").click()
        assert page.locator("#agente-bi-shell").get_attribute("data-jornada-fase") == "analise"
        page.locator("#agente-bi-examinar").click()
        page.wait_for_function("() => document.getElementById('agente-bi-shell').getAttribute('data-jornada-fase') === 'descobertas'")
        descobertas = page.locator("#agente-bi-diagnosticos-descobertas")
        assert descobertas.locator("button[data-continuidade='descobertas']").count() == 1
        assert descobertas.locator(".agente-bi-diagnostico").count() == 2
        browser.close()
