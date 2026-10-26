"""SCRUM-260: contrato definir, DSL fechada e validação, sem executor e sem Anthropic."""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from app.models import AgenteBiAnalise, AgenteBiDashboard, AgenteBiExecucao, IaChamadaTentativa
from app.services.agente_bi_analisar_service import analisar_dataset
from app.services.agente_bi_anthropic_service import chamar_anthropic_governado
from app.services.agente_bi_definir_service import (
    CONTRATO_CONTEXTO_DEFINIR,
    CONTRATO_DEFINIR,
    DSL_CONTRATO,
    OPERACOES_EXECUCAO,
    DefinirErro,
    argumentos_do_adaptador,
    definir,
    definir_selecao,
    descrever_dsl,
    esquema_rascunho_definir,
    instrucao_de_rascunho,
    montar_contexto_definir,
    preparar_fronteira,
    validar_saida,
)
from app.services.agente_bi_descoberta_service import aprovar_selecao
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_analise
from tests.conftest import seed_usuario
from tests.test_agente_bi_analisar import SENTINELA, _movimento
from tests.test_agente_bi_descoberta import _aprovar, _descobrir, _postar, _viaveis
from tests.agente_bi_fonte import fonte_sem_docstring_de_modulo
from tests.test_agente_bi_ingestao import _cliente as _cliente_ingestao
from tests.test_agente_bi_ingestao import _entrar, _ingerir, _par

ROOT = Path(__file__).resolve().parents[1]


def _pacote(aprovada):
    pacote = dict(aprovada["selecao"]["resultado"])
    pacote["id"] = aprovada["selecao"]["id"]
    return pacote


def _jornada(email, slug, conteudo=None, nome="movimento.xlsx"):
    _conta, franquia, usuario = _par(slug, email)
    escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(usuario, conteudo if conteudo is not None else _movimento(), nome, chave=f"lote-{slug}")
    dataset = ingerido["dataset"]
    perfil = perfilar_dataset(escopo, dataset["id"])
    gravado = analisar_dataset(
        escopo,
        dataset["id"],
        chave=dataset["chave"],
        rodada=1,
        pedido={"modalidades": ["A"]},
    )
    resultado = gravado["resultado"]
    ids = [
        item["id"]
        for item in resultado["sugestoes"]
        if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
    ][:6]
    assert ids, [(item.get("status"), item.get("grafico"), item.get("operacao")) for item in resultado["sugestoes"]]
    aprovada = aprovar_selecao(
        escopo,
        {
            "analise_id": gravado["analise"]["id"],
            "sugestoes": ids,
            "perguntas": [{"id": item["id"], "confirmada": True} for item in resultado["perguntas"]],
            "confirmacao": True,
        },
    )
    return usuario, franquia, escopo, perfil, _pacote(aprovada), aprovada


def _por(saida, grafico, operacao):
    return next(
        item
        for item in saida["definicoes"]
        if item["grafico"] == grafico and item["metrica"]["operacao"] == operacao
    )


def _coluna(perfil, **filtro):
    return next(coluna for coluna in perfil["colunas"] if all(coluna[chave] == valor for chave, valor in filtro.items()))


