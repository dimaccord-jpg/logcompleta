"""Integridade do AgenteBI no banco, além do ORM.

Callbacks `before_update` não alcançam `Query.update()` nem SQL direto.
Estas funções instalam a mesma proteção em PostgreSQL e SQLite: gatilhos
para histórico imutável e restrições de escopo que continuam valendo
mesmo quando a chave estrangeira do SQLite está desligada.

Estado operacional da análise (estado, etapa, tentativa, recuperação e
marcas de tempo) pode mudar. O resultado histórico, depois de gravado,
não pode ser substituído.
"""
from __future__ import annotations

from sqlalchemy import inspect, text

INTEIRO_COLUNA_MAX = 2_147_483_647

COLUNAS_HISTORICAS_ANALISE = (
    "id",
    "conta_id",
    "franquia_id",
    "usuario_id",
    "chave",
    "rodada",
    "dataset_id",
    "dataset_versao",
    "dataset_impressao",
    "contexto_json",
    "regras_negocio_json",
    "esclarecimentos_json",
    "contrato_versao",
    "created_at",
)
COLUNAS_OPERACIONAIS_ANALISE = (
    "estado",
    "etapa_jornada",
    "ia_chamada_tentativa_id",
    "chave_recuperacao",
    "iniciado_em",
    "concluido_em",
    "updated_at",
)
COLUNA_RESULTADO_ANALISE = "resultado_json"

MENSAGEM_IMUTAVEL = "registro preparado do AgenteBI e imutavel"
MENSAGEM_HISTORICO = "historico do AgenteBI e imutavel"
MENSAGEM_ESCOPO = "escopo do AgenteBI incompativel"
MENSAGEM_REFERENCIA = "dataset referenciado pelo AgenteBI"
MENSAGEM_VINCULO = "referencia de dashboard do AgenteBI e imutavel"

TRIGGER_BLOCO = "trg_agente_bi_dataset_bloco_imutavel"
TRIGGER_NOMES = (
    "trg_agente_bi_dataset_imutavel",
    "trg_agente_bi_dataset_versao_escopo",
    "trg_agente_bi_dataset_referenciado",
    "trg_agente_bi_analise_historico",
    "trg_agente_bi_analise_dataset_escopo",
    "trg_agente_bi_analise_tentativa_escopo",
    "trg_agente_bi_dashboard_imutavel",
    "trg_agente_bi_dashboard_analise_escopo",
    "trg_agente_bi_dashboard_dataset_imutavel",
    "trg_agente_bi_dashboard_dataset_preservar",
    "trg_agente_bi_dashboard_dataset_escopo",
)
# SQLite não aceita INSERT OR UPDATE num único gatilho.
TRIGGER_SQLITE_EXTRA = ("trg_agente_bi_analise_tentativa_escopo_upd",)


def nomes_gatilhos(dialeto: str) -> tuple[str, ...]:
    if dialeto == "sqlite":
        return TRIGGER_NOMES + TRIGGER_SQLITE_EXTRA
    return TRIGGER_NOMES


def nomes_gatilhos_blocos() -> tuple[str, ...]:
    """Gatilho do bloco segmentado. Só existe depois da tabela de blocos."""
    return (TRIGGER_BLOCO,)


def faixa_inteira(coluna: str) -> str:
    return f"{coluna} BETWEEN 1 AND {INTEIRO_COLUNA_MAX}"


def instalar_protecao_agente_bi(connection) -> None:
    dialeto = connection.dialect.name
    if dialeto == "postgresql":
        _instalar_postgresql(connection)
    elif dialeto == "sqlite":
        _instalar_sqlite(connection)
    else:
        raise RuntimeError(f"dialeto sem protecao do AgenteBI: {dialeto}")
    # Import tardio: a proteção da fila não altera o gatilho do histórico.
    from app.agente_bi_execucao_integridade import instalar_protecao_execucao

    instalar_protecao_execucao(connection)


