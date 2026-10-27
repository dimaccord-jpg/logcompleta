"""SCRUM-144: contexto semântico governado, sem chamada à Anthropic."""
from __future__ import annotations

import copy
import json

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_analisar_service import (
    CONTRATO_ANALISAR,
    CONTRATO_CONTEXTO_ANALISAR,
    OPERACOES,
    analisar,
    contexto_de_transferencia,
    descrever_catalogo,
    governar_representacao,
    montar_contexto,
    provedor_simulado,
    validar_saida,
)
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    argumentos_do_adaptador,
    preparar_fronteira,
)
from app.services.agente_bi_evolucao_service import CONTRATO_PATCH, preparar_contexto_semantico
from app.services.agente_bi_integracao_service import (
    IntegracaoErro,
    _canonico,
    abrir_operacao,
    autorizar_transferencia,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_dataset
from app.services.cleiton_ai_data_governance import DECISION_ALLOW, DECISION_MINIMIZE, GovernanceResult
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx

INTENCAO = "quero comparar a quantidade de veiculos por transportadora"
TELEFONE = "11987654321"
FORMULA = "=SUM(A1:A9)"
SENTINELA = "NF8842109"


def _csv(tabela: str) -> bytes:
    return tabela.encode("utf-8")


def _base(usuario, conteudo: bytes, nome: str, chave: str):
    resultado = _ingerir(usuario, conteudo, nome, chave=chave)
    escopo = escopo_do_usuario(usuario)
    dataset_id = resultado["dataset"]["id"]
    return obter_dataset(escopo, dataset_id)["estrutura"], perfilar_dataset(escopo, dataset_id)


def _liberar(perfil, estrutura, pedido):
    contexto = montar_contexto(perfil, pedido)
    return autorizar_transferencia(perfil, contexto, estrutura=estrutura)


def _frota():
    return _xlsx(
        {
            "frota": [
                ["ano_fabricacao", "quantidade_veiculos", "identificador_veiculo", "transportadora"],
                [2018, 2, "V001", "Atlas"],
                [2019, 3, "V002", "Norte"],
            ]
        }
    )


def _equivalente():
    return _xlsx(
        {
            "cadastro": [
                ["ano", "qtde", "codigo", "empresa"],
                [2017, 1, "A1", "Leste"],
                [2016, 4, "A2", "Oeste"],
            ]
        }
    )


def test_contexto_autorizado_leva_nome_referencia_e_intencao(ctx):
    _conta, _franquia, usuario = _par("semantico-a", "semantico.a@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-a")
    visto = {}

    def espiar(contexto):
        visto["contexto"] = contexto
        return provedor_simulado(contexto)

    analisar(perfil, {"modalidades": ["A", "C"], "texto": INTENCAO}, provedor=espiar)
    assert INTENCAO not in json.dumps(visto["contexto"], ensure_ascii=False)
    assert "Atlas" not in json.dumps(visto["contexto"], ensure_ascii=False)
    assert "intencao_original" not in visto["contexto"]

    liberacao = _liberar(perfil, estrutura, {"modalidades": ["C"], "texto": INTENCAO})
    payload = liberacao["payload"]
    bruto = json.dumps(payload, ensure_ascii=False)
    assert payload["contrato"] == CONTRATO_CONTEXTO_ANALISAR
    assert payload["intencao"] == INTENCAO
    assert payload["vinculo_preparacao"] == perfil["vinculo_preparacao"]
    assert payload["dataset"]["id"] == perfil["dataset"]["id"]
    assert payload["dataset"]["versao"] == perfil["dataset"]["versao"]
    assert payload["dataset"]["impressao"] == perfil["dataset"]["impressao"]
    for coluna in payload["colunas"]:
        assert coluna["nome"] is None
        assert {"arquivo", "aba", "regiao", "indice"} <= set(coluna)
        assert coluna["descricao"] is None
        assert coluna["unidade"] is None
    identidades = {(item["arquivo"], item["aba"], item["regiao"], item["indice"]) for item in payload["colunas"]}
    assert len(identidades) == len(payload["colunas"])
    assert "ano_fabricacao" not in bruto
    assert "identificador_veiculo" not in bruto
    anotado = json.dumps(
        {
            "intencao": payload["intencao"],
            "nomes": [item["nome"] for item in payload["colunas"]],
            "descricoes": [item["descricao"] for item in payload["colunas"]],
            "unidades": [item["unidade"] for item in payload["colunas"]],
        },
        ensure_ascii=False,
    )
    assert "Atlas" not in bruto
    assert "V001" not in bruto
    assert "V002" not in bruto
    assert "2018" not in anotado
    assert SENTINELA not in bruto
    assert FORMULA not in bruto


def test_governanca_allow_minimize_block_e_falhas(ctx, monkeypatch):
    _conta, _franquia, usuario = _par("semantico-b", "semantico.b@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-b")
    pedido = {"modalidades": ["C"], "texto": f"telefone {TELEFONE}"}
    liberacao = _liberar(perfil, estrutura, pedido)
    assert TELEFONE not in json.dumps(liberacao["payload"], ensure_ascii=False)
    assert isinstance(liberacao["payload"]["intencao"], str)
    assert liberacao["payload"]["intencao"] != pedido["texto"]
    assert "conteudo_minimizado_revalidado" in liberacao["verificacoes"]
    assert liberacao["payload"]["colunas"][0]["indice"] == 0
    assert liberacao["payload"]["dataset"]["id"] == perfil["dataset"]["id"]
    assert liberacao["payload"]["dataset"]["versao"] == perfil["dataset"]["versao"]
    assert liberacao["payload"]["dataset"]["impressao"] == perfil["dataset"]["impressao"]
    assert liberacao["payload"]["vinculo_preparacao"] == perfil["vinculo_preparacao"]
    assert pedido["texto"] not in json.dumps(liberacao["payload"], ensure_ascii=False)
    assert all(item["nome"] is None for item in liberacao["payload"]["colunas"])

    def minimiza_indice(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        seguro["colunas"][0]["indice"] = 99
        return GovernanceResult(
            decision=DECISION_MINIMIZE,
            safe_content=seguro,
            categories_detected=["person_name"],
            categories_removed=["person_name"],
            reason_codes=["incidental_data_minimized"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        minimiza_indice,
    )
    with pytest.raises(IntegracaoErro) as quebrada:
        _liberar(perfil, estrutura, {"modalidades": ["A"]})
    assert quebrada.value.codigo == "contexto_nao_autorizado"

    def bloqueia(*_args, **_kwargs):
        return GovernanceResult(
            decision="blocked",
            safe_content=None,
            reason_codes=["conteudo_nao_autorizado"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        bloqueia,
    )
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, {"modalidades": ["A"]})

    def indisponivel(*_args, **_kwargs):
        raise RuntimeError("classificador fora")

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        indisponivel,
    )
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, {"modalidades": ["A"]})

    def incoerente(conteudo, **_kwargs):
        return GovernanceResult(
            decision=DECISION_MINIMIZE,
            safe_content=copy.deepcopy(conteudo),
            issued_by_cleiton=True,
        )

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        incoerente,
    )
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, {"modalidades": ["A"]})

    def nao_emitida(conteudo, **_kwargs):
        return GovernanceResult(
            decision="allowed",
            safe_content=copy.deepcopy(conteudo),
            issued_by_cleiton=False,
        )

    monkeypatch.setattr(
        "app.services.agente_bi_integracao_service.govern_outbound_content",
        nao_emitida,
    )
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, {"modalidades": ["A"]})


