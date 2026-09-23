"""SCRUM-215A: operação e frequência evergreen no admin da Júlia (sem automação)."""
from __future__ import annotations

import inspect
import os
import pathlib
from unittest.mock import MagicMock

os.environ.setdefault("APP_ENV", "dev")

from flask import get_flashed_messages

from app.extensions import db
from app.models import Pauta
from app.run_cleiton_agente_regras import (
    CHAVE_FREQUENCIA_MINUTOS,
    get_evergreen_automatico_habilitado,
    get_evergreen_frequencia_minutos,
    get_frequencia_minutos,
)
from app.run_julia_agente_pipeline import obter_pauta_validada
from app.services import agent_service
from app.tasks import agent_tasks


def _criar_pauta_artigo(
    *,
    suffix: str,
    status: str = "pendente",
    status_verificacao: str = "aprovado",
    arquivada: bool = False,
    tipo: str = "artigo",
) -> Pauta:
    pauta = Pauta(
        titulo_original=f"Pauta {suffix}",
        fonte="Portal Teste",
        link=f"https://example.com/artigo/{suffix}",
        tipo=tipo,
        status=status,
        status_verificacao=status_verificacao,
        fonte_tipo="manual",
        arquivada=arquivada,
    )
    db.session.add(pauta)
    db.session.commit()
    return pauta


# --- UI / ADMIN ---


def test_215a_ui_pagina_tem_seletor_intencao_pauta_e_config(app, monkeypatch):
    html = pathlib.Path(
        "app/painel_admin/template_admin/agentes_julia.html"
    ).read_text(encoding="utf-8")
    assert 'name="intencao_editorial"' in html
    assert 'value="analysis"' in html
    assert 'value="evergreen"' in html
    assert 'name="pauta_id"' in html
    assert "Cadastrar nova pauta" in html
    assert "admin.pautas_admin" in html
    assert "Automação Evergreen" in html
    assert 'name="evergreen_modo"' in html
    assert 'name="evergreen_frequencia_minutos"' in html
    assert "admin.agentes_julia_configurar_evergreen" in html


def test_215a_ui_lista_pautas_elegiveis_na_pagina(app):
    with app.app_context():
        elegivel = _criar_pauta_artigo(suffix="ui-ok")
        _criar_pauta_artigo(suffix="ui-arquivada", arquivada=True)
        _criar_pauta_artigo(suffix="ui-publicada", status="publicada")
        lista = agent_service.listar_pautas_artigo_elegiveis_admin()
        ids = {item["id"] for item in lista}
        assert elegivel.id in ids
        assert any(item["titulo"] == "Pauta ui-ok" for item in lista)
        assert not any("ui-arquivada" in (item["titulo"] or "") for item in lista)
        assert not any("ui-publicada" in (item["titulo"] or "") for item in lista)


# --- MANUAL: intenção + pauta ---


def test_215a_analysis_consome_pauta_a_e_intencao(app, monkeypatch):
    with app.app_context():
        pauta_a = _criar_pauta_artigo(suffix="A-analysis")
        pauta_b = _criar_pauta_artigo(suffix="B-mais-recente")
        captured = {}

        def _fake_despachar(payload, app_flask):
            captured["payload"] = payload
            return True

        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.despachar", _fake_despachar
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_scout.executar_coleta", lambda: {}
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_verificador.executar_verificacao", lambda: {}
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.executar_retencao", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_customer_insight.executar_insight",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria",
            lambda: None,
        )

        resultado = agent_service.executar_artigo_manual_sincrono(
            app,
            pauta_id=pauta_a.id,
            intencao_editorial="analysis",
        )
        assert resultado.get("status") == "sucesso"
        payload = captured["payload"]
        assert payload["pauta_id"] == pauta_a.id
        assert payload["intencao_editorial"] == "analysis"
        assert payload["metadados"]["pauta_id"] == pauta_a.id
        assert payload["metadados"]["intencao_editorial"] == "analysis"
        assert pauta_b.status == "pendente"


