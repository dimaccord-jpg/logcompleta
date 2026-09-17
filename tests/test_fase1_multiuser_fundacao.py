"""Testes incrementais da Fase 1 — fundação persistente Multiuser."""
from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.infra import load_user_for_flask_login
from app.models import (
    ConfigRegras,
    Conta,
    ContaOrganizacionalBackfillInconsistencia,
    ContaVinculoOrganizacional,
    Franquia,
    User,
)
from app.services import plano_service
from app.services.cnpj_service import (
    CnpjInvalidoError,
    cnpj_valido,
    exigir_cnpj_normalizado_valido,
    normalizar_cnpj,
    normalizar_cnpj_opcional,
)
from app.services.conta_organizacional_backfill_service import (
    aplicar_backfill_multiuser_legado,
)
from app.services.conta_organizacional_service import (
    CnpjMultiuserDuplicadoError,
    DadosEmpresariaisMultiuserIncompletosError,
    VinculoOrganizacionalConflitoError,
    converter_ou_relancar_integrity_error,
    criar_vinculo_organizacional,
    encerrar_vinculo_organizacional,
    exigir_cnpj_para_ativacao_multiuser,
    identificar_constraint_integrity_error,
    listar_historico_vinculos_conta,
    marcar_conta_multiuser_ativa,
    persistir_dados_empresariais_conta,
    persistir_quantidade_assentos_contratados,
)
from app.services.cnpj_service import _digito_verificador
from tests.conftest import seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

ROOT = Path(__file__).resolve().parents[1]