def test_colunas_homonimas_conservam_referencia_tecnica(ctx):
    _conta, _franquia, usuario = _par("semantico-c", "semantico.c@test.com")
    estrutura, perfil = _base(
        usuario,
        _xlsx(
            {
                "dupla": [
                    ["transportadora", "transportadora"],
                    [1, 2],
                    [3, 4],
                ]
            }
        ),
        "dupla.xlsx",
        "semantico-c",
    )
    payload = _liberar(perfil, estrutura, {"modalidades": ["A"]})["payload"]
    assert len(payload["colunas"]) == 2
    assert all(item.get("nome") is None for item in payload["colunas"])
    assert "transportadora" not in json.dumps(payload, ensure_ascii=False)
    identidades = {(item["arquivo"], item["aba"], item["regiao"], item["indice"]) for item in payload["colunas"]}
    assert len(identidades) == 2
    assert all(item["descricao"] is None and item["unidade"] is None for item in payload["colunas"])


def test_rejeita_referencia_de_outro_dataset(ctx):
    _conta, _franquia, usuario = _par("semantico-d1", "semantico.d1@test.com")
    _outra, _outra_franquia, outro = _par("semantico-d2", "semantico.d2@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-d1")
    contexto = montar_contexto(perfil, {"modalidades": ["A"]})
    trocado = copy.deepcopy(contexto)
    trocado["dataset"]["id"] = int(trocado["dataset"]["id"]) + 1
    with pytest.raises(IntegracaoErro) as erro:
        autorizar_transferencia(perfil, trocado, estrutura=estrutura)
    assert erro.value.codigo == "contexto_nao_autorizado"

    coluna = copy.deepcopy(contexto)
    coluna["colunas"].append({**coluna["colunas"][0], "indice": 90})
    with pytest.raises(IntegracaoErro):
        autorizar_transferencia(perfil, coluna, estrutura=estrutura)

    versao = copy.deepcopy(contexto)
    versao["dataset"]["versao"] = int(versao["dataset"]["versao"]) + 1
    with pytest.raises(IntegracaoErro):
        autorizar_transferencia(perfil, versao, estrutura=estrutura)

    regiao = copy.deepcopy(contexto)
    regiao["regioes"][0]["regiao"] = 90
    with pytest.raises(IntegracaoErro):
        autorizar_transferencia(perfil, regiao, estrutura=estrutura)

    with pytest.raises(Exception) as alheio:
        perfilar_dataset(escopo_do_usuario(outro), perfil["dataset"]["id"])
    assert getattr(alheio.value, "codigo", "") == "nao_encontrado"