def test_215a_pipeline_consome_exatamente_pauta_explicita(app):
    with app.app_context():
        antiga = _criar_pauta_artigo(suffix="pipeline-antiga")
        alvo = _criar_pauta_artigo(suffix="pipeline-alvo")
        pauta = obter_pauta_validada(
            "artigo", "mission-explicita", pauta_id=alvo.id
        )
        assert pauta is not None
        assert pauta.id == alvo.id
        assert pauta.status == "em_processamento"
        db.session.refresh(antiga)
        assert antiga.status == "pendente"


def test_215a_evergreen_consome_pauta_b_e_intencao(app, monkeypatch):
    with app.app_context():
        _criar_pauta_artigo(suffix="A-old")
        pauta_b = _criar_pauta_artigo(suffix="B-evergreen")
        captured = {}

        def _fake_despachar(payload, app_flask):
            captured["payload"] = payload
            return True

        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.despachar", _fake_despachar
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_scout.executar_coleta", lambda: {}
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_verificador.executar_verificacao", lambda: {}
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.executar_retencao", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_customer_insight.executar_insight",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria",
            lambda: None,
        )

        resultado = agent_service.executar_artigo_manual_sincrono(
            app,
            pauta_id=pauta_b.id,
            intencao_editorial="evergreen",
        )
        assert resultado.get("status") == "sucesso"
        assert captured["payload"]["pauta_id"] == pauta_b.id
        assert captured["payload"]["intencao_editorial"] == "evergreen"


