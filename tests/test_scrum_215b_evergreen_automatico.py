"""SCRUM-215B: cadência automática evergreen independente do ciclo legado."""
from __future__ import annotations

import inspect
import json
import os
import pathlib
from datetime import datetime, timedelta, timezone

os.environ.setdefault("APP_ENV", "dev")

from app.extensions import db
from app.models import AuditoriaGerencial, MissaoAgente, Pauta
from app.run_cleiton_agente_orquestrador import (
    ORIGEM_EVERGREEN_AUTOMATICO,
    TIPO_DECISAO_EVERGREEN_AUTOMATICO,
    decidir_tipo_missao,
    executar_ciclo_gerencial,
    pode_executar_evergreen_por_frequencia,
    ultima_auditoria_evergreen_automatico,
    ultima_auditoria_orquestracao,
)
from app.run_cleiton_agente_regras import (
    configurar_evergreen,
    get_evergreen_automatico_habilitado,
    get_evergreen_frequencia_minutos,
    get_frequencia_minutos,
)
from app.services import agent_service
from app.services.pauta_service import (
    query_pautas_artigo_elegiveis_legado_automatico,
    selecionar_pauta_evergreen_automatico,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _criar_pauta(
    *,
    titulo: str,
    status: str = "pendente",
    status_verificacao: str = "aprovado",
    arquivada: bool = False,
    tipo: str = "artigo",
) -> Pauta:
    pauta = Pauta(
        titulo_original=titulo,
        fonte="Portal Teste",
        link=f"https://example.com/{titulo[:40]}",
        tipo=tipo,
        status=status,
        status_verificacao=status_verificacao,
        fonte_tipo="manual",
        arquivada=arquivada,
    )
    db.session.add(pauta)
    db.session.commit()
    return pauta


def _patch_ciclo_basico(monkeypatch, *, despachar_fn=None, captura=None):
    payloads = captura if captura is not None else []

    def _fake_despachar(payload, app_flask):
        payloads.append(payload)
        if despachar_fn:
            return despachar_fn(payload, app_flask)
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
        "app.run_cleiton_agente_orquestrador.executar_retencao",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "app.run_cleiton_agente_customer_insight.executar_insight",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "app.run_cleiton_agente_customer_insight.selecionar_recomendacao_prioritaria",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.run_cleiton_agente_orquestrador.dentro_janela_publicacao",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr(
        "app.run_cleiton_agente_orquestrador.pode_executar_por_frequencia",
        lambda *_a, **_k: True,
    )
    return payloads


def _registrar_evergreen_auto_sucesso(*, when: datetime | None = None) -> AuditoriaGerencial:
    entry = AuditoriaGerencial(
        tipo_decisao=TIPO_DECISAO_EVERGREEN_AUTOMATICO,
        decisao="Evergreen automático executado",
        contexto_json=json.dumps(
            {
                "origem": ORIGEM_EVERGREEN_AUTOMATICO,
                "cadencia": ORIGEM_EVERGREEN_AUTOMATICO,
                "intencao_editorial": "evergreen",
            }
        ),
        resultado="sucesso",
        created_at=when or (_utcnow() - timedelta(hours=4)),
    )
    db.session.add(entry)
    db.session.commit()
    return entry


# --- 19. Configuração ---


def test_215b_automatico_false_nao_roda(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        _criar_pauta(titulo="[evergreen] Guia de frete")
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        ev = resultado.get("evergreen_automatico") or {}
        assert ev.get("status") == "ignorado"
        assert "desabilitado" in (ev.get("motivo") or "").lower()
        assert not any(
            p.get("intencao_editorial") == "evergreen" for p in payloads
        )


def test_215b_automatico_true_config_lida(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=90)
        assert get_evergreen_automatico_habilitado() is True
        assert get_evergreen_frequencia_minutos() == 90
        cfg = agent_service.obter_evergreen_config()
        assert cfg["evergreen_automatico_habilitado"] is True
        assert cfg["modo"] == "automatico"


def test_215b_frequencia_evergreen_nao_altera_legado(app):
    with app.app_context():
        agent_service.configurar_frequencia_minutos(120)
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=45)
        assert get_frequencia_minutos() == 120
        assert get_evergreen_frequencia_minutos() == 45


