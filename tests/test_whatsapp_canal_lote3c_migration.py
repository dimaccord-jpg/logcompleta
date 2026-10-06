"""Saneamento histórico da migration r9s0t1u2v3w4 antes da reconstrução."""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError

from app.models import OnboardingCanal, OnboardingCanalConclusao
from app.services.canal_aquisicao_service import CODIGO_CONTA_EXISTENTE
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_ACEITAR_TERMOS,
    ACAO_EMITIR_SENHA,
    ACAO_ORIENTAR_CONTA,
    ACAO_REGISTRAR_NOME,
    CODIGO_LINK_EMITIDO,
    CODIGO_RESPOSTA_REGISTRADA,
    TEXTO_CONTA,
    TEXTO_EMAIL,
    TEXTO_SENHA,
)

ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = (
    ROOT / "migrations" / "versions" / "r9s0t1u2v3w4_interpretacao_conclusao_ref.py"
)
_MARCA = "/onboarding/canal/concluir/"
_TOKEN_NOVA = "bruto-nova-conta-3c"
_TOKEN_VINCULO = "bruto-vinculo-conta-3c"
_TOKEN_AMBIGUO = "bruto-ambiguo-3c"
_TOKEN_CAIXA = "bruto-caixa-3c"
_TOKEN_SEM_JORNADA = "bruto-sem-jornada-3c"
_TOKEN_CONFLITO = "bruto-conflito-3c"
_SEGREDOS = (
    _TOKEN_NOVA,
    _TOKEN_VINCULO,
    _TOKEN_AMBIGUO,
    _TOKEN_CAIXA,
    _TOKEN_SEM_JORNADA,
    _TOKEN_CONFLITO,
)


def _carregar():
    spec = importlib.util.spec_from_file_location("mig_interpretacao_conclusao_ref", _MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)
    return mig


