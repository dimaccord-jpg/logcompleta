"""Downgrade da migration j1k2l3m4n5o6 com evento autorizacao_negada já gravado."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = (
    ROOT
    / "migrations"
    / "versions"
    / "j1k2l3m4n5o6_central_plugins_autorizacao_negada.py"
)

_SQL_TIPOS_LOTE_1 = (
    "tipo_evento IN ("
    "'conexao_criada', 'conexao_estado_alterado', "
    "'restricao_usuario_alterada', 'bloqueado', 'desabilitado', "
    "'desconectado', 'revogado'"
    ")"
)
_SQL_DETALHES_LOTE_1 = (
    "detalhe_codigo IS NULL OR detalhe_codigo IN ("
    "'criada', 'estado_alterado', 'restricao_registrada', 'restricao_removida'"
    ")"
)


def _carregar():
    spec = importlib.util.spec_from_file_location("central_plugins_auth_mig", _MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)
    return mig


def _run(engine, mig, fn):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"render_as_batch": True})
        ops = Operations(context)
        original = mig.op
        try:
            mig.op = ops
            with conn.begin():
                fn()
        finally:
            mig.op = original


def _inserir(engine, tipo, detalhe):
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO plugin_evento_central (
                    tipo_evento, detalhe_codigo, conexao_id, plugin_id,
                    user_id, usuario_originador_id, created_at
                ) VALUES (
                    :tipo, :detalhe, 1, 1, 1, 1, '2026-09-30 12:00:00'
                )
                """
            ),
            {"tipo": tipo, "detalhe": detalhe},
        )


def _rejeita(engine, tipo, detalhe):
    with engine.connect() as conn:
        transacao = conn.begin()
        try:
            conn.execute(
                text(
                    """
                    INSERT INTO plugin_evento_central (
                        tipo_evento, detalhe_codigo, conexao_id, plugin_id,
                        user_id, usuario_originador_id, created_at
                    ) VALUES (
                        :tipo, :detalhe, 1, 1, 1, 1, '2026-09-30 12:00:00'
                    )
                    """
                ),
                {"tipo": tipo, "detalhe": detalhe},
            )
            transacao.commit()
        except IntegrityError:
            transacao.rollback()
            return
    raise AssertionError(f"insert {tipo}/{detalhe} foi aceito")


def test_downgrade_remove_autorizacao_negada_e_restaura_checks(tmp_path):
    db_path = tmp_path / "central_plugins_auth_mig.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE plugin_evento_central (
                    id INTEGER PRIMARY KEY,
                    tipo_evento VARCHAR(40) NOT NULL,
                    detalhe_codigo VARCHAR(40),
                    conexao_id INTEGER NOT NULL,
                    plugin_id INTEGER NOT NULL,
                    capability_id INTEGER,
                    user_id INTEGER NOT NULL,
                    usuario_originador_id INTEGER NOT NULL,
                    conta_id INTEGER,
                    estado_anterior VARCHAR(40),
                    estado_novo VARCHAR(40),
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_plugin_evento_tipo CHECK ({_SQL_TIPOS_LOTE_1}),
                    CONSTRAINT ck_plugin_evento_detalhe CHECK ({_SQL_DETALHES_LOTE_1})
                )
                """
            )
        )

    _inserir(engine, "conexao_criada", "criada")
    mig = _carregar()
    _run(engine, mig, mig.upgrade)
    _inserir(engine, "autorizacao_negada", "concessao_provedor_negada")

    with engine.connect() as conn:
        negadas = conn.execute(
            text(
                "SELECT COUNT(*) FROM plugin_evento_central "
                "WHERE tipo_evento = 'autorizacao_negada'"
            )
        ).scalar()
    assert negadas == 1

    _run(engine, mig, mig.downgrade)

    with engine.connect() as conn:
        negadas = conn.execute(
            text(
                "SELECT COUNT(*) FROM plugin_evento_central "
                "WHERE tipo_evento = 'autorizacao_negada'"
            )
        ).scalar()
        preservados = conn.execute(
            text(
                "SELECT COUNT(*) FROM plugin_evento_central "
                "WHERE tipo_evento = 'conexao_criada' AND detalhe_codigo = 'criada'"
            )
        ).scalar()
    assert negadas == 0
    assert preservados == 1
    _rejeita(engine, "autorizacao_negada", "concessao_provedor_negada")
    _rejeita(engine, "conexao_criada", "concessao_provedor_ausente")
    _inserir(engine, "conexao_estado_alterado", "estado_alterado")
    downgrade = _MIGRATION.read_text(encoding="utf-8").split("def downgrade", 1)[1]
    assert "DELETE FROM plugin_evento_central" in downgrade
    assert "autorizacao_negada" in downgrade
    assert "UPDATE plugin_evento_central" not in downgrade