def remover_funcoes_agente_bi(connection) -> None:
    """Remove a função depois que as tabelas (e os gatilhos) já caíram."""
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("DROP FUNCTION IF EXISTS agente_bi_proteger()"))
    connection.execute(text("DROP FUNCTION IF EXISTS agente_bi_execucao_proteger()"))


def _executar(connection, sql: str) -> None:
    connection.execute(text(sql))


def _diferenca(coluna: str, *, postgres: bool) -> str:
    if postgres:
        return f"NEW.{coluna} IS DISTINCT FROM OLD.{coluna}"
    return f"NEW.{coluna} IS NOT OLD.{coluna}"


def _condicao_historico(*, postgres: bool) -> str:
    partes = [_diferenca(coluna, postgres=postgres) for coluna in COLUNAS_HISTORICAS_ANALISE]
    resultado = _diferenca(COLUNA_RESULTADO_ANALISE, postgres=postgres)
    partes.append(f"(OLD.{COLUNA_RESULTADO_ANALISE} IS NOT NULL AND {resultado})")
    return " OR ".join(partes)


def _instalar_postgresql(connection) -> None:
    historico = _condicao_historico(postgres=True)
    _executar(
        connection,
        f"""
        CREATE OR REPLACE FUNCTION agente_bi_proteger()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $abi$
        BEGIN
          IF TG_TABLE_NAME = 'agente_bi_dataset_bloco' AND TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_dataset' AND TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_dataset' AND TG_OP = 'DELETE' THEN
            IF EXISTS (SELECT 1 FROM agente_bi_analise WHERE dataset_id = OLD.id)
               OR EXISTS (SELECT 1 FROM agente_bi_dashboard_dataset WHERE dataset_id = OLD.id) THEN
              RAISE EXCEPTION '{MENSAGEM_REFERENCIA}' USING ERRCODE = 'check_violation';
            END IF;
            RETURN OLD;
          ELSIF TG_TABLE_NAME = 'agente_bi_dataset' AND TG_OP = 'INSERT' THEN
            IF NEW.versao_anterior_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM agente_bi_dataset AS anterior
              WHERE anterior.id = NEW.versao_anterior_id
                AND anterior.usuario_id = NEW.usuario_id
                AND anterior.conta_id = NEW.conta_id
                AND anterior.franquia_id = NEW.franquia_id
                AND anterior.chave = NEW.chave
                AND anterior.versao = NEW.versao - 1
            ) THEN
              RAISE EXCEPTION '{MENSAGEM_ESCOPO}' USING ERRCODE = 'check_violation';
            END IF;
          ELSIF TG_TABLE_NAME = 'agente_bi_analise' AND TG_OP = 'UPDATE' THEN
            IF {historico} THEN
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
          ELSIF TG_TABLE_NAME = 'agente_bi_analise' AND TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
              SELECT 1 FROM agente_bi_dataset AS dataset
              WHERE dataset.id = NEW.dataset_id
                AND dataset.usuario_id = NEW.usuario_id
                AND dataset.conta_id = NEW.conta_id
                AND dataset.franquia_id = NEW.franquia_id
                AND dataset.versao = NEW.dataset_versao
                AND dataset.impressao = NEW.dataset_impressao
            ) THEN
              RAISE EXCEPTION '{MENSAGEM_ESCOPO}' USING ERRCODE = 'check_violation';
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
          ELSIF TG_TABLE_NAME = 'agente_bi_dashboard' AND TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_dashboard' AND TG_OP = 'INSERT' THEN
            IF NEW.analise_id IS NOT NULL AND NOT EXISTS (
              SELECT 1 FROM agente_bi_analise AS analise
              WHERE analise.id = NEW.analise_id
                AND analise.usuario_id = NEW.usuario_id
                AND analise.conta_id = NEW.conta_id
                AND analise.franquia_id = NEW.franquia_id
            ) THEN
              RAISE EXCEPTION '{MENSAGEM_ESCOPO}' USING ERRCODE = 'check_violation';
            END IF;
          ELSIF TG_TABLE_NAME = 'agente_bi_dashboard_dataset' AND TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION '{MENSAGEM_IMUTAVEL}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_dashboard_dataset' AND TG_OP = 'DELETE' THEN
            RAISE EXCEPTION '{MENSAGEM_VINCULO}' USING ERRCODE = 'check_violation';
          ELSIF TG_TABLE_NAME = 'agente_bi_dashboard_dataset' AND TG_OP = 'INSERT' THEN
            IF NOT EXISTS (
              SELECT 1 FROM agente_bi_dashboard AS painel
              WHERE painel.id = NEW.dashboard_id
                AND painel.usuario_id = NEW.usuario_id
                AND painel.conta_id = NEW.conta_id
                AND painel.franquia_id = NEW.franquia_id
            ) OR NOT EXISTS (
              SELECT 1 FROM agente_bi_dataset AS dataset
              WHERE dataset.id = NEW.dataset_id
                AND dataset.usuario_id = NEW.usuario_id
                AND dataset.conta_id = NEW.conta_id
                AND dataset.franquia_id = NEW.franquia_id
                AND dataset.versao = NEW.dataset_versao
                AND dataset.impressao = NEW.dataset_impressao
            ) THEN
              RAISE EXCEPTION '{MENSAGEM_ESCOPO}' USING ERRCODE = 'check_violation';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $abi$
        """,
    )
    existentes = set(inspect(connection).get_table_names())
    for nome, tabela, momento in _GATILHOS_POSTGRES:
        if tabela not in existentes:
            continue
        _executar(connection, f"DROP TRIGGER IF EXISTS {nome} ON {tabela}")
        _executar(
            connection,
            f"CREATE TRIGGER {nome} {momento} ON {tabela} "
            "FOR EACH ROW EXECUTE FUNCTION agente_bi_proteger()",
        )