# --- 20. Intervalo ---


def test_215b_nunca_executado_elegivel(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=180)
        assert ultima_auditoria_evergreen_automatico() is None
        assert pode_executar_evergreen_por_frequencia(None) is True


def test_215b_dentro_do_intervalo_nao_elegivel(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=180)
        ultima = _utcnow() - timedelta(minutes=30)
        assert pode_executar_evergreen_por_frequencia(ultima) is False


def test_215b_intervalo_vencido_elegivel(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=60)
        ultima = _utcnow() - timedelta(minutes=90)
        assert pode_executar_evergreen_por_frequencia(ultima) is True


def test_215b_manual_nao_desloca_referencia_automatica(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=180)
        auto = _registrar_evergreen_auto_sucesso(
            when=_utcnow() - timedelta(hours=5)
        )
        ref_antes = ultima_auditoria_evergreen_automatico()
        assert ref_antes == auto.created_at

        pauta = _criar_pauta(titulo="[evergreen] Manual não desloca")
        _patch_ciclo_basico(monkeypatch)
        agent_service.executar_artigo_manual_sincrono(
            app, pauta_id=pauta.id, intencao_editorial="evergreen"
        )
        ref_depois = ultima_auditoria_evergreen_automatico()
        assert ref_depois == ref_antes


def test_215b_analysis_news_nao_alteram_referencia_evergreen(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=180)
        auto = _registrar_evergreen_auto_sucesso(
            when=_utcnow() - timedelta(hours=2)
        )
        ref_antes = ultima_auditoria_evergreen_automatico()

        # Simula auditoria de orquestração legada (news/analysis)
        db.session.add(
            AuditoriaGerencial(
                tipo_decisao="orquestracao",
                decisao="Missão criada tipo=noticia",
                resultado="sucesso",
                created_at=_utcnow(),
            )
        )
        db.session.commit()
        assert ultima_auditoria_orquestracao() is not None
        assert ultima_auditoria_evergreen_automatico() == ref_antes == auto.created_at


# --- 21. Execução ---


def test_215b_elegivel_gera_missao_evergreen_com_pauta(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        pauta = _criar_pauta(titulo="[evergreen] O que é cubagem")
        # Evita artigo legado: simula artigo já publicado hoje
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "sucesso"
        assert ev["tipo_missao"] == "artigo"
        assert ev["intencao_editorial"] == "evergreen"
        assert ev["pauta_id"] == pauta.id
        evergreen_payloads = [
            p for p in payloads if p.get("intencao_editorial") == "evergreen"
        ]
        assert len(evergreen_payloads) == 1
        assert evergreen_payloads[0]["pauta_id"] == pauta.id
        assert evergreen_payloads[0]["tipo_missao"] == "artigo"


def test_215b_pauta_perde_elegibilidade_sem_fallback(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        pauta = _criar_pauta(titulo="[evergreen] Sem fallback")
        outra = _criar_pauta(titulo="[evergreen] Outra candidata")
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )
        payloads = _patch_ciclo_basico(monkeypatch)

        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.selecionar_pauta_evergreen_automatico",
            lambda **_k: pauta,
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.obter_pauta_artigo_elegivel_por_id",
            lambda *_a, **_k: None,
        )
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "falha"
        assert "sem fallback" in (ev.get("motivo") or "").lower()
        assert not any(p.get("pauta_id") == outra.id for p in payloads)
        assert not any(p.get("intencao_editorial") == "evergreen" for p in payloads)