def _cnpj_from_base(base12: str) -> str:
    d1 = _digito_verificador(base12, (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    d2 = _digito_verificador(base12 + str(d1), (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2))
    return base12 + str(d1) + str(d2)


CNPJ_A = _cnpj_from_base("112223330001")
CNPJ_B = _cnpj_from_base("445556660001")
CNPJ_A_MASCARA = f"{CNPJ_A[:2]}.{CNPJ_A[2:5]}.{CNPJ_A[5:8]}/{CNPJ_A[8:12]}-{CNPJ_A[12:]}"


def _preencher_empresa_ativacao(conta, *, slug: str, cnpj: str | None):
    """H08/FSD §5: nova ativação exige pacote empresarial; CNPJ permanece independente."""
    persistir_dados_empresariais_conta(
        conta.id,
        razao_social=f"Razao {slug} LTDA",
        nome_fantasia=f"Fantasia {slug}",
        cnpj=cnpj,
        email_empresarial=f"financeiro.{slug}@example.test",
        endereco_logradouro="Rua Teste",
        endereco_numero="100",
        endereco_cidade="Sao Paulo",
        endereco_uf="SP",
        endereco_cep="01001000",
    )


def _seed_cfg_planos_basico():
    seed_sistema_interno()
    db.session.add(
        ConfigRegras(
            chave="plano_valor_admin_multiuser",
            valor_real=10.0,
            valor_texto="10.00",
            descricao="teste",
        )
    )
    db.session.commit()


# --- CNPJ ---


def test_cnpj_normaliza_sem_mascara():
    assert normalizar_cnpj(CNPJ_A) == CNPJ_A


def test_cnpj_normaliza_com_mascara():
    assert normalizar_cnpj(CNPJ_A_MASCARA) == CNPJ_A


def test_cnpj_valido_aceito():
    assert cnpj_valido(CNPJ_A) is True
    assert exigir_cnpj_normalizado_valido(CNPJ_A_MASCARA) == CNPJ_A


def test_cnpj_invalido_tamanho_e_nao_numerico():
    assert cnpj_valido("123") is False
    assert cnpj_valido("12.345.678/0001") is False
    with pytest.raises(CnpjInvalidoError):
        exigir_cnpj_normalizado_valido("123")


def test_cnpj_digitos_verificadores_rejeitados():
    invalido = CNPJ_A[:12] + ("0" if CNPJ_A[12] != "0" else "1") + CNPJ_A[13]
    if cnpj_valido(invalido):
        invalido = CNPJ_A[:13] + ("0" if CNPJ_A[13] != "0" else "1")
    assert cnpj_valido(invalido) is False
    assert cnpj_valido("11111111111111") is False
    with pytest.raises(CnpjInvalidoError):
        exigir_cnpj_normalizado_valido(invalido)


def test_cnpj_aceita_whitespace_externo():
    assert normalizar_cnpj(f"  {CNPJ_A}  ") == CNPJ_A
    assert exigir_cnpj_normalizado_valido(f"\n{CNPJ_A_MASCARA}\t") == CNPJ_A


def test_cnpj_rejeita_letras_e_mascara_nao_canonica():
    rejeitados = (
        "04ABC252011000110",
        "04.252ABC.011/0001-10",
        "04-252-011-0001-10",
        "04.252.011.0001-10",
        "04/252/011/0001-10",
        "04252011000110x",
        "CNPJ 04252011000110",
        "04.252.011/0001-10 extra",
    )
    for bruto in rejeitados:
        assert cnpj_valido(bruto) is False
        with pytest.raises(CnpjInvalidoError):
            normalizar_cnpj(bruto)
        with pytest.raises(CnpjInvalidoError):
            exigir_cnpj_normalizado_valido(bruto)


def test_cnpj_null_permitido_conta_legada(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="legado-sem-cnpj")
        persistir_dados_empresariais_conta(conta.id, cnpj=None, razao_social="Legada")
        rec = db.session.get(Conta, conta.id)
        assert rec.cnpj is None
        assert rec.razao_social == "Legada"
        assert normalizar_cnpj_opcional(None) is None


def test_multiuser_novo_exige_cnpj_na_ativacao(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="mu-sem-cnpj")
        with pytest.raises(
            (CnpjInvalidoError, DadosEmpresariaisMultiuserIncompletosError)
        ):
            marcar_conta_multiuser_ativa(conta.id, exigir_cnpj=True)
        _preencher_empresa_ativacao(conta, slug="mu-sem-cnpj", cnpj=None)
        with pytest.raises(CnpjInvalidoError):
            marcar_conta_multiuser_ativa(conta.id, exigir_cnpj=True)
        _preencher_empresa_ativacao(conta, slug="mu-sem-cnpj", cnpj=CNPJ_A)
        marcar_conta_multiuser_ativa(conta.id, exigir_cnpj=True)
        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is True
        assert rec.cnpj == CNPJ_A
        exigir_cnpj_para_ativacao_multiuser(rec)


def test_cnpj_unico_entre_contas_multiuser_ativas(app):
    with app.app_context():
        a, _fa = seed_conta_franquia_cliente(slug="mu-cnpj-a")
        b, _fb = seed_conta_franquia_cliente(slug="mu-cnpj-b")
        _preencher_empresa_ativacao(a, slug="mu-cnpj-a", cnpj=CNPJ_A)
        marcar_conta_multiuser_ativa(a.id)
        _preencher_empresa_ativacao(b, slug="mu-cnpj-b", cnpj=CNPJ_A)
        with pytest.raises(CnpjMultiuserDuplicadoError):
            marcar_conta_multiuser_ativa(b.id)


def test_cnpj_de_conta_inativa_nao_bloqueia(app):
    with app.app_context():
        a, _fa = seed_conta_franquia_cliente(slug="mu-inativa")
        b, _fb = seed_conta_franquia_cliente(slug="mu-nova-ativa")
        _preencher_empresa_ativacao(a, slug="mu-inativa", cnpj=CNPJ_A)
        marcar_conta_multiuser_ativa(a.id)
        a = db.session.get(Conta, a.id)
        a.status = Conta.STATUS_INATIVA
        db.session.commit()
        _preencher_empresa_ativacao(b, slug="mu-nova-ativa", cnpj=CNPJ_A)
        marcar_conta_multiuser_ativa(b.id)
        rec = db.session.get(Conta, b.id)
        assert rec.multiuser_ativa is True
        assert rec.cnpj == CNPJ_A


def test_cnpj_integrity_error_tratado(app):
    with app.app_context():
        a, _fa = seed_conta_franquia_cliente(slug="mu-ie-a")
        b, _fb = seed_conta_franquia_cliente(slug="mu-ie-b")
        _preencher_empresa_ativacao(a, slug="mu-ie-a", cnpj=CNPJ_A)
        marcar_conta_multiuser_ativa(a.id)
        _preencher_empresa_ativacao(b, slug="mu-ie-b", cnpj=CNPJ_B)
        marcar_conta_multiuser_ativa(b.id)
        b = db.session.get(Conta, b.id)
        b.cnpj = CNPJ_A
        db.session.add(b)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        _preencher_empresa_ativacao(b, slug="mu-ie-b", cnpj=CNPJ_B)
        with pytest.raises(CnpjMultiuserDuplicadoError):
            persistir_dados_empresariais_conta(
                b.id,
                razao_social="Razao mu-ie-b LTDA",
                nome_fantasia="Fantasia mu-ie-b",
                cnpj=CNPJ_A,
                email_empresarial="financeiro.mu-ie-b@example.test",
                endereco_logradouro="Rua Teste",
                endereco_numero="100",
                endereco_cidade="Sao Paulo",
                endereco_uf="SP",
                endereco_cep="01001000",
            )


# --- Vínculo organizacional ---


def test_vinculo_papel_contratante_e_membro(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="org-papeis")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="Segunda",
            slug="segunda",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        u1 = seed_usuario(fr1.id, conta.id, email="contratante@test.com", categoria="multiuser")
        u2 = seed_usuario(fr2.id, conta.id, email="membro@test.com", categoria="multiuser")
        v1 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr1.id,
            papel=ContaVinculoOrganizacional.PAPEL_CONTRATANTE,
        )
        v2 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u2.id,
            franquia_id=fr2.id,
            papel=ContaVinculoOrganizacional.PAPEL_MEMBRO,
        )
        assert v1.papel == "contratante"
        assert v1.titular is True
        assert v1.estado == "ativo"
        assert v2.papel == "membro"
        assert v2.titular is False
        assert u1.conta_id == conta.id
        assert u1.franquia_id == fr1.id