_GATILHOS_POSTGRES = (
    (TRIGGER_BLOCO, "agente_bi_dataset_bloco", "BEFORE UPDATE"),
    ("trg_agente_bi_dataset_imutavel", "agente_bi_dataset", "BEFORE UPDATE"),
    ("trg_agente_bi_dataset_versao_escopo", "agente_bi_dataset", "BEFORE INSERT"),
    ("trg_agente_bi_dataset_referenciado", "agente_bi_dataset", "BEFORE DELETE"),
    ("trg_agente_bi_analise_historico", "agente_bi_analise", "BEFORE UPDATE"),
    ("trg_agente_bi_analise_dataset_escopo", "agente_bi_analise", "BEFORE INSERT"),
    ("trg_agente_bi_analise_tentativa_escopo", "agente_bi_analise", "BEFORE INSERT OR UPDATE"),
    ("trg_agente_bi_dashboard_imutavel", "agente_bi_dashboard", "BEFORE UPDATE"),
    ("trg_agente_bi_dashboard_analise_escopo", "agente_bi_dashboard", "BEFORE INSERT"),
    ("trg_agente_bi_dashboard_dataset_imutavel", "agente_bi_dashboard_dataset", "BEFORE UPDATE"),
    ("trg_agente_bi_dashboard_dataset_preservar", "agente_bi_dashboard_dataset", "BEFORE DELETE"),
    ("trg_agente_bi_dashboard_dataset_escopo", "agente_bi_dashboard_dataset", "BEFORE INSERT"),
)


