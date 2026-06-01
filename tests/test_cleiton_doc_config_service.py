import pytest

from app.extensions import db
from app.models import CleitonCostConfig, ConfigRegras
from app.services import cleiton_doc_config_service as svc
from app.services.cleiton_cost_service import SINGLETON_ID, get_or_create_config, save_config


def test_defaults_carregam_corretamente(ctx):
    cfg = svc.get_cleiton_doc_config()

    assert cfg.upload_enabled is True
    assert cfg.max_files_per_session == 5
    assert cfg.session_max_bytes == 15 * 1024 * 1024
    assert cfg.upload_ttl_hours == 48
    assert cfg.cleanup_enabled is True
    assert cfg.prompt_context_max_chars == 24000
    assert cfg.prompt_max_files_considered == 3
    assert cfg.pdf_max_pages == 50
    assert cfg.excel_max_rows == 5000
    assert cfg.docx_max_paragraphs == 5000
    assert cfg.txt_max_bytes == 1 * 1024 * 1024
    assert cfg.xml_max_depth == 20
    assert cfg.csv_max_rows == 10000


def test_gravacao_e_leitura_via_config_regras(ctx):
    saved = svc.salvar_cleiton_doc_config(
        {
            "upload_enabled": "0",
            "max_files_per_session": "4",
            "session_max_bytes": str(9 * 1024 * 1024),
            "upload_ttl_hours": "24",
            "cleanup_enabled": "1",
            "prompt_context_max_chars": "18000",
            "prompt_max_files_considered": "2",
            "pdf_enabled": "1",
            "pdf_max_bytes": str(4 * 1024 * 1024),
            "pdf_max_pages": "30",
            "pdf_max_chars": "90000",
            "excel_enabled": "1",
            "excel_max_bytes": str(4 * 1024 * 1024),
            "excel_max_rows": "4000",
            "excel_max_columns": "60",
            "excel_max_chars": "100000",
            "docx_enabled": "0",
            "docx_max_bytes": str(3 * 1024 * 1024),
            "docx_max_paragraphs": "3000",
            "docx_max_chars": "80000",
            "txt_enabled": "1",
            "txt_max_bytes": str(512 * 1024),
            "txt_max_chars": "70000",
            "xml_enabled": "1",
            "xml_max_bytes": str(1024 * 1024),
            "xml_max_nodes": "12000",
            "xml_max_depth": "10",
            "xml_max_chars": "60000",
            "csv_enabled": "1",
            "csv_max_bytes": str(1024 * 1024),
            "csv_max_rows": "8000",
            "csv_max_columns": "40",
            "csv_max_chars": "75000",
        }
    )

    assert saved.upload_enabled is False
    assert saved.max_files_per_session == 4
    assert saved.prompt_max_files_considered == 2
    assert saved.docx_enabled is False

    rows = ConfigRegras.query.filter(ConfigRegras.chave.like("cleiton_doc_%")).all()
    assert rows
    loaded = svc.get_cleiton_doc_config()
    assert loaded.session_max_bytes == 9 * 1024 * 1024
    assert loaded.pdf_max_pages == 30
    assert loaded.xml_max_depth == 10


