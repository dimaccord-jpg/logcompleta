"""tentativa inicial de envio do canal

Revision ID: w4x5y6z7a8b9
Revises: v3w4x5y6z7a8
Create Date: 2026-10-02

Uma tentativa 1 para cada saída já gravada. A saída lógica permanece.
Não remove colunas de evento_canal_saida e não grava segredo, endereço,
frase, destinatário nem corpo.

Manter os predicados iguais aos de TentativaEnvioCanal.
"""
from alembic import op
import sqlalchemy as sa


revision = "w4x5y6z7a8b9"
down_revision = "v3w4x5y6z7a8"
branch_labels = None
depends_on = None

_SQL_NUMERO = "numero_tentativa = 1"
_SQL_TIPO = "tipo_tentativa IN ('texto_comum', 'link_seguro')"
_SQL_ORIGEM = "origem_tentativa = 'envio_inicial'"
_SQL_STATUS = (
    "status_tentativa IN ("
    "'reservada', 'aguardando_link_seguro', 'preparando', "
    "'aceita_provider', 'erro'"
    ")"
)
_SQL_PROVIDER = "provider = 'meta_whatsapp'"
_SQL_ERRO = (
    "codigo_erro IS NULL OR codigo_erro IN ("
    "'timeout', 'http_4xx', 'http_5xx', 'resposta_invalida', "
    "'configuracao_ausente', 'falha_transporte'"
    ")"
)
_SQL_MENSAGEM = (
    "provider_message_id IS NULL OR "
    "(length(provider_message_id) BETWEEN 1 AND 200)"
)
_SQL_CORRELATION = "length(correlation_id) > 0 AND length(correlation_id) <= 64"
_SQL_CONCLUSAO = (
    "("
    "(tipo_tentativa = 'texto_comum' AND conclusao_id IS NULL)"
    " OR ("
    "tipo_tentativa = 'link_seguro' AND ("
    "conclusao_id IS NULL"
    " OR status_tentativa IN ('preparando', 'aceita_provider', 'erro')"
    ")"
    ")"
    ")"
)
_SQL_CLASSIFICACAO = (
    "classificacao_resultado IS NULL OR ("
    "classificacao_resultado = 'resultado_incerto' "
    "AND status_tentativa IN ('reservada', 'preparando') "
    "AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL "
    "AND enviado_em IS NULL"
    ")"
)
_SQL_COERENCIA = (
    "("
    "(status_tentativa = 'aceita_provider' AND provider_message_id IS NOT NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NOT NULL "
    "AND finalizado_em IS NOT NULL)"
    " OR (status_tentativa = 'erro' AND provider_message_id IS NULL "
    "AND codigo_erro IS NOT NULL AND enviado_em IS NULL)"
    " OR (status_tentativa = 'reservada' AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NULL "
    "AND finalizado_em IS NULL AND preparado_em IS NULL)"
    " OR (status_tentativa = 'aguardando_link_seguro' "
    "AND provider_message_id IS NULL AND codigo_erro IS NULL "
    "AND enviado_em IS NULL AND finalizado_em IS NULL "
    "AND preparado_em IS NULL AND conclusao_id IS NULL)"
    " OR (status_tentativa = 'preparando' AND provider_message_id IS NULL "
    "AND codigo_erro IS NULL AND enviado_em IS NULL "
    "AND finalizado_em IS NULL)"
    ")"
)

