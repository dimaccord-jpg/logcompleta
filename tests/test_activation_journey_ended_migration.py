"""Validação focada da migration w7x8y9z0a1b2 (término da jornada de ativação)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models import Lead

ROOT = Path(__file__).resolve().parents[1]


def test_migration_w7x8y9z0a1b2_na_chain_e_aditiva():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("w7x8y9z0a1b2")
    assert rev is not None
    assert rev.down_revision == "v6w7x8y9z0a1"
    nxt = script.get_revision("x8y9z0a1b2c3")
    assert nxt is not None
    assert nxt.down_revision == "w7x8y9z0a1b2"
    hmac_rev = script.get_revision("y9z0a1b2c3d4")
    assert hmac_rev is not None
    assert hmac_rev.down_revision == "x8y9z0a1b2c3"
    assert script.get_revision("y9z0a1b2c3d4") is not None

    path = ROOT / "migrations" / "versions" / "w7x8y9z0a1b2_lead_activation_journey_ended.py"
    source = path.read_text(encoding="utf-8")
    assert "activation_ended_at" in source
    assert "activation_ended_for_user_id" in source
    assert 'batch_alter_table("leads"' in source
    assert 'batch_alter_table("users"' not in source
    assert "op.create_table" not in source
    assert "drop_table" not in source
    assert "drop_column" in source


def test_model_lead_possui_campos_jornada_encerrada():
    lead_cols = {c.name for c in Lead.__table__.columns}
    assert "activation_ended_at" in lead_cols
    assert "activation_ended_for_user_id" in lead_cols
    assert "activation_opt_out_at" in lead_cols
    assert "opt_out_at" in lead_cols


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "activation_ended_mig.sqlite"
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

    mig_path = (
        ROOT / "migrations" / "versions" / "w7x8y9z0a1b2_lead_activation_journey_ended.py"
    )
    spec = importlib.util.spec_from_file_location("activation_ended_mig_mod", mig_path)
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
    assert "activation_ended_at" in lead_cols
    assert "activation_ended_for_user_id" in lead_cols

    _run(mig.downgrade)
    lead_cols = {c["name"] for c in inspect(engine).get_columns("leads")}
    assert "activation_ended_at" not in lead_cols
    assert "activation_ended_for_user_id" not in lead_cols
