import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from app.roberto_bi import _filtrar_grupos_por_min_linhas, _limitar_rows_por_data
from app.roberto_modelo import _limitar_historico_por_mes
from app.roberto_upload_store import maybe_cleanup_expired_uploads, read_upload_data, save_upload_data
from app.services.roberto_config_service import (
    DEFAULTS,
    get_roberto_config,
    salvar_roberto_config,
)
from app.upload_handler import _aplicar_limite_upload_total, get_dados_upload_cliente


@pytest.fixture
def roberto_temp_dir():
    base_root = Path(__file__).resolve().parent / ".tmp_roberto"
    base_root.mkdir(parents=True, exist_ok=True)
    tmp_dir = base_root / f"roberto-tests-{uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield tmp_dir
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_roberto_config_defaults_and_save(app):
    with app.app_context():
        cfg = get_roberto_config()
        assert cfg.upload_total_max == 10000
        assert cfg.previsao_meses == 18
        assert cfg.min_linhas_mes_modelo == 10
        assert cfg.min_linhas_uf_heatmap_ranking == 10
        assert cfg.max_pontos_dispersao == 500
        assert cfg.upload_ttl_minutes == 30
        # Valores iniciais provisórios sujeitos a calibração.
        assert cfg.max_linhas_mes_modelo == DEFAULTS["max_linhas_mes_modelo"]
        assert cfg.max_linhas_uf_heatmap == DEFAULTS["max_linhas_uf_heatmap"]
        assert cfg.max_linhas_uf_ranking == DEFAULTS["max_linhas_uf_ranking"]
        assert cfg.chat_max_history == DEFAULTS["chat_max_history"]

        salvar_roberto_config(
            {
                "upload_total_max": "12000",
                "previsao_meses": "24",
                "min_linhas_mes_modelo": "12",
                "min_linhas_uf_heatmap_ranking": "15",
                "max_pontos_dispersao": "800",
                "max_linhas_mes_modelo": "450",
                "max_linhas_uf_heatmap": "550",
                "max_linhas_uf_ranking": "650",
                "upload_ttl_minutes": "45",
                "chat_max_history": "22",
            }
        )
        cfg2 = get_roberto_config()
        assert cfg2.upload_total_max == 12000
        assert cfg2.previsao_meses == 24
        assert cfg2.min_linhas_mes_modelo == 12
        assert cfg2.min_linhas_uf_heatmap_ranking == 15
        assert cfg2.max_pontos_dispersao == 800
        assert cfg2.max_linhas_mes_modelo == 450
        assert cfg2.max_linhas_uf_heatmap == 550
        assert cfg2.max_linhas_uf_ranking == 650
        assert cfg2.upload_ttl_minutes == 45
        assert cfg2.chat_max_history == 22


def test_upload_total_cap_preserva_representatividade_temporal():
    rows = []
    for mes in ("2024-01", "2024-02", "2024-03", "2024-04"):
        rows.extend(
            [
                {"data_emissao": f"{mes}-01", "valor_frete_total": 10.0},
                {"data_emissao": f"{mes}-15", "valor_frete_total": 11.0},
                {"data_emissao": f"{mes}-28", "valor_frete_total": 12.0},
            ]
        )

    kept, stats = _aplicar_limite_upload_total(rows, 8)
    meses_kept = {str(r["data_emissao"])[:7] for r in kept}
    assert len(kept) == 8
    # Com limite suficiente para cobrir os meses, preserva presença temporal mínima.
    assert meses_kept == {"2024-01", "2024-02", "2024-03", "2024-04"}
    assert stats["registros_recebidos"] == 12
    assert stats["registros_utilizados"] == 8
    assert stats["registros_descartados"] == 4