def test_contexto_antigo_continua_aceitavel(ctx):
    _conta, _franquia, usuario = _par("semantico-e", "semantico.e@test.com")
    _estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-e")
    pedido = {"modalidades": ["A"]}
    saida = analisar(perfil, pedido)
    assert saida["contrato"] == CONTRATO_ANALISAR
    antigo = montar_contexto(perfil, pedido)
    assert antigo.pop("intencao_original") is None
    assert validar_saida(antigo, saida)["contrato"] == CONTRATO_ANALISAR
    estrutural = contexto_de_transferencia(montar_contexto(perfil, pedido))
    assert "intencao" not in estrutural
    assert "nome" not in estrutural["colunas"][0]
    assert "sugestoes" in provedor_simulado(estrutural)
    assert CONTRATO_DEFINIR == "definir.v1"
    assert CONTRATO_PATCH == "patch.definir.v1"


def test_linguagem_com_formula_nao_vira_dsl(ctx):
    _conta, _franquia, usuario = _par("semantico-f", "semantico.f@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-f")
    texto = f"soma {FORMULA} {SENTINELA}"
    payload = _liberar(perfil, estrutura, {"modalidades": ["C"], "texto": texto})["payload"]
    assert payload["intencao"] == texto
    assert payload["valores_de_celula"] is False
    assert "formulas" not in payload
    catalogo = descrever_catalogo()
    assert "SUM" not in json.dumps(catalogo, ensure_ascii=False)
    assert FORMULA not in json.dumps(catalogo, ensure_ascii=False)
    visto = {}

    def espiar(contexto):
        visto["contexto"] = contexto
        return provedor_simulado(contexto)

    saida = analisar(perfil, {"modalidades": ["C"], "texto": texto}, provedor=espiar)
    assert FORMULA not in json.dumps(visto["contexto"], ensure_ascii=False)
    assert SENTINELA not in json.dumps(visto["contexto"], ensure_ascii=False)
    assert FORMULA not in json.dumps(saida, ensure_ascii=False)
    assert SENTINELA not in json.dumps(saida, ensure_ascii=False)
    for item in saida["sugestoes"]:
        operacao = item.get("operacao")
        if operacao is not None:
            assert operacao in OPERACOES

    outra_estrutura, outro_perfil = _base(usuario, _equivalente(), "cadastro.xlsx", "semantico-f2")
    outra = _liberar(outro_perfil, outra_estrutura, {"modalidades": ["A"]})["payload"]
    assert outra["dataset"]["id"] != payload["dataset"]["id"]
    assert all(item["nome"] is None for item in outra["colunas"])
    assert all(item["unidade"] is None and item["descricao"] is None for item in outra["colunas"])
    assert all(item["unidade"] is None for item in payload["colunas"])
    numericas = [item for item in payload["colunas"] if "numero" in item["tipos"]]
    assert numericas
    assert all(item["unidade"] is None for item in numericas)
    assert "Leste" not in json.dumps(outra, ensure_ascii=False)
    bruto_outra = json.dumps(outra, ensure_ascii=False)
    for cabecalho in ("ano", "qtde", "codigo", "empresa"):
        assert cabecalho not in bruto_outra


