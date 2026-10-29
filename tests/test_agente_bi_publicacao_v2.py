"""SCRUM-269: publicação do pedido livre, da interface até a revisão seguinte.

O transporte é simulado. A planilha sintética tem gabarito conhecido.
Nenhuma chamada usa o SDK da Anthropic.
"""
from __future__ import annotations

import copy
import inspect
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiAplicacao, AgenteBiDashboard, Franquia, IaChamadaTentativa, utcnow_naive
from app.services.agente_bi_atualizacao_service import atualizar_dashboard
from app.services.agente_bi_dashboard_service import consultar_dashboard, filtrar_dashboard
from app.services.agente_bi_evolucao_service import EvolucaoErro, evoluir_dashboard
from app.services.agente_bi_ingestao_service import ArquivoBruto
from app.services.agente_bi_interpretacao_v2_service import InterpretacaoErro
from app.services.agente_bi_publicacao_v2_service import PublicacaoErro, aplicar_pedido
from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno
from tests.test_agente_bi_definir import _pacote
from tests.test_agente_bi_execucao_v2 import _dividir
from tests.test_agente_bi_ingestao import _cliente, _entrar, _par, _xlsx
from tests.test_agente_bi_interpretacao_v2 import _operacao, _plano, _resposta

TEXTO = "troque o indicador para média e filtre a categoria"
TEXTO_SOMA = "troque o indicador para soma"
TEXTO_COMPOSTO = "mude o indicador para média e crie uma contagem"
TEXTO_MEDIA = "troque o indicador para média"