def test_215b_sem_pauta_evergreen_termina_sem_gerar(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        _criar_pauta(titulo="Analysis normal de mercado")
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "ignorado"
        assert "sem pauta" in (ev.get("motivo") or "").lower()
        assert not any(p.get("intencao_editorial") == "evergreen" for p in payloads)


def test_215b_falha_evergreen_nao_quebra_legado(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        _criar_pauta(titulo="[evergreen] Falha dispatch")
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )

        def _despachar(payload, _app):
            if payload.get("intencao_editorial") == "evergreen":
                return False
            return True

        payloads = _patch_ciclo_basico(monkeypatch, despachar_fn=_despachar)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        # Legado (noticia) independente
        assert resultado.get("tipo_missao") == "noticia"
        assert resultado.get("status") == "sucesso"
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "falha"
        assert any(p.get("tipo_missao") == "noticia" for p in payloads)


# --- 22. Preservação do legado ---


def test_215b_news_continua_funcionando(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert resultado["tipo_missao"] == "noticia"
        assert resultado["status"] == "sucesso"
        assert payloads[0]["tipo_missao"] == "noticia"


def test_215b_analysis_automatico_continua(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo="Análise de fretes no Sudeste")
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert resultado["tipo_missao"] == "artigo"
        assert resultado["status"] == "sucesso"
        assert payloads[0]["tipo_missao"] == "artigo"
        assert payloads[0].get("pauta_id") == pauta.id
        assert payloads[0].get("intencao_editorial") in (None, "analysis")


def test_215b_evergreen_nao_substitui_decidir_tipo_missao():
    src = inspect.getsource(decidir_tipo_missao)
    assert "get_evergreen_automatico_habilitado" not in src
    assert 'return "evergreen"' not in src


def test_215b_legado_nao_le_evergreen_frequencia():
    src = inspect.getsource(
        __import__(
            "app.run_cleiton_agente_orquestrador",
            fromlist=["pode_executar_por_frequencia"],
        )
    )
    # pode_executar_por_frequencia está em regras; garante orquestrador legado usa get_frequencia via regras
    from app.run_cleiton_agente_regras import pode_executar_por_frequencia as pode

    src_pode = inspect.getsource(pode)
    assert "get_evergreen_frequencia_minutos" not in src_pode
    assert "get_frequencia_minutos" in src_pode


def test_215b_evergreen_nao_escreve_frequencia_minutos(app):
    with app.app_context():
        agent_service.configurar_frequencia_minutos(150)
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=40)
        assert get_frequencia_minutos() == 150


def test_215b_evergreen_nao_consome_limite_artigo_legado(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        # Simula missões evergreen já disparadas hoje
        for i in range(5):
            m = MissaoAgente(
                mission_id=f"ev-{i}",
                tipo_missao="artigo",
                status="pendente",
                payload_metadados=json.dumps(
                    {"intencao_editorial": "evergreen", "tipo_missao": "artigo"}
                ),
                created_at=_utcnow(),
            )
            db.session.add(m)
        db.session.commit()

        from app.run_cleiton_agente_orquestrador import _tentativas_artigo_hoje

        assert _tentativas_artigo_hoje() == 0

        pauta = _criar_pauta(titulo="Analysis ainda elegível")
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador.get_max_tentativas_artigo_dia",
            lambda: 1,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert resultado["tipo_missao"] == "artigo"
        assert resultado["status"] == "sucesso"
        assert any(p.get("pauta_id") == pauta.id for p in payloads)


# --- 23. Somente manual ---


def test_215b_automatico_false_legado_nao_consome_pauta_evergreen(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        evergreen = _criar_pauta(titulo="[evergreen] Só manual")
        analysis = _criar_pauta(titulo="Artigo analysis comum")
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=evergreen.id)
            .first()
            is None
        )
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=analysis.id)
            .first()
            is not None
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert resultado["tipo_missao"] == "artigo"
        assert payloads[0]["pauta_id"] == analysis.id
        assert payloads[0].get("pauta_id") != evergreen.id


def test_215b_manual_evergreen_com_automatico_false(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo="[evergreen] Manual ok")
        captured = {}

        def _despachar(payload, _app):
            captured["payload"] = payload
            return True

        _patch_ciclo_basico(monkeypatch, despachar_fn=_despachar)
        resultado = agent_service.executar_artigo_manual_sincrono(
            app, pauta_id=pauta.id, intencao_editorial="evergreen"
        )
        assert resultado["status"] == "sucesso"
        assert captured["payload"]["intencao_editorial"] == "evergreen"
        assert captured["payload"]["pauta_id"] == pauta.id
        # Manual não dispara avaliação automática side-car
        assert "evergreen_automatico" not in resultado