def test_evolucao_prepara_contexto_sem_trocar_a_rota(ctx):
    from tests.test_agente_bi_calculo import _calcular, _coluna, _gravar, _indicador, _ref
    from tests.test_agente_bi_definir import _jornada
    from tests.test_agente_bi_evolucao import _evoluir

    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(
        "semantico.evo@test.com",
        "semantico-evo",
    )
    from app.services.agente_bi_persistencia_service import obter_dataset
    from tests.test_agente_bi_analisar import SENTINELA as CELULA

    estrutura = obter_dataset(escopo, perfil["dataset"]["id"])["estrutura"]
    fronteira = preparar_fronteira(perfil, pacote, estrutura=estrutura)
    assert fronteira["chamada_anthropic"] is False
    assert "beta" not in fronteira["mensagem"]
    assert CELULA not in fronteira["mensagem"]
    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    pacote_um = _indicador(pacote, "soma", leitura="indicador_de_medida")
    pacote_um["graficos"][0]["medida"] = _ref(valor)
    from app.services.agente_bi_definir_service import definir
    from app.services.agente_bi_dashboard_service import montar_dashboard

    documento = definir(perfil, pacote_um)
    salva = _gravar(escopo, pacote["id"], documento)
    calculo = _calcular(escopo, salva["id"])
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    from app.services.agente_bi_evolucao_service import _carregar

    carregado = _carregar(escopo, painel["dashboard"]["id"])
    governado = preparar_contexto_semantico(carregado, "quero ver a tendencia de beta")
    assert governado is not None
    assert governado["intencao"] == "quero ver a tendencia de beta"
    assert all(item.get("nome") is None for item in governado["colunas"])
    assert all(item["unidade"] is None for item in governado["colunas"])
    identidades = {
        (item["referencia"]["arquivo"], item["referencia"]["aba"], item["referencia"]["regiao"], item["referencia"]["indice"])
        for item in governado["colunas"]
    }
    assert len(identidades) == len(governado["colunas"])
    assert "sul" not in json.dumps(governado, ensure_ascii=False)
    resposta = _evoluir(escopo, painel, "quero ver a tendencia de beta")
    assert resposta["situacao"] == "esclarecimento"
    assert resposta["chamada_anthropic"] is False


def test_abertura_nao_grava_texto_original_no_contexto(ctx):
    _conta, _franquia, usuario = _par("semantico-grava", "semantico.grava@test.com")
    ingerido = _ingerir(usuario, _csv("alfa,beta\n1,2\n3,4\n"), "grava.csv", chave="semantico-grava")
    escopo = escopo_do_usuario(usuario)
    pedido = {"modalidades": ["C"], "texto": INTENCAO}
    abertura = abrir_operacao(
        escopo,
        ingerido["dataset"]["id"],
        pedido,
        chave_analise="semantico-grava-analise",
    )
    linha = db.session.get(AgenteBiAnalise, abertura["analise_id"])
    assert INTENCAO not in (linha.contexto_json or "")


