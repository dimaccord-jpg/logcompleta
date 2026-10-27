"""SCRUM-265: evolução de dashboard por texto livre, com histórico e sem cobrança indevida."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDashboard, AgenteBiDataset, IaChamadaTentativa, IaConsumoEvento, utcnow_naive
from app.services.agente_bi_anthropic_service import chamar_anthropic_governado
from app.services.agente_bi_dashboard_service import consultar_dashboard, montar_dashboard
from app.services.agente_bi_definir_service import definir
from app.services.agente_bi_evolucao_service import EvolucaoErro, evoluir_dashboard
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario, obter_analise
from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno, seed_usuario
from tests.test_agente_bi_analisar import SENTINELA
from tests.test_agente_bi_calculo import _calcular, _coluna, _gravar, _indicador, _ref
from tests.test_agente_bi_definir import _jornada
from tests.test_agente_bi_ingestao import _cliente as _cliente_ingestao
from tests.test_agente_bi_ingestao import _entrar, _xlsx

PRECISAO = Context(prec=28, rounding=ROUND_HALF_EVEN)


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)


def _painel(email, slug):
    _usuario, franquia, escopo, perfil, pacote, _aprovada = _jornada(email, slug)
    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    pacote_um = _indicador(pacote, "soma", leitura="indicador_de_medida")
    pacote_um["graficos"][0]["medida"] = _ref(valor)
    documento = definir(perfil, pacote_um)
    salva = _gravar(escopo, pacote["id"], documento)
    calculo = _calcular(escopo, salva["id"])
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    return franquia, escopo, painel


def _media() -> str:
    texto = format(PRECISAO.divide(Decimal("25.25"), Decimal(3)), "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto


def _bate(grafico, indicador):
    assert grafico["status"] == indicador["status"]
    assert grafico["tipo"] == indicador["grafico"]
    assert grafico["resultado"] == indicador["resultado"]
    assert grafico["total"] == indicador["total"]
    assert grafico["unidade"] == indicador["unidade"]
    assert grafico["operacao"] == indicador["operacao"]
    assert grafico["elegiveis"] == indicador["elegiveis"]
    assert grafico["serie"] == [
        {"chave": item["chave"], "resultado": item["resultado"], "elegiveis": item["elegiveis"]}
        for item in indicador["serie"]
    ]


def _evoluir(escopo, painel, texto, parametros=None, **extras):
    corpo = {"texto": texto}
    if parametros is not None:
        corpo["parametros"] = parametros
    return evoluir_dashboard(escopo, painel["dashboard"]["id"], corpo, **extras)


def _quociente(numerador, denominador) -> str:
    texto = format(PRECISAO.divide(Decimal(numerador), Decimal(denominador)), "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto


def _valor_nao_persistido(escopo, resposta, trecho):
    agulha = json.dumps(trecho, ensure_ascii=False)
    painel = db.session.get(AgenteBiDashboard, resposta["dashboard"]["id"])
    assert agulha not in (painel.definicoes_json or "")
    definicao = obter_analise(escopo, resposta["consulta"]["definicao_id"])
    calculo = obter_analise(escopo, resposta["consulta"]["calculo_id"])
    for bloco in (
        definicao["resultado"],
        definicao.get("contexto"),
        definicao.get("regras_negocio"),
        calculo["resultado"],
        calculo.get("contexto"),
    ):
        assert agulha not in json.dumps(bloco, ensure_ascii=False)


def test_filtro_periodo_quebra_e_indicador_por_texto_sem_cobranca(ctx):
    _franquia, escopo, painel = _painel("evolucao.texto@test.com", "evolucao-texto")
    identificador = painel["dashboard"]["id"]
    datasets = AgenteBiDataset.query.count()
    antes_tentativas = IaChamadaTentativa.query.count()
    antes_consumo = IaConsumoEvento.query.count()
    assert painel["consulta"]["graficos"][0]["resultado"] == "25.25"

    filtrado = _evoluir(escopo, painel, "filtre beta igual a sul")
    assert filtrado["situacao"] == "aplicada"
    assert filtrado["via"] == "deterministica"
    assert filtrado["chamada_anthropic"] is False
    assert filtrado["cobranca"] is False
    assert filtrado["patch"]["acao"] == "definir_filtro"
    assert filtrado["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert filtrado["consulta"]["graficos"][0]["elegiveis"] == 1
    memoria = obter_analise(escopo, filtrado["consulta"]["calculo_id"])["resultado"]
    _bate(filtrado["consulta"]["graficos"][0], memoria["indicadores"][0])
    assert filtrado["revisao_anterior_id"] == identificador
    assert filtrado["dashboard"]["revisao"] == 2
    assert "sul" not in db.session.get(AgenteBiDashboard, filtrado["dashboard"]["id"]).definicoes_json
    assert consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"] == "25.25"

    periodo = _evoluir(escopo, painel, "periodo de 2024-03-01 a 2024-03-02")
    assert periodo["via"] == "deterministica"
    assert periodo["patch"]["acao"] == "alterar_periodo"
    assert periodo["consulta"]["graficos"][0]["resultado"] == "14.75"
    assert periodo["consulta"]["graficos"][0]["elegiveis"] == 2
    _bate(
        periodo["consulta"]["graficos"][0],
        obter_analise(escopo, periodo["consulta"]["calculo_id"])["resultado"]["indicadores"][0],
    )

    quebra = _evoluir(escopo, painel, "agrupe por beta")
    assert quebra["patch"]["acao"] == "alterar_agrupamento"
    assert quebra["consulta"]["graficos"][0]["tipo"] == "barra"
    assert quebra["consulta"]["graficos"][0]["serie"] == [
        {"chave": "norte", "resultado": "21", "elegiveis": 2},
        {"chave": "sul", "resultado": "4.25", "elegiveis": 1},
    ]
    _bate(
        quebra["consulta"]["graficos"][0],
        obter_analise(escopo, quebra["consulta"]["calculo_id"])["resultado"]["indicadores"][0],
    )

    indicador = _evoluir(escopo, painel, "troque para média")
    assert indicador["patch"]["acao"] == "alterar_indicador"
    assert indicador["patch"]["operacao"] == "media"
    assert indicador["consulta"]["graficos"][0]["resultado"] == _media()
    assert indicador["consulta"]["graficos"][0]["elegiveis"] == 3
    _bate(
        indicador["consulta"]["graficos"][0],
        obter_analise(escopo, indicador["consulta"]["calculo_id"])["resultado"]["indicadores"][0],
    )

    revisoes = {item["revisao"] for item in filtrado["revisoes"]}
    assert 1 in revisoes and filtrado["dashboard"]["revisao"] in revisoes
    assert AgenteBiDataset.query.count() == datasets
    assert db.session.get(AgenteBiDataset, painel["consulta"]["dataset_id"]).versao == painel["consulta"]["versao"]
    assert IaChamadaTentativa.query.count() == antes_tentativas
    assert IaConsumoEvento.query.count() == antes_consumo
    assert SENTINELA not in json.dumps(filtrado["consulta"])


def test_rejeita_invalido_esclarece_e_bloqueia_nao_autorizado(ctx):
    _franquia, escopo, painel = _painel("evolucao.rejeita@test.com", "evolucao-rejeita")
    quantidade = AgenteBiDashboard.query.count()
    analises = AgenteBiAnalise.query.count()

    with pytest.raises(EvolucaoErro) as invalido:
        _evoluir(escopo, painel, "select valor from movimento")
    assert invalido.value.codigo == "alteracao_invalida"
    assert invalido.value.status == 422
    with pytest.raises(EvolucaoErro) as imagem:
        _evoluir(escopo, painel, "leia o print do grafico")
    assert imagem.value.codigo == "alteracao_invalida"

    with pytest.raises(EvolucaoErro) as proibido:
        _evoluir(escopo, painel, "relacione com a aba faixas")
    assert proibido.value.codigo == "nao_autorizada"
    assert proibido.value.status == 403

    duvida = _evoluir(escopo, painel, "mude o indicador")
    assert duvida["situacao"] == "esclarecimento"
    assert duvida["esclarecimento"]["motivo"] == "metrica_nao_identificada"
    assert duvida["chamada_anthropic"] is False
    assert duvida["cobranca"] is False
    assert duvida["dashboard"]["id"] == painel["dashboard"]["id"]

    margem = _evoluir(escopo, painel, "crie o indicador de margem")
    assert margem["situacao"] == "esclarecimento"
    assert margem["esclarecimento"]["motivo"] == "metrica_nao_identificada"
    assert "margem" not in json.dumps(margem["consulta"])

    assert AgenteBiDashboard.query.count() == quantidade
    assert AgenteBiAnalise.query.count() == analises
    assert IaChamadaTentativa.query.count() == 0
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"


def test_explicacao_separa_comprovado_hipotese_e_nao_verificavel(ctx):
    _franquia, escopo, painel = _painel("evolucao.explica@test.com", "evolucao-explica")
    explicado = _evoluir(escopo, painel, "explique o resultado")
    assert explicado["situacao"] == "explicacao"
    assert explicado["chamada_anthropic"] is False
    assert explicado["cobranca"] is False
    assert any("25.25" in linha for linha in explicado["explicacao"]["comprovado"])
    assert explicado["explicacao"]["hipotese"] == []
    assert explicado["explicacao"]["nao_verificavel"] == []
    assert SENTINELA not in json.dumps(explicado["explicacao"])

    causa = _evoluir(escopo, painel, "por que o resultado mudou e qual o nome do cliente")
    assert causa["explicacao"]["hipotese"]
    assert causa["explicacao"]["nao_verificavel"]
    assert SENTINELA not in json.dumps(causa["explicacao"])
    assert AgenteBiDashboard.query.count() == 1


def test_isolamento_entre_usuario_e_conta(ctx):
    _franquia, escopo, painel = _painel("evolucao.dono@test.com", "evolucao-dono")
    from tests.test_agente_bi_ingestao import _par

    _outra, _outra_franquia, outro = _par("evolucao-outro", "evolucao.outro@test.com")
    colega = seed_usuario(_franquia.id, escopo.conta_id, email="evolucao.colega@test.com")
    for intruso in (escopo_do_usuario(outro), escopo_do_usuario(colega)):
        with pytest.raises(AgenteBiErro) as erro:
            evoluir_dashboard(intruso, painel["dashboard"]["id"], {"texto": "filtre beta igual a sul"})
        assert erro.value.status == 404
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"


def test_interpretacao_passa_pelo_adaptador_e_patch_invalido_nao_publica(ctx, monkeypatch):
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.chamar_anthropic_governado",
        chamar_anthropic_governado,
    )
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def reserva_proibida(*_args, **_kwargs):
        raise AssertionError("reserva")

    monkeypatch.setattr("app.services.cleiton_billable_ai_call._reservar", reserva_proibida)
    franquia, escopo, painel = _painel("evolucao.ia@test.com", "evolucao-ia")
    from app.models import CleitonCostConfig, Conta

    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    capturado = {}

    def transporte(pedido):
        capturado["pedido"] = pedido
        capturado["chamadas"] = capturado.get("chamadas", 0) + 1
        return {
            "texto": json.dumps(
                {
                    "acao": "alterar_indicador",
                    "coluna": None,
                    "operador": None,
                    "valor": None,
                    "valor_fim": None,
                    "operacao": "media",
                }
            ),
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    aplicado = _evoluir(escopo, painel, "quero ver a tendencia central do montante", transporte=transporte)
    assert aplicado["situacao"] == "aplicada"
    assert aplicado["via"] == "interpretacao"
    assert aplicado["chamada_anthropic"] is True
    assert aplicado["cobranca"] is True
    assert aplicado["consulta"]["graficos"][0]["operacao"] == "media"
    assert aplicado["consulta"]["graficos"][0]["resultado"] == _media()
    _bate(
        aplicado["consulta"]["graficos"][0],
        obter_analise(escopo, aplicado["consulta"]["calculo_id"])["resultado"]["indicadores"][0],
    )
    enviado = json.dumps(capturado["pedido"])
    assert capturado["chamadas"] == 1
    assert "10.5" not in enviado
    assert "4.25" not in enviado
    assert SENTINELA not in enviado
    assert "beta" not in enviado
    assert "norte" not in enviado
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    db.session.refresh(franquia)
    assert Decimal(franquia.consumo_acumulado) == Decimal("2")
    assert Decimal(franquia.reserva_pendente or 0) == 0

    def transporte_invalido(_pedido):
        return {
            "texto": json.dumps(
                {
                    "acao": "alterar_indicador",
                    "coluna": 99,
                    "operador": None,
                    "valor": None,
                    "valor_fim": None,
                    "operacao": "media",
                }
            ),
            "stop_reason": "end_turn",
            "input_tokens": 100,
            "output_tokens": 100,
            "recusa": False,
            "truncada": False,
        }

    quantidade = AgenteBiDashboard.query.count()
    with pytest.raises(EvolucaoErro) as invalido:
        evoluir_dashboard(
            escopo,
            aplicado["dashboard"]["id"],
            {"texto": "quero outra leitura do montante"},
            transporte=transporte_invalido,
        )
    assert invalido.value.codigo == "alteracao_invalida"
    assert AgenteBiDashboard.query.count() == quantidade
    assert consultar_dashboard(escopo, aplicado["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == _media()


def test_api_bloqueia_franquia_e_aplica_filtro(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.chamar_anthropic_governado",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("anthropic")),
    )
    with app.app_context():
        _franquia, escopo, painel = _painel("evolucao.api@test.com", "evolucao-api")
        usuario_id = escopo.usuario_id
        identificador = painel["dashboard"]["id"]
    assert client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": "filtre beta igual a sul"}).status_code == 401
    _entrar(client, usuario_id)
    monkeypatch.setattr(
        "app.agente_bi_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {"permitido": False},
    )
    bloqueado = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": "filtre beta igual a sul"})
    assert bloqueado.status_code == 403
    assert bloqueado.get_json()["codigo"] == "operacao_nao_permitida"
    monkeypatch.setattr(
        "app.agente_bi_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {"permitido": True},
    )
    resposta = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": "filtre beta igual a sul"})
    assert resposta.status_code == 201, resposta.get_data(as_text=True)
    corpo = resposta.get_json()
    assert corpo["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert corpo["cobranca"] is False
    anterior = client.get(f"/api/agente-bi/dashboards/{identificador}/consulta")
    assert anterior.status_code == 200
    assert anterior.get_json()["consulta"]["graficos"][0]["resultado"] == "25.25"


def test_fonte_nao_cria_executor_ocr_nem_cobranca_paralela():
    from pathlib import Path

    fonte = (Path(__file__).resolve().parents[1] / "app" / "services" / "agente_bi_evolucao_service.py").read_text(encoding="utf-8")
    script = (Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "agente_bi_dashboard.js").read_text(encoding="utf-8")
    assert "chamar_anthropic_governado" in fonte
    assert "calcular_definicao" in fonte
    assert "decidir_admissao_chamada_ia" not in fonte
    assert "pytesseract" not in fonte.lower()
    assert "easyocr" not in fonte.lower()
    assert "eval(" not in fonte
    assert "exec(" not in fonte
    assert "innerHTML" not in script
    assert "eval(" not in script
    assert "parametrosEmMemoria" in script
    assert "localStorage" not in script
    assert "sessionStorage" not in script
    assert "document.cookie" not in script
    assert "indexedDB" not in script


def _creditar(monkeypatch, email, slug):
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.chamar_anthropic_governado",
        chamar_anthropic_governado,
    )
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def reserva_proibida(*_args, **_kwargs):
        raise AssertionError("reserva")

    monkeypatch.setattr("app.services.cleiton_billable_ai_call._reservar", reserva_proibida)
    franquia, escopo, painel = _painel(email, slug)
    from app.models import CleitonCostConfig, Conta

    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    return franquia, escopo, painel


def _transporte_json(chamadas, corpo):
    def transporte(_pedido):
        chamadas["n"] += 1
        return {
            "texto": json.dumps(corpo),
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    return transporte


_PATCH_MEDIA = {
    "acao": "alterar_indicador",
    "coluna": None,
    "operador": None,
    "valor": None,
    "valor_fim": None,
    "operacao": "media",
}
_PATCH_TIPOS_INVALIDOS = {
    "acao": ["alterar_indicador"],
    "coluna": {"indice": 1},
    "operador": ["igual"],
    "valor": {"v": "sul"},
    "valor_fim": ["fim"],
    "operacao": {"nome": "media"},
}
_PEDIDO_SEMANTICO = "quero ver a tendencia central do montante"


def test_continuidade_conversacional_reusa_parametros_sem_persistir(ctx, monkeypatch):
    _franquia, escopo, painel = _painel("evolucao.continua@test.com", "evolucao-continua")
    from tests.test_agente_bi_ingestao import _par

    _outra, _outra_franquia, outro = _par("evolucao-continua-outro", "evolucao.continua.outro@test.com")
    colega = seed_usuario(_franquia.id, escopo.conta_id, email="evolucao.continua.colega@test.com")
    datasets = AgenteBiDataset.query.count()

    filtrado = _evoluir(escopo, painel, "filtre beta igual a sul")
    assert filtrado["situacao"] == "aplicada"
    assert filtrado["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert filtrado["consulta"]["graficos"][0]["elegiveis"] == 1
    assert set(filtrado["parametros"].values()) == {"sul"}
    assert filtrado["cobranca"] is False
    _valor_nao_persistido(escopo, filtrado, "sul")
    assert db.session.get(AgenteBiDataset, filtrado["consulta"]["dataset_id"]).versao == painel["consulta"]["versao"]

    media = _evoluir(escopo, filtrado, "troque para média", parametros=filtrado["parametros"])
    assert media["situacao"] == "aplicada"
    assert media["patch"]["acao"] == "alterar_indicador"
    assert media["patch"]["operacao"] == "media"
    assert media["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert media["consulta"]["graficos"][0]["elegiveis"] == 1
    assert set(media["parametros"].values()) == {"sul"}
    _bate(media["consulta"]["graficos"][0], obter_analise(escopo, media["consulta"]["calculo_id"])["resultado"]["indicadores"][0])
    _valor_nao_persistido(escopo, media, "sul")

    quebra = _evoluir(escopo, filtrado, "agrupe por beta", parametros=filtrado["parametros"])
    assert quebra["situacao"] == "aplicada"
    assert quebra["patch"]["acao"] == "alterar_agrupamento"
    assert quebra["consulta"]["graficos"][0]["tipo"] == "barra"
    assert quebra["consulta"]["graficos"][0]["serie"] == [
        {"chave": "sul", "resultado": "4.25", "elegiveis": 1},
    ]
    _bate(quebra["consulta"]["graficos"][0], obter_analise(escopo, quebra["consulta"]["calculo_id"])["resultado"]["indicadores"][0])

    periodo = _evoluir(escopo, painel, "periodo de 2024-03-01 a 2024-03-02")
    assert periodo["situacao"] == "aplicada"
    assert periodo["consulta"]["graficos"][0]["resultado"] == "14.75"
    assert periodo["consulta"]["graficos"][0]["elegiveis"] == 2
    assert set(periodo["parametros"].values()) == {"2024-03-01", "2024-03-02"}
    _valor_nao_persistido(escopo, periodo, "2024-03-01")
    _valor_nao_persistido(escopo, periodo, "2024-03-02")
    indicador = _evoluir(escopo, periodo, "troque para média", parametros=periodo["parametros"])
    assert indicador["situacao"] == "aplicada"
    assert indicador["patch"]["operacao"] == "media"
    assert indicador["consulta"]["graficos"][0]["resultado"] == _quociente("14.75", 2)
    assert indicador["consulta"]["graficos"][0]["elegiveis"] == 2
    assert set(indicador["parametros"].values()) == {"2024-03-01", "2024-03-02"}
    _bate(
        indicador["consulta"]["graficos"][0],
        obter_analise(escopo, indicador["consulta"]["calculo_id"])["resultado"]["indicadores"][0],
    )

    quantidade = AgenteBiDashboard.query.count()
    analises = AgenteBiAnalise.query.count()
    recarga = _evoluir(escopo, filtrado, "troque para média")
    assert recarga["situacao"] == "esclarecimento"
    assert recarga["esclarecimento"]["motivo"] == "informar_parametro"
    assert "recarregar" in recarga["esclarecimento"]["pergunta"]
    assert recarga["chamada_anthropic"] is False
    assert recarga["cobranca"] is False
    assert recarga["dashboard"]["id"] == filtrado["dashboard"]["id"]
    assert recarga["consulta"]["graficos"][0]["operacao"] == "soma"
    assert recarga["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert AgenteBiDashboard.query.count() == quantidade
    assert AgenteBiAnalise.query.count() == analises

    with pytest.raises(EvolucaoErro) as nome:
        _evoluir(escopo, filtrado, "troque para média", parametros={"nao_declarado": "sul"})
    assert nome.value.codigo == "parametro_inconsistente"
    assert nome.value.status == 422
    nome_filtro = next(iter(filtrado["parametros"]))
    with pytest.raises(EvolucaoErro) as tipo:
        _evoluir(escopo, filtrado, "troque para média", parametros={nome_filtro: ["sul"]})
    assert tipo.value.codigo == "parametro_inconsistente"
    assert AgenteBiDashboard.query.count() == quantidade

    for intruso in (escopo_do_usuario(outro), escopo_do_usuario(colega)):
        with pytest.raises(AgenteBiErro) as erro:
            evoluir_dashboard(
                intruso,
                filtrado["dashboard"]["id"],
                {"texto": "troque para média", "parametros": filtrado["parametros"]},
            )
        assert erro.value.codigo == "nao_encontrado"
        assert erro.value.status == 404
    assert consultar_dashboard(escopo, filtrado["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "4.25"

    capturado = {}

    def adaptador(**kwargs):
        capturado["mensagem"] = kwargs["mensagem"]
        return {
            "ok": True,
            "codigo": "ok",
            "texto": json.dumps(_PATCH_MEDIA),
            "evento_id": None,
            "repetida": False,
            "incerto": False,
        }

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", adaptador)
    interpretado = _evoluir(
        escopo,
        filtrado,
        _PEDIDO_SEMANTICO,
        parametros=filtrado["parametros"],
    )
    assert interpretado["situacao"] == "aplicada"
    assert "sul" not in capturado["mensagem"]
    assert "4.25" not in capturado["mensagem"]
    assert "parametros" not in json.loads(capturado["mensagem"])
    assert AgenteBiDataset.query.count() == datasets


def test_tipos_invalidos_rejeitam_contrato_sem_cobranca_duplicada(ctx, monkeypatch):
    franquia, escopo, painel = _creditar(monkeypatch, "evolucao.tipos@test.com", "evolucao-tipos")
    chamadas = {"n": 0}
    quantidade = AgenteBiDashboard.query.count()
    with pytest.raises(EvolucaoErro) as invalido:
        _evoluir(
            escopo,
            painel,
            _PEDIDO_SEMANTICO,
            transporte=_transporte_json(chamadas, _PATCH_TIPOS_INVALIDOS),
        )
    assert invalido.value.codigo == "alteracao_invalida"
    assert invalido.value.status == 422
    assert chamadas["n"] == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    db.session.refresh(franquia)
    assert Decimal(franquia.consumo_acumulado) == Decimal("2")
    assert Decimal(franquia.reserva_pendente or 0) == 0
    assert AgenteBiDashboard.query.count() == quantidade

    repetido = _evoluir(
        escopo,
        painel,
        _PEDIDO_SEMANTICO,
        transporte=_transporte_json(chamadas, _PATCH_TIPOS_INVALIDOS),
    )
    assert repetido["situacao"] == "esclarecimento"
    assert repetido["esclarecimento"]["motivo"] == "pedido_repetido"
    assert repetido["chamada_anthropic"] is False
    assert repetido["cobranca"] is False
    assert "Reformule" in repetido["esclarecimento"]["pergunta"]
    assert chamadas["n"] == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    db.session.refresh(franquia)
    assert Decimal(franquia.consumo_acumulado) == Decimal("2")
    assert AgenteBiDashboard.query.count() == quantidade
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"


def test_repeticao_semantica_nao_chama_de_novo_nem_recupera_revisao(ctx, monkeypatch):
    franquia, escopo, painel = _creditar(monkeypatch, "evolucao.repete@test.com", "evolucao-repete")
    chamadas = {"n": 0}
    primeiro = _evoluir(
        escopo,
        painel,
        _PEDIDO_SEMANTICO,
        transporte=_transporte_json(chamadas, _PATCH_MEDIA),
    )
    assert primeiro["situacao"] == "aplicada"
    assert primeiro["via"] == "interpretacao"
    assert primeiro["cobranca"] is True
    assert primeiro["consulta"]["graficos"][0]["operacao"] == "media"
    assert chamadas["n"] == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    db.session.refresh(franquia)
    consumo = Decimal(franquia.consumo_acumulado)
    assert consumo == Decimal("2")
    quantidade = AgenteBiDashboard.query.count()

    repetido = _evoluir(
        escopo,
        painel,
        _PEDIDO_SEMANTICO,
        transporte=_transporte_json(chamadas, _PATCH_MEDIA),
    )
    assert repetido["situacao"] == "esclarecimento"
    assert repetido["esclarecimento"]["motivo"] == "pedido_repetido"
    assert "coluna" in repetido["esclarecimento"]["pergunta"]
    assert "operação" in repetido["esclarecimento"]["pergunta"]
    assert repetido["chamada_anthropic"] is False
    assert repetido["cobranca"] is False
    assert repetido["revisao_anterior_id"] is None
    assert repetido["dashboard"]["id"] == painel["dashboard"]["id"]
    assert repetido["dashboard"]["id"] != primeiro["dashboard"]["id"]
    assert chamadas["n"] == 1
    assert IaChamadaTentativa.query.filter_by(franquia_id=franquia.id).count() == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    db.session.refresh(franquia)
    assert Decimal(franquia.consumo_acumulado) == consumo
    assert AgenteBiDashboard.query.count() == quantidade
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"
    assert consultar_dashboard(escopo, primeiro["dashboard"]["id"])["consulta"]["graficos"][0]["operacao"] == "media"


def test_api_continuidade_e_tipos_invalidos(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)

    def adaptador(**_kwargs):
        adaptador.chamadas += 1
        return {
            "ok": True,
            "codigo": "ok",
            "texto": json.dumps(_PATCH_TIPOS_INVALIDOS),
            "evento_id": None,
            "repetida": False,
            "incerto": False,
        }

    adaptador.chamadas = 0
    with app.app_context():
        _franquia, escopo, painel = _painel("evolucao.http@test.com", "evolucao-http")
        usuario_id = escopo.usuario_id
        identificador = painel["dashboard"]["id"]
        from tests.test_agente_bi_ingestao import _par

        _outra, _outra_franquia, outro = _par("evolucao-http-outro", "evolucao.http.outro@test.com")
        outro_id = outro.id
    _entrar(client, usuario_id)
    monkeypatch.setattr(
        "app.agente_bi_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {"permitido": True},
    )
    filtro = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"texto": "filtre beta igual a sul"},
    )
    assert filtro.status_code == 201, filtro.get_data(as_text=True)
    corpo = filtro.get_json()
    assert corpo["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert set(corpo["parametros"].values()) == {"sul"}
    assert "sul" not in (filtro.headers.get("Set-Cookie") or "")
    novo = corpo["dashboard"]["id"]
    media = client.post(
        f"/api/agente-bi/dashboards/{novo}/evolucao",
        json={"texto": "troque para média", "parametros": corpo["parametros"]},
    )
    assert media.status_code == 201, media.get_data(as_text=True)
    assert media.get_json()["consulta"]["graficos"][0]["operacao"] == "media"
    assert media.get_json()["consulta"]["graficos"][0]["resultado"] == "4.25"
    assert media.get_json()["consulta"]["graficos"][0]["elegiveis"] == 1
    recarga = client.post(f"/api/agente-bi/dashboards/{novo}/evolucao", json={"texto": "troque para média"})
    assert recarga.status_code == 200
    assert recarga.get_json()["situacao"] == "esclarecimento"
    assert recarga.get_json()["esclarecimento"]["motivo"] == "informar_parametro"
    assert "recarregar" in recarga.get_json()["esclarecimento"]["pergunta"]
    _entrar(client, outro_id)
    alheio = client.post(
        f"/api/agente-bi/dashboards/{novo}/evolucao",
        json={"texto": "troque para média", "parametros": corpo["parametros"]},
    )
    assert alheio.status_code == 404
    assert alheio.get_json()["codigo"] == "nao_encontrado"
    assert "sul" not in alheio.get_data(as_text=True)
    assert "4.25" not in alheio.get_data(as_text=True)

    _entrar(client, usuario_id)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", adaptador)
    invalido = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"texto": _PEDIDO_SEMANTICO},
    )
    assert invalido.status_code == 422, invalido.get_data(as_text=True)
    assert invalido.get_json()["codigo"] == "alteracao_invalida"
    assert adaptador.chamadas == 1


_PEDIDO_SEMANTICO_COM_CABECALHO = "quero ver a tendencia de beta"


def test_pedido_semantico_com_cabecalho_esclarece_interpretacao_indisponivel(ctx):
    _franquia, escopo, painel = _painel("evolucao.cabecalho@test.com", "evolucao-cabecalho")
    quantidade = AgenteBiDashboard.query.count()
    analises = AgenteBiAnalise.query.count()

    resposta = _evoluir(escopo, painel, _PEDIDO_SEMANTICO_COM_CABECALHO)

    assert resposta["situacao"] == "esclarecimento"
    assert resposta["esclarecimento"]["motivo"] == "interpretacao_indisponivel"
    assert resposta["esclarecimento"]["pergunta"] == (
        "Esse pedido precisa de interpretação e o adaptador governado não está disponível. Cite a coluna e a operação."
    )
    assert resposta["chamada_anthropic"] is False
    assert resposta["cobranca"] is False
    assert resposta["via"] == "nenhuma"
    assert resposta["patch"] is None
    assert resposta["dashboard"]["id"] == painel["dashboard"]["id"]
    assert AgenteBiDashboard.query.count() == quantidade
    assert AgenteBiAnalise.query.count() == analises
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"


def test_pedido_semantico_com_cabecalho_nao_aciona_transporte_anthropic(ctx):
    _franquia, escopo, painel = _painel("evolucao.transporte@test.com", "evolucao-transporte")
    chamadas = {"n": 0}

    def transporte(_pedido):
        chamadas["n"] += 1
        raise AssertionError("transporte")

    antes_tentativas = IaChamadaTentativa.query.count()
    antes_consumo = IaConsumoEvento.query.count()
    resposta = _evoluir(escopo, painel, _PEDIDO_SEMANTICO_COM_CABECALHO, transporte=transporte)

    assert chamadas["n"] == 0
    assert resposta["chamada_anthropic"] is False
    assert resposta["cobranca"] is False
    assert resposta["esclarecimento"]["motivo"] == "interpretacao_indisponivel"
    assert IaChamadaTentativa.query.count() == antes_tentativas
    assert IaConsumoEvento.query.count() == antes_consumo


def test_agrupamento_fora_da_regiao_autorizada_permanece_403(ctx):
    conteudo = _xlsx(
        {
            "movimento": [
                ["alfa", "beta", "quando", "qtd", "valor"],
                [SENTINELA, "norte", date(2024, 3, 1), 10, 10.5],
                ["K200", "sul", date(2024, 3, 2), 4, 4.25],
                [SENTINELA, "norte", date(2024, 3, 3), 10, 10.5],
            ],
            "externa": [
                ["destino", "peso"],
                ["leste", 1],
                ["oeste", 2],
                ["norte", 3],
            ],
        }
    )
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(
        "evolucao.regiao@test.com",
        "evolucao-regiao",
        conteudo,
    )
    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    pacote_um = _indicador(pacote, "soma", leitura="indicador_de_medida")
    pacote_um["graficos"][0]["medida"] = _ref(valor)
    documento = definir(perfil, pacote_um)
    salva = _gravar(escopo, pacote["id"], documento)
    calculo = _calcular(escopo, salva["id"])
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    quantidade = AgenteBiDashboard.query.count()
    analises = AgenteBiAnalise.query.count()

    with pytest.raises(EvolucaoErro) as erro:
        _evoluir(escopo, painel, "agrupe por destino")

    assert erro.value.codigo == "nao_autorizada"
    assert erro.value.status == 403
    assert AgenteBiDashboard.query.count() == quantidade
    assert AgenteBiAnalise.query.count() == analises
    assert consultar_dashboard(escopo, painel["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "25.25"