def test_215b_analysis_normal_permanece_elegivel_legado(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo="Tendências de frete 2026")
        assert selecionar_pauta_evergreen_automatico() is None
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is not None
        )


# --- Isolamento: heurística evergreen fora do legado automático ---

_TITULO_HEURISTICO_EVERGREEN = "Como calcular o custo do frete rodoviário?"


def _criar_item_serie(*, titulo: str, ordem: int = 1) -> tuple:
    from app.models import SerieEditorial, SerieItemEditorial

    serie = SerieEditorial(
        nome=f"Serie teste {titulo[:20]}",
        tema="logística",
        ativo=True,
        cadencia_dias=1,
    )
    db.session.add(serie)
    db.session.flush()
    item = SerieItemEditorial(
        serie_id=serie.id,
        ordem=ordem,
        titulo_planejado=titulo,
        data_planejada=_utcnow() - timedelta(hours=1),
        status="planejado",
    )
    db.session.add(item)
    db.session.commit()
    return serie, item


def test_215b_backlog_heuristica_nao_consumida_automatico_false(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is None
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert not any(p.get("pauta_id") == pauta.id for p in payloads)
        assert resultado.get("tipo_missao") != "artigo" or resultado.get("pauta_id") != pauta.id
        db.session.refresh(pauta)
        assert pauta.status == "pendente"


def test_215b_backlog_heuristica_nao_consumida_automatico_true(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        pauta = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is None
        )
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        legado = [p for p in payloads if p.get("intencao_editorial") != "evergreen"]
        assert not any(p.get("pauta_id") == pauta.id for p in legado)
        ev = resultado.get("evergreen_automatico") or {}
        assert ev.get("pauta_id") == pauta.id
        assert ev.get("status") == "sucesso"


def test_215b_backlog_marcador_evergreen_nao_consumido(app):
    with app.app_context():
        pauta = _criar_pauta(titulo="[evergreen] Guia de cubagem")
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is None
        )


def test_215b_backlog_analysis_tag_preservada(app):
    with app.app_context():
        pauta = _criar_pauta(
            titulo="[analysis] Tendências do transporte rodoviário em 2026"
        )
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is not None
        )


def test_215b_backlog_analysis_normal_preservada(app):
    with app.app_context():
        pauta = _criar_pauta(titulo="Impactos recentes da nova tabela de frete")
        assert (
            query_pautas_artigo_elegiveis_legado_automatico()
            .filter_by(id=pauta.id)
            .first()
            is not None
        )


def test_215b_serie_heuristica_evergreen_nao_selecionada(app):
    with app.app_context():
        from app.run_cleiton_agente_serie import selecionar_item_para_missao

        _criar_item_serie(titulo=_TITULO_HEURISTICO_EVERGREEN)
        item, motivo = selecionar_item_para_missao()
        assert item is None
        assert motivo is None


def test_215b_serie_marcador_evergreen_nao_selecionada(app):
    with app.app_context():
        from app.run_cleiton_agente_serie import selecionar_item_para_missao

        _criar_item_serie(titulo="[evergreen] O que é cubagem")
        item, motivo = selecionar_item_para_missao()
        assert item is None
        assert motivo is None


def test_215b_serie_analysis_preservada(app):
    with app.app_context():
        from app.run_cleiton_agente_serie import selecionar_item_para_missao

        _serie, expected = _criar_item_serie(
            titulo="[analysis] Tendências do transporte rodoviário em 2026"
        )
        item, motivo = selecionar_item_para_missao()
        assert item is not None
        assert item.id == expected.id
        assert motivo in ("serie_dia", "serie_atrasada")