def test_definicoes_validas_de_kpis_sinteticos(ctx):
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada("definir.kpi@test.com", "definir-kpi")
    antes = json.dumps(perfil, sort_keys=True)
    saida = definir(perfil, pacote)
    assert json.dumps(perfil, sort_keys=True) == antes
    assert saida["contrato"] == CONTRATO_DEFINIR
    assert saida["dsl"] == DSL_CONTRATO
    assert saida["provedor"] == "simulado"
    assert saida["chamada_anthropic"] is False
    assert saida["apto_anthropic"] is False
    assert saida["execucao"] == "nao_executada"
    assert saida["origem_selecao_id"] == pacote["id"]
    assert saida["origem_descoberta_id"] == pacote["origem_analise_id"]
    assert saida["origem_contrato"] == "selecao.descoberta.v1"
    assert saida["descoberta_contrato"] == "analisar.v1"
    assert saida["vinculo_preparacao"] == pacote["vinculo_preparacao"]
    assert 1 <= len(saida["definicoes"]) <= 6
    assert [item["sugestao_id"] for item in saida["definicoes"]] == [item["id"] for item in pacote["graficos"]]
    assert SENTINELA not in json.dumps(saida)

    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    quando = next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 0 and "data" in coluna["dimensoes"])
    indicador = next(
        item
        for item in saida["definicoes"]
        if item["grafico"] == "indicador"
        and item["metrica"]["operacao"] == "soma"
        and item["metrica"]["coluna"]["aba"] == valor["aba"]
        and item["metrica"]["coluna"]["indice"] == valor["indice"]
    )
    assert indicador["status"] == "definida"
    assert indicador["metrica"]["unidade"] == "valor"
    assert indicador["metrica"]["coluna"]["indice"] == valor["indice"]
    assert indicador["granularidade"] == "total"
    assert indicador["dimensoes"] == []
    assert indicador["filtros"] == []
    assert indicador["parametros"] == []
    assert indicador["relacionamentos"] == []
    assert indicador["juncao"] == "nao_executada"
    assert indicador["esclarecimento"] is None
    assert indicador["leitura"] == "indicador_de_medida"

    linha = _por(saida, "linha", "soma")
    assert linha["status"] == "definida"
    assert linha["metrica"]["unidade"] == "valor"
    assert linha["granularidade"] in {"data", "tempo"}
    assert linha["dimensoes"] == [
        {
            "papel": "agrupamento",
            "coluna": {
                "arquivo": quando["arquivo"],
                "aba": quando["aba"],
                "regiao": quando["regiao"],
                "indice": quando["indice"],
            },
            "granularidade": linha["granularidade"],
            "unidade": None,
        }
    ]
    assert linha["leitura"] == "serie_temporal_de_medida"

    barra = next(item for item in saida["definicoes"] if item["grafico"] == "barra" and item["metrica"]["operacao"] == "soma")
    assert barra["status"] == "definida"
    assert barra["granularidade"] == "categoria"
    assert barra["metrica"]["unidade"] == "valor"
    assert barra["dimensoes"][0]["papel"] == "agrupamento"
    assert barra["dimensoes"][0]["unidade"] is None
    assert barra["leitura"] == "medida_por_categoria"
    origem_barra = next(item for item in pacote["graficos"] if item["id"] == barra["sugestao_id"])
    assert barra["dimensoes"][0]["coluna"] == origem_barra["agrupamento"]

    dispersoes = [item for item in saida["definicoes"] if item["grafico"] == "dispersao"]
    if dispersoes:
        dispersao = dispersoes[0]
        assert dispersao["status"] == "definida"
        assert dispersao["metrica"]["operacao"] == "par_numerico"
        assert dispersao["granularidade"] == "par"
        assert dispersao["metrica"]["unidade"] in {"valor", "quantidade"}
        assert dispersao["dimensoes"][0]["papel"] == "segunda_medida"
        assert dispersao["dimensoes"][0]["unidade"] in {"valor", "quantidade"}
        assert dispersao["relacionamentos"] == []

    catalogo = descrever_dsl()
    assert catalogo["agregacoes"] == [
        "contagem",
        "contagem_distinta",
        "count_if",
        "maximo",
        "media",
        "minimo",
        "participacao",
        "razao",
        "soma",
    ]
    assert catalogo["par_numerico"] == "par_numerico"
    assert catalogo["juncoes_executaveis"] == ["muitos_para_um"]
    assert "adimensional" in catalogo["unidades"]
    assert catalogo["expressoes_executaveis"] == []
    assert "executor_ausente" in catalogo["limitacoes"]
    assert "sem_sql" in catalogo["limitacoes"]
    assert "sem_python" in catalogo["limitacoes"]
    esquema = esquema_rascunho_definir()
    assert esquema["additionalProperties"] is False
    item = esquema["properties"]["definicoes"]["items"]
    assert item["additionalProperties"] is False
    assert esquema["properties"]["definicoes"]["maxItems"] == 6
    assert item["properties"]["metrica"]["properties"]["operacao"]["enum"] == sorted(OPERACOES_EXECUCAO)
    assert "sql" not in item["properties"]
    assert "python" not in item["properties"]
    assert "Nao devolva SQL" in instrucao_de_rascunho()
    assert SENTINELA not in instrucao_de_rascunho()

    contagem_pacote = copy.deepcopy(pacote)
    grafico_contagem = next(
        item for item in contagem_pacote["graficos"] if item["grafico"] == "indicador" and item["operacao"] == "soma"
    )
    grafico_contagem["operacao"] = "contagem"
    grafico_contagem["leitura"] = "contagem_de_registros"
    grafico_contagem["agrupamento"] = None
    grafico_contagem["segunda_medida"] = None
    contagem_pacote["graficos"] = [grafico_contagem]
    contagem = definir(perfil, contagem_pacote)
    indicador_contagem = contagem["definicoes"][0]
    assert indicador_contagem["status"] == "definida"
    assert indicador_contagem["metrica"]["operacao"] == "contagem"
    assert indicador_contagem["metrica"]["unidade"] == "contagem"
    assert indicador_contagem["metrica"]["coluna"]["indice"] == grafico_contagem["medida"]["indice"]
    assert indicador_contagem["granularidade"] == "total"
    assert indicador_contagem["leitura"] == "contagem_de_registros"
    assert indicador_contagem["dimensoes"] == []
    assert indicador_contagem["filtros"] == []
    assert indicador_contagem["juncao"] == "nao_executada"
    assert indicador_contagem["esclarecimento"] is None