def test_booleanos_habilitado_desabilitado(ctx):
    saved = svc.salvar_cleiton_doc_config(
        {
            "upload_enabled": "off",
            "max_files_per_session": "5",
            "session_max_bytes": str(15 * 1024 * 1024),
            "upload_ttl_hours": "48",
            "cleanup_enabled": "false",
            "prompt_context_max_chars": "24000",
            "prompt_max_files_considered": "3",
            "pdf_enabled": "no",
            "pdf_max_bytes": str(5 * 1024 * 1024),
            "pdf_max_pages": "50",
            "pdf_max_chars": "120000",
            "excel_enabled": "0",
            "excel_max_bytes": str(5 * 1024 * 1024),
            "excel_max_rows": "5000",
            "excel_max_columns": "80",
            "excel_max_chars": "120000",
            "docx_enabled": "1",
            "docx_max_bytes": str(5 * 1024 * 1024),
            "docx_max_paragraphs": "5000",
            "docx_max_chars": "120000",
            "txt_enabled": "1",
            "txt_max_bytes": str(1 * 1024 * 1024),
            "txt_max_chars": "120000",
            "xml_enabled": "1",
            "xml_max_bytes": str(2 * 1024 * 1024),
            "xml_max_nodes": "20000",
            "xml_max_depth": "20",
            "xml_max_chars": "120000",
            "csv_enabled": "1",
            "csv_max_bytes": str(2 * 1024 * 1024),
            "csv_max_rows": "10000",
            "csv_max_columns": "80",
            "csv_max_chars": "120000",
        }
    )

    assert saved.upload_enabled is False
    assert saved.cleanup_enabled is False
    assert saved.pdf_enabled is False
    assert saved.excel_enabled is False


def test_validacao_ttl_invalido(ctx):
    try:
        svc.salvar_cleiton_doc_config(
            {
                "upload_enabled": "1",
                "max_files_per_session": "5",
                "session_max_bytes": str(15 * 1024 * 1024),
                "upload_ttl_hours": "0",
                "cleanup_enabled": "1",
                "prompt_context_max_chars": "24000",
                "prompt_max_files_considered": "3",
                "pdf_enabled": "1",
                "pdf_max_bytes": str(5 * 1024 * 1024),
                "pdf_max_pages": "50",
                "pdf_max_chars": "120000",
                "excel_enabled": "1",
                "excel_max_bytes": str(5 * 1024 * 1024),
                "excel_max_rows": "5000",
                "excel_max_columns": "80",
                "excel_max_chars": "120000",
                "docx_enabled": "1",
                "docx_max_bytes": str(5 * 1024 * 1024),
                "docx_max_paragraphs": "5000",
                "docx_max_chars": "120000",
                "txt_enabled": "1",
                "txt_max_bytes": str(1 * 1024 * 1024),
                "txt_max_chars": "120000",
                "xml_enabled": "1",
                "xml_max_bytes": str(2 * 1024 * 1024),
                "xml_max_nodes": "20000",
                "xml_max_depth": "20",
                "xml_max_chars": "120000",
                "csv_enabled": "1",
                "csv_max_bytes": str(2 * 1024 * 1024),
                "csv_max_rows": "10000",
                "csv_max_columns": "80",
                "csv_max_chars": "120000",
            }
        )
        raise AssertionError("Era esperado ValueError")
    except ValueError as exc:
        assert "upload_ttl_hours" in str(exc)


def test_validacao_maximo_arquivos_invalido(ctx):
    try:
        svc.salvar_cleiton_doc_config({**_payload_valido(), "max_files_per_session": "0"})
        raise AssertionError("Era esperado ValueError")
    except ValueError as exc:
        assert "max_files_per_session" in str(exc)


def test_validacao_arquivos_considerados_maior_que_sessao(ctx):
    try:
        svc.salvar_cleiton_doc_config(
            {**_payload_valido(), "max_files_per_session": "2", "prompt_max_files_considered": "3"}
        )
        raise AssertionError("Era esperado ValueError")
    except ValueError as exc:
        assert "prompt_max_files_considered" in str(exc)


def test_validacao_bytes_invalidos(ctx):
    try:
        svc.salvar_cleiton_doc_config({**_payload_valido(), "pdf_max_bytes": "-1"})
        raise AssertionError("Era esperado ValueError")
    except ValueError as exc:
        assert "pdf_max_bytes" in str(exc)