_BACKFILL = """
INSERT INTO tentativa_envio_canal (
    saida_id,
    numero_tentativa,
    tipo_tentativa,
    origem_tentativa,
    status_tentativa,
    codigo_erro,
    provider,
    provider_message_id,
    conclusao_id,
    criado_em,
    preparado_em,
    enviado_em,
    finalizado_em,
    correlation_id,
    classificacao_resultado
)
SELECT
    s.id,
    1,
    CASE
        WHEN s.status_envio IN ('aguardando_link_seguro', 'preparando_link')
            THEN 'link_seguro'
        WHEN s.conclusao_id IS NOT NULL
            THEN 'link_seguro'
        WHEN i.id IS NOT NULL AND (
            i.conclusao_id IS NOT NULL
            OR i.acao IN ('emitir_link_senha', 'orientar_conta_existente')
            OR i.codigo IN (
                'link_emitido',
                'link_nao_emitido',
                'existing_account_verification_required'
            )
            OR i.etapa_final = 'aguardando_senha'
        )
            THEN 'link_seguro'
        ELSE 'texto_comum'
    END,
    'envio_inicial',
    CASE s.status_envio
        WHEN 'reservado' THEN 'reservada'
        WHEN 'aguardando_link_seguro' THEN 'aguardando_link_seguro'
        WHEN 'preparando_link' THEN 'preparando'
        WHEN 'aceito_provider' THEN 'aceita_provider'
        WHEN 'erro' THEN 'erro'
    END,
    CASE WHEN s.status_envio = 'erro' THEN s.codigo_erro ELSE NULL END,
    s.provider,
    CASE
        WHEN s.status_envio = 'aceito_provider' THEN s.provider_message_id
        ELSE NULL
    END,
    CASE
        WHEN s.status_envio IN ('aguardando_link_seguro', 'preparando_link')
            OR s.conclusao_id IS NOT NULL
            OR (
                i.id IS NOT NULL AND (
                    i.conclusao_id IS NOT NULL
                    OR i.acao IN ('emitir_link_senha', 'orientar_conta_existente')
                    OR i.codigo IN (
                        'link_emitido',
                        'link_nao_emitido',
                        'existing_account_verification_required'
                    )
                    OR i.etapa_final = 'aguardando_senha'
                )
            )
        THEN s.conclusao_id
        ELSE NULL
    END,
    s.criado_em,
    NULL,
    CASE WHEN s.status_envio = 'aceito_provider' THEN s.enviado_em ELSE NULL END,
    CASE WHEN s.status_envio = 'aceito_provider' THEN s.enviado_em ELSE NULL END,
    s.correlation_id,
    CASE
        WHEN e.classificacao_resultado = 'resultado_incerto'
            AND s.status_envio IN ('reservado', 'preparando_link')
            AND s.provider_message_id IS NULL
        THEN 'resultado_incerto'
        ELSE NULL
    END
FROM evento_canal_saida AS s
LEFT JOIN interpretacao_conversacional_canal AS i
    ON i.id = s.interpretacao_id
LEFT JOIN estado_entrega_canal_saida AS e
    ON e.saida_id = s.id
"""


def upgrade():
    op.create_table(
        "tentativa_envio_canal",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("saida_id", sa.Integer(), nullable=False),
        sa.Column("numero_tentativa", sa.Integer(), nullable=False),
        sa.Column("tipo_tentativa", sa.String(length=32), nullable=False),
        sa.Column("origem_tentativa", sa.String(length=32), nullable=False),
        sa.Column("status_tentativa", sa.String(length=32), nullable=False),
        sa.Column("codigo_erro", sa.String(length=32), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),
        sa.Column("conclusao_id", sa.Integer(), nullable=True),
        sa.Column("criado_em", sa.DateTime(), nullable=False),
        sa.Column("preparado_em", sa.DateTime(), nullable=True),
        sa.Column("enviado_em", sa.DateTime(), nullable=True),
        sa.Column("finalizado_em", sa.DateTime(), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("classificacao_resultado", sa.String(length=32), nullable=True),
        sa.CheckConstraint(_SQL_NUMERO, name="ck_tentativa_envio_canal_numero"),
        sa.CheckConstraint(_SQL_TIPO, name="ck_tentativa_envio_canal_tipo"),
        sa.CheckConstraint(_SQL_ORIGEM, name="ck_tentativa_envio_canal_origem"),
        sa.CheckConstraint(_SQL_STATUS, name="ck_tentativa_envio_canal_status"),
        sa.CheckConstraint(_SQL_PROVIDER, name="ck_tentativa_envio_canal_provider"),
        sa.CheckConstraint(_SQL_ERRO, name="ck_tentativa_envio_canal_erro"),
        sa.CheckConstraint(_SQL_MENSAGEM, name="ck_tentativa_envio_canal_mensagem"),
        sa.CheckConstraint(_SQL_CORRELATION, name="ck_tentativa_envio_canal_correlation"),
        sa.CheckConstraint(_SQL_CONCLUSAO, name="ck_tentativa_envio_canal_conclusao"),
        sa.CheckConstraint(
            _SQL_CLASSIFICACAO,
            name="ck_tentativa_envio_canal_classificacao",
        ),
        sa.CheckConstraint(_SQL_COERENCIA, name="ck_tentativa_envio_canal_coerencia"),
        sa.ForeignKeyConstraint(
            ["saida_id"],
            ["evento_canal_saida.id"],
            name="fk_tentativa_envio_canal_saida",
        ),
        sa.ForeignKeyConstraint(
            ["conclusao_id"],
            ["onboarding_canal_conclusao.id"],
            name="fk_tentativa_envio_canal_conclusao",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "saida_id",
            "numero_tentativa",
            name="uq_tentativa_envio_canal_saida_numero",
        ),
    )
    op.create_index(
        "ix_tentativa_envio_canal_provider_message_id",
        "tentativa_envio_canal",
        ["provider_message_id"],
    )
    op.execute(_BACKFILL)


def downgrade():
    op.drop_index(
        "ix_tentativa_envio_canal_provider_message_id",
        table_name="tentativa_envio_canal",
    )
    op.drop_table("tentativa_envio_canal")