def test_rejeita_json_invalido(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.json@test.com", "definir-json")
    contexto = montar_contexto_definir(perfil, pacote)
    for bruto in (None, [], "definicao", {"contrato": CONTRATO_DEFINIR}, {"definicoes": []}):
        with pytest.raises(DefinirErro) as erro:
            validar_saida(contexto, bruto)
        assert erro.value.codigo == "contrato_saida_invalido"

    def rascunho_quebrado(_contexto):
        return {"definicoes": [{"status": "definida"}]}

    with pytest.raises(DefinirErro) as quebrado:
        definir(perfil, pacote, provedor=rascunho_quebrado)
    assert quebrado.value.codigo == "contrato_saida_invalido"

    def texto(_contexto):
        return "isto nao e json"

    with pytest.raises(DefinirErro) as invalido:
        definir(perfil, pacote, provedor=texto)
    assert invalido.value.codigo == "contrato_saida_invalido"


def test_rejeita_referencia_inexistente(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.ref@test.com", "definir-ref")
    inexistente = copy.deepcopy(pacote)
    inexistente["graficos"][0]["medida"] = dict(inexistente["graficos"][0]["medida"])
    inexistente["graficos"][0]["medida"]["indice"] = 99

    def nao_chamar(_contexto):
        raise AssertionError("provedor")

    with pytest.raises(DefinirErro) as erro:
        definir(perfil, inexistente, provedor=nao_chamar)
    assert erro.value.codigo == "referencia_inexistente"

    cruzada = copy.deepcopy(pacote)
    outra = next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 1)
    grafico = next(item for item in cruzada["graficos"] if item["regiao"]["aba"] == 0)
    grafico["medida"] = {
        "arquivo": outra["arquivo"],
        "aba": outra["aba"],
        "regiao": outra["regiao"],
        "indice": outra["indice"],
    }
    with pytest.raises(DefinirErro) as erro_cruzada:
        definir(perfil, cruzada, provedor=nao_chamar)
    assert erro_cruzada.value.codigo == "juncao_nao_confirmada"


def test_rejeita_agregacao_e_unidade_incompativeis(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.tipo@test.com", "definir-tipo")
    texto = next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 0 and "texto" in coluna["tipos"] and coluna["dimensao_principal"] not in {"valor", "quantidade"})
    incompativel = copy.deepcopy(pacote)
    grafico = next(item for item in incompativel["graficos"] if item["grafico"] == "indicador" and item["operacao"] == "soma")
    grafico["medida"] = {
        "arquivo": texto["arquivo"],
        "aba": texto["aba"],
        "regiao": texto["regiao"],
        "indice": texto["indice"],
    }
    grafico["agrupamento"] = None
    grafico["segunda_medida"] = None
    with pytest.raises(DefinirErro) as erro:
        definir(perfil, incompativel)
    assert erro.value.codigo == "operacao_incompativel"

    contexto = montar_contexto_definir(perfil, pacote)
    saida = definir(perfil, pacote)
    unidade = copy.deepcopy(saida)
    indicador = next(item for item in unidade["definicoes"] if item["grafico"] == "indicador" and item["metrica"]["operacao"] == "soma")
    indicador["metrica"]["unidade"] = "quantidade"
    with pytest.raises(DefinirErro) as erro_unidade:
        validar_saida(contexto, unidade)
    assert erro_unidade.value.codigo == "unidade_incompativel"

    grano = copy.deepcopy(saida)
    linha = next(item for item in grano["definicoes"] if item["grafico"] == "linha")
    linha["granularidade"] = "categoria"
    with pytest.raises(DefinirErro) as erro_grano:
        validar_saida(contexto, grano)
    assert erro_grano.value.codigo == "granularidade_incompativel"


def test_bloqueia_ambiguidade_nao_resolvida(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.ambigua@test.com", "definir-ambigua")
    ambiguo = copy.deepcopy(perfil)
    for item in ambiguo["qualidade"]["por_regiao"]:
        item["datas"]["ambiguas"] = 1
    saida = definir(ambiguo, pacote)
    linha = _por(saida, "linha", "soma")
    assert linha["status"] == "bloqueada"
    assert linha["esclarecimento"] == "resolver_data_ambigua"
    indicador = _por(saida, "indicador", "soma")
    assert indicador["status"] == "definida"
    assert indicador["esclarecimento"] is None

    def forcar(contexto):
        proposta = __import__(
            "app.services.agente_bi_definir_service",
            fromlist=["provedor_simulado"],
        ).provedor_simulado(contexto)
        for item in proposta["definicoes"]:
            if item["grafico"] == "linha":
                item["status"] = "definida"
                item["esclarecimento"] = None
        return proposta

    with pytest.raises(DefinirErro) as erro:
        definir(ambiguo, pacote, provedor=forcar)
    assert erro.value.codigo == "ambiguidade_nao_resolvida"

    sem_unidade = copy.deepcopy(perfil)
    for coluna in sem_unidade["colunas"]:
        if coluna["dimensao_principal"] == "valor":
            coluna["dimensao_principal"] = "indefinida"
    bloqueada = definir(sem_unidade, pacote)
    soma = _por(bloqueada, "indicador", "soma")
    assert soma["status"] == "bloqueada"
    assert soma["esclarecimento"] == "informar_unidade"
    assert soma["metrica"]["unidade"] is None


def test_rejeita_mais_de_seis_graficos(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.teto@test.com", "definir-teto")
    excesso = copy.deepcopy(pacote)
    while len(excesso["graficos"]) < 7:
        item = copy.deepcopy(excesso["graficos"][0])
        item["id"] = f"s{len(excesso['graficos']) + 1}"
        excesso["graficos"].append(item)
    for indice, item in enumerate(excesso["graficos"], start=1):
        item["id"] = f"s{indice}"

    def nao_chamar(_contexto):
        raise AssertionError("provedor")

    with pytest.raises(DefinirErro) as erro:
        definir(perfil, excesso, provedor=nao_chamar)
    assert erro.value.codigo == "limite_graficos"


def test_nao_executa_sql_nem_python(ctx, monkeypatch):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.exec@test.com", "definir-exec")
    executou = {"valor": False}

    def bomba(*_args, **_kwargs):
        executou["valor"] = True
        raise AssertionError("executou")

    monkeypatch.setattr("builtins.exec", bomba)
    monkeypatch.setattr("builtins.eval", bomba)

    def com_sql(_contexto):
        return {"definicoes": [], "sql": "SELECT 1"}

    with pytest.raises(DefinirErro) as erro_sql:
        definir(perfil, pacote, provedor=com_sql)
    assert erro_sql.value.codigo == "expressao_nao_permitida"

    def com_python(_contexto):
        return {"python": "import os\nprint(1)"}

    with pytest.raises(DefinirErro) as erro_python:
        definir(perfil, pacote, provedor=com_python)
    assert erro_python.value.codigo == "expressao_nao_permitida"

    def com_texto(_contexto):
        return "SELECT * FROM fretes"

    with pytest.raises(DefinirErro) as erro_texto:
        definir(perfil, pacote, provedor=com_texto)
    assert erro_texto.value.codigo == "expressao_nao_permitida"
    assert executou["valor"] is False

    fonte = (ROOT / "app" / "services" / "agente_bi_definir_service.py").read_text(encoding="utf-8")
    assert "chamar_anthropic_governado" not in fonte
    assert "sqlite3" not in fonte
    worker = (ROOT / "app" / "run_agente_bi_worker.py").read_text(encoding="utf-8")
    assert "agente_bi_definir_service" not in worker


def test_filtros_parametros_e_juncao(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("definir.filtro@test.com", "definir-filtro")
    contexto = montar_contexto_definir(perfil, pacote)
    saida = definir(perfil, pacote)
    valor = _por(saida, "indicador", "soma")
    filtrada = copy.deepcopy(saida)
    alvo = next(item for item in filtrada["definicoes"] if item["id"] == valor["id"])
    alvo["filtros"] = [
        {
            "coluna": dict(alvo["metrica"]["coluna"]),
            "operador": "maior_ou_igual",
            "parametro": "teto",
            "parametro_fim": None,
        }
    ]
    alvo["parametros"] = [{"nome": "teto", "tipo": "numero", "obrigatorio": True}]
    aceita = validar_saida(contexto, filtrada)
    assert any(item["filtros"] and item["filtros"][0]["parametro"] == "teto" for item in aceita["definicoes"])

    tipo_errado = copy.deepcopy(filtrada)
    alvo_tipo = next(item for item in tipo_errado["definicoes"] if item["metrica"]["operacao"] == "soma" and item["grafico"] == "indicador")
    alvo_tipo["parametros"][0]["tipo"] = "texto"
    with pytest.raises(DefinirErro) as erro_tipo:
        validar_saida(contexto, tipo_errado)
    assert erro_tipo.value.codigo == "filtro_incompativel"

    incompleto = copy.deepcopy(saida)
    alvo_incompleto = next(item for item in incompleto["definicoes"] if item["grafico"] == "indicador" and item["metrica"]["operacao"] == "soma")
    alvo_incompleto["filtros"] = [
        {
            "coluna": dict(alvo_incompleto["metrica"]["coluna"]),
            "operador": "entre",
            "parametro": "inicio",
            "parametro_fim": None,
        }
    ]
    alvo_incompleto["parametros"] = [{"nome": "inicio", "tipo": "numero", "obrigatorio": True}]
    with pytest.raises(DefinirErro) as erro_incompleto:
        validar_saida(contexto, incompleto)
    assert erro_incompleto.value.codigo == "ambiguidade_nao_resolvida"

    alvo_incompleto["status"] = "bloqueada"
    alvo_incompleto["esclarecimento"] = "informar_parametro"
    bloqueado = validar_saida(contexto, incompleto)
    achado = next(item for item in bloqueado["definicoes"] if item["id"] == alvo_incompleto["id"])
    assert achado["status"] == "bloqueada"
    assert achado["esclarecimento"] == "informar_parametro"

    if contexto["relacoes"]:
        juncao = copy.deepcopy(saida)
        alvo_juncao = next(item for item in juncao["definicoes"] if item["grafico"] == "indicador" and item["metrica"]["operacao"] == "soma")
        alvo_juncao["relacionamentos"] = [{"indice": 0, "juncao": "nao_executada"}]
        with pytest.raises(DefinirErro) as erro_juncao:
            validar_saida(contexto, juncao)
        assert erro_juncao.value.codigo == "ambiguidade_nao_resolvida"
        alvo_juncao["status"] = "bloqueada"
        alvo_juncao["esclarecimento"] = "confirmar_relacao_candidata"
        confirmada = validar_saida(contexto, juncao)
        bloqueada = next(item for item in confirmada["definicoes"] if item["id"] == alvo_juncao["id"])
        assert bloqueada["status"] == "bloqueada"
        assert bloqueada["juncao"] == "nao_executada"
        assert bloqueada["relacionamentos"] == [{"indice": 0, "juncao": "nao_executada"}]


def test_isolamento_autorizacao_e_adaptador_sem_chamada(ctx, monkeypatch):
    usuario, franquia, escopo, perfil, pacote, _aprovada = _jornada("definir.iso@test.com", "definir-iso")
    _outra_conta, _outra_franquia, outro = _par("definir-outro", "definir.outro@test.com")
    colega = seed_usuario(franquia.id, usuario.conta_id, email="definir.colega@test.com")
    outro_escopo = escopo_do_usuario(outro)
    colega_escopo = escopo_do_usuario(colega)
    tentativas = IaChamadaTentativa.query.count()
    execucoes = AgenteBiExecucao.query.count()
    paineis = AgenteBiDashboard.query.count()

    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    gravada = definir_selecao(escopo, {"analise_id": pacote["id"]})
    assert gravada["definicao"]["etapa"] == "definicao"
    assert gravada["definicao"]["contrato_versao"] == CONTRATO_DEFINIR
    assert gravada["definicao"]["usuario_id"] == escopo.usuario_id
    assert gravada["definicao"]["resultado"]["chamada_anthropic"] is False
    assert SENTINELA not in json.dumps(gravada)
    assert IaChamadaTentativa.query.count() == tentativas
    assert AgenteBiExecucao.query.count() == execucoes
    assert AgenteBiDashboard.query.count() == paineis
    relida = obter_analise(escopo, gravada["definicao"]["id"])
    assert relida["resultado"]["contrato"] == CONTRATO_DEFINIR
    assert relida["contexto"]["contrato"] == CONTRATO_CONTEXTO_DEFINIR
    assert relida["contexto"]["apto_anthropic"] is False
    with pytest.raises(Exception) as outro_erro:
        definir_selecao(outro_escopo, {"analise_id": pacote["id"]})
    assert getattr(outro_erro.value, "codigo", "") == "nao_encontrado"
    with pytest.raises(Exception) as colega_erro:
        obter_analise(colega_escopo, gravada["definicao"]["id"])
    assert getattr(colega_erro.value, "codigo", "") == "nao_encontrado"

    fronteira = preparar_fronteira(perfil, pacote, modelo="")
    assert fronteira["chamada_anthropic"] is False
    assert fronteira["liberado"] is True
    assert fronteira["saida_estruturada"] is None
    assert SENTINELA not in fronteira["mensagem"]
    assert "valores_de_celula" in fronteira["mensagem"]
    pedido = argumentos_do_adaptador(fronteira, escopo, chave_idempotencia="definir.local.teste")
    assert pedido["invocar"] is False
    assert pedido["usuario_id"] == escopo.usuario_id
    assert pedido["conta_id"] == escopo.conta_id
    assert pedido["franquia_id"] == escopo.franquia_id
    assert pedido["transporte"] is None
    assinatura = set(inspect.signature(chamar_anthropic_governado).parameters)
    assert set(pedido) - {"invocar"} <= assinatura

    promovido = copy.deepcopy(perfil)
    promovido["governanca"]["apto_anthropic"] = True
    with pytest.raises(DefinirErro) as indevido:
        definir(promovido, pacote)
    assert indevido.value.codigo == "autorizacao_indevida"

    def provedor_recusado(_contexto):
        raise AssertionError("anthropic")

    provedor_recusado.__name__ = "chamar_anthropic_governado"
    with pytest.raises(DefinirErro) as recusa:
        definir(perfil, pacote, provedor=provedor_recusado)
    assert recusa.value.codigo == "provedor_nao_autorizado"
    assert AgenteBiAnalise.query.filter_by(contrato_versao=CONTRATO_DEFINIR).count() == 1


def test_api_grava_reserva_contrato_e_nao_chama_anthropic(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)

    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    with app.app_context():
        _conta, _franquia, usuario = _par("definir-api", "definir.api@test.com")
        _outra, _outra_franquia, outro = _par("definir-api-outro", "definir.api.outro@test.com")
        usuario_id = usuario.id
        outro_id = outro.id
    assert client.post("/api/agente-bi/definicao", json={"analise_id": 1}).status_code == 401
    _entrar(client, usuario_id)
    preparacao = _postar(client, _movimento(), "movimento.xlsx")
    assert preparacao.status_code == 201
    dataset_id = preparacao.get_json()["dataset"]["id"]
    descoberta = _descobrir(client, dataset_id, {"modalidades": ["A"]})
    aprovacao = _aprovar(
        client,
        descoberta["analise_id"],
        _viaveis(descoberta["resultado"])[:6],
        descoberta["resultado"],
    )
    assert aprovacao.status_code == 201
    selecao_id = aprovacao.get_json()["selecao"]["id"]
    resposta = client.post("/api/agente-bi/definicao", json={"analise_id": selecao_id})
    assert resposta.status_code == 201, resposta.get_data(as_text=True)
    corpo = resposta.get_json()
    assert corpo["definicao"]["contrato_versao"] == CONTRATO_DEFINIR
    assert corpo["definicao"]["resultado"]["execucao"] == "nao_executada"
    assert SENTINELA not in resposta.get_data(as_text=True)
    reservado = client.post("/api/agente-bi/analises", json={"contrato_versao": CONTRATO_DEFINIR})
    assert reservado.status_code == 422
    assert reservado.get_json()["codigo"] == "contrato_reservado"
    contexto = client.post(
        "/api/agente-bi/analises",
        json={"contexto": {"contrato": CONTRATO_CONTEXTO_DEFINIR}},
    )
    assert contexto.status_code == 422
    assert contexto.get_json()["codigo"] == "contrato_reservado"
    _entrar(client, outro_id)
    alheio = client.post("/api/agente-bi/definicao", json={"analise_id": selecao_id})
    assert alheio.status_code == 404
    assert alheio.get_json()["codigo"] == "nao_encontrado"
    fonte = fonte_sem_docstring_de_modulo(ROOT / "app" / "agente_bi_api_routes.py")
    assert "def " in fonte
    assert "definir.v1" not in fonte
    assert "chamar_anthropic" not in fonte