def test_checkbox_desmarcado_persiste_como_desligado(ctx):
    svc.salvar_cleiton_doc_config({**_payload_valido(), "upload_enabled": "on", "pdf_enabled": "on"})
    saved = svc.salvar_cleiton_doc_config(
        {
            **_payload_valido(),
            "upload_enabled": None,
            "pdf_enabled": None,
            "excel_enabled": None,
        }
    )
    assert saved.upload_enabled is False
    assert saved.pdf_enabled is False
    assert saved.excel_enabled is False

    row_upload = ConfigRegras.query.filter_by(chave="cleiton_doc_upload_enabled").first()
    row_pdf = ConfigRegras.query.filter_by(chave="cleiton_doc_pdf_enabled").first()
    assert row_upload is not None and row_upload.valor_inteiro == 0
    assert row_pdf is not None and row_pdf.valor_inteiro == 0


def test_leitura_relacao_invalida_faz_clamp_sem_resetar_defaults(ctx, caplog):
    svc.salvar_cleiton_doc_config(
        {
            **_payload_valido(),
            "max_files_per_session": "2",
            "prompt_max_files_considered": "2",
        }
    )
    row_max = ConfigRegras.query.filter_by(chave="cleiton_doc_max_files_per_session").first()
    row_prompt = ConfigRegras.query.filter_by(chave="cleiton_doc_prompt_max_files_considered").first()
    row_prompt.valor_inteiro = 5
    row_max.valor_inteiro = 2
    db.session.commit()

    caplog.clear()
    cfg = svc.get_cleiton_doc_config()

    assert cfg.max_files_per_session == 2
    assert cfg.prompt_max_files_considered == 2
    assert cfg.session_max_bytes == 15 * 1024 * 1024
    assert cfg.upload_ttl_hours == 48
    assert any("clamp" in rec.message.lower() for rec in caplog.records)


def test_falha_documental_nao_salva_parcialmente_custo(ctx):
    save_config(
        runtime_monthly_cost=111.0,
        month_seconds=2592000,
        allocation_percent=1.0,
        overhead_factor=1.0,
        cost_per_million_tokens=None,
    )
    cost_row = get_or_create_config()
    assert float(cost_row.runtime_monthly_cost) == 111.0

    with pytest.raises(ValueError) as exc:
        svc.salvar_agentes_cleiton_config(
            cost_kwargs={
                "runtime_monthly_cost": 999.0,
                "month_seconds": 2592000,
                "allocation_percent": 1.0,
                "overhead_factor": 1.0,
                "cost_per_million_tokens": None,
            },
            doc_campos={**_payload_valido(), "max_files_per_session": "2", "prompt_max_files_considered": "3"},
        )
    assert "prompt_max_files_considered" in str(exc.value)

    db_row = db.session.get(CleitonCostConfig, SINGLETON_ID)
    assert float(db_row.runtime_monthly_cost) == 111.0


def _payload_valido():
    return {
        "upload_enabled": "1",
        "max_files_per_session": "5",
        "session_max_bytes": str(15 * 1024 * 1024),
        "upload_ttl_hours": "48",
        "cleanup_enabled": "1",
        "prompt_context_max_chars": "24000",
        "prompt_max_files_considered": "3",
        "pdf_enabled": "1",
        "pdf_max_bytes": str(5 * 1024 * 1024),
        "pdf_max_pages": "50",
        "pdf_max_chars": "120000",
        "excel_enabled": "1",
        "excel_max_bytes": str(5 * 1024 * 1024),
        "excel_max_rows": "5000",
        "excel_max_columns": "80",
        "excel_max_chars": "120000",
        "docx_enabled": "1",
        "docx_max_bytes": str(5 * 1024 * 1024),
        "docx_max_paragraphs": "5000",
        "docx_max_chars": "120000",
        "txt_enabled": "1",
        "txt_max_bytes": str(1 * 1024 * 1024),
        "txt_max_chars": "120000",
        "xml_enabled": "1",
        "xml_max_bytes": str(2 * 1024 * 1024),
        "xml_max_nodes": "20000",
        "xml_max_depth": "20",
        "xml_max_chars": "120000",
        "csv_enabled": "1",
        "csv_max_bytes": str(2 * 1024 * 1024),
        "csv_max_rows": "10000",
        "csv_max_columns": "80",
        "csv_max_chars": "120000",
    }
