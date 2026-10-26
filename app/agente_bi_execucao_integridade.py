"""Integridade da fila de execução do AgenteBI.

A análise já protege o histórico. Esta fila é operacional: identidade,
vínculo e o diário de transições não se reescrevem. O estado, o lease
e o fencing mudam. A instalação é ignorada enquanto a tabela não existe,
para as migrations anteriores continuarem a chamar a proteção geral.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

MENSAGEM_IMUTAVEL = "registro preparado do AgenteBI e imutavel"
MENSAGEM_HISTORICO = "historico do AgenteBI e imutavel"
MENSAGEM_ESCOPO = "escopo do AgenteBI incompativel"

TRIGGER_EVENTO_UPDATE = "trg_agente_bi_execucao_evento_imutavel"
TRIGGER_EVENTO_DELETE = "trg_agente_bi_execucao_evento_preservar"
TRIGGER_EXECUCAO_IDENTIDADE = "trg_agente_bi_execucao_identidade"
TRIGGER_EXECUCAO_TENTATIVA = "trg_agente_bi_execucao_tentativa"
TRIGGER_EXECUCAO_DELETE = "trg_agente_bi_execucao_preservar"


def instalar_protecao_execucao(connection) -> None:
    nomes = set(inspect(connection).get_table_names())
    if "agente_bi_execucao" not in nomes or "agente_bi_execucao_evento" not in nomes:
        return
    dialeto = connection.dialect.name
    if dialeto == "postgresql":
        _instalar_postgresql(connection)
        return
    if dialeto == "sqlite":
        _instalar_sqlite(connection)
        return
    raise RuntimeError(f"dialeto sem protecao da execucao do AgenteBI: {dialeto}")


def _executar(connection, sql: str) -> None:
    connection.execute(text(sql))


def _instalar_postgresql(connection) -> None:
    _executar(
        connection,
        f"""
        CREATE OR REPLACE FUNCTION agente_bi_execucao_proteger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $abi$
        BEGIN
          IF TG_TABLE_NAME = 'agente_bi_execucao_evento' AND TG_OP IN ('UPDATE', 'DELETE') THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_execucao' AND TG_OP = 'DELETE' THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_execucao' AND TG_OP = 'UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.operacao_id IS DISTINCT FROM OLD.operacao_id
               OR NEW.analise_id IS DISTINCT FROM OLD.analise_id
               OR NEW.usuario_id IS DISTINCT FROM OLD.usuario_id
               OR NEW.conta_id IS DISTINCT FROM OLD.conta_id
               OR NEW.franquia_id IS DISTINCT FROM OLD.franquia_id
               OR NEW.chave_idempotencia IS DISTINCT FROM OLD.chave_idempotencia
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
            THEN
              RAISE EXCEPTION '{MENSAGEM_HISTORICO}' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.ia_chamada_tentativa_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM ia_chamada_tentativa AS tentativa
              WHERE tentativa.id = NEW.ia_chamada_tentativa_id
                AND tentativa.usuario_id = NEW.usuario_id
                AND tentativa.conta_id = NEW.conta_id
                AND tentativa.franquia_id = NEW.franquia_id
            ) THEN
              RAISE EXCEPTION '{MENSAGEM_ESCOPO}' USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $abi$
        """,
    )
    gatilhos = (
        (TRIGGER_EVENTO_UPDATE, "agente_bi_execucao_evento", "BEFORE UPDATE"),
        (TRIGGER_EVENTO_DELETE, "agente_bi_execucao_evento", "BEFORE DELETE"),
        (TRIGGER_EXECUCAO_IDENTIDADE, "agente_bi_execucao", "BEFORE UPDATE"),
        (TRIGGER_EXECUCAO_TENTATIVA, "agente_bi_execucao", "BEFORE UPDATE"),
        (TRIGGER_EXECUCAO_DELETE, "agente_bi_execucao", "BEFORE DELETE"),
    )
    for nome, tabela, momento in gatilhos:
        _executar(connection, f"DROP TRIGGER IF EXISTS {nome} ON {tabela}")
        _executar(
            connection,
            f"CREATE TRIGGER {nome} {momento} ON {tabela} "
            "FOR EACH ROW EXECUTE FUNCTION agente_bi_execucao_proteger()",
        )


def _instalar_sqlite(connection) -> None:
    comandos = (
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_EVENTO_UPDATE}
        BEFORE UPDATE ON agente_bi_execucao_evento
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_EVENTO_DELETE}
        BEFORE DELETE ON agente_bi_execucao_evento
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_EXECUCAO_DELETE}
        BEFORE DELETE ON agente_bi_execucao
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_EXECUCAO_IDENTIDADE}
        BEFORE UPDATE ON agente_bi_execucao
        FOR EACH ROW
        WHEN (
          NEW.id IS NOT OLD.id
          OR NEW.operacao_id IS NOT OLD.operacao_id
          OR NEW.analise_id IS NOT OLD.analise_id
          OR NEW.usuario_id IS NOT OLD.usuario_id
          OR NEW.conta_id IS NOT OLD.conta_id
          OR NEW.franquia_id IS NOT OLD.franquia_id
          OR NEW.chave_idempotencia IS NOT OLD.chave_idempotencia
          OR NEW.created_at IS NOT OLD.created_at
        )
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_HISTORICO}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_EXECUCAO_TENTATIVA}
        BEFORE UPDATE ON agente_bi_execucao
        FOR EACH ROW
        WHEN NEW.ia_chamada_tentativa_id IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_ESCOPO}')
          WHERE NOT EXISTS (
            SELECT 1 FROM ia_chamada_tentativa AS tentativa
            WHERE tentativa.id = NEW.ia_chamada_tentativa_id
              AND tentativa.usuario_id = NEW.usuario_id
              AND tentativa.conta_id = NEW.conta_id
              AND tentativa.franquia_id = NEW.franquia_id
          );
        END
        """,
    )
    for comando in comandos:
        _executar(connection, comando)
