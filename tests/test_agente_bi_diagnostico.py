"""Diagnóstico guiado e limite estrutural da lista de colunas.

Usa dados sintéticos e o provedor simulado. Não chama Claude nem Gemini.
"""
from __future__ import annotations

import json

import pytest

from app.models import AgenteBiAnalise, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_analisar_service import (
    AnalisarErro,
    analisar,
    montar_contexto,
    provedor_simulado,
)
from app.services.agente_bi_descoberta_service import (
    DescobertaErro,
    aprovar_selecao,
    diagnosticar_perfil,
    diagnosticar_resultado,
    diagnostico_de_integracao,
)
from app.services.agente_bi_integracao_service import (
    IntegracaoErro,
    _recusar_sujo,
    autorizar_transferencia,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import MAX_LISTA, escopo_do_usuario
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx


def _csv_largo(quantidade: int) -> bytes:
    nomes = [f"c{indice:02d}" for indice in range(quantidade)]
    valores = ["a" if indice else "10" for indice in range(quantidade)]
    return (",".join(nomes) + "\n" + ",".join(valores) + "\n").encode("utf-8")


def _coluna(indice: int) -> dict:
    return {"indice": indice}


def test_lista_de_colunas_respeita_o_orcamento_persistido_e_as_outras_listas_nao():
    for quantidade in (64, 65, 104):
        _recusar_sujo({"colunas": [_coluna(indice) for indice in range(quantidade)]})

    with pytest.raises(IntegracaoErro) as outra:
        _recusar_sujo({"regioes": [_coluna(indice) for indice in range(65)]})
    assert outra.value.codigo == "contexto_nao_autorizado"
    assert outra.value.detalhes is None

    with pytest.raises(IntegracaoErro) as proibido:
        _recusar_sujo({"colunas": [_coluna(indice) for indice in range(70)] + [{"valor": "oculto"}]})
    assert proibido.value.codigo == "contexto_nao_autorizado"

    with pytest.raises(IntegracaoErro) as digitos:
        _recusar_sujo({"colunas": [{"nota": "123456"}]})
    assert digitos.value.codigo == "contexto_nao_autorizado"

    with pytest.raises(IntegracaoErro) as excedido:
        _recusar_sujo({"colunas": [_coluna(indice) for indice in range(MAX_LISTA + 1)]})
    assert excedido.value.codigo == "limite_estrutural_excedido"
    assert excedido.value.detalhes["quantidade"] == MAX_LISTA + 1
    assert excedido.value.detalhes["limite"] == MAX_LISTA
    diagnostico = diagnostico_de_integracao(excedido.value)
    assert diagnostico["codigo"] == "limite_estrutural_excedido"
    assert "não é uma falha genérica de autorização" in diagnostico["mensagem"]
    assert str(MAX_LISTA + 1) in diagnostico["mensagem"]
    assert str(MAX_LISTA) in diagnostico["mensagem"]
    assert "local" not in diagnostico


def test_dataset_largo_autoriza_as_colunas_sem_truncar(ctx):
    _conta, _franquia, usuario = _par("diag-largo", "diag.largo@test.com")
    escopo = escopo_do_usuario(usuario)
    for quantidade in (64, 65, 104):
        ingerido = _ingerir(
            usuario,
            _csv_largo(quantidade),
            f"largo-{quantidade}.csv",
            chave=f"lote-largo-{quantidade}",
        )
        perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
        assert len(perfil["colunas"]) == quantidade
        contexto = montar_contexto(perfil, {"modalidades": ["A"]})
        assert len(contexto["colunas"]) == quantidade
        assert [item["indice"] for item in contexto["colunas"]] == list(range(quantidade))
        liberacao = autorizar_transferencia(
            perfil,
            contexto,
            estrutura=ingerido["preparacao"]["estrutura"],
        )
        assert len(liberacao["payload"]["colunas"]) == quantidade
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0


def test_cabecalho_ambiguo_localiza_regiao_e_preserva_analise_independente(ctx):
    _conta, _franquia, usuario = _par("diag-guia", "diag.guia@test.com")
    escopo = escopo_do_usuario(usuario)
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
    ingerido = _ingerir(usuario, planilha, "blocos.xlsx", chave="lote-guia")
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    diagnosticos = diagnosticar_perfil(perfil, ingerido["preparacao"]["metadados_preparacao"])
    ambiguos = [item for item in diagnosticos if item["codigo"] == "cabecalho_ambiguo"]
    assert ambiguos
    ambiguo = ambiguos[0]
    assert ambiguo["categoria"] == "correcao"
    assert ambiguo["local"]["arquivo"] == 0
    assert "coluna" not in ambiguo["local"]
    assert "linha" not in ambiguo["local"]
    assert "Revise os títulos das colunas" in ambiguo["mensagem"]
    assert "Hipótese analítica" in ambiguo["mensagem"]
    assert ambiguo["continuidade_independente"] is True

    from app.services.agente_bi_analisar_service import analisar_dataset

    gravado = analisar_dataset(
        escopo,
        ingerido["dataset"]["id"],
        chave="lote-guia",
        rodada=1,
        pedido={"modalidades": ["A"]},
        provedor=provedor_simulado,
    )
    resultado = gravado["resultado"]
    viaveis = [item for item in resultado["sugestoes"] if item["status"] == "viavel"]
    bloqueadas = [item for item in resultado["sugestoes"] if item["status"] != "viavel"]
    assert viaveis
    assert all(item["status"] == "viavel" for item in viaveis)
    assert bloqueadas
    assert all(item["status"] in {"condicional", "dependente_de_novos_dados"} for item in bloqueadas)
    with pytest.raises(DescobertaErro) as recusa:
        aprovar_selecao(
            escopo,
            {
                "analise_id": gravado["analise"]["id"],
                "sugestoes": [bloqueadas[0]["id"]],
                "perguntas": [],
                "confirmacao": True,
            },
        )
    assert recusa.value.codigo == "sugestao_nao_executavel"
    assert IaConsumoEvento.query.count() == 0


def test_proposta_invalida_nao_e_publicada(ctx):
    _conta, _franquia, usuario = _par("diag-invalida", "diag.invalida@test.com")
    escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(usuario, b"origem,peso\nnorte,10\nsul,4\n", "valido.csv", chave="lote-valido")
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    antes = AgenteBiAnalise.query.count()

    def provedor(contexto):
        proposta = provedor_simulado(contexto)
        assert proposta["sugestoes"]
        proposta["sugestoes"][0]["operacao"] = "inventada"
        return proposta

    with pytest.raises(AnalisarErro) as erro:
        analisar(perfil, {"modalidades": ["A"]}, provedor=provedor)
    assert erro.value.codigo == "metrica_inventada"
    assert AgenteBiAnalise.query.count() == antes


def test_coluna_parcialmente_vazia_usa_linha_comprovada(ctx):
    _conta, _franquia, usuario = _par("diag-vazia", "diag.vazia@test.com")
    escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(
        usuario,
        b"codigo,valor\nA,10\nB,\nC,30\n",
        "vazia.csv",
        chave="lote-vazia",
    )
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    coluna = next(item for item in perfil["colunas"] if item["indice"] == 1)
    assert coluna["ausentes"] >= 1
    assert coluna["presentes"] >= 1
    assert coluna["coluna_planilha"] == 2
    linha = next(
        registro["linha"]
        for registro in ingerido["preparacao"]["dados_tipados"]
        if registro["valores"][1].get("tipo") == "ausente"
    )
    diagnosticos = diagnosticar_perfil(perfil, ingerido["preparacao"]["metadados_preparacao"])
    parcial = next(item for item in diagnosticos if item["codigo"] == "coluna_parcialmente_vazia")
    assert parcial["local"]["coluna"] == 2
    assert parcial["local"]["linha"] == linha
    assert "coluna 2" in parcial["mensagem"]
    assert f"linha {linha}" in parcial["mensagem"]
    assert "Há 1 valor ausente." in parcial["mensagem"]
    assert "Hipótese analítica" in parcial["mensagem"]
    assert "Fato:" in parcial["mensagem"]
    assert parcial["continuidade_independente"] is True


def test_problema_sem_coordenada_nao_inventa_local():
    diagnosticos = diagnosticar_resultado(
        {
            "descobertas": [{"codigo": "dados_incompletos", "regiao": None, "relacao": None}],
            "sugestoes": [{"id": "s1", "status": "viavel", "operacao": "soma"}],
        }
    )
    assert diagnosticos
    incompleto = next(item for item in diagnosticos if item["codigo"] == "dados_incompletos")
    assert "local" not in incompleto
    assert "Não há coluna, linha ou região comprovada" in incompleto["mensagem"]
    assert incompleto["continuidade_independente"] is True


def test_relacao_candidata_pede_confirmacao_sem_juncao(ctx):
    _conta, _franquia, usuario = _par("diag-relacao", "diag.relacao@test.com")
    from datetime import date

    escopo = escopo_do_usuario(usuario)
    planilha = _xlsx(
        {
            "movimento": [
                ["item", "qtd", "quando"],
                ["K100", 10, date(2024, 3, 1)],
                ["K200", 4, date(2024, 3, 2)],
                ["K100", 10, date(2024, 3, 3)],
            ],
            "faixas": [
                ["alfa", "beta"],
                ["K100", 1],
                ["K200", 2],
                ["K300", 3],
            ],
        }
    )
    ingerido = _ingerir(usuario, planilha, "relacao.xlsx", chave="lote-relacao")
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    assert perfil["relacoes"]
    assert all(item["juncao"] == "nao_executada" for item in perfil["relacoes"])
    diagnosticos = diagnosticar_perfil(perfil, ingerido["preparacao"]["metadados_preparacao"])
    relacao = next(item for item in diagnosticos if item["codigo"] == "relacao_candidata_nao_confirmada")
    assert relacao["categoria"] == "confirmacao"
    assert len(relacao["regioes"]) == 2
    assert "A junção não foi executada" in relacao["mensagem"]
    assert "não executa a junção" in relacao["mensagem"]


def test_cpf_protegido_nao_revela_valor(ctx):
    _conta, _franquia, usuario = _par("diag-cpf", "diag.cpf@test.com")
    documento = "123.456.789-09"
    escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(
        usuario,
        f"codigo,faixa,qtd\n{documento},ate10,1\n".encode("utf-8"),
        "restrito.csv",
        chave="lote-cpf",
    )
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    metadados = ingerido["preparacao"]["metadados_preparacao"]
    diagnosticos = diagnosticar_perfil(perfil, metadados)
    protegido = next(item for item in diagnosticos if item["codigo"] == "dados_pessoais_protegidos")
    texto = json.dumps(protegido, ensure_ascii=False)
    registro = metadados["protecao"]["colunas"][0]
    correspondente = next(
        item
        for item in perfil["colunas"]
        if (item["arquivo"], item["aba"], item["regiao"], item["indice"])
        == (registro["arquivo"], registro["aba"], registro["regiao"], registro["indice"])
    )
    assert documento not in texto
    assert "12345678909" not in texto
    assert protegido["acao"] == "nenhuma"
    assert protegido["categoria"] == "protecao"
    assert protegido["local"]["coluna"] == correspondente["coluna_planilha"]
    assert protegido["local"]["coluna"] == 1
    assert "O valor não é mostrado" in protegido["mensagem"]
    assert "A coluna exata não foi comprovada" not in protegido["mensagem"]
    if protegido.get("categoria_dado"):
        assert protegido["categoria_dado"] == "cpf"


def test_protecao_informa_categoria_quando_a_varredura_comprova():
    perfil = {
        "regioes": [],
        "colunas": [],
        "relacoes": [],
        "qualidade": {},
        "governanca": {"categorias_detectadas": {"cpf": 1}},
    }
    metadados = {
        "protecao": {
            "decisao": "minimizada",
            "colunas": [{"arquivo": 0, "aba": 0, "regiao": 1, "indice": 2}],
        }
    }
    diagnostico = diagnosticar_perfil(perfil, metadados)[0]
    assert diagnostico["codigo"] == "dados_pessoais_protegidos"
    assert diagnostico["categoria_dado"] == "cpf"
    assert diagnostico["local"] == {"arquivo": 0, "aba": 0, "regiao": 1}
    assert "coluna" not in diagnostico["local"]
    assert "categoria cpf" in diagnostico["mensagem"]
    assert "região 1" in diagnostico["mensagem"]
    assert "A coluna exata não foi comprovada." in diagnostico["mensagem"]
    assert "coluna 2" not in diagnostico["mensagem"]
    assert "Nenhuma ação é necessária" in diagnostico["mensagem"]
    assert "123" not in diagnostico["mensagem"]
    assert diagnostico["continuidade_independente"] is True


def test_upload_corrigido_recupera_a_versao_correspondente(ctx):
    _conta, _franquia, usuario = _par("diag-versao", "diag.versao@test.com")
    escopo = escopo_do_usuario(usuario)
    primeiro = _ingerir(usuario, b"codigo,valor\nA,\nB,2\n", "origem.csv", chave="lote-correcao")
    assert primeiro["reutilizado"] is False
    perfil = perfilar_dataset(escopo, primeiro["dataset"]["id"])
    assert any(item["codigo"] == "coluna_parcialmente_vazia" for item in diagnosticar_perfil(perfil, primeiro["preparacao"]["metadados_preparacao"]))
    segundo = _ingerir(usuario, b"codigo,valor\nA,1\nB,2\n", "origem.csv", chave="lote-correcao")
    assert segundo["reutilizado"] is False
    assert segundo["dataset"]["versao"] == primeiro["dataset"]["versao"] + 1
    assert segundo["dataset"]["id"] != primeiro["dataset"]["id"]
    assert segundo["dataset"]["impressao"] != primeiro["dataset"]["impressao"]
    perfil_novo = perfilar_dataset(escopo, segundo["dataset"]["id"])
    assert not any(
        item["codigo"] == "coluna_parcialmente_vazia"
        for item in diagnosticar_perfil(perfil_novo, segundo["preparacao"]["metadados_preparacao"])
    )
    repetido = _ingerir(usuario, b"codigo,valor\nA,1\nB,2\n", "origem.csv", chave="lote-correcao")
    assert repetido["reutilizado"] is True
    assert repetido["dataset"]["versao"] == segundo["dataset"]["versao"]
    assert repetido["dataset"]["impressao"] == segundo["dataset"]["impressao"]


def test_dado_insuficiente_indica_o_que_falta():
    diagnosticos = diagnosticar_resultado(
        {
            "descobertas": [],
            "sugestoes": [
                {
                    "id": "s1",
                    "status": "dependente_de_novos_dados",
                    "operacao": None,
                    "lacuna": "medida_numerica_ausente",
                    "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
                },
                {"id": "s2", "status": "viavel", "operacao": "contagem"},
            ],
        }
    )
    lacuna = next(item for item in diagnosticos if item["codigo"] == "medida_numerica_ausente")
    assert lacuna["categoria"] == "dados_adicionais"
    assert "medida numérica" in lacuna["mensagem"]
    assert lacuna["local"]["regiao"] == 0
    assert lacuna["continuidade_independente"] is True
    assert "coluna" not in lacuna["local"]


def test_xlsx_iniciado_em_c_localiza_a_celula_d3(ctx):
    _conta, _franquia, usuario = _par("diag-d3", "diag.d3@test.com")
    escopo = escopo_do_usuario(usuario)
    planilha = _xlsx(
        {
            "fretes": [
                [None, None, "origem", "valor"],
                [None, None, "norte", 10],
                [None, None, "sul", None],
            ]
        }
    )
    ingerido = _ingerir(usuario, planilha, "deslocada.xlsx", chave="lote-d3")
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    coluna = next(item for item in perfil["colunas"] if item["indice"] == 1)
    assert coluna["coluna_planilha"] == 4
    assert coluna["linhas_ausentes"] == [3]
    diagnosticos = diagnosticar_perfil(perfil, ingerido["preparacao"]["metadados_preparacao"])
    parcial = next(item for item in diagnosticos if item["codigo"] == "coluna_parcialmente_vazia")
    assert parcial["local"]["coluna"] == 4
    assert parcial["local"]["linha"] == 3
    assert "coluna 4" in parcial["mensagem"]
    assert "linha 3" in parcial["mensagem"]
    assert "coluna 1" not in parcial["mensagem"]
    assert parcial["continuidade_independente"] is True


def test_protecao_usa_coluna_fisica_quando_o_perfil_comprova():
    perfil = {
        "regioes": [],
        "colunas": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 1,
                "indice": 2,
                "coluna_planilha": 5,
                "dominio_inconsistente": False,
                "presentes": 1,
                "ausentes": 0,
            }
        ],
        "relacoes": [],
        "qualidade": {},
        "governanca": {"categorias_detectadas": {"cpf": 1}},
    }
    metadados = {
        "protecao": {
            "decisao": "minimizada",
            "colunas": [{"arquivo": 0, "aba": 0, "regiao": 1, "indice": 2}],
        }
    }
    diagnostico = diagnosticar_perfil(perfil, metadados)[0]
    assert diagnostico["codigo"] == "dados_pessoais_protegidos"
    assert diagnostico["local"] == {"arquivo": 0, "aba": 0, "regiao": 1, "coluna": 5}
    assert "coluna 5" in diagnostico["mensagem"]
    assert "coluna 2" not in diagnostico["mensagem"]
    assert "A coluna exata não foi comprovada" not in diagnostico["mensagem"]
    assert "O valor não é mostrado" in diagnostico["mensagem"]
    assert diagnostico["continuidade_independente"] is True