@pytest.fixture(autouse=True)
def _transporte_simulado(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def _preparar(email, slug):
    from app.models import CleitonCostConfig, Conta
    from app.services.agente_bi_analisar_service import analisar_dataset
    from app.services.agente_bi_calculo_service import calcular_definicao
    from app.services.agente_bi_dashboard_service import montar_dashboard
    from app.services.agente_bi_definir_service import definir
    from app.services.agente_bi_descoberta_service import aprovar_selecao
    from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
    from app.services.agente_bi_persistencia_service import escopo_do_usuario
    from tests.test_agente_bi_calculo import _gravar
    from tests.test_agente_bi_execucao_v2 import _planilha
    from tests.test_agente_bi_ingestao import _ingerir

    _conta, _franquia, usuario = _par(slug, email)
    escopo = escopo_do_usuario(usuario)
    ingerido = _ingerir(usuario, _planilha(), "movimento.xlsx", chave=f"lote-{slug}")
    perfil = perfilar_dataset(escopo, ingerido["dataset"]["id"])
    gravado = analisar_dataset(
        escopo,
        ingerido["dataset"]["id"],
        chave=ingerido["dataset"]["chave"],
        rodada=1,
        pedido={"modalidades": ["A"]},
    )
    descoberta = gravado["resultado"]
    soma = next(
        item
        for item in descoberta["sugestoes"]
        if item["status"] == "viavel"
        and item["grafico"] == "indicador"
        and item["operacao"] == "soma"
        and item["medida"]["aba"] == 0
        and item["medida"]["indice"] == 1
        and item["juncao"] == "nao_executada"
    )
    aprovada = aprovar_selecao(
        escopo,
        {
            "analise_id": gravado["analise"]["id"],
            "sugestoes": [soma["id"]],
            "perguntas": [{"id": item["id"], "confirmada": True} for item in descoberta["perguntas"]],
            "confirmacao": True,
        },
    )
    pacote = _pacote(aprovada)
    documento = definir(perfil, pacote)
    salva = _gravar(escopo, pacote["id"], documento)
    calculado = calcular_definicao(escopo, {"analise_id": salva["id"]})
    painel = montar_dashboard(escopo, {"analise_id": calculado["calculo"]["id"]})
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    franquia = db.session.get(Franquia, escopo.franquia_id)
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    return {
        "escopo": escopo,
        "dashboard_id": painel["dashboard"]["id"],
        "revisao": painel["dashboard"]["revisao"],
    }


def _por_indice(mensagem, indice):
    return next(
        item["referencia"]
        for item in mensagem["colunas"]
        if item["referencia"]["aba"] == 0 and item["referencia"]["indice"] == indice
    )


def _enviar(montar):
    estado = {"n": 0}

    def transporte(pedido):
        import json

        estado["n"] += 1
        mensagem = json.loads(pedido["messages"][0]["content"])
        return _resposta(montar(mensagem))

    return transporte, estado


def _resultado(consulta, operacao):
    return next(item for item in consulta["graficos"] if item["operacao"] == operacao)


def _planilha_nova():
    return _xlsx(
        {
            "movimento": [
                ["categoria", "parte", "todo", "quando"],
                ["norte", 5, 4, date(2024, 1, 10)],
                ["norte", 5, 4, date(2024, 1, 20)],
                ["sul", 5, 2, date(2024, 2, 1)],
            ]
        }
    )


def _media_e_filtro(mensagem):
    grafico = mensagem["graficos"][0]["id"]
    return _plano(
        [
            _operacao(id="o1", acao="alterar_indicador", grafico=grafico, operacao="media"),
            _operacao(
                id="o2",
                acao="definir_filtro",
                grafico=grafico,
                coluna=_por_indice(mensagem, 0),
                operador="igual",
                parametros=[{"forma": "tipado", "nome": "faixa", "tipo": "texto"}],
            ),
        ]
    )


def _so_soma(mensagem):
    return _plano(
        [
            _operacao(
                id="o1",
                acao="alterar_indicador",
                grafico=mensagem["graficos"][0]["id"],
                operacao="soma",
            )
        ]
    )


def _so_media(mensagem):
    return _plano(
        [
            _operacao(
                id="o1",
                acao="alterar_indicador",
                grafico=mensagem["graficos"][0]["id"],
                operacao="media",
            )
        ]
    )


def _composto(mensagem):
    grafico = mensagem["graficos"][0]["id"]
    return _plano(
        [
            _operacao(id="o1", acao="alterar_indicador", grafico=grafico, operacao="media"),
            _operacao(
                id="o2",
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_por_indice(mensagem, 1),
                operacao="contagem",
                apresentacao="indicador",
            ),
        ]
    )


def test_jornada_publica_reabre_filtra_atualiza_e_continua(ctx):
    base = _preparar("publicacao.jornada@test.com", "publicacao-jornada")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    paineis = AgenteBiDashboard.query.count()
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == 0

    transporte, estado = _enviar(_media_e_filtro)
    sem_valor = aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    assert sem_valor["situacao"] == "esclarecimento"
    assert sem_valor["esclarecimento"]["motivo"] == "informar_parametro"
    assert sem_valor["esclarecimento"]["parametros"] == ["faixa"]
    assert "norte" not in sem_valor["esclarecimento"]["pergunta"]
    assert AgenteBiDashboard.query.count() == paineis
    assert estado["n"] == 1

    publicado = aplicar_pedido(
        escopo,
        original,
        {"texto": TEXTO, "parametros": {"faixa": "norte"}},
        transporte=transporte,
    )
    assert publicado["ok"] is True
    assert publicado["situacao"] == "aplicada"
    assert publicado["reusada"] is False
    assert publicado["chamada_anthropic"] is False
    grafico = _resultado(publicado["consulta"], "media")
    assert grafico["resultado"] == "2"
    assert grafico["elegiveis"] == 2
    novo = publicado["dashboard"]["id"]
    assert novo != original
    assert publicado["revisao_anterior_id"] == original
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"
    assert AgenteBiDashboard.query.count() == paineis + 1

    repetido = aplicar_pedido(
        escopo,
        original,
        {"texto": TEXTO, "parametros": {"faixa": "norte"}},
        transporte=transporte,
    )
    assert repetido["reusada"] is True
    assert repetido["dashboard"]["id"] == novo
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert estado["n"] == 1

    tentativas = IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count()
    reaberto = consultar_dashboard(escopo, novo)
    assert _resultado(reaberto["consulta"], "media")["resultado"] == "2"
    filtrado = filtrar_dashboard(escopo, novo, {"parametros": {"faixa": "sul"}})
    assert filtrado["persistido"] is False
    assert filtrado["dashboard"]["id"] == novo
    assert _resultado(filtrado["consulta"], "media")["resultado"] == "1"
    assert _resultado(filtrado["consulta"], "media")["elegiveis"] == 1
    assert AgenteBiDashboard.query.count() == paineis + 1

    atualizado = atualizar_dashboard(
        escopo,
        novo,
        modo="substituir",
        arquivos=[ArquivoBruto(nome="movimento.xlsx", mime=None, conteudo=_planilha_nova())],
        parametros={"faixa": "norte"},
    )
    assert atualizado["recalculado"] is True
    assert atualizado["dashboard"]["id"] != novo
    assert _resultado(atualizado["consulta"], "media")["resultado"] == "5"
    assert _resultado(atualizado["consulta"], "media")["elegiveis"] == 2
    assert consultar_dashboard(escopo, novo)["consulta"]["graficos"][0]["resultado"] == "2"
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == tentativas

    seguinte = aplicar_pedido(
        escopo,
        atualizado["dashboard"]["id"],
        {"texto": TEXTO_SOMA, "parametros": {"faixa": "norte"}},
        transporte=_enviar(_so_soma)[0],
    )
    assert seguinte["situacao"] == "aplicada"
    assert seguinte["revisao_anterior_id"] == atualizado["dashboard"]["id"]
    assert _resultado(seguinte["consulta"], "soma")["resultado"] == "10"
    assert _resultado(seguinte["consulta"], "soma")["elegiveis"] == 2
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"
    assert consultar_dashboard(escopo, novo)["consulta"]["graficos"][0]["resultado"] == "2"

    with pytest.raises(EvolucaoErro) as recusa:
        evoluir_dashboard(escopo, novo, {"texto": "filtre a categoria igual a sul"})
    assert recusa.value.codigo == "analise_indisponivel"
    assert consultar_dashboard(escopo, novo)["consulta"]["graficos"][0]["resultado"] == "2"


def test_pedido_composto_publica_os_graficos_juntos(ctx):
    base = _preparar("publicacao.composto@test.com", "publicacao-composto")
    escopo = base["escopo"]
    paineis = AgenteBiDashboard.query.count()
    transporte, _estado = _enviar(_composto)
    publicado = aplicar_pedido(escopo, base["dashboard_id"], {"texto": TEXTO_COMPOSTO}, transporte=transporte)
    assert publicado["situacao"] == "aplicada"
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert _resultado(publicado["consulta"], "media")["resultado"] == _dividir("5", "3")
    assert _resultado(publicado["consulta"], "contagem")["resultado"] == "3"
    assert len(publicado["consulta"]["graficos"]) == 2
    assert consultar_dashboard(escopo, base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"


def test_operacao_invalida_nao_publica(ctx):
    base = _preparar("publicacao.invalida@test.com", "publicacao-invalida")
    escopo = base["escopo"]
    paineis = AgenteBiDashboard.query.count()
    aplicacoes = AgenteBiAplicacao.query.count()

    def grafico_ausente(mensagem):
        return _plano([_operacao(grafico="g9")])

    with pytest.raises(InterpretacaoErro) as erro:
        aplicar_pedido(escopo, base["dashboard_id"], {"texto": "consulte o gráfico ausente"}, transporte=_enviar(grafico_ausente)[0])
    assert erro.value.codigo == "referencia_inexistente"
    assert AgenteBiDashboard.query.count() == paineis
    assert AgenteBiAplicacao.query.count() == aplicacoes
    assert consultar_dashboard(escopo, base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"


def test_falha_no_fim_desfaz_a_publicacao(ctx, monkeypatch):
    base = _preparar("publicacao.falha@test.com", "publicacao-falha")
    escopo = base["escopo"]
    paineis = AgenteBiDashboard.query.count()
    aplicacoes = AgenteBiAplicacao.query.count()

    def estourar(*_args, **_kwargs):
        raise RuntimeError("concorrencia")

    monkeypatch.setattr("app.services.agente_bi_publicacao_v2_service.concluir_aplicacao", estourar)
    with pytest.raises(RuntimeError, match="concorrencia"):
        aplicar_pedido(escopo, base["dashboard_id"], {"texto": TEXTO_MEDIA}, transporte=_enviar(_so_media)[0])
    assert AgenteBiDashboard.query.count() == paineis
    assert AgenteBiAplicacao.query.count() == aplicacoes
    assert consultar_dashboard(escopo, base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"


def test_revisao_concorrente_nao_cria_ramo(ctx, monkeypatch):
    base = _preparar("publicacao.ramo@test.com", "publicacao-ramo")
    escopo = base["escopo"]
    paineis = AgenteBiDashboard.query.count()

    def revisao_ocupada(*_args, **_kwargs):
        return base["revisao"] + 2

    monkeypatch.setattr("app.services.agente_bi_publicacao_v2_service._proxima_revisao", revisao_ocupada)
    with pytest.raises(PublicacaoErro) as erro:
        aplicar_pedido(escopo, base["dashboard_id"], {"texto": TEXTO_MEDIA}, transporte=_enviar(_so_media)[0])
    assert erro.value.codigo == "conflito_versao"
    assert AgenteBiDashboard.query.count() == paineis
    assert AgenteBiAplicacao.query.filter_by(usuario_id=escopo.usuario_id, estado="concluida").count() == 0
    assert consultar_dashboard(escopo, base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"


def test_v1_continua_consultavel_sem_modelo(ctx):
    base = _preparar("publicacao.v1@test.com", "publicacao-v1")
    antes = IaChamadaTentativa.query.filter_by(franquia_id=base["escopo"].franquia_id).count()
    consulta = consultar_dashboard(base["escopo"], base["dashboard_id"])
    filtrado = filtrar_dashboard(base["escopo"], base["dashboard_id"], {"parametros": {}})
    assert consulta["consulta"]["graficos"][0]["resultado"] == "5"
    assert filtrado["consulta"]["graficos"][0]["resultado"] == "5"
    assert filtrado["persistido"] is False
    assert IaChamadaTentativa.query.filter_by(franquia_id=base["escopo"].franquia_id).count() == antes


def test_rota_publica_aplica_e_repete_sem_duplicar(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        base = _preparar("publicacao.rota@test.com", "publicacao-rota")
        usuario_id = base["escopo"].usuario_id
        identificador = base["dashboard_id"]
        media = _dividir("5", "3")
    transporte, estado = _enviar(_so_media)
    import app.services.agente_bi_publicacao_v2_service as publicacao

    real = publicacao.aplicar_pedido

    def envolvido(escopo, dashboard_id, corpo, transporte=None):
        return real(escopo, dashboard_id, corpo, transporte=transporte or envolvido.transporte)

    envolvido.transporte = transporte
    monkeypatch.setattr(publicacao, "aplicar_pedido", envolvido)

    assert client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": TEXTO_MEDIA}).status_code == 401
    _entrar(client, usuario_id)
    resposta = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": TEXTO_MEDIA})
    assert resposta.status_code == 201, resposta.get_data(as_text=True)
    corpo = resposta.get_json()
    assert corpo["situacao"] == "aplicada"
    assert corpo["consulta"]["graficos"][0]["resultado"] == media
    assert corpo["consulta"]["graficos"][0]["operacao"] == "media"
    novo = corpo["dashboard"]["id"]
    aberto = client.get(f"/api/agente-bi/dashboards/{novo}/consulta")
    assert aberto.status_code == 200
    assert aberto.get_json()["consulta"]["graficos"][0]["resultado"] == media
    anterior = client.get(f"/api/agente-bi/dashboards/{identificador}/consulta")
    assert anterior.get_json()["consulta"]["graficos"][0]["resultado"] == "5"
    repetido = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": TEXTO_MEDIA})
    assert repetido.status_code == 200
    assert repetido.get_json()["reusada"] is True
    assert repetido.get_json()["dashboard"]["id"] == novo
    assert estado["n"] == 1


def test_caminho_publico_nao_usa_classificador():
    from app.agente_bi_api_routes import post_dashboard_evolucao
    from pathlib import Path

    fonte = inspect.getsource(post_dashboard_evolucao)
    modulo = Path(__file__).resolve().parents[1] / "app" / "services" / "agente_bi_publicacao_v2_service.py"
    texto = modulo.read_text(encoding="utf-8")
    assert "_classificar" not in fonte
    assert "evoluir_dashboard" not in fonte
    assert "_classificar" not in texto
    assert "aplicar_pedido" in fonte


def _contratos_v2_reservados():
    from app.services.agente_bi_calculo_service import CONTRATO_CALCULO_V2
    from app.services.agente_bi_contrato_v2_service import CONTRATO_DEFINIR_V2, CONTRATO_PATCH_V2
    from app.services.agente_bi_interpretacao_v2_service import CONTRATO_CONTEXTO_PLANO
    from app.services.agente_bi_leitura_service import CONTRATO_CONTEXTO_DEFINICAO_V2

    return (
        CONTRATO_CALCULO_V2,
        CONTRATO_DEFINIR_V2,
        CONTRATO_PATCH_V2,
        CONTRATO_CONTEXTO_PLANO,
        CONTRATO_CONTEXTO_DEFINICAO_V2,
    )


def _memoria_calculo_v2_legitima():
    from app.services.agente_bi_calculo_service import CONTRATO_CALCULO_V2, calcular_definicao_v2
    from tests.test_agente_bi_execucao_v2 import _base, _compor, _criar, _persistir_plano, _persistir_v2, _por_id, _soma

    base = _base(
        "reserva.calculo.v2@test.com",
        "reserva-calculo-v2",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    parte = base["colunas"]["parte"]
    definicao = _compor(base, [_criar("o1", "t1", parte, "soma", "indicador")])
    plano_id = _persistir_plano(base, definicao["plano"])["id"]
    calculado = calcular_definicao_v2(
        base["escopo"],
        {"definicao": definicao, "alvos": ["g2"], "plano_id": plano_id},
    )
    memoria = calculado["calculo"]
    assert memoria["contrato"] == CONTRATO_CALCULO_V2
    assert _por_id(memoria, "g2")["resultado"] == "5"
    definicao_v2 = _persistir_v2(base, definicao)
    return base, memoria, definicao_v2, plano_id


def _proxima_rodada(escopo, chave: str) -> int:
    from sqlalchemy import func

    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            chave=chave,
        )
        .scalar()
    )
    return 1 if atual is None else int(atual) + 1


def test_api_generica_recusa_calculo_v2_adulterado(app, monkeypatch):
    from app.services.agente_bi_calculo_service import CONTRATO_CALCULO_V2, EXECUTOR
    from app.services.agente_bi_dashboard_service import consultar_dashboard

    client = _cliente(app, monkeypatch)
    with app.app_context():
        base, memoria, definicao_v2, plano_id = _memoria_calculo_v2_legitima()
        assert consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"
        usuario_id = base["escopo"].usuario_id
        rodada = _proxima_rodada(base["escopo"], base["chave"])
        adulterada = copy.deepcopy(memoria)
        indicador = next(item for item in adulterada["indicadores"] if item["resultado"] == "5" and item["id"] == "g2")
        indicador["resultado"] = "999"
        adulterada["origem_definicao_id"] = definicao_v2["id"]
        adulterada["persistido"] = True
        contexto = {
            "definicao_id": definicao_v2["id"],
            "plano_id": plano_id,
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
        }
        contexto.pop("contrato", None)
        corpo = {
            "chave": base["chave"],
            "rodada": rodada,
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": "execucao",
            "estado": "concluida",
            "contexto": contexto,
            "regras_negocio": {"executor": EXECUTOR, "ia_calculo": False, "plano_id": plano_id},
            "esclarecimentos": {"bloqueios": []},
            "contrato_versao": CONTRATO_CALCULO_V2,
            "resultado": adulterada,
        }
        assert "contrato" not in corpo["contexto"]
        assert corpo["resultado"]["contrato"] == CONTRATO_CALCULO_V2
        antes_analises = AgenteBiAnalise.query.count()
        antes_paineis = AgenteBiDashboard.query.count()
        dashboard_id = base["dashboard_id"]
        escopo = base["escopo"]

    _entrar(client, usuario_id)
    resposta = client.post("/api/agente-bi/analises", json=corpo)
    assert resposta.status_code == 422
    assert resposta.get_json()["codigo"] == "contrato_reservado"
    assert "999" not in resposta.get_data(as_text=True)

    with app.app_context():
        assert AgenteBiAnalise.query.count() == antes_analises
        assert AgenteBiDashboard.query.count() == antes_paineis
        assert AgenteBiAnalise.query.filter(AgenteBiAnalise.resultado_json.contains('"999"')).count() == 0
        assert consultar_dashboard(escopo, dashboard_id)["consulta"]["graficos"][0]["resultado"] == "5"


def test_api_generica_reserva_todos_os_contratos_v2(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    contratos = _contratos_v2_reservados()
    with app.app_context():
        _conta, _franquia, usuario = _par("reserva-v2", "reserva.v2@test.com")
        usuario_id = usuario.id
        antes_analises = AgenteBiAnalise.query.count()
        antes_paineis = AgenteBiDashboard.query.count()
    _entrar(client, usuario_id)
    for contrato in contratos:
        por_versao = client.post("/api/agente-bi/analises", json={"contrato_versao": contrato})
        assert por_versao.status_code == 422, contrato
        assert por_versao.get_json()["codigo"] == "contrato_reservado"
        por_bloco = client.post("/api/agente-bi/analises", json={"contexto": {"contrato": contrato}})
        assert por_bloco.status_code == 422, contrato
        assert por_bloco.get_json()["codigo"] == "contrato_reservado"
        por_resultado = client.post("/api/agente-bi/analises", json={"resultado": {"contrato": contrato}})
        assert por_resultado.status_code == 422, contrato
        assert por_resultado.get_json()["codigo"] == "contrato_reservado"
    with app.app_context():
        assert AgenteBiAnalise.query.count() == antes_analises
        assert AgenteBiDashboard.query.count() == antes_paineis


def test_concorrencia_postgres_quando_disponivel():
    uri = (os.environ.get("AGENTE_BI_EXECUCAO_PG_URI") or "").strip()
    if not uri:
        pytest.skip("PostgreSQL descartável não configurado")
    from tests.test_agente_bi_integracao_postgres import _uri

    _uri()