def test_vinculo_papel_invalido(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="org-papel-inv")
        user = seed_usuario(fr.id, conta.id, email="inv@test.com", categoria="multiuser")
        with pytest.raises(ValueError, match="Papel organizacional inválido"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=user.id,
                franquia_id=fr.id,
                papel="gestor",
            )
        with pytest.raises(ValueError, match="Papel organizacional inválido"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=user.id,
                franquia_id=fr.id,
                papel="admin",
            )


def test_encerramento_preserva_row_e_historico(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="org-hist")
        user = seed_usuario(fr.id, conta.id, email="hist@test.com", categoria="multiuser")
        vinculo = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=fr.id,
            papel="contratante",
        )
        vid = vinculo.id
        encerrar_vinculo_organizacional(vid)
        row = db.session.get(ContaVinculoOrganizacional, vid)
        assert row is not None
        assert row.estado == "encerrado"
        assert row.encerrado_em is not None
        assert row.papel == "contratante"
        assert row.user_id == user.id
        assert row.conta_id == conta.id
        assert row.franquia_id == fr.id
        hist = listar_historico_vinculos_conta(conta.id)
        assert len(hist) == 1
        assert hist[0].id == vid
        novo = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=fr.id,
            papel="membro",
        )
        assert novo.id != vid
        assert novo.estado == "ativo"
        hist2 = listar_historico_vinculos_conta(conta.id)
        assert len(hist2) == 2


def test_contratante_sempre_titular_membro_nunca(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="tit-ok")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="Segunda",
            slug="segunda",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        u1 = seed_usuario(fr1.id, conta.id, email="tit.c@test.com", categoria="multiuser")
        u2 = seed_usuario(fr2.id, conta.id, email="tit.m@test.com", categoria="multiuser")
        v1 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr1.id,
            papel="contratante",
        )
        v2 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u2.id,
            franquia_id=fr2.id,
            papel="membro",
        )
        assert v1.titular is True
        assert v2.titular is False


def test_caller_nao_consegue_titularidade_contraditoria(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="tit-contra")
        user = seed_usuario(fr.id, conta.id, email="tit.x@test.com", categoria="multiuser")
        with pytest.raises(ValueError, match="Titularidade é derivada do papel"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=user.id,
                franquia_id=fr.id,
                papel="contratante",
                titular=False,
            )
        with pytest.raises(ValueError, match="Titularidade é derivada do papel"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=user.id,
                franquia_id=fr.id,
                papel="membro",
                titular=True,
            )


def test_insert_direto_papel_titular_contraditorio_falha(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="tit-ck")
        user = seed_usuario(fr.id, conta.id, email="tit.ck@test.com", categoria="multiuser")
        row = ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=fr.id,
            papel="contratante",
            estado="ativo",
            titular=False,
            origem="dominio",
        )
        db.session.add(row)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        row2 = ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=fr.id,
            papel="membro",
            estado="ativo",
            titular=True,
            origem="dominio",
        )
        db.session.add(row2)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_um_contratante_ativo_por_conta_e_historico(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="tit-uniq")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="Segunda",
            slug="segunda",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        u1 = seed_usuario(fr1.id, conta.id, email="c1@test.com", categoria="multiuser")
        u2 = seed_usuario(fr2.id, conta.id, email="c2@test.com", categoria="multiuser")
        v1 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr1.id,
            papel="contratante",
        )
        with pytest.raises(VinculoOrganizacionalConflitoError, match="Contratante ativo"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u2.id,
                franquia_id=fr2.id,
                papel="contratante",
            )
        row_segundo = ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=u2.id,
            franquia_id=fr2.id,
            papel="contratante",
            estado="ativo",
            titular=True,
            origem="dominio",
        )
        db.session.add(row_segundo)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()
        encerrar_vinculo_organizacional(v1.id)
        v2 = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u2.id,
            franquia_id=fr2.id,
            papel="contratante",
        )
        assert v2.titular is True
        hist = listar_historico_vinculos_conta(conta.id)
        assert len(hist) == 2
        assert {h.estado for h in hist} == {"ativo", "encerrado"}
        encerrado = db.session.get(ContaVinculoOrganizacional, v1.id)
        assert encerrado.estado == "encerrado"
        assert encerrado.papel == "contratante"
        row_direto = ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr1.id,
            papel="contratante",
            estado="ativo",
            titular=True,
            origem="dominio",
        )
        db.session.add(row_direto)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def test_no_maximo_um_vinculo_ativo_por_user_e_franquia(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="org-uniq")
        u1 = seed_usuario(fr.id, conta.id, email="u1@test.com", categoria="multiuser")
        criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr.id,
            papel="contratante",
        )
        with pytest.raises(ValueError, match="Já existe vínculo organizacional ativo"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u1.id,
                franquia_id=fr.id,
                papel="membro",
            )
        fr2 = Franquia(
            conta_id=conta.id,
            nome="Outra",
            slug="outra",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        u2 = seed_usuario(fr2.id, conta.id, email="u2@test.com", categoria="multiuser")
        u2.franquia_id = fr.id
        db.session.commit()
        with pytest.raises(ValueError, match="Já existe vínculo organizacional ativo"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u2.id,
                franquia_id=fr.id,
                papel="membro",
            )


def test_constraint_vinculo_ativo_dispara_integrity_error(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="org-ie")
        u1 = seed_usuario(fr.id, conta.id, email="ie1@test.com", categoria="multiuser")
        criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr.id,
            papel="contratante",
        )
        row = ContaVinculoOrganizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr.id,
            papel="membro",
            estado="ativo",
            titular=False,
            origem="dominio",
        )
        db.session.add(row)
        with pytest.raises(IntegrityError):
            db.session.flush()
        db.session.rollback()