def _instalar_sqlite(connection) -> None:
    historico = _condicao_historico(postgres=False)
    comandos = (
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dataset_imutavel
        BEFORE UPDATE ON agente_bi_dataset
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dataset_versao_escopo
        BEFORE INSERT ON agente_bi_dataset
        FOR EACH ROW
        WHEN NEW.versao_anterior_id IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_ESCOPO}')
          WHERE NOT EXISTS (
            SELECT 1 FROM agente_bi_dataset AS anterior
            WHERE anterior.id = NEW.versao_anterior_id
              AND anterior.usuario_id = NEW.usuario_id
              AND anterior.conta_id = NEW.conta_id
              AND anterior.franquia_id = NEW.franquia_id
              AND anterior.chave = NEW.chave
              AND anterior.versao = NEW.versao - 1
          );
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dataset_referenciado
        BEFORE DELETE ON agente_bi_dataset
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_REFERENCIA}')
          WHERE EXISTS (SELECT 1 FROM agente_bi_analise WHERE dataset_id = OLD.id)
             OR EXISTS (SELECT 1 FROM agente_bi_dashboard_dataset WHERE dataset_id = OLD.id);
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_analise_historico
        BEFORE UPDATE ON agente_bi_analise
        FOR EACH ROW
        WHEN ({historico})
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_HISTORICO}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_analise_dataset_escopo
        BEFORE INSERT ON agente_bi_analise
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_ESCOPO}')
          WHERE NOT EXISTS (
            SELECT 1 FROM agente_bi_dataset AS dataset
            WHERE dataset.id = NEW.dataset_id
              AND dataset.usuario_id = NEW.usuario_id
              AND dataset.conta_id = NEW.conta_id
              AND dataset.franquia_id = NEW.franquia_id
              AND dataset.versao = NEW.dataset_versao
              AND dataset.impressao = NEW.dataset_impressao
          );
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_analise_tentativa_escopo
        BEFORE INSERT ON agente_bi_analise
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
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dashboard_imutavel
        BEFORE UPDATE ON agente_bi_dashboard
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dashboard_analise_escopo
        BEFORE INSERT ON agente_bi_dashboard
        FOR EACH ROW
        WHEN NEW.analise_id IS NOT NULL
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_ESCOPO}')
          WHERE NOT EXISTS (
            SELECT 1 FROM agente_bi_analise AS analise
            WHERE analise.id = NEW.analise_id
              AND analise.usuario_id = NEW.usuario_id
              AND analise.conta_id = NEW.conta_id
              AND analise.franquia_id = NEW.franquia_id
          );
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dashboard_dataset_imutavel
        BEFORE UPDATE ON agente_bi_dashboard_dataset
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dashboard_dataset_preservar
        BEFORE DELETE ON agente_bi_dashboard_dataset
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_VINCULO}');
        END
        """,
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_dashboard_dataset_escopo
        BEFORE INSERT ON agente_bi_dashboard_dataset
        FOR EACH ROW
        BEGIN
          SELECT RAISE(ABORT, '{MENSAGEM_ESCOPO}')
          WHERE NOT EXISTS (
            SELECT 1 FROM agente_bi_dashboard AS painel
            WHERE painel.id = NEW.dashboard_id
              AND painel.usuario_id = NEW.usuario_id
              AND painel.conta_id = NEW.conta_id
              AND painel.franquia_id = NEW.franquia_id
          ) OR NOT EXISTS (
            SELECT 1 FROM agente_bi_dataset AS dataset
            WHERE dataset.id = NEW.dataset_id
              AND dataset.usuario_id = NEW.usuario_id
              AND dataset.conta_id = NEW.conta_id
              AND dataset.franquia_id = NEW.franquia_id
              AND dataset.versao = NEW.dataset_versao
              AND dataset.impressao = NEW.dataset_impressao
          );
        END
        """,
    )
    # Tentativa também na atualização operacional: o gatilho de histórico não a cobre.
    comandos = comandos + (
        f"""
        CREATE TRIGGER IF NOT EXISTS trg_agente_bi_analise_tentativa_escopo_upd
        BEFORE UPDATE ON agente_bi_analise
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
    existentes = set(inspect(connection).get_table_names())
    tabelas_necessarias = {
        "agente_bi_dataset",
        "agente_bi_analise",
        "agente_bi_dashboard",
        "agente_bi_dashboard_dataset",
    }
    if tabelas_necessarias <= existentes:
        for comando in comandos:
            _executar(connection, comando)
    if "agente_bi_dataset_bloco" in existentes:
        _executar(
            connection,
            f"""
            CREATE TRIGGER IF NOT EXISTS {TRIGGER_BLOCO}
            BEFORE UPDATE ON agente_bi_dataset_bloco
            FOR EACH ROW
            BEGIN
              SELECT RAISE(ABORT, '{MENSAGEM_IMUTAVEL}');
            END
            """,
        )