@pytest.mark.parametrize("coluna_planilha", [None, 0, True, -3])
def test_coluna_planilha_invalida_nao_inventa_coordenada(coluna_planilha):
    perfil = {
        "regioes": [],
        "colunas": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "indice": 1,
                "coluna_planilha": coluna_planilha,
                "dominio_inconsistente": False,
                "presentes": 2,
                "ausentes": 1,
                "linhas_ausentes": [3],
            },
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 1,
                "indice": 0,
                "coluna_planilha": 3,
                "dominio_inconsistente": False,
                "presentes": 2,
                "ausentes": 0,
                "linhas_ausentes": [],
            },
        ],
        "relacoes": [],
        "qualidade": {},
    }
    diagnosticos = diagnosticar_perfil(perfil, None)
    parcial = next(item for item in diagnosticos if item["codigo"] == "coluna_parcialmente_vazia")
    assert parcial["local"] == {"arquivo": 0, "aba": 0, "regiao": 0, "linha": 3}
    assert "coluna" not in parcial["local"]
    assert "coluna 1" not in parcial["mensagem"]
    assert "coluna 0" not in parcial["mensagem"]
    assert "linha 3" in parcial["mensagem"]
    assert parcial["continuidade_independente"] is True

    metadados = {
        "protecao": {
            "decisao": "minimizada",
            "colunas": [{"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1}],
        }
    }
    perfil["governanca"] = {"categorias_detectadas": {"email": 1}}
    protegidos = [
        item
        for item in diagnosticar_perfil(perfil, metadados)
        if item["codigo"] == "dados_pessoais_protegidos"
    ]
    assert len(protegidos) == 1
    assert protegidos[0]["local"] == {"arquivo": 0, "aba": 0, "regiao": 0}
    assert "coluna" not in protegidos[0]["local"]
    assert "A coluna exata não foi comprovada." in protegidos[0]["mensagem"]
    assert "categoria email" in protegidos[0]["mensagem"]
    assert "@" not in protegidos[0]["mensagem"]
    assert protegidos[0]["continuidade_independente"] is True