def _integrity_sqlite(msg: str) -> IntegrityError:
    class _Orig:
        diag = None

        def __str__(self):
            return msg

    return IntegrityError("INSERT", {}, _Orig())


def _integrity_pg(constraint_name: str) -> IntegrityError:
    class _Diag:
        pass

    _Diag.constraint_name = constraint_name

    class _OrigPg:
        diag = _Diag()

        def __str__(self):
            return 'duplicate key value violates unique constraint "%s"' % constraint_name

    return IntegrityError("INSERT", {}, _OrigPg())


def test_integrity_error_constraints_esperadas_convertidas(app):
    from app.services.conta_organizacional_rules import (
        UQ_CONTA_CNPJ_MULTIUSER_ATIVA,
        UQ_VINCULO_CONTRATANTE_ATIVO,
        UQ_VINCULO_FRANQUIA_ATIVO,
        UQ_VINCULO_USER_ATIVO,
    )

    casos_sqlite = (
        ("UNIQUE constraint failed: conta.cnpj", CnpjMultiuserDuplicadoError),
        (
            "UNIQUE constraint failed: conta_vinculo_organizacional.user_id",
            VinculoOrganizacionalConflitoError,
        ),
        (
            "UNIQUE constraint failed: conta_vinculo_organizacional.franquia_id",
            VinculoOrganizacionalConflitoError,
        ),
        (
            "UNIQUE constraint failed: conta_vinculo_organizacional.conta_id",
            VinculoOrganizacionalConflitoError,
        ),
        (
            f"UNIQUE constraint failed: {UQ_CONTA_CNPJ_MULTIUSER_ATIVA}",
            CnpjMultiuserDuplicadoError,
        ),
        (
            f"UNIQUE constraint failed: {UQ_VINCULO_CONTRATANTE_ATIVO}",
            VinculoOrganizacionalConflitoError,
        ),
    )
    for msg, tipo in casos_sqlite:
        exc = _integrity_sqlite(msg)
        assert identificar_constraint_integrity_error(exc) is not None
        with pytest.raises(tipo):
            converter_ou_relancar_integrity_error(exc)

    casos_pg = (
        (UQ_CONTA_CNPJ_MULTIUSER_ATIVA, CnpjMultiuserDuplicadoError),
        (UQ_VINCULO_USER_ATIVO, VinculoOrganizacionalConflitoError),
        (UQ_VINCULO_FRANQUIA_ATIVO, VinculoOrganizacionalConflitoError),
        (UQ_VINCULO_CONTRATANTE_ATIVO, VinculoOrganizacionalConflitoError),
    )
    for nome, tipo in casos_pg:
        exc = _integrity_pg(nome)
        assert identificar_constraint_integrity_error(exc) == nome
        with pytest.raises(tipo):
            converter_ou_relancar_integrity_error(exc)

    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="ie-map")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="F2",
            slug="f2",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.commit()
        u1 = seed_usuario(fr1.id, conta.id, email="ie.u1@test.com", categoria="multiuser")
        u2 = seed_usuario(fr2.id, conta.id, email="ie.u2@test.com", categoria="multiuser")
        criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=u1.id,
            franquia_id=fr1.id,
            papel="contratante",
        )
        # Isola uq_user_ativo: mesma pessoa em Franquia distinta (SQLite pode
        # reportar uq_franquia_ativo primeiro se user+franquia coincidirem).
        u1.franquia_id = fr2.id
        db.session.commit()
        with pytest.raises(VinculoOrganizacionalConflitoError, match="este User"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u1.id,
                franquia_id=fr2.id,
                papel="membro",
            )
        u1.franquia_id = fr1.id
        db.session.commit()
        u2.franquia_id = fr1.id
        db.session.commit()
        with pytest.raises(VinculoOrganizacionalConflitoError, match="esta Franquia"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u2.id,
                franquia_id=fr1.id,
                papel="membro",
            )
        u2.franquia_id = fr2.id
        db.session.commit()
        with pytest.raises(VinculoOrganizacionalConflitoError, match="Contratante ativo"):
            criar_vinculo_organizacional(
                conta_id=conta.id,
                user_id=u2.id,
                franquia_id=fr2.id,
                papel="contratante",
            )


