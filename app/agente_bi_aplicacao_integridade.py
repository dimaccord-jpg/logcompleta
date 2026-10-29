"""Integridade da aplicação v2 do AgenteBI.

A fila de execução continua sendo a operação da análise. Esta tabela só
amarra o plano já gravado aos artefatos publicados. A identidade não se
reescreve. O estado preparado pode passar a concluído uma vez.

A instalação é ignorada enquanto as tabelas referenciadas não existem,
para o create_all parcial das migrations anteriores não quebrar.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

from app.agente_bi_integridade import MENSAGEM_ESCOPO, sql_reinicio_do_escopo

ESTADOS_APLICACAO = ("preparada", "concluida")
CONTRATO_PLANO_APLICACAO = "patch.definir.v2"
CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO = ("definir.v1", "definir.v2")
CONTRATO_DEFINICAO_APLICACAO = "definir.v2"
CONTRATO_CALCULO_APLICACAO = "calculo.v2"
MENSAGEM_APLICACAO = "identidade da aplicacao do AgenteBI e imutavel"

COLUNAS_IDENTIDADE_APLICACAO = (
    "conta_id",
    "franquia_id",
    "usuario_id",
    "plano_id",
    "plano_contrato",
    "dataset_id",
    "dataset_versao",
    "dataset_impressao",
    "dashboard_id",
    "dashboard_chave",
    "revisao_base",
    "definicao_anterior_id",
    "definicao_anterior_contrato",
    "preparada_em",
    "created_at",
)

TRIGGER_PROTEGER = "trg_agente_bi_aplicacao_proteger"
TRIGGER_REFERENCIA = "trg_agente_bi_aplicacao_referencia"
TRIGGER_CONCLUSAO = "trg_agente_bi_aplicacao_conclusao"
TRIGGER_IDENTIDADE = "trg_agente_bi_aplicacao_identidade"
TRIGGER_PRESERVAR = "trg_agente_bi_aplicacao_preservar"

_TABELAS = (
    "agente_bi_aplicacao",
    "agente_bi_analise",
    "agente_bi_dataset",
    "agente_bi_dashboard",
    "agente_bi_dashboard_dataset",
)


def nomes_gatilhos_aplicacao(dialeto: str) -> tuple[str, ...]:
    if dialeto == "postgresql":
        return (TRIGGER_PROTEGER,)
    return (TRIGGER_REFERENCIA, TRIGGER_CONCLUSAO, TRIGGER_IDENTIDADE, TRIGGER_PRESERVAR)


def sql_estado_aplicacao() -> str:
    lista = ", ".join("'" + valor + "'" for valor in ESTADOS_APLICACAO)
    return "estado IN (" + lista + ")"


def sql_plano_contrato_aplicacao() -> str:
    return "plano_contrato = '" + CONTRATO_PLANO_APLICACAO + "'"


def sql_anterior_contrato_aplicacao() -> str:
    lista = ", ".join("'" + valor + "'" for valor in CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO)
    return "definicao_anterior_contrato IN (" + lista + ")"


def sql_artefatos_aplicacao() -> str:
    preparada = (
        "estado = 'preparada'"
        " AND definicao_id IS NULL AND definicao_contrato IS NULL"
        " AND calculo_id IS NULL AND calculo_contrato IS NULL"
        " AND dashboard_publicado_id IS NULL AND revisao_publicada IS NULL"
        " AND concluida_em IS NULL"
    )
    concluida = (
        "estado = 'concluida'"
        " AND definicao_id IS NOT NULL"
        " AND definicao_contrato = '" + CONTRATO_DEFINICAO_APLICACAO + "'"
        " AND calculo_id IS NOT NULL"
        " AND calculo_contrato = '" + CONTRATO_CALCULO_APLICACAO + "'"
        " AND dashboard_publicado_id IS NOT NULL"
        " AND revisao_publicada IS NOT NULL"
        " AND concluida_em IS NOT NULL"
    )
    return "(" + preparada + ") OR (" + concluida + ")"


def sql_papeis_aplicacao() -> str:
    return (
        "definicao_anterior_id <> plano_id"
        " AND (definicao_id IS NULL OR (definicao_id <> plano_id"
        " AND definicao_id <> definicao_anterior_id))"
        " AND (calculo_id IS NULL OR (calculo_id <> plano_id"
        " AND calculo_id <> definicao_anterior_id AND calculo_id <> definicao_id))"
        " AND (dashboard_publicado_id IS NULL OR dashboard_publicado_id <> dashboard_id)"
        " AND (revisao_publicada IS NULL OR revisao_publicada > revisao_base)"
    )


def instalar_protecao_aplicacao(connection, *, reinicio=None) -> None:
    nomes = set(inspect(connection).get_table_names())
    if not set(_TABELAS) <= nomes:
        return
    if reinicio is None:
        from app.agente_bi_integridade import _tabela_reinicio_presente

        reinicio = _tabela_reinicio_presente(connection)
    dialeto = connection.dialect.name
    if dialeto == "postgresql":
        _instalar_postgresql(connection, reinicio=reinicio)
        return
    if dialeto == "sqlite":
        _instalar_sqlite(connection, reinicio=reinicio)
        return
    raise RuntimeError("dialeto sem protecao da aplicacao do AgenteBI: " + dialeto)


def remover_protecao_aplicacao(connection) -> None:
    dialeto = connection.dialect.name
    nomes = set(inspect(connection).get_table_names())
    if "agente_bi_aplicacao" in nomes:
        if dialeto == "postgresql":
            connection.execute(
                text("DROP TRIGGER IF EXISTS " + TRIGGER_PROTEGER + " ON agente_bi_aplicacao")
            )
        elif dialeto == "sqlite":
            for nome in nomes_gatilhos_aplicacao("sqlite"):
                connection.execute(text("DROP TRIGGER IF EXISTS " + nome))
    if dialeto == "postgresql":
        connection.execute(text("DROP FUNCTION IF EXISTS agente_bi_aplicacao_proteger()"))


def recusar_exclusao_aplicacao(mapper, connection, target):
    raise ValueError(MENSAGEM_APLICACAO)


def recusar_mutacao_aplicacao(mapper, connection, target):
    estado = inspect(target)
    historico_estado = estado.attrs.estado.history
    if historico_estado.deleted:
        anterior = historico_estado.deleted[0]
    else:
        anterior = target.estado
    if anterior == "concluida":
        raise ValueError(MENSAGEM_APLICACAO)
    for nome in COLUNAS_IDENTIDADE_APLICACAO:
        if _alterou(estado.attrs[nome]):
            raise ValueError(MENSAGEM_APLICACAO)


def _alterou(atributo) -> bool:
    historico = atributo.history
    if not historico.has_changes():
        return False
    if historico.deleted and historico.added and historico.deleted[0] == historico.added[0]:
        return False
    return True


def _existe_analise(alias: str, id_coluna: str, contrato_coluna: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM agente_bi_analise AS " + alias + " WHERE "
        + alias + ".id = NEW." + id_coluna
        + " AND " + alias + ".usuario_id = NEW.usuario_id"
        + " AND " + alias + ".conta_id = NEW.conta_id"
        + " AND " + alias + ".franquia_id = NEW.franquia_id"
        + " AND " + alias + ".dataset_id = NEW.dataset_id"
        + " AND " + alias + ".dataset_versao = NEW.dataset_versao"
        + " AND " + alias + ".dataset_impressao = NEW.dataset_impressao"
        + " AND " + alias + ".contrato_versao = NEW." + contrato_coluna
        + ")"
    )


def _existe_dataset() -> str:
    return (
        "EXISTS (SELECT 1 FROM agente_bi_dataset AS dataset WHERE "
        "dataset.id = NEW.dataset_id"
        " AND dataset.usuario_id = NEW.usuario_id"
        " AND dataset.conta_id = NEW.conta_id"
        " AND dataset.franquia_id = NEW.franquia_id"
        " AND dataset.versao = NEW.dataset_versao"
        " AND dataset.impressao = NEW.dataset_impressao)"
    )


def _existe_painel(id_coluna: str, revisao_coluna: str, *, publicado: bool) -> str:
    extra = ""
    if publicado:
        extra = (
            " AND painel.analise_id = NEW.calculo_id"
            " AND painel.estado_publicacao = 'publicado'"
        )
    return (
        "EXISTS (SELECT 1 FROM agente_bi_dashboard AS painel WHERE "
        "painel.id = NEW." + id_coluna
        + " AND painel.usuario_id = NEW.usuario_id"
        + " AND painel.conta_id = NEW.conta_id"
        + " AND painel.franquia_id = NEW.franquia_id"
        + " AND painel.chave = NEW.dashboard_chave"
        + " AND painel.revisao = NEW." + revisao_coluna
        + extra
        + ")"
    )


def _existe_vinculo() -> str:
    return (
        "EXISTS (SELECT 1 FROM agente_bi_dashboard_dataset AS vinculo WHERE "
        "vinculo.dashboard_id = NEW.dashboard_publicado_id"
        " AND vinculo.usuario_id = NEW.usuario_id"
        " AND vinculo.conta_id = NEW.conta_id"
        " AND vinculo.franquia_id = NEW.franquia_id"
        " AND vinculo.dataset_id = NEW.dataset_id"
        " AND vinculo.dataset_versao = NEW.dataset_versao"
        " AND vinculo.dataset_impressao = NEW.dataset_impressao)"
    )


def _falha_base() -> str:
    return " OR ".join(
        (
            "NOT (" + _existe_analise("plano", "plano_id", "plano_contrato") + ")",
            "NOT (" + _existe_analise("anterior", "definicao_anterior_id", "definicao_anterior_contrato") + ")",
            "NOT (" + _existe_dataset() + ")",
            "NOT (" + _existe_painel("dashboard_id", "revisao_base", publicado=False) + ")",
        )
    )


def _falha_conclusao() -> str:
    return " OR ".join(
        (
            "NOT (" + _existe_analise("definicao", "definicao_id", "definicao_contrato") + ")",
            "NOT (" + _existe_analise("calculo", "calculo_id", "calculo_contrato") + ")",
            "NOT (" + _existe_painel("dashboard_publicado_id", "revisao_publicada", publicado=True) + ")",
            "NOT (" + _existe_vinculo() + ")",
        )
    )


def _diferenca(coluna: str, *, postgres: bool) -> str:
    if postgres:
        return "NEW." + coluna + " IS DISTINCT FROM OLD." + coluna
    return "NEW." + coluna + " IS NOT OLD." + coluna


def _falha_identidade(*, postgres: bool) -> str:
    partes = [_diferenca(coluna, postgres=postgres) for coluna in COLUNAS_IDENTIDADE_APLICACAO]
    partes.append("OLD.estado = 'concluida'")
    return " OR ".join(partes)


def _ramo_delete_aplicacao(*, reinicio: bool) -> str:
    if not reinicio:
        return (
            "IF TG_OP = 'DELETE' THEN\n"
            "                RAISE EXCEPTION '" + MENSAGEM_APLICACAO
            + "' USING ERRCODE = 'check_violation';\n"
            "              END IF;"
        )
    return (
        "IF TG_OP = 'DELETE' THEN\n"
        "                IF NOT " + sql_reinicio_do_escopo() + " THEN\n"
        "                  RAISE EXCEPTION '" + MENSAGEM_APLICACAO
        + "' USING ERRCODE = 'check_violation';\n"
        "                END IF;\n"
        "                RETURN OLD;\n"
        "              END IF;"
    )


def _instalar_postgresql(connection, *, reinicio: bool) -> None:
    identidade = _falha_identidade(postgres=True)
    base = _falha_base()
    conclusao = _falha_conclusao()
    ramo_delete = _ramo_delete_aplicacao(reinicio=reinicio)
    connection.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION agente_bi_aplicacao_proteger()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $apl$
            BEGIN
              """
            + ramo_delete
            + """
              IF TG_OP = 'UPDATE' AND ("""
            + identidade
            + """) THEN
                RAISE EXCEPTION '"""
            + MENSAGEM_APLICACAO
            + """' USING ERRCODE = 'check_violation';
              END IF;
              IF """
            + base
            + """ THEN
                RAISE EXCEPTION '"""
            + MENSAGEM_ESCOPO
            + """' USING ERRCODE = 'check_violation';
              END IF;
              IF NEW.estado = 'concluida' AND ("""
            + conclusao
            + """) THEN
                RAISE EXCEPTION '"""
            + MENSAGEM_ESCOPO
            + """' USING ERRCODE = 'check_violation';
              END IF;
              RETURN NEW;
            END;
            $apl$
            """
        )
    )
    connection.execute(text("DROP TRIGGER IF EXISTS " + TRIGGER_PROTEGER + " ON agente_bi_aplicacao"))
    connection.execute(
        text(
            "CREATE TRIGGER " + TRIGGER_PROTEGER
            + " BEFORE INSERT OR UPDATE OR DELETE ON agente_bi_aplicacao"
            + " FOR EACH ROW EXECUTE FUNCTION agente_bi_aplicacao_proteger()"
        )
    )


