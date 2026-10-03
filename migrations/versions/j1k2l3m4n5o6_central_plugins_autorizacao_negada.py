"""central de plugins: evento de autorizacao negada

Revision ID: j1k2l3m4n5o6
Revises: i0j1k2l3m4n5
Create Date: 2026-09-29

SCRUM-222 lote 2. Amplia a taxonomia fechada de PluginEventoCentral para
registrar negação operacional. Não cria provider, segredo nem coluna livre.

Manter os predicados iguais a PluginEventoCentral.TIPOS e DETALHES.
"""
from alembic import op


revision = "j1k2l3m4n5o6"
down_revision = "i0j1k2l3m4n5"
branch_labels = None
depends_on = None

_SQL_TIPOS = (
    "tipo_evento IN ("
    "'conexao_criada', 'conexao_estado_alterado', "
    "'restricao_usuario_alterada', 'bloqueado', 'desabilitado', "
    "'desconectado', 'revogado', 'autorizacao_negada'"
    ")"
)
_SQL_DETALHES = (
    "detalhe_codigo IS NULL OR detalhe_codigo IN ("
    "'criada', 'estado_alterado', 'restricao_registrada', 'restricao_removida', "
    "'plugin_inexistente', 'plugin_indisponivel', 'conexao_inexistente', "
    "'conexao_nao_pertence', 'conexao_nao_operacional', 'capability_inexistente', "
    "'capability_indisponivel', 'bloqueada_pela_plataforma', "
    "'bloqueada_pelo_usuario', 'concessao_provedor_negada', "
    "'concessao_provedor_ausente', 'conta_incompativel', 'contexto_invalido'"
    ")"
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


def upgrade():
    with op.batch_alter_table("plugin_evento_central", schema=None) as batch_op:
        batch_op.drop_constraint("ck_plugin_evento_tipo", type_="check")
        batch_op.create_check_constraint("ck_plugin_evento_tipo", _SQL_TIPOS)
        batch_op.drop_constraint("ck_plugin_evento_detalhe", type_="check")
        batch_op.create_check_constraint("ck_plugin_evento_detalhe", _SQL_DETALHES)


def downgrade():
    # autorizacao_negada e os motivos novos só existem nesta revisão.
    # O schema do lote 1 não os aceita. Apagar essas linhas antes de
    # restaurar os checks. Não reescrever o tipo para um valor antigo.
    op.execute(
        "DELETE FROM plugin_evento_central "
        "WHERE tipo_evento = 'autorizacao_negada'"
    )
    with op.batch_alter_table("plugin_evento_central", schema=None) as batch_op:
        batch_op.drop_constraint("ck_plugin_evento_tipo", type_="check")
        batch_op.create_check_constraint("ck_plugin_evento_tipo", _SQL_TIPOS_LOTE_1)
        batch_op.drop_constraint("ck_plugin_evento_detalhe", type_="check")
        batch_op.create_check_constraint("ck_plugin_evento_detalhe", _SQL_DETALHES_LOTE_1)