def test_upload_total_cap_quando_limite_menor_que_meses_mantem_amplitude_temporal():
    rows = [
        {"data_emissao": "2024-01-02"},
        {"data_emissao": "2024-02-02"},
        {"data_emissao": "2024-03-02"},
        {"data_emissao": "2024-04-02"},
        {"data_emissao": "2024-05-02"},
    ]
    kept, stats = _aplicar_limite_upload_total(rows, 2)
    assert len(kept) == 2
    meses = sorted(str(r["data_emissao"])[:7] for r in kept)
    assert meses[0] == "2024-01"
    assert meses[1] == "2024-05"
    assert stats["registros_recebidos"] == 5
    assert stats["registros_utilizados"] == 2
    assert stats["registros_descartados"] == 3


def test_upload_store_respects_ttl(roberto_temp_dir, monkeypatch):
    import app.roberto_upload_store as store

    monkeypatch.setattr(store, "_base_dir", lambda: str(roberto_temp_dir))
    upload_id = save_upload_data([{"data_emissao": "2024-01-01"}])

    assert read_upload_data(upload_id, 30) == [{"data_emissao": "2024-01-01"}]

    fpath = roberto_temp_dir / f"{upload_id}.json"
    payload = json.loads(fpath.read_text(encoding="utf-8"))
    payload["created_at"] = (datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=120)).isoformat()
    fpath.write_text(json.dumps(payload), encoding="utf-8")

    assert read_upload_data(upload_id, 30) is None
    assert not fpath.exists()


def test_get_dados_upload_cliente_nao_varre_diretorio_por_request_comum(roberto_temp_dir, monkeypatch, app):
    import app.roberto_upload_store as store

    monkeypatch.setattr(store, "_base_dir", lambda: str(roberto_temp_dir))
    upload_id = save_upload_data([{"data_emissao": "2024-01-01"}])

    def _listdir_fail(_path):
        raise AssertionError("listdir não deveria ser chamado no caminho comum de leitura")

    monkeypatch.setattr(store.os, "listdir", _listdir_fail)
    app.config["SECRET_KEY"] = "test-secret"
    with app.test_request_context("/api/roberto_bi/custo_medio", method="GET"):
        from flask import session

        session["roberto_upload_ref"] = upload_id
        rows = get_dados_upload_cliente()
        assert rows == [{"data_emissao": "2024-01-01"}]


def test_cleanup_periodico_evitando_varredura_frequente(roberto_temp_dir, monkeypatch):
    import app.roberto_upload_store as store

    monkeypatch.setattr(store, "_base_dir", lambda: str(roberto_temp_dir))
    # Primeira chamada cria metadado de sweep
    maybe_cleanup_expired_uploads(30, min_interval_seconds=3600)

    def _fail_cleanup(_ttl):
        raise AssertionError("cleanup_expired_uploads não deveria rodar antes do intervalo")

    monkeypatch.setattr(store, "cleanup_expired_uploads", _fail_cleanup)
    # Segunda chamada imediata deve pular sweep pesado.
    assert maybe_cleanup_expired_uploads(30, min_interval_seconds=3600) == 0


def test_cleanup_expired_uploads_ignora_arquivo_meta(roberto_temp_dir, monkeypatch):
    import app.roberto_upload_store as store

    monkeypatch.setattr(store, "_base_dir", lambda: str(roberto_temp_dir))
    meta_path = roberto_temp_dir / ".cleanup_meta.json"
    meta_path.write_text(json.dumps({"last_run_at": datetime.now(UTC).isoformat()}), encoding="utf-8")
    removed = store.cleanup_expired_uploads(30)
    assert removed == 0
    assert meta_path.exists()


def test_bi_min_linhas_e_limite_por_data():
    grupos = {
        "SP": [{"data_emissao": "2024-01-01"}] * 4,
        "RJ": [{"data_emissao": "2024-01-01"}] * 2,
    }
    filtrado = _filtrar_grupos_por_min_linhas(grupos, 3)
    assert set(filtrado.keys()) == {"SP"}

    rows = [
        {"data_emissao": "2024-01-01"},
        {"data_emissao": "2024-01-03"},
        {"data_emissao": "2024-01-02"},
    ]
    limited = _limitar_rows_por_data(rows, 2)
    assert [r["data_emissao"] for r in limited] == ["2024-01-03", "2024-01-02"]


