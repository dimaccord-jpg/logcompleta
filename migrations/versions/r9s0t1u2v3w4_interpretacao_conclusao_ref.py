"""referência interna da conclusão na interpretação do canal

Revision ID: r9s0t1u2v3w4
Revises: q8r9s0t1u2v3
Create Date: 2026-10-01

Aponta a conclusão já persistida pelo Lote 2. Não grava token bruto,
URL com token, senha, e-mail nem texto do usuário.

Registros antigos que ainda guardam a rota de conclusão são saneados
na tabela existente, e a ausência da rota é confirmada, antes de
qualquer batch_alter_table. No SQLite essa alteração reconstrói a
tabela com INSERT ... SELECT; o texto copiado já precisa estar limpo.
O tipo sai de acao, codigo e etapa_final. conclusao_id só é preenchido
quando existe uma única conclusão da mesma jornada e da finalidade
correspondente. O saneamento de segredo é irreversível por design.

Manter o predicado igual ao de InterpretacaoConversacionalCanal._SQL_SEM_LINK.
"""
from alembic import op
import sqlalchemy as sa


revision = "r9s0t1u2v3w4"
down_revision = "q8r9s0t1u2v3"
branch_labels = None
depends_on = None

_MARCA_LINK = "/onboarding/canal/concluir/"
_SQL_SEM_LINK = (
    "texto_resposta IS NULL OR "
    f"lower(texto_resposta) NOT LIKE '%{_MARCA_LINK}%'"
)
_TEXTO_DEFINIR_SENHA = (
    "Seu cadastro está quase pronto. Por segurança, crie sua senha no link enviado."
)
_TEXTO_VINCULAR_CONTA = (
    "Este e-mail já tem conta. Entre e confirme a conexão no link enviado."
)

# acao + codigo distinguem nova conta de vínculo. etapa_final, quando
# presente, precisa concordar. Divergência não escolhe um tipo.
_SQL_FINALIDADE = """(
    CASE
        WHEN interpretacao_conversacional_canal.acao = 'orientar_conta_existente'
         AND interpretacao_conversacional_canal.codigo = 'existing_account_verification_required'
         AND (
            interpretacao_conversacional_canal.etapa_final IS NULL
            OR interpretacao_conversacional_canal.etapa_final = 'coletando_email'
         )
        THEN 'vincular_conta'
        WHEN interpretacao_conversacional_canal.acao IN ('emitir_link_senha', 'aceitar_termos')
         AND interpretacao_conversacional_canal.codigo = 'link_emitido'
         AND (
            interpretacao_conversacional_canal.etapa_final IS NULL
            OR interpretacao_conversacional_canal.etapa_final = 'aguardando_senha'
         )
        THEN 'definir_senha'
        ELSE NULL
    END
)"""


def _sql_literal(texto: str) -> str:
    return "'" + texto.replace("'", "''") + "'"


def _adicionar_referencia() -> None:
    with op.batch_alter_table("interpretacao_conversacional_canal", schema=None) as batch_op:
        batch_op.add_column(sa.Column("conclusao_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_interpretacao_conversacional_conclusao",
            "onboarding_canal_conclusao",
            ["conclusao_id"],
            ["id"],
        )


def _sanear_texto_historico() -> None:
    op.execute(
        sa.text(
            f"""
            UPDATE interpretacao_conversacional_canal
            SET texto_resposta = CASE {_SQL_FINALIDADE}
                WHEN 'vincular_conta' THEN {_sql_literal(_TEXTO_VINCULAR_CONTA)}
                WHEN 'definir_senha' THEN {_sql_literal(_TEXTO_DEFINIR_SENHA)}
                ELSE NULL
            END
            WHERE texto_resposta IS NOT NULL
              AND lower(texto_resposta) LIKE '%{_MARCA_LINK}%'
            """
        )
    )


def _backfill_conclusao() -> None:
    # Uma única conclusão da jornada e da finalidade. Estado e recência
    # não desempatam: mais de uma linha deixa conclusao_id nulo.
    op.execute(
        sa.text(
            f"""
            UPDATE interpretacao_conversacional_canal
            SET conclusao_id = (
                SELECT c.id
                FROM onboarding_canal_conclusao AS c
                WHERE c.onboarding_id = interpretacao_conversacional_canal.onboarding_id
                  AND c.finalidade = {_SQL_FINALIDADE}
            )
            WHERE conclusao_id IS NULL
              AND onboarding_id IS NOT NULL
              AND (
                SELECT COUNT(*)
                FROM onboarding_canal_conclusao AS c
                WHERE c.onboarding_id = interpretacao_conversacional_canal.onboarding_id
                  AND c.finalidade = {_SQL_FINALIDADE}
              ) = 1
            """
        )
    )


def _exigir_rota_ausente() -> None:
    restantes = op.get_bind().execute(
        sa.text(
            f"""
            SELECT COUNT(*)
            FROM interpretacao_conversacional_canal
            WHERE texto_resposta IS NOT NULL
              AND lower(texto_resposta) LIKE '%{_MARCA_LINK}%'
            """
        )
    ).scalar()
    if restantes != 0:
        raise RuntimeError(
            "interpretacao_conversacional_canal ainda contem a rota de conclusao"
        )


def _criar_check_sem_link() -> None:
    with op.batch_alter_table("interpretacao_conversacional_canal", schema=None) as batch_op:
        batch_op.create_check_constraint(
            "ck_interpretacao_conversacional_sem_link",
            _SQL_SEM_LINK,
        )


def upgrade():
    # A reconstrução do SQLite copia texto_resposta. A rota histórica
    # precisa ter saído antes do primeiro batch_alter_table.
    _sanear_texto_historico()
    _exigir_rota_ausente()
    _adicionar_referencia()
    _backfill_conclusao()
    _criar_check_sem_link()


def downgrade():
    """Saneamento de segredo é irreversível por design.

    Não restaura tokens nem URLs removidos. Remove só o CHECK, a FK e
    conclusao_id, devolvendo a estrutura anterior.
    """
    with op.batch_alter_table("interpretacao_conversacional_canal", schema=None) as batch_op:
        batch_op.drop_constraint(
            "ck_interpretacao_conversacional_sem_link",
            type_="check",
        )
        batch_op.drop_constraint(
            "fk_interpretacao_conversacional_conclusao",
            type_="foreignkey",
        )
        batch_op.drop_column("conclusao_id")