_FRASES = (
    "comparar R$/km",
    "comparar 2025 e 2026",
    "frete de 01/03 a 31/03",
)
_BLOQUEIOS_TECNICOS = (
    "classifier_unavailable",
    "classifier_exception",
    "missing_safe_content",
    "inconsistent_governance_result",
    "invalid_decision",
    "unsupported_purpose",
)


def _carregado(perfil, estrutura):
    dataset = dict(perfil["dataset"])
    dataset["estrutura"] = estrutura
    return {
        "dataset": dataset,
        "perfil": perfil,
        "autorizadas": [item for item in perfil["colunas"] if isinstance(item, dict)],
        "definicoes": [],
    }


def _governado(decisao, conteudo, codigos):
    minimizado = decisao == DECISION_MINIMIZE
    return GovernanceResult(
        decision=decisao,
        safe_content=conteudo,
        reason_codes=list(codigos),
        categories_detected=["person_name"] if minimizado else [],
        categories_removed=["person_name"] if minimizado else [],
        issued_by_cleiton=True,
    )


def test_allow_preserva_intencao_na_descoberta_e_na_evolucao(ctx, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    _conta, _franquia, usuario = _par("semantico-frases", "semantico.frases@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-frases")
    carregado = _carregado(perfil, estrutura)
    for frase in _FRASES:
        payload = _liberar(perfil, estrutura, {"modalidades": ["C"], "texto": frase})["payload"]
        assert payload["intencao"] == frase
        assert payload["dataset"]["id"] == perfil["dataset"]["id"]
        governado = preparar_contexto_semantico(carregado, frase)
        assert governado is not None
        assert governado["intencao"] == frase
        assert all(item.get("nome") is None for item in governado["colunas"])


def test_bloqueio_tecnico_encerra_sem_segunda_avaliacao(ctx, monkeypatch):
    _conta, _franquia, usuario = _par("semantico-tec", "semantico.tec@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-tec")
    for codigo in _BLOQUEIOS_TECNICOS:
        chamadas = {"n": 0}

        def governar(conteudo, **_kwargs):
            chamadas["n"] += 1
            return _governado("blocked", None, [codigo])

        monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", governar)
        with pytest.raises(IntegracaoErro) as erro:
            _liberar(perfil, estrutura, {"modalidades": ["C"], "texto": INTENCAO})
        assert erro.value.codigo == "contexto_nao_autorizado"
        assert chamadas["n"] == 1


def test_bloqueio_de_conteudo_respeita_a_segunda_decisao(ctx, monkeypatch):
    _conta, _franquia, usuario = _par("semantico-cont", "semantico.cont@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", "semantico-cont")
    pedido = {"modalidades": ["C"], "texto": INTENCAO}
    recusas = {"n": 0}

    def recusa(conteudo, **_kwargs):
        recusas["n"] += 1
        return _governado("blocked", None, ["conteudo_nao_autorizado"])

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", recusa)
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, pedido)
    assert recusas["n"] == 2

    chamadas = []

    def reduz(conteudo, **_kwargs):
        chamadas.append(copy.deepcopy(conteudo))
        if len(chamadas) == 1:
            return _governado("blocked", None, ["ambiguous_high_risk_span"])
        return _governado(DECISION_ALLOW, copy.deepcopy(conteudo), ["no_incidental_categories"])

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", reduz)
    liberacao = _liberar(perfil, estrutura, pedido)
    assert len(chamadas) == 2
    assert chamadas[1]["intencao"] is None
    assert chamadas[1]["dataset"] == chamadas[0]["dataset"]
    assert [item["indice"] for item in chamadas[1]["colunas"]] == [item["indice"] for item in chamadas[0]["colunas"]]
    assert liberacao["payload"]["intencao"] is None
    assert INTENCAO not in json.dumps(liberacao["payload"], ensure_ascii=False)
    assert "intencao_nao_transportada" in liberacao["verificacoes"]
    assert "anotacoes_opcionais_removidas" in liberacao["verificacoes"]
    assert "conteudo_identico" not in liberacao["verificacoes"]
    assert liberacao["payload"]["dataset"]["impressao"] == perfil["dataset"]["impressao"]

    trocas = {"n": 0}

    def troca_indice(conteudo, **_kwargs):
        trocas["n"] += 1
        if trocas["n"] == 1:
            return _governado("blocked", None, ["conteudo_nao_autorizado"])
        seguro = copy.deepcopy(conteudo)
        seguro["colunas"][0]["indice"] = 99
        return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", troca_indice)
    with pytest.raises(IntegracaoErro):
        _liberar(perfil, estrutura, pedido)
    assert trocas["n"] == 2