def test_215a_pauta_inexistente_rejeitada(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"
    from app.painel_admin.admin_routes import (
        admin_bp,
        agentes_julia_executar_artigo_manual,
    )

    app.register_blueprint(admin_bp)
    monkeypatch.setattr(
        "app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True
    )
    monkeypatch.setenv("ADMIN_CLEITON_EXEC_MODE", "sync")
    monkeypatch.setenv("APP_ENV", "dev")

    with app.app_context():
        with app.test_request_context(
            "/admin/agentes/julia/executar-artigo-manual",
            method="POST",
            data={"intencao_editorial": "analysis", "pauta_id": "999999"},
        ):
            response = agentes_julia_executar_artigo_manual.__wrapped__()
            assert response.status_code == 302
            msgs = get_flashed_messages(with_categories=True)
            assert any("não encontrada" in m.lower() for _c, m in msgs)


def test_215a_pauta_inelegivel_rejeitada(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"
    from app.painel_admin.admin_routes import (
        admin_bp,
        agentes_julia_executar_artigo_manual,
    )

    app.register_blueprint(admin_bp)
    monkeypatch.setattr(
        "app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True
    )
    monkeypatch.setenv("ADMIN_CLEITON_EXEC_MODE", "sync")

    with app.app_context():
        pauta = _criar_pauta_artigo(
            suffix="pub", status="publicada", status_verificacao="aprovado"
        )
        with app.test_request_context(
            "/admin/agentes/julia/executar-artigo-manual",
            method="POST",
            data={"intencao_editorial": "evergreen", "pauta_id": str(pauta.id)},
        ):
            response = agentes_julia_executar_artigo_manual.__wrapped__()
            assert response.status_code == 302
            msgs = get_flashed_messages(with_categories=True)
            assert any("elegível" in m.lower() for _c, m in msgs)


def test_215a_intencao_invalida_rejeitada(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"
    from app.painel_admin.admin_routes import (
        admin_bp,
        agentes_julia_executar_artigo_manual,
    )

    app.register_blueprint(admin_bp)
    monkeypatch.setattr(
        "app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True
    )

    with app.app_context():
        pauta = _criar_pauta_artigo(suffix="ok")
        with app.test_request_context(
            "/admin/agentes/julia/executar-artigo-manual",
            method="POST",
            data={"intencao_editorial": "news", "pauta_id": str(pauta.id)},
        ):
            response = agentes_julia_executar_artigo_manual.__wrapped__()
            assert response.status_code == 302
            msgs = get_flashed_messages(with_categories=True)
            assert any("inválida" in m.lower() for _c, m in msgs)


def test_215a_pauta_explicita_nao_faz_fallback(app, monkeypatch):
    with app.app_context():
        pauta_antiga = _criar_pauta_artigo(suffix="antiga-elegivel")
        pauta_alvo = _criar_pauta_artigo(
            suffix="alvo-inelegivel", status="falha"
        )

        pauta = obter_pauta_validada(
            "artigo", "mission-no-fallback", pauta_id=pauta_alvo.id
        )
        assert pauta is None
        db.session.refresh(pauta_antiga)
        assert pauta_antiga.status == "pendente"


def test_215a_legado_sem_pauta_id_preservado(app, monkeypatch):
    with app.app_context():
        pauta_a = _criar_pauta_artigo(suffix="legada-1")
        _criar_pauta_artigo(suffix="legada-2")
        pauta = obter_pauta_validada("artigo", "mission-legado", pauta_id=None)
        assert pauta is not None
        assert pauta.id == pauta_a.id
        assert pauta.status == "em_processamento"


def test_215a_pauta_id_malformado_abc_nao_faz_fallback(app, monkeypatch):
    from app.run_julia_agente_pipeline import executar_pipeline

    with app.app_context():
        pauta_b = _criar_pauta_artigo(suffix="B-elegivel-abc")
        redacao = {"chamada": 0}

        def _boom(*_a, **_k):
            redacao["chamada"] += 1
            raise AssertionError("redação não deve ser chamada")

        monkeypatch.setattr("app.run_julia_agente_pipeline.gerar_conteudo", _boom)
        ok = executar_pipeline(
            {
                "mission_id": "mission-abc",
                "tipo_missao": "artigo",
                "pauta_id": "abc",
            },
            app,
        )
        assert ok is False
        assert redacao["chamada"] == 0
        db.session.refresh(pauta_b)
        assert pauta_b.status == "pendente"


def test_215a_pauta_id_malformado_12x_nao_faz_fallback(app, monkeypatch):
    from app.run_julia_agente_pipeline import executar_pipeline

    with app.app_context():
        pauta_b = _criar_pauta_artigo(suffix="B-elegivel-12x")
        redacao = {"chamada": 0}

        def _boom(*_a, **_k):
            redacao["chamada"] += 1
            raise AssertionError("redação não deve ser chamada")

        monkeypatch.setattr("app.run_julia_agente_pipeline.gerar_conteudo", _boom)
        ok = executar_pipeline(
            {
                "mission_id": "mission-12x",
                "tipo_missao": "artigo",
                "pauta_id": "12x",
            },
            app,
        )
        assert ok is False
        assert redacao["chamada"] == 0
        db.session.refresh(pauta_b)
        assert pauta_b.status == "pendente"


def test_215a_pauta_id_numerico_inexistente_nao_consome_b(app, monkeypatch):
    from app.run_julia_agente_pipeline import executar_pipeline

    with app.app_context():
        pauta_b = _criar_pauta_artigo(suffix="B-elegivel-inexistente")
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_conteudo",
            lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sem redação")),
        )
        ok = executar_pipeline(
            {
                "mission_id": "mission-inexistente",
                "tipo_missao": "artigo",
                "pauta_id": 999999,
            },
            app,
        )
        assert ok is False
        db.session.refresh(pauta_b)
        assert pauta_b.status == "pendente"


def test_215a_pauta_id_valido_consome_somente_a(app):
    with app.app_context():
        pauta_a = _criar_pauta_artigo(suffix="A-alvo-valido")
        pauta_b = _criar_pauta_artigo(suffix="B-outra-elegivel")
        pauta = obter_pauta_validada(
            "artigo", "mission-somente-a", pauta_id=pauta_a.id
        )
        assert pauta is not None
        assert pauta.id == pauta_a.id
        assert pauta.status == "em_processamento"
        db.session.refresh(pauta_b)
        assert pauta_b.status == "pendente"


def test_215a_pipeline_sem_pauta_id_usa_selecao_legada(app, monkeypatch):
    from app.run_julia_agente_pipeline import executar_pipeline

    with app.app_context():
        pauta_a = _criar_pauta_artigo(suffix="legado-pipeline-1")
        _criar_pauta_artigo(suffix="legado-pipeline-2")

        def _fake_conteudo(titulo, *_a, **_k):
            raise RuntimeError(f"parada-apos-selecao:{titulo}")

        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_conteudo", _fake_conteudo
        )
        ok = executar_pipeline(
            {"mission_id": "mission-sem-pauta", "tipo_missao": "artigo"},
            app,
        )
        assert ok is False
        db.session.refresh(pauta_a)
        assert pauta_a.status == "falha"
        assert pauta_a.mission_id == "mission-sem-pauta"


def test_215a_pipeline_pauta_id_none_preserva_legado(app, monkeypatch):
    from app.run_julia_agente_pipeline import (
        _resolver_pauta_id_do_payload,
        executar_pipeline,
    )

    resolvido = _resolver_pauta_id_do_payload(
        {"mission_id": "x", "tipo_missao": "artigo", "pauta_id": None}
    )
    assert resolvido.explicit is False
    assert resolvido.invalid is False

    with app.app_context():
        pauta_a = _criar_pauta_artigo(suffix="none-legado-1")

        def _fake_conteudo(*_a, **_k):
            raise RuntimeError("parada-apos-selecao-none")

        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_conteudo", _fake_conteudo
        )
        ok = executar_pipeline(
            {
                "mission_id": "mission-none",
                "tipo_missao": "artigo",
                "pauta_id": None,
            },
            app,
        )
        assert ok is False
        db.session.refresh(pauta_a)
        assert pauta_a.status == "falha"
        assert pauta_a.mission_id == "mission-none"