def test_integrity_error_desconhecido_nao_mascara():
    exc = _integrity_sqlite("UNIQUE constraint failed: conta.slug")
    with pytest.raises(IntegrityError):
        converter_ou_relancar_integrity_error(exc)
    assert identificar_constraint_integrity_error(exc) is None

    exc_pg = _integrity_pg("fk_outra_tabela_nao_relacionada")
    with pytest.raises(IntegrityError):
        converter_ou_relancar_integrity_error(exc_pg)
    assert identificar_constraint_integrity_error(exc_pg) is None


# --- Quantity ---


def test_quantity_valida_invalida_e_persistencia(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="qtd-ok")
        franquia.limite_total = Decimal("100")
        db.session.commit()
        persistir_quantidade_assentos_contratados(conta.id, 8)
        rec = db.session.get(Conta, conta.id)
        fr = db.session.get(Franquia, franquia.id)
        assert rec.quantidade_assentos_contratados == 8
        assert fr.limite_total == Decimal("100")
        with pytest.raises(ValueError):
            persistir_quantidade_assentos_contratados(conta.id, 0)
        with pytest.raises(ValueError):
            persistir_quantidade_assentos_contratados(conta.id, -1)
        with pytest.raises(ValueError):
            persistir_quantidade_assentos_contratados(conta.id, "3")  # type: ignore[arg-type]


def test_quantity_nao_altera_stripe(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="qtd-stripe")
        fake_stripe = MagicMock()
        with patch.dict("sys.modules", {"stripe": fake_stripe}):
            persistir_quantidade_assentos_contratados(conta.id, 3)
        fake_stripe.assert_not_called()
        assert not fake_stripe.method_calls


# --- Configuração ADM ---


def test_config_admin_preco_limite_minimo_persistidos(app):
    with app.app_context():
        _seed_cfg_planos_basico()
        resultado = plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="49.90",
            franquia_limite_total_raw="1000",
            quantidade_minima_raw="5",
        )
        assert resultado["valor_plano"] == Decimal("49.90")
        assert resultado["franquia_limite_total"] == Decimal("1000.000000")
        assert resultado["quantidade_minima"] == 5
        planos = plano_service.listar_planos_saas_admin()
        mu = next(p for p in planos if p["codigo"] == "multiuser")
        assert mu["valor_admin"] == Decimal("49.90")
        assert mu["franquia_referencia"] == Decimal("1000.000000")
        assert mu["quantidade_minima"] == 5
        assert isinstance(mu["gateway_config"], dict)
        assert mu["gateway_config"]["configuracao_valida"] is False

        plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="59.90",
            franquia_limite_total_raw="1500",
            quantidade_minima_raw="7",
        )
        mu2 = next(
            p for p in plano_service.listar_planos_saas_admin() if p["codigo"] == "multiuser"
        )
        assert mu2["valor_admin"] == Decimal("59.90")
        assert mu2["franquia_referencia"] == Decimal("1500.000000")
        assert mu2["quantidade_minima"] == 7
        assert plano_service.obter_quantidade_minima_multiuser_admin() == 7


def test_config_admin_sem_hardcode_runtime():
    source = inspect.getsource(plano_service)
    assert "49.90" not in source
    assert "49,90" not in source
    assert "return 5" not in source
    assert "quantidade_minima = 5" not in source
    assert "CHAVE_QUANTIDADE_MINIMA_MULTIUSER" in source
    assert plano_service.PLANOS_GATEWAY_MONETIZACAO == ("starter", "pro")


def test_multiuser_nao_exposto_na_contratacao_publica(app):
    with app.app_context():
        _seed_cfg_planos_basico()
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo="multiuser",
            valor_plano_raw="49.90",
            franquia_limite_total_raw="1000",
            quantidade_minima_raw="5",
        )
        from app.services.cleiton_monetizacao_service import listar_planos_contratacao_publica

        publicos = listar_planos_contratacao_publica()
        assert all(p["codigo"] != "multiuser" for p in publicos)
        assert {p["codigo"] for p in publicos} <= {"free", "starter", "pro"}


# --- Compatibilidade ---


def test_user_antigo_continua_carregando(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="user-legado")
        user = seed_usuario(fr.id, conta.id, email="legado@test.com", categoria="free")
        loaded = load_user_for_flask_login(user.id)
        assert loaded is not None
        assert loaded.id == user.id
        assert loaded.conta_id == conta.id
        assert loaded.franquia_id == fr.id


