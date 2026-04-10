from types import SimpleNamespace

import app.run_roberto_chat as rc


def test_chat_roberto_reply_sem_chave(monkeypatch):
    monkeypatch.setattr(
        rc,
        "get_contexto_bi_roberto_upload_only",
        lambda: {"unidos": [{"peso_real": 1, "valor_frete_total": 1}], "serie_temporal": {}, "qualidade_base": {}, "recomendacoes_analise": []},
    )
    monkeypatch.setattr(rc, "_register_snapshot_processing_event", lambda **kwargs: None)
    monkeypatch.setattr(rc, "_get_client", lambda: None)
    out = rc.chat_roberto_reply("Analise custo medio", [], max_history=10)
    assert "GEMINI_API_KEY_ROBERTO" in out["reply"]
    assert isinstance(out.get("suggestions"), list)


def test_chat_roberto_reply_aplica_janela_historico(monkeypatch):
    captured = {}
    proc_events = []

    def _fake_governed(client, *, model, contents, agent, flow_type, api_key_label):
        captured["model"] = model
        captured["contents"] = contents
        captured["agent"] = agent
        captured["flow_type"] = flow_type
        captured["api_key_label"] = api_key_label
        return SimpleNamespace(text="Resposta Roberto")

    monkeypatch.setattr(rc, "_get_client", lambda: object())
    monkeypatch.setattr(rc, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        rc,
        "heatmap_brasil_upload_only",
        lambda: {
            "ufs": ["GO", "SP", "MG"],
            "valores": [0.12, 0.08, 0.02],
            "nivel_temperatura": ["muito_quente", "quente", "neutro"],
            "tendencia_alta": [True, True, False],
            "qualidade_uf": [None, None, None],
        },
    )
    monkeypatch.setattr(
        rc,
        "get_contexto_bi_roberto_upload_only",
        lambda: {
            "unidos": [
                {
                    "peso_real": 10.0,
                    "peso_registro": 1.0,
                    "valor_frete_total": 20.0,
                    "uf_destino": "SP",
                    "modal": "rodoviario",
                }
            ],
            "serie_temporal": {"meses": ["2024-01"], "valores": [2.0], "previsao_meses": ["2024-02"]},
            "qualidade_base": {},
            "recomendacoes_analise": [],
        },
    )
    monkeypatch.setattr(rc, "_register_snapshot_processing_event", lambda **kwargs: proc_events.append(kwargs))
    monkeypatch.setattr(rc, "cleiton_governed_generate_content", _fake_governed)

    history = [
        {"role": "user", "content": "m1"},
        {"role": "model", "content": "m2"},
        {"role": "user", "content": "m3"},
        {"role": "model", "content": "m4"},
        {"role": "user", "content": "m5"},
    ]
    out = rc.chat_roberto_reply("Consolidar cenario", history, max_history=2)

    assert out["reply"] == "Resposta Roberto"
    assert captured["agent"] == "roberto"
    assert captured["flow_type"] == "roberto_chat_fretes"
    assert captured["api_key_label"] == "GEMINI_API_KEY_ROBERTO"
    assert "Usuario: m5" in captured["contents"] or "Usuário: m5" in captured["contents"] or "UsuÃ¡rio: m5" in captured["contents"]
    assert "Roberto: m4" in captured["contents"]
    assert '"graficos_tela"' in captured["contents"]
    assert '"heatmap_brasil_ufs_destino"' in captured["contents"]
    assert '"ufs_em_alta":["GO","SP"]' in captured["contents"]
    assert '"ranking_ufs_destino"' in captured["contents"]
    assert '"proporcao_por_modal"' in captured["contents"]
    assert '"dispersao_peso_x_custo"' in captured["contents"]
    assert len(proc_events) == 1
    assert proc_events[0]["status"] == "success"
    assert proc_events[0]["rows_processed"] == 0


def test_chat_roberto_reply_sem_upload_ativo_nao_chama_modelo(monkeypatch):
    monkeypatch.setattr(rc, "get_contexto_bi_roberto_upload_only", lambda: None)
    monkeypatch.setattr(rc, "_get_client", lambda: object())

    called = {"llm": 0}

    def _fake_llm(*args, **kwargs):
        called["llm"] += 1
        return SimpleNamespace(text="nao deveria chamar")

    monkeypatch.setattr(rc, "cleiton_governed_generate_content", _fake_llm)
    out = rc.chat_roberto_reply("Analise os dados", [], max_history=5)
    assert out.get("requires_upload") is True
    assert out.get("snapshot", {}).get("upload_ativo") is False
    assert called["llm"] == 0