def test_215a_resolver_distingue_ausente_de_malformado():
    from app.run_julia_agente_pipeline import _resolver_pauta_id_do_payload

    ausente = _resolver_pauta_id_do_payload({"tipo_missao": "artigo"})
    assert ausente == (False, None, False)

    malformado = _resolver_pauta_id_do_payload(
        {"tipo_missao": "artigo", "pauta_id": "abc"}
    )
    assert malformado.explicit is True
    assert malformado.invalid is True
    assert malformado.pauta_id is None

    valido = _resolver_pauta_id_do_payload(
        {"tipo_missao": "artigo", "pauta_id": "123"}
    )
    assert valido == (True, 123, False)

    via_meta = _resolver_pauta_id_do_payload(
        {"tipo_missao": "artigo", "metadados": {"pauta_id": "12x"}}
    )
    assert via_meta.explicit is True and via_meta.invalid is True


# --- SYNC / ASYNC ---


def test_215a_parametros_caminho_sincrono(app, monkeypatch):
    captured = {}

    def _fake_orq(app_flask, **kwargs):
        captured.update(kwargs)
        return {"status": "sucesso", "motivo": "ok"}

    monkeypatch.setattr("app.run_cleiton.executar_orquestracao", _fake_orq)
    resultado = agent_service.executar_artigo_manual_sincrono(
        app, pauta_id=42, intencao_editorial="evergreen"
    )
    assert resultado["status"] == "sucesso"
    assert captured["pauta_id"] == 42
    assert captured["intencao_editorial"] == "evergreen"
    assert captured["tipo_missao_forcado"] == "artigo"


def test_215a_parametros_caminho_async(app, monkeypatch):
    captured = {}

    def _fake_orq(app_flask, **kwargs):
        captured.update(kwargs)
        return {"status": "sucesso", "motivo": "async-ok"}

    monkeypatch.setattr("app.run_cleiton.executar_orquestracao", _fake_orq)
    monkeypatch.setattr(
        "app.tasks.agent_tasks.persistir_ultima_execucao_manual",
        lambda *a, **k: None,
    )
    agent_tasks.run_artigo_manual_background(
        app,
        None,
        pauta_id=77,
        intencao_editorial="analysis",
    )
    assert captured["pauta_id"] == 77
    assert captured["intencao_editorial"] == "analysis"