def test_ranking_heatmap_desacoplados_por_parametros(app, monkeypatch):
    import app.roberto_bi as bi

    dataset = []
    for i in range(1, 81):
        dataset.append(
            {
                "uf_destino": "SP",
                "peso_real": 10.0,
                "valor_frete_total": float(10 + i),
                "data_emissao": f"2024-01-{(i % 28) + 1:02d}",
                "modal": "rodoviario",
                "peso_registro": 1.0,
            }
        )

    with app.app_context():
        salvar_roberto_config(
            {
                "upload_total_max": "10000",
                "previsao_meses": "18",
                "min_linhas_mes_modelo": "10",
                "min_linhas_uf_heatmap_ranking": "1",
                "max_pontos_dispersao": "500",
                "max_linhas_mes_modelo": "300",
                    "max_linhas_uf_heatmap": "20",
                    "max_linhas_uf_ranking": "40",
                "upload_ttl_minutes": "30",
            }
        )

        monkeypatch.setattr(bi, "_get_bi_dataset", lambda: dataset)

        lens_custo = []

        def _fake_custo(rows):
            lens_custo.append(len(rows))
            return 1.0

        monkeypatch.setattr(bi, "calcular_custo_robusto_rs_kg", _fake_custo)
        out_ranking = bi.ranking_ufs()
        assert out_ranking["ufs"] == ["SP"]
        assert lens_custo == [40]

        lens_heatmap = []

        def _fake_prever(historico, _indices):
            lens_heatmap.append(len(historico))
            return {
                "previsao_numerica": {"valores_rs_kg": [1.0, 1.1]},
                "qualidade_previsao": {},
            }

        monkeypatch.setattr(bi, "roberto_prever", _fake_prever)
        out_heatmap = bi.heatmap_brasil()
        assert out_heatmap["ufs"] == ["SP"]
        assert lens_heatmap == [20]


def test_roberto_config_validacao_cruzada_bloqueia_combinacao_invalida(app):
    with app.app_context():
        # min_linhas_mes_modelo > max_linhas_mes_modelo
        try:
            salvar_roberto_config(
                {
                    "upload_total_max": "10000",
                    "previsao_meses": "18",
                    "min_linhas_mes_modelo": "500",
                    "min_linhas_uf_heatmap_ranking": "10",
                    "max_pontos_dispersao": "500",
                    "max_linhas_mes_modelo": "100",
                    "max_linhas_uf_heatmap": "300",
                    "max_linhas_uf_ranking": "300",
                    "upload_ttl_minutes": "30",
                }
            )
            assert False, "Esperava ValueError para relação min/max mensal inválida"
        except ValueError as e:
            assert "min_linhas_mes_modelo" in str(e)

        # mínimo UF maior que máximo do ranking
        try:
            salvar_roberto_config(
                {
                    "upload_total_max": "10000",
                    "previsao_meses": "18",
                    "min_linhas_mes_modelo": "10",
                    "min_linhas_uf_heatmap_ranking": "400",
                    "max_pontos_dispersao": "500",
                    "max_linhas_mes_modelo": "500",
                    "max_linhas_uf_heatmap": "500",
                    "max_linhas_uf_ranking": "100",
                    "upload_ttl_minutes": "30",
                }
            )
            assert False, "Esperava ValueError para relação min/max UF inválida"
        except ValueError as e:
            assert "max_linhas_uf_ranking" in str(e)


def test_modelo_limita_linhas_por_mes():
    historico = [
        {"data_emissao": "2024-01-01", "valor": 10, "peso": 1},
        {"data_emissao": "2024-01-02", "valor": 10, "peso": 1},
        {"data_emissao": "2024-01-03", "valor": 10, "peso": 1},
        {"data_emissao": "2024-02-01", "valor": 11, "peso": 1},
        {"data_emissao": "2024-02-02", "valor": 11, "peso": 1},
    ]
    out = _limitar_historico_por_mes(historico, 2)
    jan = [r for r in out if str(r.get("data_emissao")).startswith("2024-01")]
    fev = [r for r in out if str(r.get("data_emissao")).startswith("2024-02")]
    assert len(jan) == 2
    assert len(fev) == 2
