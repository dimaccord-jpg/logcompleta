"""fase 2 etapa 2: Conta, Franquia, vínculo User; remapeia eventos de consumo

Revision ID: h1i2j3k4l5m6
Revises: g4h5i6j7k8l9
Create Date: 2026-04-03

Decisões:
- Tabelas `conta` e `franquia` com slugs únicos (franquia: única por conta).
- Linha reservada `conta.slug=sistema-interno` + `franquia.slug=operacional-interno` para consumo
  sistema/cron/CLI (não mascarar como usuário; aponta para franquia operacional interna).
- Backfill: para cada usuário existente, cria-se uma Conta e uma Franquia "principal";
  `user.conta_id` / `user.franquia_id` passam a ser obrigatórios após o backfill.
- Eventos em `ia_consumo_evento` e `processing_events`: realinhados pelo `usuario_id` quando
  existir; eventos `origem_sistema=true` associados à franquia sistema interna.
- Elimina a semântica legada etapa1 onde `conta_id` coincidia com `user.id` (pseudo-conta).

Requer PostgreSQL (setval/sequences). Ambiente local do projeto usa Postgres.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "h1i2j3k4l5m6"
down_revision = "g4h5i6j7k8l9"
branch_labels = None
depends_on = None


def _sync_seq(conn, table: str):
    conn.execute(
        text(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"(SELECT COALESCE(MAX(id), 1) FROM {table}))"
        )
    )


def upgrade():
    op.create_table(
        "conta",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ativa"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_conta_slug", "conta", ["slug"], unique=True)
    op.create_index("ix_conta_status", "conta", ["status"], unique=False)

    op.create_table(
        "franquia",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conta_id", sa.Integer(), nullable=False),
        sa.Column("nome", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="ativa"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["conta_id"], ["conta.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conta_id", "slug", name="uq_franquia_conta_slug"),
    )
    op.create_index("ix_franquia_conta_id", "franquia", ["conta_id"], unique=False)
    op.create_index("ix_franquia_slug", "franquia", ["slug"], unique=False)
    op.create_index("ix_franquia_status", "franquia", ["status"], unique=False)

    conn = op.get_bind()

    conn.execute(
        text(
            """
            INSERT INTO conta (nome, slug, status, created_at)
            VALUES ('Sistema interno', 'sistema-interno', 'ativa', CURRENT_TIMESTAMP)
            """
        )
    )
    _sync_seq(conn, "conta")
    sistema_conta_id = conn.execute(text("SELECT id FROM conta WHERE slug = 'sistema-interno'")).scalar()

    conn.execute(
        text(
            """
            INSERT INTO franquia (conta_id, nome, slug, status, created_at)
            VALUES (:cid, 'Operacional interno', 'operacional-interno', 'ativa', CURRENT_TIMESTAMP)
            """
        ),
        {"cid": sistema_conta_id},
    )
    _sync_seq(conn, "franquia")

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(sa.Column("conta_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("franquia_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_user_conta_id", "conta", ["conta_id"], ["id"])
        batch_op.create_foreign_key("fk_user_franquia_id", "franquia", ["franquia_id"], ["id"])

    op.create_index("ix_user_conta_id", "user", ["conta_id"], unique=False)
    op.create_index("ix_user_franquia_id", "user", ["franquia_id"], unique=False)

    users = conn.execute(text('SELECT id, email, full_name FROM "user" ORDER BY id')).fetchall()
    for row in users:
        uid = int(row[0])
        email = row[1] or ""
        full_name = row[2] or ""
        slug_c = f"legacy-u{uid}"[:80]
        nome_c = (full_name or email or f"Conta {uid}")[:255]
        conn.execute(
            text(
                """
                INSERT INTO conta (nome, slug, status, created_at)
                VALUES (:nome, :slug, 'ativa', CURRENT_TIMESTAMP)
                """
            ),
            {"nome": nome_c, "slug": slug_c},
        )
        cid = conn.execute(text("SELECT id FROM conta WHERE slug = :slug"), {"slug": slug_c}).scalar()
        conn.execute(
            text(
                """
                INSERT INTO franquia (conta_id, nome, slug, status, created_at)
                VALUES (:cid, 'Principal', 'principal', 'ativa', CURRENT_TIMESTAMP)
                """
            ),
            {"cid": cid},
        )
        fid = conn.execute(
            text("SELECT id FROM franquia WHERE conta_id = :cid AND slug = 'principal'"),
            {"cid": cid},
        ).scalar()
        conn.execute(
            text('UPDATE "user" SET conta_id = :cid, franquia_id = :fid WHERE id = :uid'),
            {"cid": cid, "fid": fid, "uid": uid},
        )

    conn.execute(
        text(
            """
            UPDATE ia_consumo_evento AS e
            SET conta_id = u.conta_id, franquia_id = u.franquia_id
            FROM "user" AS u
            WHERE e.usuario_id IS NOT NULL AND e.usuario_id = u.id
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE processing_events AS e
            SET conta_id = u.conta_id, franquia_id = u.franquia_id
            FROM "user" AS u
            WHERE e.usuario_id IS NOT NULL AND e.usuario_id = u.id
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE ia_consumo_evento
            SET conta_id = (SELECT id FROM conta WHERE slug = 'sistema-interno' LIMIT 1),
                franquia_id = (SELECT id FROM franquia WHERE slug = 'operacional-interno' LIMIT 1)
            WHERE origem_sistema IS TRUE
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE processing_events
            SET conta_id = (SELECT id FROM conta WHERE slug = 'sistema-interno' LIMIT 1),
                franquia_id = (SELECT id FROM franquia WHERE slug = 'operacional-interno' LIMIT 1)
            WHERE origem_sistema IS TRUE
            """
        )
    )

    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("conta_id", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("franquia_id", existing_type=sa.Integer(), nullable=False)


def downgrade():
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.alter_column("franquia_id", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("conta_id", existing_type=sa.Integer(), nullable=True)

    conn = op.get_bind()
    conn.execute(
        text(
            """
            UPDATE ia_consumo_evento SET conta_id = NULL, franquia_id = NULL
            WHERE conta_id IN (SELECT id FROM conta WHERE slug = 'sistema-interno')
            """
        )
    )
    conn.execute(
        text(
            """
            UPDATE processing_events SET conta_id = NULL, franquia_id = NULL
            WHERE conta_id IN (SELECT id FROM conta WHERE slug = 'sistema-interno')
            """
        )
    )

    op.drop_index("ix_user_franquia_id", table_name="user")
    op.drop_index("ix_user_conta_id", table_name="user")
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_constraint("fk_user_franquia_id", type_="foreignkey")
        batch_op.drop_constraint("fk_user_conta_id", type_="foreignkey")
        batch_op.drop_column("franquia_id")
        batch_op.drop_column("conta_id")

    op.drop_table("franquia")
    op.drop_table("conta")