def test_215b_manual_heuristica_evergreen_ok(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        captured = {}

        def _despachar(payload, _app):
            captured["payload"] = payload
            return True

        _patch_ciclo_basico(monkeypatch, despachar_fn=_despachar)
        resultado = agent_service.executar_artigo_manual_sincrono(
            app, pauta_id=pauta.id, intencao_editorial="evergreen"
        )
        assert resultado["status"] == "sucesso"
        assert captured["payload"]["pauta_id"] == pauta.id
        assert captured["payload"]["intencao_editorial"] == "evergreen"


def test_215b_manual_analysis_explicita_nao_bloqueada(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        pauta = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        captured = {}

        def _despachar(payload, _app):
            captured["payload"] = payload
            return True

        _patch_ciclo_basico(monkeypatch, despachar_fn=_despachar)
        resultado = agent_service.executar_artigo_manual_sincrono(
            app, pauta_id=pauta.id, intencao_editorial="analysis"
        )
        assert resultado["status"] == "sucesso"
        assert captured["payload"]["pauta_id"] == pauta.id
        assert captured["payload"]["intencao_editorial"] == "analysis"


def test_215b_sidecar_seleciona_heuristica_evergreen(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        pauta = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        assert selecionar_pauta_evergreen_automatico().id == pauta.id
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: True,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "sucesso"
        assert ev["pauta_id"] == pauta.id
        assert ev["tipo_missao"] == "artigo"
        evergreen_payloads = [
            p for p in payloads if p.get("intencao_editorial") == "evergreen"
        ]
        assert len(evergreen_payloads) == 1
        assert evergreen_payloads[0]["pauta_id"] == pauta.id


def test_215b_legado_e_sidecar_nao_consomem_mesma_pauta(app, monkeypatch):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=30)
        analysis = _criar_pauta(titulo="Impactos recentes da nova tabela de frete")
        evergreen = _criar_pauta(titulo=_TITULO_HEURISTICO_EVERGREEN)
        monkeypatch.setattr(
            "app.run_cleiton_agente_orquestrador._artigo_publicado_hoje",
            lambda: False,
        )
        payloads = _patch_ciclo_basico(monkeypatch)
        resultado = executar_ciclo_gerencial(app, bypass_frequencia=True)
        assert resultado["status"] == "sucesso"
        assert resultado.get("pauta_id") == analysis.id
        ev = resultado["evergreen_automatico"]
        assert ev["status"] == "sucesso"
        assert ev["pauta_id"] == evergreen.id
        pauta_ids = [p.get("pauta_id") for p in payloads]
        assert pauta_ids.count(analysis.id) == 1
        assert pauta_ids.count(evergreen.id) == 1
        assert analysis.id != evergreen.id


# --- 24. Admin ---


def test_215b_admin_ultima_execucao_e_proxima(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=120)
        ultima = _registrar_evergreen_auto_sucesso(
            when=_utcnow() - timedelta(minutes=30)
        )
        cfg = agent_service.obter_evergreen_config()
        assert cfg["ultima_execucao_automatica"] == ultima.created_at
        assert cfg["proxima_elegibilidade"] is not None
        expected = ultima.created_at + timedelta(minutes=120)
        assert cfg["proxima_elegibilidade"] == expected


def test_215b_admin_automatico_false_sem_proxima_enganosa(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=False, frequencia_minutos=60)
        cfg = agent_service.obter_evergreen_config()
        assert cfg["proxima_elegibilidade"] is None
        assert "manual" in (cfg["proxima_elegibilidade_label"] or "").lower()
        assert "desabilitada" in (cfg["proxima_elegibilidade_label"] or "").lower()


def test_215b_admin_nunca_executado_proximo_acionamento(app):
    with app.app_context():
        configurar_evergreen(automatico_habilitado=True, frequencia_minutos=60)
        cfg = agent_service.obter_evergreen_config()
        assert cfg["ultima_execucao_automatica"] is None
        assert cfg["proxima_elegibilidade"] is None
        assert "próximo acionamento" in (
            cfg["proxima_elegibilidade_label"] or ""
        ).lower() or "proximo acionamento" in (
            cfg["proxima_elegibilidade_label"] or ""
        ).lower()


def test_215b_ui_mostra_proxima_elegibilidade():
    html = pathlib.Path(
        "app/painel_admin/template_admin/agentes_julia.html"
    ).read_text(encoding="utf-8")
    assert "Próxima elegibilidade evergreen" in html
    assert "Última execução automática evergreen" in html
    assert "Automação desabilitada — geração manual disponível." in html
    assert "Próxima execução garantida" not in html
