"""
Backfill determinístico de legado Multiuser para a fundação organizacional (Fase 1).
Opera sobre uma conexão SQLAlchemy (Alembic ou sessão de teste). Sem Stripe.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from sqlalchemy import inspect as sa_inspect, text

from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    InconsistenciaPlanejada,
    ORIGEM_BACKFILL_LEGADO,
    PAPEL_CONTRATANTE,
    SLUG_CONTA_SISTEMA,
    ContaLegada,
    FranquiaLegada,
    UserLegado,
    classificar_backfill_multiuser_legado,
)

logger = logging.getLogger(__name__)

TABELA_EXISTE = "existe"
TABELA_NAO_EXISTE = "nao_existe"
TABELA_INCONCLUSIVA = "inconclusivo"
CONTAGEM_CONHECIDA = "contagem_conhecida"
TABELA_AUSENTE_CONCLUSIVAMENTE = "tabela_ausente_conclusivamente"
RESERVAS_INCONCLUSIVO = "inconclusivo"
TABELA_RESERVAS_BACKFILL = "conta_multiuser_convite"
PG_UNDEFINED_TABLE = "42P01"
PG_UNDEFINED_COLUMN = "42703"
CODIGO_BACKFILL_CAPACITY_INSUFICIENTE = "capacity_insuficiente_backfill"


class InspecaoSchemaInconclusivaError(RuntimeError):
    """Introspecção de schema ou contagem de reservas sem prova conclusiva."""


@dataclass(frozen=True)
class ResultadoReservasBackfill:
    estado: str
    quantidade: int | None = None


@dataclass
class RelatorioBackfillMultiuser:
    contas_marcadas: int = 0
    quantidades_persistidas: int = 0
    vinculos_criados: int = 0
    inconsistencias: int = 0
    usuarios_multiuser_encontrados: int = 0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fetch_legado(connection) -> tuple[list[ContaLegada], list[FranquiaLegada], list[UserLegado], int | None]:
    contas_rows = connection.execute(
        text("SELECT id, slug, status FROM conta ORDER BY id")
    ).fetchall()
    franquias_rows = connection.execute(
        text("SELECT id, conta_id FROM franquia ORDER BY id")
    ).fetchall()
    users_rows = connection.execute(
        text('SELECT id, conta_id, franquia_id, categoria FROM "user" ORDER BY id')
    ).fetchall()

    contas = [
        ContaLegada(id=int(r[0]), slug=r[1] or "", status=r[2] or "")
        for r in contas_rows
    ]
    franquias = [
        FranquiaLegada(id=int(r[0]), conta_id=int(r[1]))
        for r in franquias_rows
    ]
    users = [
        UserLegado(
            id=int(r[0]),
            conta_id=int(r[1]),
            franquia_id=int(r[2]),
            categoria=(r[3] or ""),
        )
        for r in users_rows
    ]
    sistema = next((c.id for c in contas if c.slug == SLUG_CONTA_SISTEMA), None)
    return contas, franquias, users, sistema


def _carregar_estado_persistido(connection) -> tuple[set[int], set[int], set[int], set[tuple]]:
    ativos = connection.execute(
        text(
            """
            SELECT user_id, franquia_id, conta_id, papel
              FROM conta_vinculo_organizacional
             WHERE estado = :estado
            """
        ),
        {"estado": ESTADO_ATIVO},
    ).fetchall()
    users_com_ativo = {int(r[0]) for r in ativos}
    franquias_com_ativo = {int(r[1]) for r in ativos}
    contas_com_contratante = {
        int(r[2]) for r in ativos if (r[3] or "") == PAPEL_CONTRATANTE
    }
    inconsistencias = connection.execute(
        text(
            """
            SELECT conta_id, franquia_id, user_id, codigo
              FROM conta_organizacional_backfill_inconsistencia
            """
        )
    ).fetchall()
    chaves_inconsistencia = {
        (r[0], r[1], r[2], r[3]) for r in inconsistencias
    }
    return (
        users_com_ativo,
        franquias_com_ativo,
        contas_com_contratante,
        chaves_inconsistencia,
    )


def _connection_dialect_name(connection) -> str:
    dialect = getattr(connection, "dialect", None)
    name = getattr(dialect, "name", None) if dialect is not None else None
    if name:
        return str(name)
    engine = getattr(connection, "engine", None)
    dialect = getattr(engine, "dialect", None) if engine is not None else None
    name = getattr(dialect, "name", None) if dialect is not None else None
    return str(name) if name else ""


def _bloquear_conta_backfill(connection, conta_id: int) -> None:
    """Lock da Conta alinhado à raiz F2. SQLite ignora FOR UPDATE."""
    sql = "SELECT id FROM conta WHERE id = :id"
    if _connection_dialect_name(connection) != "sqlite":
        sql += " FOR UPDATE"
    connection.execute(text(sql), {"id": int(conta_id)})


def _nomes_tabelas_inspector(connection) -> list[str] | None:
    try:
        return list(sa_inspect(connection).get_table_names())
    except Exception:
        try:
            engine = getattr(connection, "engine", None)
            if engine is None:
                return None
            return list(sa_inspect(engine).get_table_names())
        except Exception:
            return None


def _estado_tabela(connection, table_name: str) -> str:
    names = _nomes_tabelas_inspector(connection)
    if names is None:
        return TABELA_INCONCLUSIVA
    return TABELA_EXISTE if table_name in names else TABELA_NAO_EXISTE


def _iter_excecao_sql(exc: BaseException):
    seen: set[int] = set()
    atual: BaseException | None = exc
    while atual is not None and id(atual) not in seen:
        seen.add(id(atual))
        yield atual
        orig = getattr(atual, "orig", None)
        atual = orig if isinstance(orig, BaseException) and orig is not atual else None


def _sqlstate_de_excecao(exc: BaseException) -> str | None:
    for item in _iter_excecao_sql(exc):
        for attr in ("pgcode", "sqlstate"):
            val = getattr(item, attr, None)
            if val:
                return str(val).strip().upper()
        diag = getattr(item, "diag", None)
        if diag is not None:
            for attr in ("sqlstate", "pgcode"):
                val = getattr(diag, attr, None)
                if val:
                    return str(val).strip().upper()
    return None


def _nome_tabela_diag(exc: BaseException) -> str | None:
    for item in _iter_excecao_sql(exc):
        diag = getattr(item, "diag", None)
        if diag is None:
            continue
        nome = getattr(diag, "table_name", None)
        if nome:
            return str(nome)
    return None


def _mensagens_excecao_sql(exc: BaseException) -> str:
    partes = []
    for item in _iter_excecao_sql(exc):
        partes.append(str(item))
        args = getattr(item, "args", None)
        if args:
            partes.extend(str(a) for a in args)
    return "\n".join(partes)


def _sqlite_tabela_esperada_ausente(exc: BaseException, table_name: str) -> bool:
    msg = _mensagens_excecao_sql(exc)
    padrao = rf"(?i)(?:^|[\s(\[])no such table:\s*{re.escape(table_name)}(?=$|[\s)\].,;])"
    return re.search(padrao, msg) is not None


def _postgres_tabela_esperada_ausente(exc: BaseException, table_name: str) -> bool:
    sqlstate = _sqlstate_de_excecao(exc)
    if sqlstate != PG_UNDEFINED_TABLE:
        return False
    diag_nome = _nome_tabela_diag(exc)
    if diag_nome:
        return diag_nome == table_name
    msg = _mensagens_excecao_sql(exc)
    if not msg or not table_name:
        return False
    padrao = (
        r"(?:relation|table)\s+"
        rf'(?:"{re.escape(table_name)}"|{re.escape(table_name)})'
        r"\s+does not exist"
    )
    return re.search(padrao, msg, flags=re.IGNORECASE) is not None


def _classificar_erro_consulta_tabela(exc: BaseException, table_name: str) -> str:
    """
    CONTAGEM_CONHECIDA nunca sai daqui (só de SQL bem-sucedido).
    TABELA_AUSENTE_CONCLUSIVAMENTE: 42P01 da tabela esperada, ou
    SQLite 'no such table: <esperada>'.
    Qualquer 42703, outro SQLSTATE, no such column, ou resto → INCONCLUSIVO.
    """
    sqlstate = _sqlstate_de_excecao(exc)
    if sqlstate:
        if sqlstate == PG_UNDEFINED_COLUMN:
            return RESERVAS_INCONCLUSIVO
        if sqlstate == PG_UNDEFINED_TABLE:
            if _postgres_tabela_esperada_ausente(exc, table_name):
                return TABELA_AUSENTE_CONCLUSIVAMENTE
            return RESERVAS_INCONCLUSIVO
        return RESERVAS_INCONCLUSIVO
    if _sqlite_tabela_esperada_ausente(exc, table_name):
        return TABELA_AUSENTE_CONCLUSIVAMENTE
    return RESERVAS_INCONCLUSIVO


def _table_exists(connection, table_name: str) -> bool:
    """True/False apenas quando a inspeção é conclusiva. Inconclusivo levanta."""
    estado = _estado_tabela(connection, table_name)
    if estado == TABELA_INCONCLUSIVA:
        raise InspecaoSchemaInconclusivaError(
            f"inspecao de schema inconclusiva para tabela {table_name}"
        )
    return estado == TABELA_EXISTE


def _contar_reservas_pendentes_validas_sql(connection, conta_id: int, agora: datetime) -> int:
    reservas = connection.execute(
        text(
            """
            SELECT COUNT(*)
              FROM conta_multiuser_convite
             WHERE conta_id = :id
               AND estado = :estado
               AND expires_at > :agora
            """
        ),
        {"id": int(conta_id), "estado": "pendente", "agora": agora},
    ).scalar()
    return int(reservas or 0)


def _resolver_reservas_backfill(
    connection, conta_id: int, agora: datetime
) -> ResultadoReservasBackfill:
    estado = _estado_tabela(connection, TABELA_RESERVAS_BACKFILL)
    if estado == TABELA_NAO_EXISTE:
        return ResultadoReservasBackfill(TABELA_AUSENTE_CONCLUSIVAMENTE, 0)
    try:
        quantidade = _contar_reservas_pendentes_validas_sql(connection, conta_id, agora)
        return ResultadoReservasBackfill(CONTAGEM_CONHECIDA, quantidade)
    except Exception as exc:
        classif = _classificar_erro_consulta_tabela(exc, TABELA_RESERVAS_BACKFILL)
        if classif == TABELA_AUSENTE_CONCLUSIVAMENTE:
            return ResultadoReservasBackfill(TABELA_AUSENTE_CONCLUSIVAMENTE, 0)
        raise InspecaoSchemaInconclusivaError(
            "contagem de reservas inconclusiva"
        ) from exc


def _contar_reservas_backfill(connection, conta_id: int, agora: datetime) -> int:
    """
    Consulta executou → inteiro.
    Tabela comprovadamente inexistente → 0.
    Qualquer outra incerteza → RAISE. Nunca engole Exception como zero.
    """
    resultado = _resolver_reservas_backfill(connection, conta_id, agora)
    if resultado.estado == RESERVAS_INCONCLUSIVO or resultado.quantidade is None:
        raise InspecaoSchemaInconclusivaError("contagem de reservas inconclusiva")
    return int(resultado.quantidade)


def _contar_ativos_backfill(connection, conta_id: int) -> int:
    ativos = connection.execute(
        text(
            """
            SELECT COUNT(*)
              FROM conta_vinculo_organizacional
             WHERE conta_id = :id
               AND estado = :estado
            """
        ),
        {"id": int(conta_id), "estado": ESTADO_ATIVO},
    ).scalar()
    return int(ativos or 0)


def _contar_comprometido_backfill(connection, conta_id: int, agora: datetime) -> int:
    return _contar_ativos_backfill(connection, conta_id) + _contar_reservas_backfill(
        connection, conta_id, agora
    )


def _ler_quantity_persistida(connection, conta_id: int) -> int | None:
    row = connection.execute(
        text("SELECT quantidade_assentos_contratados FROM conta WHERE id = :id"),
        {"id": int(conta_id)},
    ).first()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _vinculo_ja_coberto(
    vinculo,
    users_com_ativo: set[int],
    franquias_com_ativo: set[int],
    contas_com_contratante: set[int],
) -> bool:
    if vinculo.user_id in users_com_ativo:
        return True
    if vinculo.franquia_id in franquias_com_ativo:
        return True
    if vinculo.papel == PAPEL_CONTRATANTE and vinculo.conta_id in contas_com_contratante:
        return True
    return False


def _listar_vinculos_novos(
    plano,
    users_com_ativo: set[int],
    franquias_com_ativo: set[int],
    contas_com_contratante: set[int],
) -> list:
    users = set(users_com_ativo)
    franquias = set(franquias_com_ativo)
    contratantes = set(contas_com_contratante)
    novos = []
    for vinculo in plano.vinculos:
        if _vinculo_ja_coberto(vinculo, users, franquias, contratantes):
            continue
        novos.append(vinculo)
        users.add(vinculo.user_id)
        franquias.add(vinculo.franquia_id)
        if vinculo.papel == PAPEL_CONTRATANTE:
            contratantes.add(vinculo.conta_id)
    return novos


def _contar_vinculos_novos_por_conta(
    plano,
    users_com_ativo: set[int],
    franquias_com_ativo: set[int],
    contas_com_contratante: set[int],
) -> dict[int, int]:
    counts: dict[int, int] = {}
    for vinculo in _listar_vinculos_novos(
        plano, users_com_ativo, franquias_com_ativo, contas_com_contratante
    ):
        counts[vinculo.conta_id] = counts.get(vinculo.conta_id, 0) + 1
    return counts


def aplicar_backfill_multiuser_legado(connection) -> RelatorioBackfillMultiuser:
    """
    1. classifica legado
    2. marca contas Multiuser
    3. persiste quantity local somente se NULL, nunca abaixo do comprometido atual + vínculos novos
    4. cria vínculos determinísticos ainda inexistentes
    5. registra inconsistências sem destruir dados e sem duplicar

    Determinístico e reexecutável: não inventa vínculo em ambiguidade e não
    reinsere o que já existe. A Fase 2 deve reutilizar esta rotina antes do
    enforcement. Não promove a Fase 1 isoladamente para produção.
    """
    contas, franquias, users, sistema_id = _fetch_legado(connection)
    plano = classificar_backfill_multiuser_legado(
        contas=contas,
        franquias=franquias,
        users=users,
        sistema_conta_id=sistema_id,
    )
    agora = _utcnow()
    rel = RelatorioBackfillMultiuser(
        usuarios_multiuser_encontrados=sum(
            1
            for u in users
            if (u.categoria or "").strip().lower() in {"multiuser", "enterprise"}
        )
    )

    _contar_reservas_backfill(connection, 0, agora)

    (
        users_com_ativo,
        franquias_com_ativo,
        contas_com_contratante,
        chaves_inconsistencia,
    ) = _carregar_estado_persistido(connection)
    candidatos = _listar_vinculos_novos(
        plano,
        users_com_ativo,
        franquias_com_ativo,
        contas_com_contratante,
    )
    candidatos_por_conta: dict[int, list] = {}
    for vinculo in candidatos:
        candidatos_por_conta.setdefault(vinculo.conta_id, []).append(vinculo)

    extras_inconsistencias: list[InconsistenciaPlanejada] = []
    planos_mutacao: list[tuple[int, int | None, list, list]] = []

    for conta_id in plano.contas_multiuser_ids:
        _bloquear_conta_backfill(connection, conta_id)
        comprometido = _contar_comprometido_backfill(connection, conta_id, agora)
        pendentes = list(candidatos_por_conta.get(conta_id, []))
        quantity = _ler_quantity_persistida(connection, conta_id)
        plan_qtd = plano.quantidade_por_conta.get(conta_id)
        projetado = int(comprometido) + len(pendentes)

        if quantity is not None:
            qtd_inicial = None
            if projetado <= int(quantity):
                aceitos = pendentes
                rejeitados: list = []
            else:
                room = max(0, int(quantity) - comprometido)
                aceitos = pendentes[:room]
                rejeitados = pendentes[room:]
        else:
            fonte = int(plan_qtd) if plan_qtd is not None else 0
            qtd_inicial = max(fonte, projetado)
            aceitos = pendentes
            rejeitados = []

        planos_mutacao.append((conta_id, qtd_inicial, aceitos, rejeitados))

    for conta_id, qtd_inicial, aceitos, rejeitados in planos_mutacao:
        connection.execute(
            text(
                """
                UPDATE conta
                   SET multiuser_ativa = :flag
                 WHERE id = :id
                """
            ),
            {"id": conta_id, "flag": True},
        )
        rel.contas_marcadas += 1

        if qtd_inicial is not None:
            result = connection.execute(
                text(
                    """
                    UPDATE conta
                       SET quantidade_assentos_contratados = :qtd
                     WHERE id = :id
                       AND quantidade_assentos_contratados IS NULL
                    """
                ),
                {"id": conta_id, "qtd": int(qtd_inicial)},
            )
            if int(getattr(result, "rowcount", 0) or 0) > 0:
                rel.quantidades_persistidas += 1

        for vinculo in aceitos:
            connection.execute(
                text(
                    """
                    INSERT INTO conta_vinculo_organizacional (
                        conta_id, user_id, franquia_id, papel, estado, titular,
                        origem, criado_por_user_id, iniciado_em, encerrado_em,
                        created_at, updated_at
                    ) VALUES (
                        :conta_id, :user_id, :franquia_id, :papel, :estado, :titular,
                        :origem, NULL, :iniciado_em, NULL, :created_at, :updated_at
                    )
                    """
                ),
                {
                    "conta_id": vinculo.conta_id,
                    "user_id": vinculo.user_id,
                    "franquia_id": vinculo.franquia_id,
                    "papel": vinculo.papel,
                    "estado": ESTADO_ATIVO,
                    "titular": bool(vinculo.titular),
                    "origem": ORIGEM_BACKFILL_LEGADO,
                    "iniciado_em": agora,
                    "created_at": agora,
                    "updated_at": agora,
                },
            )
            rel.vinculos_criados += 1
            users_com_ativo.add(vinculo.user_id)
            franquias_com_ativo.add(vinculo.franquia_id)
            if vinculo.papel == PAPEL_CONTRATANTE:
                contas_com_contratante.add(vinculo.conta_id)

        for vinculo in rejeitados:
            extras_inconsistencias.append(
                InconsistenciaPlanejada(
                    codigo=CODIGO_BACKFILL_CAPACITY_INSUFICIENTE,
                    detalhe="vínculo de backfill omitido: quantity existente não comporta o comprometido projetado",
                    conta_id=vinculo.conta_id,
                    franquia_id=vinculo.franquia_id,
                    user_id=vinculo.user_id,
                )
            )

    for item in list(plano.inconsistencias) + extras_inconsistencias:
        chave = (item.conta_id, item.franquia_id, item.user_id, item.codigo)
        if chave in chaves_inconsistencia:
            continue
        connection.execute(
            text(
                """
                INSERT INTO conta_organizacional_backfill_inconsistencia (
                    conta_id, franquia_id, user_id, codigo, detalhe, created_at
                ) VALUES (
                    :conta_id, :franquia_id, :user_id, :codigo, :detalhe, :created_at
                )
                """
            ),
            {
                "conta_id": item.conta_id,
                "franquia_id": item.franquia_id,
                "user_id": item.user_id,
                "codigo": item.codigo,
                "detalhe": item.detalhe[:255],
                "created_at": agora,
            },
        )
        rel.inconsistencias += 1
        chaves_inconsistencia.add(chave)

    logger.info(
        "Backfill organizacional Multiuser: contas=%s quantity=%s vinculos=%s inconsistencias=%s users_multiuser=%s",
        rel.contas_marcadas,
        rel.quantidades_persistidas,
        rel.vinculos_criados,
        rel.inconsistencias,
        rel.usuarios_multiuser_encontrados,
    )
    return rel


def relatorio_as_dict(rel: RelatorioBackfillMultiuser) -> dict:
    return asdict(rel)