def test_conta_representa_dados_empresariais_completos(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="empresa-ok")
        persistir_dados_empresariais_conta(
            conta.id,
            razao_social="Logcompleta Agentes Inteligentes LTDA",
            nome_fantasia="AgenteFrete",
            cnpj=CNPJ_A_MASCARA,
            email_empresarial="contato@empresa.com",
            endereco_logradouro="Rua A",
            endereco_numero="100",
            endereco_complemento="Sala 1",
            endereco_bairro="Centro",
            endereco_cidade="São Paulo",
            endereco_uf="sp",
            endereco_cep="01310-100",
        )
        rec = db.session.get(Conta, conta.id)
        assert rec.razao_social == "Logcompleta Agentes Inteligentes LTDA"
        assert rec.nome_fantasia == "AgenteFrete"
        assert rec.cnpj == CNPJ_A
        assert rec.email_empresarial == "contato@empresa.com"
        assert rec.endereco_logradouro == "Rua A"
        assert rec.endereco_numero == "100"
        assert rec.endereco_complemento == "Sala 1"
        assert rec.endereco_bairro == "Centro"
        assert rec.endereco_cidade == "São Paulo"
        assert rec.endereco_uf == "SP"
        assert rec.endereco_cep == "01310100"


def test_conta_antiga_sem_dados_empresariais_valida(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="conta-antiga")
        rec = db.session.get(Conta, conta.id)
        snap = {
            rec.razao_social,
            rec.nome_fantasia,
            rec.cnpj,
            rec.email_empresarial,
            rec.endereco_logradouro,
        }
        assert snap == {None}
        assert rec.multiuser_ativa is False
        assert rec.quantidade_assentos_contratados is None


def test_starter_pro_free_nao_exigem_cnpj(app):
    with app.app_context():
        for slug, cat in (("c-free", "free"), ("c-st", "starter"), ("c-pro", "pro")):
            conta, fr = seed_conta_franquia_cliente(slug=slug)
            seed_usuario(fr.id, conta.id, email=f"{slug}@test.com", categoria=cat)
            rec = db.session.get(Conta, conta.id)
            assert rec.cnpj is None
            with pytest.raises(ValueError, match="marcada como Multiuser"):
                exigir_cnpj_para_ativacao_multiuser(rec)


# --- Backfill ---


def _executar_backfill():
    rel = aplicar_backfill_multiuser_legado(db.session.connection())
    db.session.commit()
    return rel


def test_backfill_primeira_franquia_um_user_cria_contratante(app):
    with app.app_context():
        seed_sistema_interno()
        conta, fr1 = seed_conta_franquia_cliente(slug="bf-um")
        user = seed_usuario(fr1.id, conta.id, email="um@test.com", categoria="multiuser")
        rel = _executar_backfill()
        vinculos = ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).all()
        assert rel.vinculos_criados == 1
        assert len(vinculos) == 1
        assert vinculos[0].user_id == user.id
        assert vinculos[0].papel == "contratante"
        assert vinculos[0].titular is True


def test_backfill_primeira_franquia_dois_users_nao_inventa_contratante(app):
    with app.app_context():
        seed_sistema_interno()
        conta, fr1 = seed_conta_franquia_cliente(slug="bf-dois")
        seed_usuario(fr1.id, conta.id, email="d1@test.com", categoria="multiuser")
        seed_usuario(fr1.id, conta.id, email="d2@test.com", categoria="multiuser")
        rel = _executar_backfill()
        vinculos = ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).all()
        assert vinculos == []
        assert rel.vinculos_criados == 0
        inconsistencias = ContaOrganizacionalBackfillInconsistencia.query.filter_by(
            conta_id=conta.id
        ).all()
        assert any(i.codigo == "multiplos_users_mesma_franquia" for i in inconsistencias)
        assert any(i.codigo == "conta_sem_contratante_deterministico" for i in inconsistencias)


def test_backfill_primeira_franquia_tres_users_nao_inventa_contratante(app):
    with app.app_context():
        seed_sistema_interno()
        conta, fr1 = seed_conta_franquia_cliente(slug="bf-tres")
        seed_usuario(fr1.id, conta.id, email="t1@test.com", categoria="multiuser")
        seed_usuario(fr1.id, conta.id, email="t2@test.com", categoria="multiuser")
        seed_usuario(fr1.id, conta.id, email="t3@test.com", categoria="multiuser")
        rel = _executar_backfill()
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).count() == 0
        assert rel.vinculos_criados == 0
        assert ContaOrganizacionalBackfillInconsistencia.query.filter_by(
            conta_id=conta.id,
            codigo="multiplos_users_mesma_franquia",
        ).count() >= 1


