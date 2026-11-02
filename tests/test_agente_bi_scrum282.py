"""SCRUM-282 e SCRUM-283: rótulo seguro, achado contradito e tela parada.

Datasets sintéticos e transportes falsos. Não chama Claude, Gemini nem outra IA faturável.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from app.services.agente_bi_analisar_service import (
    anotacoes_comprovadas,
    montar_contexto,
    texto_de_cabecalho_transferivel,
)
from app.services.agente_bi_definir_service import preparar_fronteira
from app.services.agente_bi_descoberta_service import diagnosticar_resultado
from app.services.agente_bi_evolucao_service import preparar_contexto_semantico
from app.services.agente_bi_integracao_service import autorizar_transferencia
from app.services.agente_bi_perfil_semantico_service import FINALIDADE_TRANSFERENCIA, perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_dataset
from app.services.cleiton_ai_data_governance import DECISION_ALLOW, DECISION_BLOCK, DECISION_MINIMIZE, GovernanceResult
from tests.test_agente_bi_colaboracao import _Claude, _Gemini, _ate
from tests.test_agente_bi_descoberta_claude import _pedido
from tests.test_agente_bi_execucao import _seed_financeiro
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx

ROTULOS = (
    "Data",
    "ID_Veiculo",
    "Placa_Ficticia",
    "Motorista_Ficticio",
    "Status_Disponibilidade",
    "Escalado",
    "Viagem_Realizada",
    "Origem",
    "Destino",
    "Peso",
    "Volume",
    "valor",
)
PROIBIDOS = (
    "OPERGEL",
    "CELULA_PLACA",
    "CELULA_MOTORISTA",
    "CELULA_STATUS",
    "CELULA_ESCALA",
    "CELULA_VIAGEM",
    "CELULA_ORIGEM",
    "CELULA_DESTINO",
    "VH100",
    "VH200",
    "10.55",
    "4.25",
    "8.75",
    "2024-03-01",
)


def _opergel() -> bytes:
    return _xlsx(
        {
            "OPERGEL": [
                list(ROTULOS),
                [
                    date(2024, 3, 1),
                    "VH100",
                    "CELULA_PLACA",
                    "CELULA_MOTORISTA",
                    "CELULA_STATUS",
                    "CELULA_ESCALA",
                    "CELULA_VIAGEM",
                    "CELULA_ORIGEM",
                    "CELULA_DESTINO",
                    10.55,
                    4.25,
                    8.75,
                ],
                [
                    date(2024, 3, 2),
                    "VH200",
                    "CELULA_PLACA",
                    "CELULA_MOTORISTA",
                    "CELULA_STATUS",
                    "CELULA_ESCALA",
                    "CELULA_VIAGEM",
                    "CELULA_ORIGEM",
                    "CELULA_DESTINO",
                    3.5,
                    1.25,
                    2.5,
                ],
            ]
        }
    )


def _base(usuario, conteudo, nome, chave):
    ingerido = _ingerir(usuario, conteudo, nome, chave=chave)
    escopo = escopo_do_usuario(usuario)
    dataset_id = ingerido["dataset"]["id"]
    return escopo, dataset_id, obter_dataset(escopo, dataset_id)["estrutura"], perfilar_dataset(escopo, dataset_id)


def _carregado(perfil, estrutura):
    dataset = dict(perfil["dataset"])
    dataset["estrutura"] = estrutura
    return {
        "dataset": dataset,
        "perfil": perfil,
        "autorizadas": [item for item in perfil["colunas"] if isinstance(item, dict)],
        "definicoes": [],
    }


def _sem_vazamento(texto: str) -> None:
    for proibido in PROIBIDOS:
        assert proibido not in texto
    assert "OPERGEL.xlsx" not in texto


def _nomes_seguros(documento) -> None:
    nomes = [item.get("nome") for item in documento["colunas"]]
    assert nomes
    assert set(ROTULOS) <= set(nomes)
    assert all(nome in ROTULOS for nome in nomes)


def _anotacoes_nulas(documento) -> None:
    regioes = documento.get("regioes") or []
    if regioes:
        assert all(item.get("nome_arquivo") is None and item.get("nome_aba") is None for item in regioes)
    else:
        assert all(item.get("nome_arquivo") is None and item.get("nome_aba") is None for item in documento["colunas"])
    assert all(item.get("descricao") is None and item.get("unidade") is None for item in documento["colunas"])


def _governado(decisao, conteudo, categorias):
    return GovernanceResult(
        decision=decisao,
        safe_content=conteudo,
        categories_detected=list(categorias),
        issued_by_cleiton=True,
    )


def _estrutura(deteccao, rotulo):
    return {
        "regioes": [
            {
                "arquivo": 0,
                "aba": 0,
                "indice": 0,
                "cabecalho_deteccao": deteccao,
                "cabecalho_linha": 1,
                "candidatos": [{"linha": 1, "rotulos": [rotulo]}],
            }
        ]
    }


def _coluna():
    return [{"arquivo": 0, "aba": 0, "regiao": 0, "indice": 0}]


def _proposta(contexto, regiao):
    local = {"arquivo": regiao["arquivo"], "aba": regiao["aba"], "regiao": regiao["regiao"]}
    return {
        "vinculo_preparacao": contexto["vinculo_preparacao"],
        "descobertas": [{"codigo": "cabecalho_ambiguo", "regiao": local}],
        "perguntas": [{"id": "p1", "codigo": "confirmar_rotulos"}],
        "sugestoes": [
            {
                "id": "s1",
                "status": "condicional",
                "operacao": "soma",
                "regiao": local,
                "perguntas": ["p1"],
            }
        ],
    }


def _regiao(contexto, *, inequivoca):
    for regiao in contexto["regioes"]:
        ambigua = regiao["cabecalho"] != "inequivoca" or "cabecalho_ambiguo" in regiao["ambiguidades"]
        if ambigua != inequivoca:
            return regiao
    raise AssertionError(contexto["regioes"])


def _critica_numerica(perfil):
    coluna = next(item for item in perfil["colunas"] if item.get("dominio_inconsistente") is False)
    referencia = {chave: coluna[chave] for chave in ("arquivo", "aba", "regiao", "indice")}
    return {
        "referencias": [{chave: coluna[chave] for chave in ("arquivo", "aba", "regiao")}],
        "medidas": [referencia],
        "operacoes": ["soma"],
        "evidencias": [],
        "objecoes": [{"codigo": "evidencia_ausente", "referencia": referencia}],
    }


def _sem_sdk(monkeypatch):
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


def test_rotulo_so_passa_sem_digito_e_com_allow(ctx, monkeypatch):
    assert texto_de_cabecalho_transferivel(None) is None
    assert texto_de_cabecalho_transferivel("   ") is None
    assert texto_de_cabecalho_transferivel("A" * 81) is None
    for rotulo in ("ABC1D23", "Ano 2025", "KM2", "529.982.247-25", "12345678901"):
        assert texto_de_cabecalho_transferivel(rotulo) is None

    ambiguo = anotacoes_comprovadas(_estrutura("ambigua", "Data"), _coluna())
    assert ambiguo["colunas"] == {}
    assert texto_de_cabecalho_transferivel(ambiguo["colunas"].get((0, 0, 0, 0))) is None

    comprovado = anotacoes_comprovadas(_estrutura("inequivoca", "Data"), _coluna())
    assert comprovado["colunas"][(0, 0, 0, 0)] == "Data"

    vistos = []

    def governar(conteudo, **kwargs):
        vistos.append((conteudo, kwargs.get("purpose"), kwargs.get("agent"), kwargs.get("decisao")))
        decisao = governar.decisao
        if isinstance(decisao, Exception):
            raise decisao
        categorias = ["person_name"] if decisao != DECISION_ALLOW or governar.com_categoria else []
        return _governado(decisao, conteudo, categorias)

    governar.decisao = DECISION_ALLOW
    governar.com_categoria = False
    monkeypatch.setattr("app.services.agente_bi_analisar_service.govern_outbound_content", governar)
    assert texto_de_cabecalho_transferivel("Data") == "Data"
    assert vistos[-1][0] == "Data"
    assert vistos[-1][1] == FINALIDADE_TRANSFERENCIA
    assert vistos[-1][2] == "agente_bi"

    governar.com_categoria = True
    assert texto_de_cabecalho_transferivel("Data") is None
    governar.com_categoria = False
    for decisao in (DECISION_MINIMIZE, DECISION_BLOCK, RuntimeError("classificador")):
        governar.decisao = decisao
        assert texto_de_cabecalho_transferivel("Motorista_Ficticio") is None


def test_opergel_entrega_rotulos_nas_tres_rotas_sem_celula_arquivo_ou_aba(ctx, monkeypatch):
    _sem_sdk(monkeypatch)
    from tests.test_agente_bi_definir import _jornada
    from tests.test_agente_bi_descoberta_claude import _condicional

    usuario, _franquia, escopo, _analise = _seed_financeiro(email="scrum282.opergel@test.com")
    ingerido = _ingerir(usuario, _opergel(), "OPERGEL.xlsx", chave="lote-scrum282")
    dataset_id = ingerido["dataset"]["id"]
    estrutura = obter_dataset(escopo, dataset_id)["estrutura"]
    perfil = perfilar_dataset(escopo, dataset_id)
    assert estrutura["regioes"][0]["cabecalho_deteccao"] == "inequivoca"

    payload = autorizar_transferencia(perfil, montar_contexto(perfil, {"modalidades": ["A"]}), estrutura=estrutura)["payload"]
    _nomes_seguros(payload)
    _anotacoes_nulas(payload)
    _sem_vazamento(json.dumps(payload, ensure_ascii=False))

    _usuario_j, _franquia_j, escopo_j, perfil_j, pacote, _aprovada = _jornada(
        "scrum282.definir@test.com",
        "scrum282-definir",
        conteudo=_opergel(),
        nome="OPERGEL.xlsx",
    )
    estrutura_j = obter_dataset(escopo_j, perfil_j["dataset"]["id"])["estrutura"]
    fronteira = preparar_fronteira(perfil_j, pacote, estrutura=estrutura_j)
    definido = json.loads(fronteira["mensagem"])
    _nomes_seguros(definido)
    _anotacoes_nulas(definido)
    _sem_vazamento(fronteira["mensagem"])

    evoluido = preparar_contexto_semantico(_carregado(perfil_j, estrutura_j), "comparar volumes")
    assert evoluido is not None
    _nomes_seguros(evoluido)
    _anotacoes_nulas(evoluido)
    _sem_vazamento(json.dumps(evoluido, ensure_ascii=False))

    claude = _Claude([_condicional(perfil), _condicional(perfil)])
    gemini = _Gemini(json.dumps(_critica_numerica(perfil)))
    _ate(escopo, dataset_id, _pedido(perfil, ["A"]), claude, gemini)
    assert claude.pedidos
    assert gemini.geracoes
    claude_texto = claude.pedidos[0]["messages"][0]["content"]
    gemini_texto = gemini.geracoes[0]["contents"]
    if not isinstance(gemini_texto, str):
        gemini_texto = json.dumps(gemini_texto, ensure_ascii=False)
    for texto in (claude_texto, gemini_texto):
        documento = json.loads(texto)
        contexto = documento.get("contexto", documento)
        _nomes_seguros(contexto)
        _sem_vazamento(texto)


def test_cabecalho_ambiguo_contradito_nao_vira_fato_nem_bloqueio(ctx):
    _conta, _franquia, usuario = _par("scrum282-fato", "scrum282.fato@test.com")
    _escopo, _dataset_id, _estrutura_clara, perfil = _base(
        usuario,
        _xlsx({"clara": [["Data", "valor"], [date(2024, 3, 1), 2], [date(2024, 3, 2), 3]]}),
        "clara.xlsx",
        "lote-scrum282-clara",
    )
    contexto = montar_contexto(perfil, {"modalidades": ["A"]})
    clara = _regiao(contexto, inequivoca=True)
    contradito = diagnosticar_resultado(_proposta(contexto, clara), contexto)
    cabecalho = next(item for item in contradito if item["codigo"] == "cabecalho_ambiguo")
    assert "Fato" not in cabecalho["mensagem"]
    assert "não foi comprovada" in cabecalho["mensagem"]
    assert cabecalho["nivel"] == "informacao"
    assert all(item["nivel"] != "bloqueio" for item in contradito)
    assert "o cabeçalho está ambíguo" not in cabecalho["mensagem"]

    _conta_b, _franquia_b, usuario_b = _par("scrum282-amb", "scrum282.amb@test.com")
    planilha = _xlsx(
        {
            "blocos": [
                ["origem", "destino", "peso"],
                ["norte", "sul", 10],
                ["leste", "oeste", 4],
                [None, None, None],
                ["grupo", "familia"],
                ["origem", "destino"],
                ["norte", "sul"],
            ]
        }
    )
    _escopo_b, _dataset_b, _estrutura_b, perfil_b = _base(usuario_b, planilha, "blocos.xlsx", "lote-scrum282-amb")
    contexto_b = montar_contexto(perfil_b, {"modalidades": ["A"]})
    ambigua = _regiao(contexto_b, inequivoca=False)
    atual = diagnosticar_resultado(_proposta(contexto_b, ambigua), contexto_b)
    cabecalho_b = next(item for item in atual if item["codigo"] == "cabecalho_ambiguo")
    assert cabecalho_b["mensagem"].startswith("Fato: o cabeçalho está ambíguo")
    assert cabecalho_b["nivel"] == "bloqueio"

    sem_contexto = diagnosticar_resultado(_proposta(contexto, clara))
    solto = next(item for item in sem_contexto if item["codigo"] == "cabecalho_ambiguo")
    assert solto["mensagem"].startswith("Fato: o cabeçalho está ambíguo")
    assert solto["nivel"] == "bloqueio"