def _engine(tmp_path):
    db_path = tmp_path / "interpretacao_conclusao_ref.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")

    @event.listens_for(engine, "connect")
    def _chaves_estrangeiras(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


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


def _hash(rotulo: str) -> str:
    return hashlib.sha256(rotulo.encode("utf-8")).hexdigest()


def _preparar(engine):
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE evento_canal_recebido (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE onboarding_canal (id INTEGER PRIMARY KEY)"))
        conn.execute(
            text(
                """
                CREATE TABLE onboarding_canal_conclusao (
                    id INTEGER PRIMARY KEY,
                    onboarding_id INTEGER NOT NULL,
                    finalidade VARCHAR(40) NOT NULL,
                    token_hash VARCHAR(64) NOT NULL UNIQUE,
                    estado VARCHAR(20) NOT NULL,
                    emitido_em DATETIME NOT NULL,
                    expira_em DATETIME NOT NULL,
                    consumido_em DATETIME,
                    atualizada_em DATETIME NOT NULL,
                    FOREIGN KEY(onboarding_id) REFERENCES onboarding_canal (id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE interpretacao_conversacional_canal (
                    id INTEGER PRIMARY KEY,
                    evento_id INTEGER NOT NULL UNIQUE,
                    codigo VARCHAR(64) NOT NULL,
                    acao VARCHAR(64) NOT NULL,
                    onboarding_id INTEGER,
                    etapa_final VARCHAR(40),
                    texto_resposta VARCHAR(1024),
                    FOREIGN KEY(evento_id) REFERENCES evento_canal_recebido (id),
                    FOREIGN KEY(onboarding_id) REFERENCES onboarding_canal (id)
                )
                """
            )
        )
        for identificador in range(1, 8):
            conn.execute(
                text("INSERT INTO evento_canal_recebido (id) VALUES (:id)"),
                {"id": identificador},
            )
        for identificador in range(1, 5):
            conn.execute(
                text("INSERT INTO onboarding_canal (id) VALUES (:id)"),
                {"id": identificador},
            )
        conclusoes = (
            (10, 1, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "emitido", None),
            (11, 1, OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA, "revogado", None),
            (20, 2, OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA, "emitido", None),
            (21, 2, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "revogado", None),
            (30, 3, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "emitido", None),
            (31, 3, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "revogado", None),
            (40, 4, OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA, "consumido", "2026-09-02 12:00:00"),
        )
        for conclusao_id, onboarding_id, finalidade, estado, consumido_em in conclusoes:
            conn.execute(
                text(
                    """
                    INSERT INTO onboarding_canal_conclusao (
                        id, onboarding_id, finalidade, token_hash, estado,
                        emitido_em, expira_em, consumido_em, atualizada_em
                    ) VALUES (
                        :id, :onboarding_id, :finalidade, :token_hash, :estado,
                        '2026-09-01 12:00:00', '2026-09-08 12:00:00',
                        :consumido_em, '2026-09-01 12:00:00'
                    )
                    """
                ),
                {
                    "id": conclusao_id,
                    "onboarding_id": onboarding_id,
                    "finalidade": finalidade,
                    "token_hash": _hash(f"conclusao-{conclusao_id}"),
                    "estado": estado,
                    "consumido_em": consumido_em,
                },
            )
        interpretacoes = (
            (
                1,
                1,
                CODIGO_LINK_EMITIDO,
                ACAO_ACEITAR_TERMOS,
                1,
                OnboardingCanal.ETAPA_SENHA,
                f"Abra {_MARCA}{_TOKEN_NOVA} para seguir",
            ),
            (
                2,
                2,
                CODIGO_CONTA_EXISTENTE,
                ACAO_ORIENTAR_CONTA,
                2,
                OnboardingCanal.ETAPA_EMAIL,
                f"Confirme em {_MARCA}{_TOKEN_VINCULO}",
            ),
            (
                3,
                3,
                CODIGO_LINK_EMITIDO,
                ACAO_EMITIR_SENHA,
                3,
                OnboardingCanal.ETAPA_SENHA,
                f"Link {_MARCA}{_TOKEN_AMBIGUO}",
            ),
            (
                4,
                4,
                CODIGO_RESPOSTA_REGISTRADA,
                ACAO_REGISTRAR_NOME,
                2,
                OnboardingCanal.ETAPA_NOME,
                TEXTO_EMAIL,
            ),
            (
                5,
                5,
                CODIGO_LINK_EMITIDO,
                ACAO_EMITIR_SENHA,
                4,
                OnboardingCanal.ETAPA_SENHA,
                f"Veja /Onboarding/Canal/Concluir/{_TOKEN_CAIXA}",
            ),
            (
                6,
                6,
                CODIGO_LINK_EMITIDO,
                ACAO_ACEITAR_TERMOS,
                None,
                OnboardingCanal.ETAPA_SENHA,
                f"{_MARCA}{_TOKEN_SEM_JORNADA}",
            ),
            (
                7,
                7,
                CODIGO_LINK_EMITIDO,
                ACAO_ORIENTAR_CONTA,
                1,
                OnboardingCanal.ETAPA_SENHA,
                f"Rota {_MARCA}{_TOKEN_CONFLITO}",
            ),
        )
        for row in interpretacoes:
            conn.execute(
                text(
                    """
                    INSERT INTO interpretacao_conversacional_canal (
                        id, evento_id, codigo, acao, onboarding_id,
                        etapa_final, texto_resposta
                    ) VALUES (
                        :id, :evento_id, :codigo, :acao, :onboarding_id,
                        :etapa_final, :texto_resposta
                    )
                    """
                ),
                {
                    "id": row[0],
                    "evento_id": row[1],
                    "codigo": row[2],
                    "acao": row[3],
                    "onboarding_id": row[4],
                    "etapa_final": row[5],
                    "texto_resposta": row[6],
                },
            )


def _tabelas(engine):
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars()
        )


def _linhas(engine):
    with engine.connect() as conn:
        return {
            row["id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT id, codigo, acao, onboarding_id, etapa_final,
                           texto_resposta, conclusao_id
                    FROM interpretacao_conversacional_canal
                    ORDER BY id
                    """
                )
            ).mappings()
        }


def _colunas(engine):
    with engine.connect() as conn:
        return {
            row[1]
            for row in conn.execute(
                text("PRAGMA table_info(interpretacao_conversacional_canal)")
            )
        }


def _assert_sem_segredo(linhas):
    for row in linhas.values():
        texto = row["texto_resposta"] or ""
        assert _MARCA.casefold() not in texto.casefold()
        for segredo in _SEGREDOS:
            assert segredo not in texto
            assert segredo.casefold() not in texto.casefold()


def _assert_interpretacao_preservada(antes, depois):
    assert set(antes) == set(depois)
    for identificador, original in antes.items():
        atual = depois[identificador]
        assert atual["codigo"] == original["codigo"]
        assert atual["acao"] == original["acao"]
        assert atual["onboarding_id"] == original["onboarding_id"]
        assert atual["etapa_final"] == original["etapa_final"]


def _recusar_rota(engine):
    with engine.connect() as conn:
        transacao = conn.begin()
        try:
            conn.execute(text("INSERT INTO evento_canal_recebido (id) VALUES (90)"))
            conn.execute(
                text(
                    """
                    INSERT INTO interpretacao_conversacional_canal (
                        id, evento_id, codigo, acao, texto_resposta
                    ) VALUES (
                        90, 90, :codigo, :acao, :texto
                    )
                    """
                ),
                {
                    "codigo": CODIGO_LINK_EMITIDO,
                    "acao": ACAO_EMITIR_SENHA,
                    "texto": f"{_MARCA}qualquer-coisa",
                },
            )
            transacao.commit()
        except IntegrityError:
            transacao.rollback()
            return
    raise AssertionError("insert da rota de conclusao foi aceito")


def test_mensagens_e_ordem_acompanham_o_tipo_estruturado():
    mig = _carregar()
    assert mig._TEXTO_DEFINIR_SENHA == TEXTO_SENHA
    assert mig._TEXTO_VINCULAR_CONTA == TEXTO_CONTA
    assert mig._SQL_SEM_LINK == (
        "texto_resposta IS NULL OR "
        "lower(texto_resposta) NOT LIKE '%/onboarding/canal/concluir/%'"
    )
    fonte = _MIGRATION.read_text(encoding="utf-8")
    for valor in (
        ACAO_ACEITAR_TERMOS,
        ACAO_EMITIR_SENHA,
        ACAO_ORIENTAR_CONTA,
        CODIGO_LINK_EMITIDO,
        CODIGO_CONTA_EXISTENTE,
        OnboardingCanal.ETAPA_SENHA,
        OnboardingCanal.ETAPA_EMAIL,
        OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA,
        OnboardingCanalConclusao.FINALIDADE_VINCULAR_CONTA,
    ):
        assert valor in fonte
    corpo = fonte.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    ordem = (
        "_sanear_texto_historico",
        "_exigir_rota_ausente",
        "_adicionar_referencia",
        "_backfill_conclusao",
        "_criar_check_sem_link",
    )
    posicoes = [corpo.index(nome) for nome in ordem]
    assert posicoes == sorted(posicoes)
    downgrade = fonte.split("def downgrade()", 1)[1]
    assert "irreversível por design" in downgrade
    assert "UPDATE" not in downgrade
    assert "token_hash" not in fonte
    assert "substr(" not in fonte.casefold()


def _rotas_sensiveis(conn):
    return conn.execute(
        text(
            """
            SELECT COUNT(*)
            FROM interpretacao_conversacional_canal
            WHERE texto_resposta IS NOT NULL
              AND lower(texto_resposta) LIKE :marca
            """
        ),
        {"marca": f"%{_MARCA}%"},
    ).scalar()


def test_texto_sensivel_ausente_antes_da_primeira_reconstrucao(tmp_path):
    """A primeira reconstrução não pode copiar URL nem token histórico."""
    engine = _engine(tmp_path)
    _preparar(engine)
    mig = _carregar()
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    observados = []

    with engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"render_as_batch": True})
        ops = Operations(context)
        original_batch = ops.batch_alter_table

        def batch_interceptado(*args, **kwargs):
            if not observados:
                colunas = {
                    row[1]
                    for row in conn.execute(
                        text("PRAGMA table_info(interpretacao_conversacional_canal)")
                    )
                }
                textos = {
                    row["id"]: row["texto_resposta"]
                    for row in conn.execute(
                        text(
                            """
                            SELECT id, texto_resposta
                            FROM interpretacao_conversacional_canal
                            ORDER BY id
                            """
                        )
                    ).mappings()
                }
                observados.append(
                    {
                        "colunas": colunas,
                        "textos": textos,
                        "rotas": _rotas_sensiveis(conn),
                    }
                )
            return original_batch(*args, **kwargs)

        ops.batch_alter_table = batch_interceptado
        original = mig.op
        try:
            mig.op = ops
            with conn.begin():
                mig.upgrade()
        finally:
            mig.op = original

    assert observados, "a reconstrucao estrutural nao ocorreu"
    instante = observados[0]
    assert instante["rotas"] == 0
    assert "conclusao_id" not in instante["colunas"]
    textos = instante["textos"]
    for texto in textos.values():
        bruto = texto or ""
        assert _MARCA.casefold() not in bruto.casefold()
        for segredo in _SEGREDOS:
            assert segredo.casefold() not in bruto.casefold()
    assert textos[1] == TEXTO_SENHA
    assert textos[2] == TEXTO_CONTA
    assert textos[3] == TEXTO_SENHA
    assert textos[4] == TEXTO_EMAIL
    assert textos[5] == TEXTO_SENHA
    assert textos[6] == TEXTO_SENHA
    assert textos[7] is None


def test_upgrade_saneia_url_historica_antes_do_check(tmp_path):
    engine = _engine(tmp_path)
    _preparar(engine)
    tabelas = _tabelas(engine)
    mig = _carregar()
    _run(engine, mig, mig.upgrade)
    assert _tabelas(engine) == tabelas
    assert "conclusao_id" in _colunas(engine)

    linhas = _linhas(engine)
    _assert_sem_segredo(linhas)
    assert linhas[1]["texto_resposta"] == TEXTO_SENHA
    assert linhas[1]["conclusao_id"] == 10
    assert linhas[5]["texto_resposta"] == TEXTO_SENHA
    assert linhas[5]["conclusao_id"] == 40
    assert linhas[2]["texto_resposta"] == TEXTO_CONTA
    assert linhas[2]["conclusao_id"] == 20
    assert linhas[3]["texto_resposta"] == TEXTO_SENHA
    assert linhas[3]["conclusao_id"] is None
    assert linhas[4]["texto_resposta"] == TEXTO_EMAIL
    assert linhas[4]["conclusao_id"] is None
    assert linhas[6]["texto_resposta"] == TEXTO_SENHA
    assert linhas[6]["conclusao_id"] is None
    assert linhas[7]["texto_resposta"] is None
    assert linhas[7]["conclusao_id"] is None
    assert "Abra" not in (linhas[1]["texto_resposta"] or "")
    _recusar_rota(engine)

    with engine.connect() as conn:
        antes = {
            row["id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT id, codigo, acao, onboarding_id, etapa_final, texto_resposta
                    FROM interpretacao_conversacional_canal
                    ORDER BY id
                    """
                )
            ).mappings()
        }
        conclusoes = conn.execute(
            text(
                """
                SELECT id, onboarding_id, finalidade, token_hash, estado
                FROM onboarding_canal_conclusao
                ORDER BY id
                """
            )
        ).mappings().all()
    _run(engine, mig, mig.downgrade)
    assert "conclusao_id" not in _colunas(engine)
    depois = _linhas_sem_conclusao(engine)
    _assert_interpretacao_preservada(antes, depois)
    _assert_sem_segredo(depois)
    for identificador, original in antes.items():
        assert depois[identificador]["texto_resposta"] == original["texto_resposta"]
    with engine.connect() as conn:
        conclusoes_depois = conn.execute(
            text(
                """
                SELECT id, onboarding_id, finalidade, token_hash, estado
                FROM onboarding_canal_conclusao
                ORDER BY id
                """
            )
        ).mappings().all()
    assert list(conclusoes_depois) == list(conclusoes)

    _run(engine, mig, mig.upgrade)
    reidratadas = _linhas(engine)
    assert reidratadas[1]["conclusao_id"] == 10
    assert reidratadas[2]["conclusao_id"] == 20
    assert reidratadas[3]["conclusao_id"] is None
    assert reidratadas[4]["texto_resposta"] == TEXTO_EMAIL
    assert reidratadas[4]["conclusao_id"] is None
    assert reidratadas[5]["conclusao_id"] == 40
    _assert_sem_segredo(reidratadas)
    _recusar_rota(engine)


def _linhas_sem_conclusao(engine):
    with engine.connect() as conn:
        return {
            row["id"]: row
            for row in conn.execute(
                text(
                    """
                    SELECT id, codigo, acao, onboarding_id, etapa_final, texto_resposta
                    FROM interpretacao_conversacional_canal
                    ORDER BY id
                    """
                )
            ).mappings()
        }