@pytest.mark.parametrize(
    "campo",
    ["indice", "arquivo", "regiao", "dataset", "versao", "impressao", "vinculo"],
)
def test_governanca_nao_altera_referencia_tecnica(ctx, monkeypatch, campo):
    _conta, _franquia, usuario = _par(f"semantico-ref-{campo}", f"semantico.ref.{campo}@test.com")
    estrutura, perfil = _base(usuario, _frota(), "frota.xlsx", f"semantico-ref-{campo}")

    def alterar(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        if campo == "indice":
            seguro["colunas"][0]["indice"] += 1
        elif campo == "arquivo":
            seguro["colunas"][0]["arquivo"] += 1
        elif campo == "regiao":
            seguro["regioes"][0]["regiao"] += 1
        elif campo == "dataset":
            seguro["dataset"]["id"] += 1
        elif campo == "versao":
            seguro["dataset"]["versao"] += 1
        elif campo == "impressao":
            seguro["dataset"]["impressao"] = f"{seguro['dataset']['impressao']}x"
        else:
            seguro["vinculo_preparacao"] = f"{seguro['vinculo_preparacao']}-outro"
        return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", alterar)
    with pytest.raises(IntegracaoErro) as erro:
        _liberar(perfil, estrutura, {"modalidades": ["A"]})
    assert erro.value.codigo == "contexto_nao_autorizado"


def _coord(coluna) -> tuple:
    return (coluna["arquivo"], coluna["aba"], coluna["regiao"], coluna["indice"])


def _anotacoes_de_nome_nulas(documento) -> None:
    vistos = []

    def andar(no):
        if isinstance(no, dict):
            for chave, valor in no.items():
                if chave in {"nome_arquivo", "nome_aba"}:
                    vistos.append((chave, valor))
                else:
                    andar(valor)
        elif isinstance(no, list):
            for item in no:
                andar(item)

    andar(documento)
    assert {chave for chave, _valor in vistos} == {"nome_arquivo", "nome_aba"}
    assert all(valor is None for _chave, valor in vistos)


def _sem_identificadores(mensagem: str) -> None:
    assert "ABC1D23" not in mensagem
    assert "ABC1D23.xlsx" not in mensagem
    assert "XYZ-9876" not in mensagem
    assert "ABC-1234" not in mensagem


def test_cabecalho_de_placa_nao_entra_no_transporte(ctx, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    planilha = _xlsx(
        {
            "XYZ-9876": [
                ["ABC1D23", "ABC-1234", "quantidade"],
                ["a", "b", 2],
                ["c", "d", 2],
                ["e", "f", 4],
            ]
        }
    )
    from tests.test_agente_bi_definir import _jornada

    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(
        "semantico.placa@test.com",
        "semantico-placa",
        conteudo=planilha,
        nome="ABC1D23.xlsx",
    )
    estrutura = obter_dataset(escopo, perfil["dataset"]["id"])["estrutura"]
    bruto_estrutura = json.dumps(estrutura, ensure_ascii=False)
    assert estrutura["arquivos"][0]["nome"] == "ABC1D23.xlsx"
    assert estrutura["abas"][0]["nome"] == "XYZ-9876"
    assert "ABC1D23" in bruto_estrutura
    assert "ABC-1234" in bruto_estrutura
    assert "XYZ-9876" in bruto_estrutura
    originais = [_coord(item) for item in perfil["colunas"]]
    regioes_originais = [(item["arquivo"], item["aba"], item["regiao"]) for item in perfil["regioes"]]
    assert len(set(originais)) == len(originais) == 3

    payload = _liberar(perfil, estrutura, {"modalidades": ["A"]})["payload"]
    mensagem_integracao = _canonico(payload)
    integrado = json.loads(mensagem_integracao)
    _sem_identificadores(mensagem_integracao)
    _anotacoes_de_nome_nulas(integrado)
    assert [_coord(item) for item in integrado["colunas"]] == originais
    assert [(item["arquivo"], item["aba"], item["regiao"]) for item in integrado["regioes"]] == regioes_originais
    assert all(item["nome"] is None for item in integrado["colunas"])

    fronteira = preparar_fronteira(perfil, pacote, estrutura=estrutura)
    pedido = argumentos_do_adaptador(fronteira, escopo, chave_idempotencia="semantico.placa.local")
    assert pedido["invocar"] is False
    mensagem_definicao = pedido["mensagem"]
    assert mensagem_definicao == fronteira["mensagem"]
    definido = json.loads(mensagem_definicao)
    _sem_identificadores(mensagem_definicao)
    _anotacoes_de_nome_nulas(definido)
    assert [_coord(item) for item in definido["colunas"]] == originais
    assert [(item["arquivo"], item["aba"], item["regiao"]) for item in definido["regioes"]] == regioes_originais
    assert all(item["nome"] is None for item in definido["colunas"])

    carregado = _carregado(perfil, estrutura)
    governado = preparar_contexto_semantico(carregado, "comparar quantidades")
    assert governado is not None
    mensagem_evolucao = json.dumps(governado, ensure_ascii=True, sort_keys=True)
    evoluido = json.loads(mensagem_evolucao)
    _sem_identificadores(mensagem_evolucao)
    _anotacoes_de_nome_nulas(evoluido)
    referencias = [_coord(item["referencia"]) for item in evoluido["colunas"]]
    assert referencias == originais
    assert all(item.get("nome") is None for item in evoluido["colunas"])

    persistida = obter_dataset(escopo, perfil["dataset"]["id"])["estrutura"]
    assert persistida["arquivos"][0]["nome"] == "ABC1D23.xlsx"
    assert persistida["abas"][0]["nome"] == "XYZ-9876"
    assert "ABC1D23" in json.dumps(persistida, ensure_ascii=False)
    assert "ABC-1234" in json.dumps(persistida, ensure_ascii=False)


def test_governar_representacao_separa_bloqueio_tecnico_de_conteudo():
    base = {
        "amostras": False,
        "valores_de_celula": False,
        "rotulos": False,
        "apto_anthropic": False,
        "autorizado_para_provedor": False,
        "transferencia": "bloqueada",
        "intencao": "comparar R$/km",
        "descricao": None,
        "unidade": None,
        "indice": 0,
        "dataset": {"id": 7, "versao": 1, "impressao": "ab"},
        "vinculo_preparacao": "vinculo",
    }
    from app.services.agente_bi_analisar_service import TransferenciaRecusada

    for codigo in _BLOQUEIOS_TECNICOS:
        chamadas = {"n": 0}

        def governar(conteudo, **_kwargs):
            chamadas["n"] += 1
            return _governado("blocked", None, [codigo])

        with pytest.raises(TransferenciaRecusada):
            governar_representacao(copy.deepcopy(base), governar=governar)
        assert chamadas["n"] == 1

    vistos = []

    def por_conteudo(conteudo, **_kwargs):
        vistos.append(copy.deepcopy(conteudo))
        if len(vistos) == 1:
            return _governado("blocked", None, ["conteudo_nao_autorizado"])
        return _governado(DECISION_ALLOW, copy.deepcopy(conteudo), ["no_incidental_categories"])

    autorizado = governar_representacao(copy.deepcopy(base), governar=por_conteudo)
    assert len(vistos) == 2
    assert vistos[1]["intencao"] is None
    assert vistos[1]["indice"] == 0
    assert vistos[1]["dataset"] == base["dataset"]
    assert vistos[1]["vinculo_preparacao"] == "vinculo"
    assert autorizado["payload"]["intencao"] is None
    assert autorizado["anotacoes_removidas"] is True
    assert "comparar R$/km" not in json.dumps(autorizado["payload"], ensure_ascii=False)