def test_backfill_master_primeira_franquia_e_ambiguidades(app):
    with app.app_context():
        seed_sistema_interno()
        conta, fr1 = seed_conta_franquia_cliente(slug="bf-mu")
        fr2 = Franquia(
            conta_id=conta.id,
            nome="F2",
            slug="f2",
            status=Franquia.STATUS_ACTIVE,
        )
        db.session.add(fr2)
        db.session.flush()
        master = seed_usuario(fr1.id, conta.id, email="master@test.com", categoria="multiuser")
        membro = seed_usuario(fr2.id, conta.id, email="membro@test.com", categoria="multiuser")
        extra = seed_usuario(fr1.id, conta.id, email="extra@test.com", categoria="multiuser")
        extra.franquia_id = fr1.id
        db.session.commit()

        rel = _executar_backfill()

        rec = db.session.get(Conta, conta.id)
        assert rec.multiuser_ativa is True
        assert rec.quantidade_assentos_contratados == 2
        vinculos = ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).all()
        papéis = {(v.user_id, v.papel, v.franquia_id) for v in vinculos}
        assert (master.id, "contratante", fr1.id) not in papéis
        assert (membro.id, "membro", fr2.id) in papéis
        assert all(v.papel != "contratante" for v in vinculos)
        assert all(v.user_id != extra.id for v in vinculos)
        inconsistencias = ContaOrganizacionalBackfillInconsistencia.query.filter_by(
            conta_id=conta.id
        ).all()
        assert any(i.codigo == "multiplos_users_mesma_franquia" for i in inconsistencias)
        assert rel.vinculos_criados == 1
        assert master.conta_id == conta.id
        assert master.franquia_id == fr1.id


def test_backfill_reexecucao_classifica_novo_e_nao_inventa_ambiguo(app):
    with app.app_context():
        seed_sistema_interno()
        conta_ok, fr_ok = seed_conta_franquia_cliente(slug="bf-re-ok")
        seed_usuario(fr_ok.id, conta_ok.id, email="re.ok@test.com", categoria="multiuser")
        conta_amb, fr_amb = seed_conta_franquia_cliente(slug="bf-re-amb")
        seed_usuario(fr_amb.id, conta_amb.id, email="re.a1@test.com", categoria="multiuser")
        seed_usuario(fr_amb.id, conta_amb.id, email="re.a2@test.com", categoria="multiuser")
        rel1 = _executar_backfill()
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta_ok.id, papel="contratante"
            ).count()
            == 1
        )
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta_amb.id).count() == 0
        inc1 = ContaOrganizacionalBackfillInconsistencia.query.filter_by(
            conta_id=conta_amb.id
        ).count()
        assert inc1 >= 1

        rel2 = _executar_backfill()
        assert rel2.vinculos_criados == 0
        assert (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta_ok.id, papel="contratante"
            ).count()
            == 1
        )
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta_amb.id).count() == 0
        assert (
            ContaOrganizacionalBackfillInconsistencia.query.filter_by(
                conta_id=conta_amb.id
            ).count()
            == inc1
        )

        conta_nova, fr_nova = seed_conta_franquia_cliente(slug="bf-re-nova")
        novo = seed_usuario(
            fr_nova.id, conta_nova.id, email="re.novo@test.com", categoria="multiuser"
        )
        rel3 = _executar_backfill()
        assert rel3.vinculos_criados == 1
        criado = ContaVinculoOrganizacional.query.filter_by(conta_id=conta_nova.id).one()
        assert criado.user_id == novo.id
        assert criado.papel == "contratante"
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta_amb.id).count() == 0
        assert rel1.vinculos_criados >= 1


# --- Migration ---


def test_migration_a2b3c4d5e6f7_na_chain():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    rev = script.get_revision("a2b3c4d5e6f7")
    assert rev is not None
    assert rev.down_revision == "z0a1b2c3d4e5"
    source = (
        ROOT / "migrations" / "versions" / "a2b3c4d5e6f7_fase1_multiuser_fundacao.py"
    ).read_text(encoding="utf-8")
    assert "User.conta_id" in source or "user.conta_id" in source.lower() or "Não altera User.conta_id" in source
    assert "stripe" not in source.lower() or "Não chama Stripe" in source
    assert "uq_conta_cnpj_multiuser_ativa" in source
    assert "conta_vinculo_organizacional" in source
    assert "plano_quantidade_minima_admin_multiuser" in source
    assert "PRECONDIÇÃO BLOQUEANTE DE DEPLOY" in source
    assert "uq_conta_vinculo_org_contratante_ativo" in source
    assert "ck_conta_vinculo_org_papel_titular" in source
    deploy = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "PRECONDIÇÃO BLOQUEANTE DE DEPLOY — Plano Multiusuário Fase 1" in deploy
    assert "não pode ser promovida isoladamente" in deploy


def test_models_fase1_possuem_campos_e_vinculo():
    conta_cols = {c.name for c in Conta.__table__.columns}
    for required in (
        "razao_social",
        "nome_fantasia",
        "cnpj",
        "email_empresarial",
        "endereco_logradouro",
        "endereco_numero",
        "endereco_complemento",
        "endereco_bairro",
        "endereco_cidade",
        "endereco_uf",
        "endereco_cep",
        "quantidade_assentos_contratados",
        "multiuser_ativa",
    ):
        assert required in conta_cols
    user_cols = {c.name for c in User.__table__.columns}
    assert "conta_id" in user_cols
    assert "franquia_id" in user_cols
    vinculo_cols = {c.name for c in ContaVinculoOrganizacional.__table__.columns}
    for required in (
        "conta_id",
        "user_id",
        "franquia_id",
        "papel",
        "estado",
        "titular",
        "origem",
        "criado_por_user_id",
        "iniciado_em",
        "encerrado_em",
    ):
        assert required in vinculo_cols
    assert ContaVinculoOrganizacional.PAPEL_CONTRATANTE == "contratante"
    assert ContaVinculoOrganizacional.PAPEL_MEMBRO == "membro"


