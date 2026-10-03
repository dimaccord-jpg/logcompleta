"""canal de aquisicao: identidade externa, quota guest e onboarding

Revision ID: m4n5o6p7q8r9
Revises: l3m4n5o6p7q8
Create Date: 2026-09-30

Infraestrutura provider-neutral do lote de canal. Não cria webhook, token,
telefone nem User. A unicidade da identidade ativa é provedor + sujeito
externo, só enquanto estado != 'revogada'.

Manter os predicados iguais aos de IdentidadeCanalExterna, InteracaoGuestCanal
e OnboardingCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "m4n5o6p7q8r9"
down_revision = "l3m4n5o6p7q8"
branch_labels = None
depends_on = None

_SQL_ESTADO = "estado IN ('guest', 'cadastro_em_andamento', 'vinculada', 'revogada', 'bloqueada')"
_SQL_IDENTIDADE_ATIVA = "estado != 'revogada'"
_SQL_COERENCIA = "((estado = 'guest' AND user_id IS NULL AND vinculada_em IS NULL AND revogada_em IS NULL) OR (estado = 'cadastro_em_andamento' AND user_id IS NULL AND vinculada_em IS NULL AND revogada_em IS NULL) OR (estado = 'bloqueada' AND revogada_em IS NULL AND ((user_id IS NULL AND vinculada_em IS NULL) OR (user_id IS NOT NULL AND vinculada_em IS NOT NULL))) OR (estado = 'vinculada' AND user_id IS NOT NULL AND vinculada_em IS NOT NULL AND revogada_em IS NULL) OR (estado = 'revogada' AND revogada_em IS NOT NULL AND ((user_id IS NULL AND vinculada_em IS NULL) OR (user_id IS NOT NULL AND vinculada_em IS NOT NULL))))"
_SQL_RESULTADO = "resultado IN ('interacao_guest_concluida_com_sucesso', 'mensagem_cadastro', 'coleta_nome', 'coleta_email', 'coleta_cargo', 'resposta_entrevista', 'aceite_termos', 'cta', 'mensagem_duplicada', 'falha_tecnica', 'solicitacao_rejeitada')"
_SQL_ETAPA = "etapa IN ('convite_cadastro', 'coletando_nome', 'coletando_email', 'coletando_cargo', 'coletando_entrevista', 'aguardando_termos', 'aguardando_senha', 'concluido', 'cancelado', 'expirado')"
_SQL_JORNADA_ABERTA = "etapa IN ('convite_cadastro', 'coletando_nome', 'coletando_email', 'coletando_cargo', 'coletando_entrevista', 'aguardando_termos', 'aguardando_senha')"
_SQL_TERMINAIS = "((etapa = 'expirado' AND expirada_em IS NOT NULL AND cancelada_em IS NULL AND concluida_em IS NULL) OR (etapa = 'cancelado' AND cancelada_em IS NOT NULL AND expirada_em IS NULL AND concluida_em IS NULL) OR (etapa = 'concluido' AND concluida_em IS NOT NULL AND expirada_em IS NULL AND cancelada_em IS NULL) OR (etapa IN ('convite_cadastro', 'coletando_nome', 'coletando_email', 'coletando_cargo', 'coletando_entrevista', 'aguardando_termos', 'aguardando_senha') AND expirada_em IS NULL AND cancelada_em IS NULL AND concluida_em IS NULL))"
_SQL_TERMOS = "((termos_apresentados_em IS NULL AND termos_referencia IS NULL AND termos_aceitos_em IS NULL) OR (termos_apresentados_em IS NOT NULL AND termos_referencia IS NOT NULL AND termos_aceitos_em IS NULL) OR (termos_apresentados_em IS NOT NULL AND termos_referencia IS NOT NULL AND termos_aceitos_em IS NOT NULL AND termos_aceitos_em >= termos_apresentados_em))"
_SQL_EMAIL = "email_normalizado IS NULL OR (length(email_normalizado) > 0 AND email_normalizado = lower(email_normalizado))"
_SQL_TEXTOS = "(nome IS NULL OR length(nome) > 0) AND (job_role IS NULL OR length(job_role) > 0) AND (termos_referencia IS NULL OR length(termos_referencia) > 0) AND (origem_aquisicao IS NULL OR length(origem_aquisicao) > 0) AND (correlation_id IS NULL OR length(correlation_id) > 0) AND length(taxonomia_versao) > 0"


def upgrade():
    op.create_table(
        "identidade_canal_externa",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provedor", sa.String(length=32), nullable=False),
        sa.Column("sujeito_externo", sa.String(length=120), nullable=False),
        sa.Column("contexto_destino", sa.String(length=120), nullable=True),
        sa.Column("estado", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("interacoes_uteis", sa.Integer(), nullable=False),
        sa.Column("criada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.Column("vinculada_em", sa.DateTime(), nullable=True),
        sa.Column("revogada_em", sa.DateTime(), nullable=True),
        sa.CheckConstraint(_SQL_ESTADO, name="ck_identidade_canal_estado"),
        sa.CheckConstraint(_SQL_COERENCIA, name="ck_identidade_canal_coerencia"),
        sa.CheckConstraint(
            "interacoes_uteis >= 0",
            name="ck_identidade_canal_interacoes_uteis",
        ),
        sa.CheckConstraint("length(provedor) > 0", name="ck_identidade_canal_provedor"),
        sa.CheckConstraint(
            "length(sujeito_externo) > 0",
            name="ck_identidade_canal_sujeito",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_identidade_canal_externa_estado",
        "identidade_canal_externa",
        ["estado"],
        unique=False,
    )
    op.create_index(
        "ix_identidade_canal_externa_user_id",
        "identidade_canal_externa",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "uq_identidade_canal_provider_subject_ativa",
        "identidade_canal_externa",
        ["provedor", "sujeito_externo"],
        unique=True,
        postgresql_where=sa.text(_SQL_IDENTIDADE_ATIVA),
        sqlite_where=sa.text(_SQL_IDENTIDADE_ATIVA),
    )

    op.create_table(
        "interacao_guest_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identidade_id", sa.Integer(), nullable=False),
        sa.Column("evento_externo_id", sa.String(length=120), nullable=False),
        sa.Column("resultado", sa.String(length=64), nullable=False),
        sa.Column("registrada_em", sa.DateTime(), nullable=False),
        sa.CheckConstraint(_SQL_RESULTADO, name="ck_interacao_guest_resultado"),
        sa.CheckConstraint(
            "length(evento_externo_id) > 0",
            name="ck_interacao_guest_evento",
        ),
        sa.ForeignKeyConstraint(["identidade_id"], ["identidade_canal_externa.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "identidade_id",
            "evento_externo_id",
            name="uq_interacao_guest_identidade_evento",
        ),
    )
    op.create_index(
        "ix_interacao_guest_canal_identidade_id",
        "interacao_guest_canal",
        ["identidade_id"],
        unique=False,
    )

    op.create_table(
        "onboarding_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("identidade_id", sa.Integer(), nullable=False),
        sa.Column("etapa", sa.String(length=40), nullable=False),
        sa.Column("nome", sa.String(length=150), nullable=True),
        sa.Column("email_normalizado", sa.String(length=150), nullable=True),
        sa.Column("job_role", sa.String(length=100), nullable=True),
        sa.Column("respostas_entrevista_json", sa.Text(), nullable=True),
        sa.Column("termos_apresentados_em", sa.DateTime(), nullable=True),
        sa.Column("termos_referencia", sa.String(length=80), nullable=True),
        sa.Column("termos_aceitos_em", sa.DateTime(), nullable=True),
        sa.Column("pausas_interacao_guest", sa.Integer(), nullable=False),
        sa.Column("taxonomia_versao", sa.String(length=40), nullable=False),
        sa.Column("iniciada_em", sa.DateTime(), nullable=False),
        sa.Column("atualizada_em", sa.DateTime(), nullable=False),
        sa.Column("expira_em", sa.DateTime(), nullable=False),
        sa.Column("expirada_em", sa.DateTime(), nullable=True),
        sa.Column("cancelada_em", sa.DateTime(), nullable=True),
        sa.Column("concluida_em", sa.DateTime(), nullable=True),
        sa.Column("origem_aquisicao", sa.String(length=40), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(_SQL_ETAPA, name="ck_onboarding_canal_etapa"),
        sa.CheckConstraint(_SQL_TERMINAIS, name="ck_onboarding_canal_terminais"),
        sa.CheckConstraint(_SQL_TERMOS, name="ck_onboarding_canal_termos"),
        sa.CheckConstraint(_SQL_EMAIL, name="ck_onboarding_canal_email"),
        sa.CheckConstraint(
            "pausas_interacao_guest >= 0",
            name="ck_onboarding_canal_pausas",
        ),
        sa.CheckConstraint(_SQL_TEXTOS, name="ck_onboarding_canal_textos"),
        sa.ForeignKeyConstraint(["identidade_id"], ["identidade_canal_externa.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_onboarding_canal_identidade_id",
        "onboarding_canal",
        ["identidade_id"],
        unique=False,
    )
    op.create_index(
        "uq_onboarding_canal_jornada_aberta",
        "onboarding_canal",
        ["identidade_id"],
        unique=True,
        postgresql_where=sa.text(_SQL_JORNADA_ABERTA),
        sqlite_where=sa.text(_SQL_JORNADA_ABERTA),
    )


def downgrade():
    op.drop_index("uq_onboarding_canal_jornada_aberta", table_name="onboarding_canal")
    op.drop_index("ix_onboarding_canal_identidade_id", table_name="onboarding_canal")
    op.drop_table("onboarding_canal")
    op.drop_index(
        "ix_interacao_guest_canal_identidade_id",
        table_name="interacao_guest_canal",
    )
    op.drop_table("interacao_guest_canal")
    op.drop_index(
        "uq_identidade_canal_provider_subject_ativa",
        table_name="identidade_canal_externa",
    )
    op.drop_index(
        "ix_identidade_canal_externa_user_id",
        table_name="identidade_canal_externa",
    )
    op.drop_index(
        "ix_identidade_canal_externa_estado",
        table_name="identidade_canal_externa",
    )
    op.drop_table("identidade_canal_externa")
