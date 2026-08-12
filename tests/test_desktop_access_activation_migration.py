"""Validação focada da migration u5v6w7x8y9z0 (campos de ativação)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models import DesktopAccessE2ETestRun, Lead

ROOT = Path(__file__).resolve().parents[1]


def test_migration_u5v6w7x8y9z0_na_chain_e_aditiva():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("u5v6w7x8y9z0")
    assert rev is not None
    assert rev.down_revision == "t4u5v6w7x8y9"
    heads = script.get_heads()
    assert "u5v6w7x8y9z0" in heads

    path = ROOT / "migrations" / "versions" / "u5v6w7x8y9z0_desktop_access_activation_fields.py"
    source = path.read_text(encoding="utf-8")
    assert "activation_email_1_sent_at" in source
    assert "activation_email_2_sent_at" in source
    assert "activation_opt_out_at" in source
    assert "activation_sequence_started_at" in source
    assert "leads" in source
    assert "desktop_access_e2e_test_run" in source
    assert "drop_column" in source
    assert "funnel_event" not in source
    assert "create_table" not in source


def test_models_possuem_campos_ativacao():
    lead_cols = {c.name for c in Lead.__table__.columns}
    for required in (
        "activation_email_1_sent_at",
        "activation_email_2_sent_at",
        "activation_opt_out_at",
    ):
        assert required in lead_cols

    e2e_cols = {c.name for c in DesktopAccessE2ETestRun.__table__.columns}
    for required in (
        "activation_email_1_sent_at",
        "activation_email_2_sent_at",
        "activation_opt_out_at",
        "activation_sequence_started_at",
    ):
        assert required in e2e_cols


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "activation_mig.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE leads (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(150) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE desktop_access_e2e_test_run (
                    id INTEGER PRIMARY KEY,
                    run_id VARCHAR(64) NOT NULL,
                    user_id INTEGER NOT NULL
                )
                """
            )
        )

    mig_path = (
        ROOT
        / "migrations"
        / "versions"
        / "u5v6w7x8y9z0_desktop_access_activation_fields.py"
    )
    spec = importlib.util.spec_from_file_location("activation_mig_mod", mig_path)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    def _run(fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"render_as_batch": True}
            )
            ops = Operations(context)
            original_op = mig.op
            try:
                mig.op = ops
                with conn.begin():
                    fn()
            finally:
                mig.op = original_op

    _run(mig.upgrade)
    lead_cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    e2e_cols = {
        c["name"] for c in inspect(engine).get_columns("desktop_access_e2e_test_run")
    }
    assert "activation_email_1_sent_at" in lead_cols
    assert "activation_email_2_sent_at" in lead_cols
    assert "activation_opt_out_at" in lead_cols
    assert "activation_email_1_sent_at" in e2e_cols
    assert "activation_email_2_sent_at" in e2e_cols
    assert "activation_opt_out_at" in e2e_cols
    assert "activation_sequence_started_at" in e2e_cols

    _run(mig.downgrade)
    lead_cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    e2e_cols = {
        c["name"] for c in inspect(engine).get_columns("desktop_access_e2e_test_run")
    }
    assert "activation_email_1_sent_at" not in lead_cols
    assert "activation_email_2_sent_at" not in lead_cols
    assert "activation_opt_out_at" not in lead_cols
    assert "activation_email_1_sent_at" not in e2e_cols
    assert "activation_sequence_started_at" not in e2e_cols