def test_migration_upgrade_downgrade_sqlite(tmp_path):
    import importlib.util

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from sqlalchemy import create_engine, inspect, text

    db_path = tmp_path / "fase1_mu_fundacao.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE conta (
                    id INTEGER PRIMARY KEY,
                    nome VARCHAR(255) NOT NULL,
                    slug VARCHAR(80) NOT NULL,
                    status VARCHAR(30) NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE franquia (
                    id INTEGER PRIMARY KEY,
                    conta_id INTEGER NOT NULL,
                    nome VARCHAR(255) NOT NULL,
                    slug VARCHAR(80) NOT NULL,
                    status VARCHAR(30) NOT NULL,
                    created_at DATETIME NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE "user" (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(150) NOT NULL,
                    full_name VARCHAR(150) NOT NULL,
                    categoria VARCHAR(50),
                    conta_id INTEGER NOT NULL,
                    franquia_id INTEGER NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                CREATE TABLE config_regras (
                    id INTEGER PRIMARY KEY,
                    chave VARCHAR(80) NOT NULL,
                    valor_texto VARCHAR(500),
                    valor_inteiro INTEGER,
                    valor_real FLOAT,
                    descricao VARCHAR(255),
                    updated_at DATETIME
                )
                """
            )
        )
        conn.execute(
            text(
                "INSERT INTO conta (id, nome, slug, status, created_at) "
                "VALUES (1, 'Cliente', 'bf-sql', 'ativa', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                "INSERT INTO franquia (id, conta_id, nome, slug, status, created_at) "
                "VALUES (10, 1, 'Principal', 'principal', 'active', CURRENT_TIMESTAMP)"
            )
        )
        conn.execute(
            text(
                'INSERT INTO "user" (id, email, full_name, categoria, conta_id, franquia_id) '
                "VALUES (100, 'm@test.com', 'Master', 'multiuser', 1, 10)"
            )
        )

    mig_path = (
        ROOT / "migrations" / "versions" / "a2b3c4d5e6f7_fase1_multiuser_fundacao.py"
    )
    spec = importlib.util.spec_from_file_location("fase1_mu_mig", mig_path)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)

    def _run(fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"render_as_batch": True}
            )
            ops = Operations(context)
            original_op = mig.op
            try:
                mig.op = ops
                with conn.begin():
                    fn()
            finally:
                mig.op = original_op

    _run(mig.upgrade)
    insp = inspect(engine)
    conta_cols = {c["name"] for c in insp.get_columns("conta")}
    assert "cnpj" in conta_cols
    assert "quantidade_assentos_contratados" in conta_cols
    assert "multiuser_ativa" in conta_cols
    assert "conta_vinculo_organizacional" in insp.get_table_names()
    with engine.begin() as conn:
        qtd = conn.execute(
            text("SELECT valor_inteiro FROM config_regras WHERE chave = :c"),
            {"c": "plano_quantidade_minima_admin_multiuser"},
        ).scalar()
        assert qtd == 5
        ativa = conn.execute(text("SELECT multiuser_ativa FROM conta WHERE id = 1")).scalar()
        assert ativa in (1, True)
        papel = conn.execute(
            text("SELECT papel FROM conta_vinculo_organizacional WHERE user_id = 100")
        ).scalar()
        assert papel == "contratante"

    _run(mig.downgrade)
    insp = inspect(engine)
    conta_cols = {c["name"] for c in insp.get_columns("conta")}
    assert "cnpj" not in conta_cols
    assert "conta_vinculo_organizacional" not in insp.get_table_names()

    _run(mig.upgrade)
    insp = inspect(engine)
    conta_cols = {c["name"] for c in insp.get_columns("conta")}
    assert "cnpj" in conta_cols
    assert "quantidade_assentos_contratados" in conta_cols
    assert "multiuser_ativa" in conta_cols
    assert "conta_vinculo_organizacional" in insp.get_table_names()
    idx_names = {ix["name"] for ix in insp.get_indexes("conta_vinculo_organizacional")}
    assert "uq_conta_vinculo_org_contratante_ativo" in idx_names
    with engine.begin() as conn:
        papel = conn.execute(
            text("SELECT papel FROM conta_vinculo_organizacional WHERE user_id = 100")
        ).scalar()
        assert papel == "contratante"
        titular = conn.execute(
            text("SELECT titular FROM conta_vinculo_organizacional WHERE user_id = 100")
        ).scalar()
        assert titular in (1, True)


def test_servicos_fase1_nao_importam_stripe():
    import app.services.conta_organizacional_service as org
    import app.services.conta_organizacional_backfill_service as bf
    import app.services.cnpj_service as cnpj

    for mod in (org, bf, cnpj):
        src = inspect.getsource(mod)
        assert "import stripe" not in src
        assert "stripe." not in src