def _sql_preservar_aplicacao(*, reinicio: bool) -> str:
    if reinicio:
        corpo = (
            "SELECT RAISE(ABORT, '" + MENSAGEM_APLICACAO + "')"
            " WHERE NOT " + sql_reinicio_do_escopo() + ";"
        )
    else:
        corpo = "SELECT RAISE(ABORT, '" + MENSAGEM_APLICACAO + "');"
    return (
        "CREATE TRIGGER " + TRIGGER_PRESERVAR
        + " BEFORE DELETE ON agente_bi_aplicacao"
        + " FOR EACH ROW BEGIN " + corpo + " END"
    )


def _instalar_sqlite(connection, *, reinicio: bool) -> None:
    for nome in nomes_gatilhos_aplicacao("sqlite"):
        connection.execute(text("DROP TRIGGER IF EXISTS " + nome))
    identidade = _falha_identidade(postgres=False)
    comandos = (
        (
            "CREATE TRIGGER " + TRIGGER_REFERENCIA
            + " BEFORE INSERT ON agente_bi_aplicacao"
            + " FOR EACH ROW BEGIN SELECT RAISE(ABORT, '"
            + MENSAGEM_ESCOPO
            + "') WHERE "
            + _falha_base()
            + " OR (NEW.estado = 'concluida' AND ("
            + _falha_conclusao()
            + ")); END"
        ),
        (
            "CREATE TRIGGER " + TRIGGER_CONCLUSAO
            + " BEFORE UPDATE ON agente_bi_aplicacao"
            + " FOR EACH ROW WHEN NEW.estado = 'concluida'"
            + " BEGIN SELECT RAISE(ABORT, '"
            + MENSAGEM_ESCOPO
            + "') WHERE "
            + _falha_conclusao()
            + "; END"
        ),
        (
            "CREATE TRIGGER " + TRIGGER_IDENTIDADE
            + " BEFORE UPDATE ON agente_bi_aplicacao"
            + " FOR EACH ROW WHEN "
            + identidade
            + " BEGIN SELECT RAISE(ABORT, '"
            + MENSAGEM_APLICACAO
            + "'); END"
        ),
        _sql_preservar_aplicacao(reinicio=reinicio),
    )
    for comando in comandos:
        connection.execute(text(comando))