def test_215a_persistir_ultima_execucao_manual_usa_app(tmp_path, monkeypatch):
    src = inspect.getsource(agent_tasks.run_artigo_manual_background)
    assert "app_flask=" not in src
    assert "app=app" in src
    src_cleiton = inspect.getsource(agent_tasks.run_cleiton_background)
    assert "app_flask=" not in src_cleiton

    resultado = {"status": "sucesso", "motivo_final": "ok-async"}
    agent_service.persistir_ultima_execucao_manual(
        resultado, "Executar artigo agora", app=MagicMock(config={"DATA_DIR": str(tmp_path)})
    )
    lido = agent_service.ler_ultima_execucao_manual(
        app=MagicMock(config={"DATA_DIR": str(tmp_path)})
    )
    assert lido is not None
    assert lido["status"] == "sucesso"
    assert lido["motivo"] == "ok-async"


# --- CONFIG ---


def test_215a_salva_modo_manual_e_frequencia(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"
    from app.painel_admin.admin_routes import (
        admin_bp,
        agentes_julia_configurar_evergreen,
    )

    app.register_blueprint(admin_bp)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setattr(
        "app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True
    )

    with app.app_context():
        legado_antes = get_frequencia_minutos()
        with app.test_request_context(
            "/admin/agentes/julia/evergreen",
            method="POST",
            data={
                "evergreen_modo": "manual",
                "evergreen_frequencia_minutos": "30",
            },
        ):
            response = agentes_julia_configurar_evergreen.__wrapped__()
            assert response.status_code == 302
            assert get_evergreen_automatico_habilitado() is False
            assert get_evergreen_frequencia_minutos() == 30
            assert get_frequencia_minutos() == legado_antes


def test_215a_salva_modo_automatico(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"
    from app.painel_admin.admin_routes import (
        admin_bp,
        agentes_julia_configurar_evergreen,
    )

    app.register_blueprint(admin_bp)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setattr(
        "app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True
    )

    with app.app_context():
        with app.test_request_context(
            "/admin/agentes/julia/evergreen",
            method="POST",
            data={
                "evergreen_modo": "automatico",
                "evergreen_frequencia_minutos": "15",
            },
        ):
            response = agentes_julia_configurar_evergreen.__wrapped__()
            assert response.status_code == 302
            cfg = agent_service.obter_evergreen_config()
            assert cfg["modo"] == "automatico"
            assert cfg["evergreen_automatico_habilitado"] is True
            assert cfg["evergreen_frequencia_minutos"] == 15


def test_215a_salvar_evergreen_nao_altera_frequencia_legada(app, monkeypatch):
    with app.app_context():
        agent_service.configurar_frequencia_minutos(120)
        agent_service.configurar_evergreen_admin(
            automatico_habilitado=True, frequencia_minutos=45
        )
        assert get_frequencia_minutos() == 120
        assert get_evergreen_frequencia_minutos() == 45
        from app.models import ConfigRegras

        row = ConfigRegras.query.filter_by(chave=CHAVE_FREQUENCIA_MINUTOS).first()
        assert row is not None
        assert int(row.valor_inteiro) == 120


def test_215a_decidir_tipo_missao_nao_vira_slot_evergreen():
    """SCRUM-215A/B: decidir_tipo_missao permanece artigo|noticia; evergreen é cadência aparte."""
    src_decidir = inspect.getsource(
        __import__(
            "app.run_cleiton_agente_orquestrador", fromlist=["decidir_tipo_missao"]
        ).decidir_tipo_missao
    )
    assert "get_evergreen_automatico_habilitado" not in src_decidir
    assert 'return "evergreen"' not in src_decidir
    assert "return 'evergreen'" not in src_decidir
