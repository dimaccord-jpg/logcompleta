"""Testes incrementais da Fase 2 — integridade, capacidade e ciclo Multiuser."""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.extensions import db
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaOrganizacionalBackfillInconsistencia,
    ContaVinculoOrganizacional,
    Franquia,
    MultiuserFranquiaCodigo,
    User,
)
from app.services import plano_service
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
)
from app.services.cleiton_plano_resolver import CODIGO_MULTIUSER, PlanoResolvidoCleiton
from app.services.conta_multiuser_capacidade_service import (
    ORDEM_LOCK,
    aplicar_atribuicao_plano_multiuser_admin,
    bloquear_conta_para_capacidade,
    listar_divergencias_conta,
    materializar_franquias_faltantes,
    ocupar_assento,
    snapshot_capacidade,
)
from app.services.conta_multiuser_ciclo_service import (
    alinhar_franquia_ao_ciclo_conta,
    alinhar_franquias_sem_ciclo_ao_canonico,
    resolver_ciclo_canonico_conta,
)
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    DivergenciaImpeditivaError,
    VinculoInconsistenteError,
)
from app.services.conta_multiuser_reconciliacao_service import (
    executar_reconciliacao_multiuser_antes_enforcement,
)
from app.services.conta_organizacional_backfill_service import (
    aplicar_backfill_multiuser_legado,
)
from app.services.conta_organizacional_rules import (
    CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY,
    CODIGO_DIV_CICLO_DIVERGENTE,
    CODIGO_DIV_FRANQUIAS_MAIOR_QUE_QUANTITY,
    CODIGO_DIV_FRANQUIAS_MENOR_QUE_CAPACITY,
    CODIGO_DIV_QUANTITY_EXTERNA_LOCAL,
    FONTE_CICLO_DIVERGENTE,
    FONTE_CICLO_FRANQUIAS_UNANIMES,
    FONTE_CICLO_INCONCLUSIVO,
    FONTE_CICLO_VINCULO_MONETIZACAO,
    ORDEM_LOCK_CAPACIDADE,
)
from app.services.conta_organizacional_service import (
    criar_vinculo_organizacional,
    persistir_quantidade_assentos_contratados,
)
from app.services.user_plan_control_service import atribuir_plano_para_usuario
from tests.conftest import (
    preencher_dados_empresariais_minimos_teste,
    seed_conta_franquia_cliente as _seed_conta_franquia_cliente_raw,
    seed_sistema_interno,
    seed_usuario,
)

ROOT = Path(__file__).resolve().parents[1]
INICIO = datetime(2026, 4, 1, 12, 0, 0)
FIM = datetime(2026, 5, 1, 12, 0, 0)


def seed_conta_franquia_cliente(slug="conta-cli"):
    conta, fr = _seed_conta_franquia_cliente_raw(slug)
    preencher_dados_empresariais_minimos_teste(conta, slug)
    return conta, fr


def _preparar_planos_admin(*, limite="200"):
    seed_sistema_interno()
    plano_service.atualizar_parametros_plano_admin(
        plano_codigo="multiuser",
        valor_plano_raw="10.00",
        franquia_limite_total_raw=limite,
        quantidade_minima_raw="5",
    )
    for codigo in ("free", "starter", "pro", "avulso"):
        plano_service.atualizar_parametros_plano_admin(
            plano_codigo=codigo,
            valor_plano_raw="10.00",
            franquia_limite_total_raw=limite,
        )


def _extra_franquia(conta: Conta, slug: str, *, consumo=None, inicio=None, fim=None) -> Franquia:
    fr = Franquia(
        conta_id=conta.id,
        nome=slug,
        slug=slug,
        status=Franquia.STATUS_ACTIVE,
        consumo_acumulado=Decimal(consumo) if consumo is not None else Decimal("0"),
        inicio_ciclo=inicio,
        fim_ciclo=fim,
    )
    db.session.add(fr)
    db.session.commit()
    return fr


def _plano_mu() -> PlanoResolvidoCleiton:
    return PlanoResolvidoCleiton(
        codigo=CODIGO_MULTIUSER,
        fonte="teste",
        usuario_referencia_id=None,
        categoria_raw="multiuser",
        pendencias=(),
    )


# --- Reconciliação F1 → F2 ---


def test_reconciliacao_executa_backfill_antes_enforcement(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-rec-1")
        user = seed_usuario(fr.id, conta.id, email="f2.rec1@test.com", categoria="multiuser")
        rec = executar_reconciliacao_multiuser_antes_enforcement()
        db.session.refresh(conta)
        assert rec.backfill.contas_marcadas >= 1
        assert rec.contas_analisadas >= 1
        assert conta.multiuser_ativa is True
        assert conta.quantidade_assentos_contratados == 1
        vinculo = ContaVinculoOrganizacional.query.filter_by(user_id=user.id, estado="ativo").one()
        assert vinculo.papel == "contratante"
        assert vinculo.franquia_id == fr.id


def test_novo_legado_apos_primeiro_backfill_e_capturado(app):
    with app.app_context():
        conta1, fr1 = seed_conta_franquia_cliente(slug="f2-rec-a")
        seed_usuario(fr1.id, conta1.id, email="f2.rec.a@test.com", categoria="multiuser")
        rel1 = executar_reconciliacao_multiuser_antes_enforcement()
        assert rel1.backfill.vinculos_criados == 1

        conta2, fr2 = seed_conta_franquia_cliente(slug="f2-rec-b")
        novo = seed_usuario(fr2.id, conta2.id, email="f2.rec.b@test.com", categoria="multiuser")
        rel2 = executar_reconciliacao_multiuser_antes_enforcement()
        assert rel2.backfill.vinculos_criados == 1
        assert ContaVinculoOrganizacional.query.filter_by(user_id=novo.id, estado="ativo").one()
        db.session.refresh(conta2)
        assert conta2.multiuser_ativa is True


def test_ambiguidade_permanece_sem_vinculo_na_reconciliacao(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-amb")
        seed_usuario(fr.id, conta.id, email="f2.amb1@test.com", categoria="multiuser")
        seed_usuario(fr.id, conta.id, email="f2.amb2@test.com", categoria="multiuser")
        rel = executar_reconciliacao_multiuser_antes_enforcement()
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id).count() == 0
        assert rel.contas_ambiguas >= 1
        assert (
            ContaOrganizacionalBackfillInconsistencia.query.filter_by(conta_id=conta.id).count()
            >= 1
        )


def test_reconciliacao_nao_duplica_inconsistencia(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-inc")
        seed_usuario(fr.id, conta.id, email="f2.inc1@test.com", categoria="multiuser")
        seed_usuario(fr.id, conta.id, email="f2.inc2@test.com", categoria="multiuser")
        rel1 = executar_reconciliacao_multiuser_antes_enforcement()
        n1 = ContaOrganizacionalBackfillInconsistencia.query.filter_by(conta_id=conta.id).count()
        rel2 = executar_reconciliacao_multiuser_antes_enforcement()
        n2 = ContaOrganizacionalBackfillInconsistencia.query.filter_by(conta_id=conta.id).count()
        assert n1 == n2
        assert rel2.backfill.inconsistencias == 0
        assert rel1.backfill.inconsistencias >= 1


def test_reconciliacao_nao_sobrescreve_quantity_governada(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-qtd")
        seed_usuario(fr.id, conta.id, email="f2.qtd@test.com", categoria="multiuser")
        _extra_franquia(conta, "extra-qtd")
        persistir_quantidade_assentos_contratados(conta.id, 7)
        executar_reconciliacao_multiuser_antes_enforcement()
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 7


# --- Writes administrativos ---


def test_atribuir_multiuser_produz_governanca(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-adm")
        user = seed_usuario(fr.id, conta.id, email="f2.adm@test.com", categoria="free")
        consumo_antes = Decimal(fr.consumo_acumulado or 0)
        resultado = atribuir_plano_para_usuario(
            email=user.email,
            plano_raw="multiuser",
            quantidade_franquias_raw="5",
        )
        db.session.refresh(conta)
        db.session.refresh(user)
        db.session.refresh(fr)
        assert resultado.plano_novo == "multiuser"
        assert conta.multiuser_ativa is True
        assert conta.quantidade_assentos_contratados == 5
        assert user.categoria == "multiuser"
        assert user.conta_id == conta.id
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 5
        vinculo = ContaVinculoOrganizacional.query.filter_by(
            user_id=user.id, estado="ativo"
        ).one()
        assert vinculo.papel == "contratante"
        assert vinculo.conta_id == user.conta_id == fr.conta_id
        assert vinculo.franquia_id == user.franquia_id
        assert resultado.franquias_multiuser_criadas == 4
        assert len(resultado.codigos_gerados) == 4
        assert fr.consumo_acumulado == consumo_antes
        assert conta.cnpj is not None


def test_atribuir_multiuser_idempotente_na_repeticao(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-idemp")
        user = seed_usuario(fr.id, conta.id, email="f2.idemp@test.com", categoria="free")
        r1 = atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        r2 = atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 5
        assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id, estado="ativo").count() == 1
        assert ContaVinculoOrganizacional.query.filter_by(
            conta_id=conta.id, papel="contratante", estado="ativo"
        ).count() == 1
        assert r2.franquias_multiuser_criadas == 0
        assert MultiuserFranquiaCodigo.query.filter_by(conta_id=conta.id).count() == 4
        db.session.refresh(conta)
        assert conta.quantidade_assentos_contratados == 5
        assert r1.conta_id == r2.conta_id


def test_atribuir_multiuser_legado_sem_cnpj_nao_bloqueia(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = _seed_conta_franquia_cliente_raw(slug="f2-cnpj")
        user = seed_usuario(fr.id, conta.id, email="f2.cnpj@test.com", categoria="free")
        conta.multiuser_ativa = True
        db.session.add(conta)
        db.session.commit()
        atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="3"
        )
        db.session.refresh(conta)
        assert conta.cnpj is None
        assert conta.multiuser_ativa is True


# --- Capacidade ---


def test_quantity_5_ativos_4_permite_novo_vinculo(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-cap-ok")
        u1 = seed_usuario(fr1.id, conta.id, email="f2.cap.m@test.com", categoria="multiuser")
        aplicar_atribuicao_plano_multiuser_admin(
            user=u1,
            quantidade_assentos=5,
            limite_referencia=Decimal("200"),
            admin_user_id=None,
        )
        livres = []
        ocupada = {v.franquia_id for v in ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id, estado="ativo")}
        for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc()):
            if fr.id not in ocupada:
                livres.append(fr)
        for i, fr in enumerate(livres[:3]):
            u = seed_usuario(fr.id, conta.id, email=f"f2.cap.m{i}@test.com", categoria="multiuser")
            ocupar_assento(conta_id=conta.id, user_id=u.id, franquia_id=fr.id, papel="membro")
        snap = snapshot_capacidade(conta.id)
        assert snap.quantidade_contratada == 5
        assert snap.vinculos_ativos == 4
        assert snap.capacidade_livre == 1
        alvo = [f for f in Franquia.query.filter_by(conta_id=conta.id) if f.id not in {
            v.franquia_id for v in ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id, estado="ativo")
        }][0]
        u5 = seed_usuario(alvo.id, conta.id, email="f2.cap.last@test.com", categoria="multiuser")
        ocupar_assento(conta_id=conta.id, user_id=u5.id, franquia_id=alvo.id, papel="membro")
        assert snapshot_capacidade(conta.id).vinculos_ativos == 5


def test_quantity_5_ativos_5_bloqueia_novo_vinculo(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-cap-full")
        master = seed_usuario(fr1.id, conta.id, email="f2.full.m@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=master.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        livres = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if fr.id != master.franquia_id
        ]
        for i, fr in enumerate(livres):
            u = seed_usuario(fr.id, conta.id, email=f"f2.full.u{i}@test.com", categoria="multiuser")
            ocupar_assento(conta_id=conta.id, user_id=u.id, franquia_id=fr.id, papel="membro")
        extra_fr = livres[0]
        extra = seed_usuario(extra_fr.id, conta.id, email="f2.full.x@test.com", categoria="multiuser")
        extra.conta_id = conta.id
        db.session.commit()
        with pytest.raises(CapacidadeEsgotadaError):
            ocupar_assento(conta_id=conta.id, user_id=extra.id, papel="membro")
        assert snapshot_capacidade(conta.id).vinculos_ativos == 5


def test_quantity_7_ativos_4_capacidade_livre_3(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-livres")
        master = seed_usuario(fr1.id, conta.id, email="f2.liv.m@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=master.email, plano_raw="multiuser", quantidade_franquias_raw="7"
        )
        livres = [
            fr
            for fr in Franquia.query.filter_by(conta_id=conta.id).order_by(Franquia.id.asc())
            if fr.id != master.franquia_id
        ]
        for i in range(3):
            u = seed_usuario(livres[i].id, conta.id, email=f"f2.liv.u{i}@test.com", categoria="multiuser")
            ocupar_assento(conta_id=conta.id, user_id=u.id, franquia_id=livres[i].id, papel="membro")
        snap = snapshot_capacidade(conta.id)
        assert snap.quantidade_contratada == 7
        assert snap.vinculos_ativos == 4
        assert snap.capacidade_livre == 3
        assert snap.franquias_disponiveis == 3


def test_ativos_maior_que_quantity_detectado_como_divergencia(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-div-at")
        fr2 = _extra_franquia(conta, "f2-div-at-2")
        u1 = seed_usuario(fr1.id, conta.id, email="f2.div.a@test.com", categoria="multiuser")
        u2 = seed_usuario(fr2.id, conta.id, email="f2.div.b@test.com", categoria="multiuser")
        criar_vinculo_organizacional(
            conta_id=conta.id, user_id=u1.id, franquia_id=fr1.id, papel="contratante"
        )
        criar_vinculo_organizacional(
            conta_id=conta.id, user_id=u2.id, franquia_id=fr2.id, papel="membro"
        )
        # Estado legado inconsistente: o write governado recusa quantity <
        # comprometido; o detector permanece responsável por evidenciar
        # divergência já persistida fora desse helper.
        conta.quantidade_assentos_contratados = 1
        db.session.add(conta)
        db.session.commit()
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY for d in divs)


def test_capacidade_ociosa_nao_cria_user(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-ocio")
        user = seed_usuario(fr.id, conta.id, email="f2.ocio@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        assert User.query.filter_by(conta_id=conta.id).count() == 1
        snap = snapshot_capacidade(conta.id)
        assert snap.vinculos_ativos == 1
        assert snap.capacidade_livre == 4


# --- Concorrência / invariantes ---


def test_ultimo_assento_segunda_operacao_bloqueada(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-race-seat")
        master = seed_usuario(fr.id, conta.id, email="f2.race.m@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=master.email, plano_raw="multiuser", quantidade_franquias_raw="2"
        )
        livre = [
            x
            for x in Franquia.query.filter_by(conta_id=conta.id)
            if x.id != master.franquia_id
        ][0]
        a = seed_usuario(livre.id, conta.id, email="f2.race.a@test.com", categoria="multiuser")
        b = seed_usuario(livre.id, conta.id, email="f2.race.b@test.com", categoria="multiuser")
        ocupar_assento(conta_id=conta.id, user_id=a.id, franquia_id=livre.id, papel="membro")
        with pytest.raises((CapacidadeEsgotadaError, VinculoInconsistenteError)):
            ocupar_assento(conta_id=conta.id, user_id=b.id, franquia_id=livre.id, papel="membro")
        assert snapshot_capacidade(conta.id).vinculos_ativos == 2


def test_dois_users_mesma_franquia_rejeitado(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-same-fr")
        u1 = seed_usuario(fr.id, conta.id, email="f2.sf1@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=u1.email, plano_raw="multiuser", quantidade_franquias_raw="3"
        )
        fr_livre = [
            x for x in Franquia.query.filter_by(conta_id=conta.id) if x.id != u1.franquia_id
        ][0]
        u2 = seed_usuario(fr_livre.id, conta.id, email="f2.sf2@test.com", categoria="multiuser")
        ocupar_assento(conta_id=conta.id, user_id=u2.id, franquia_id=fr_livre.id, papel="membro")
        u3 = seed_usuario(fr_livre.id, conta.id, email="f2.sf3@test.com", categoria="multiuser")
        with pytest.raises(VinculoInconsistenteError):
            ocupar_assento(conta_id=conta.id, user_id=u3.id, franquia_id=fr_livre.id, papel="membro")


def test_mesmo_user_duas_franquias_rejeitado(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-same-u")
        u1 = seed_usuario(fr.id, conta.id, email="f2.su@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=u1.email, plano_raw="multiuser", quantidade_franquias_raw="3"
        )
        outra = [
            x for x in Franquia.query.filter_by(conta_id=conta.id) if x.id != u1.franquia_id
        ][0]
        with pytest.raises(VinculoInconsistenteError):
            ocupar_assento(conta_id=conta.id, user_id=u1.id, franquia_id=outra.id, papel="membro")


def test_franquia_de_conta_diferente_rejeitada(app):
    with app.app_context():
        _preparar_planos_admin()
        a, fa = seed_conta_franquia_cliente(slug="f2-cross-a")
        b, fb = seed_conta_franquia_cliente(slug="f2-cross-b")
        ua = seed_usuario(fa.id, a.id, email="f2.cross.a@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=ua.email, plano_raw="multiuser", quantidade_franquias_raw="3"
        )
        with pytest.raises(VinculoInconsistenteError):
            ocupar_assento(conta_id=a.id, user_id=ua.id, franquia_id=fb.id)


def test_lock_raiz_e_ordem_documentada():
    from app.services import conta_multiuser_capacidade_service as cap

    src_lock = inspect.getsource(cap._query_conta_lock)
    src_user = inspect.getsource(cap._query_user_lock)
    src_fr = inspect.getsource(cap._query_franquia_lock)
    assert "with_for_update" in src_lock
    assert "with_for_update" in src_user
    assert "with_for_update" in src_fr
    src_cap = inspect.getsource(ocupar_assento)
    assert "bloquear_conta_para_capacidade" in src_cap
    assert src_cap.find("bloquear_conta_para_capacidade") < src_cap.find("_bloquear_user")
    assert src_cap.find("_bloquear_user") < src_cap.find("_bloquear_franquia")
    assert ORDEM_LOCK == ORDEM_LOCK_CAPACIDADE == ("conta", "vinculo_user", "franquia")


def test_sqlite_nao_reproduz_for_update_postgres():
    """Limitação documentada: SQLite ignora FOR UPDATE; a proteção testada é revalidação + unique."""
    from app.services import conta_multiuser_capacidade_service as cap

    src = inspect.getsource(cap._query_conta_lock) + inspect.getsource(bloquear_conta_para_capacidade)
    assert "with_for_update" in src
    assert "FOR UPDATE" in src


# --- Franquias ---


def test_capacity_materializa_franquia_faltante(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="f2-mat")
        conta.multiuser_ativa = True
        conta.quantidade_assentos_contratados = 3
        db.session.commit()
        criadas = materializar_franquias_faltantes(
            conta, 3, limite_referencia=Decimal("50"), aplicar_limite_nas_existentes=False
        )
        db.session.commit()
        assert len(criadas) == 2
        assert Franquia.query.filter_by(conta_id=conta.id).count() == 3
        assert all(fr.slug.startswith("multiuser-") for fr in criadas)


def test_quantity_menor_nao_deleta_franquias_extras(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-extra")
        user = seed_usuario(fr.id, conta.id, email="f2.extra@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        _extra_franquia(conta, "f2-extra-manual")
        _extra_franquia(conta, "f2-extra-manual-2")
        n_antes = Franquia.query.filter_by(conta_id=conta.id).count()
        atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
        )
        assert Franquia.query.filter_by(conta_id=conta.id).count() == n_antes
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_FRANQUIAS_MAIOR_QUE_QUANTITY for d in divs)


def test_consumo_existente_preservado_ao_materializar(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-cons")
        fr.consumo_acumulado = Decimal("12.5")
        fr.limite_total = Decimal("80")
        conta.multiuser_ativa = True
        conta.quantidade_assentos_contratados = 3
        db.session.commit()
        materializar_franquias_faltantes(
            conta, 3, limite_referencia=Decimal("50"), aplicar_limite_nas_existentes=False
        )
        db.session.commit()
        db.session.refresh(fr)
        assert fr.consumo_acumulado == Decimal("12.5")
        assert fr.limite_total == Decimal("80")


def test_franquias_abaixo_da_quantity_detectadas(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="f2-falt")
        persistir_quantidade_assentos_contratados(conta.id, 4)
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_FRANQUIAS_MENOR_QUE_CAPACITY for d in divs)


# --- Ciclo ---


def test_ciclo_determinavel_pelo_vinculo_monetario(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-v")
        db.session.add(
            ContaMonetizacaoVinculo(
                conta_id=conta.id,
                provider="stripe",
                ativo=True,
                vigencia_externa_inicio=INICIO,
                vigencia_externa_fim=FIM,
                snapshot_normalizado_json="{}",
            )
        )
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is True
        assert ciclo.fonte == FONTE_CICLO_VINCULO_MONETIZACAO
        assert ciclo.inicio == INICIO
        assert ciclo.fim == FIM
        fr2 = _extra_franquia(conta, "f2-ciclo-new")
        alinhar_franquia_ao_ciclo_conta(fr2, ciclo)
        db.session.commit()
        db.session.refresh(fr2)
        assert fr2.inicio_ciclo == INICIO
        assert fr2.fim_ciclo == FIM
        db.session.refresh(fr)
        assert fr.inicio_ciclo is None


def test_nova_franquia_herda_ciclo_corrente(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-new")
        fr.inicio_ciclo = INICIO
        fr.fim_ciclo = FIM
        fr.consumo_acumulado = Decimal("9")
        conta.multiuser_ativa = True
        conta.quantidade_assentos_contratados = 2
        db.session.commit()
        criadas = materializar_franquias_faltantes(
            conta, 2, limite_referencia=Decimal("40"), aplicar_limite_nas_existentes=False
        )
        db.session.commit()
        assert len(criadas) == 1
        assert criadas[0].inicio_ciclo == INICIO
        assert criadas[0].fim_ciclo == FIM
        db.session.refresh(fr)
        assert fr.consumo_acumulado == Decimal("9")
        assert fr.inicio_ciclo == INICIO


def test_franquias_com_periodos_divergentes_nao_escolhe(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-ciclo-div")
        fr1.inicio_ciclo = INICIO
        fr1.fim_ciclo = FIM
        _extra_franquia(
            conta,
            "f2-ciclo-div-2",
            inicio=INICIO + timedelta(days=3),
            fim=FIM + timedelta(days=3),
        )
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.inicio is None
        assert ciclo.fim is None
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_CICLO_DIVERGENTE for d in divs)


def test_ausencia_de_ciclo_seguro_nao_inventa_data(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="f2-ciclo-none")
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.inconclusivo is True
        assert ciclo.fonte == FONTE_CICLO_INCONCLUSIVO
        assert ciclo.inicio is None
        assert ciclo.fim is None
        criadas = materializar_franquias_faltantes(
            db.session.get(Conta, conta.id),
            2,
            limite_referencia=None,
            aplicar_limite_nas_existentes=False,
        )
        db.session.commit()
        assert criadas[0].inicio_ciclo is None
        assert criadas[0].fim_ciclo is None


def test_ciclo_unanime_nas_franquias(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-ciclo-un")
        fr1.inicio_ciclo = INICIO
        fr1.fim_ciclo = FIM
        _extra_franquia(conta, "f2-ciclo-un-2", inicio=INICIO, fim=FIM)
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.fonte == FONTE_CICLO_FRANQUIAS_UNANIMES
        assert ciclo.determinavel is True


def _vinculo_monetario(conta: Conta, *, inicio, fim) -> ContaMonetizacaoVinculo:
    row = ContaMonetizacaoVinculo(
        conta_id=conta.id,
        provider="stripe",
        ativo=True,
        vigencia_externa_inicio=inicio,
        vigencia_externa_fim=fim,
        snapshot_normalizado_json="{}",
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_vinculo_monetario_zero_nao_e_ciclo_determinavel(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-mz")
        _vinculo_monetario(conta, inicio=INICIO, fim=INICIO)
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.fonte == FONTE_CICLO_DIVERGENTE
        assert ciclo.inicio is None
        assert ciclo.fim is None
        assert not alinhar_franquia_ao_ciclo_conta(fr, ciclo)
        db.session.refresh(fr)
        assert fr.inicio_ciclo is None
        assert fr.fim_ciclo is None
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_CICLO_DIVERGENTE for d in divs)


def test_vinculo_monetario_negativo_nao_e_ciclo_determinavel(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-mn")
        _vinculo_monetario(conta, inicio=FIM, fim=INICIO)
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.inicio is None
        assert ciclo.fim is None
        assert not alinhar_franquia_ao_ciclo_conta(fr, ciclo)
        criadas = materializar_franquias_faltantes(
            db.session.get(Conta, conta.id),
            2,
            limite_referencia=None,
            aplicar_limite_nas_existentes=False,
        )
        db.session.commit()
        assert criadas[0].inicio_ciclo is None
        assert criadas[0].fim_ciclo is None


def test_vinculo_monetario_invalido_nao_cai_em_franquias_validas(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-mfb")
        fr.inicio_ciclo = INICIO
        fr.fim_ciclo = FIM
        _extra_franquia(conta, "f2-ciclo-mfb-2", inicio=INICIO, fim=FIM)
        _vinculo_monetario(conta, inicio=INICIO, fim=INICIO)
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.fonte == FONTE_CICLO_DIVERGENTE


def test_franquias_unanimes_zero_nao_sao_determinaveis(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-ciclo-uz")
        fr1.inicio_ciclo = INICIO
        fr1.fim_ciclo = INICIO
        _extra_franquia(conta, "f2-ciclo-uz-2", inicio=INICIO, fim=INICIO)
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.inicio is None
        assert ciclo.fim is None
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_CICLO_DIVERGENTE for d in divs)


def test_franquias_unanimes_negativo_nao_sao_determinaveis(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-ciclo-uneg")
        fr1.inicio_ciclo = FIM
        fr1.fim_ciclo = INICIO
        _extra_franquia(conta, "f2-ciclo-uneg-2", inicio=FIM, fim=INICIO)
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True


def test_franquia_valida_mais_invalida_nao_e_unanimidade(app):
    with app.app_context():
        conta, fr1 = seed_conta_franquia_cliente(slug="f2-ciclo-mix")
        fr1.inicio_ciclo = INICIO
        fr1.fim_ciclo = FIM
        _extra_franquia(conta, "f2-ciclo-mix-inv", inicio=INICIO, fim=INICIO)
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.determinavel is False
        assert ciclo.divergente is True
        assert ciclo.fonte == FONTE_CICLO_DIVERGENTE
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_CICLO_DIVERGENTE for d in divs)


def test_nova_franquia_nao_herda_ciclo_monetario_invalido(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-prop")
        fr.consumo_acumulado = Decimal("4")
        conta.multiuser_ativa = True
        conta.quantidade_assentos_contratados = 2
        _vinculo_monetario(conta, inicio=FIM, fim=INICIO)
        criadas = materializar_franquias_faltantes(
            db.session.get(Conta, conta.id),
            2,
            limite_referencia=Decimal("40"),
            aplicar_limite_nas_existentes=False,
        )
        db.session.commit()
        assert len(criadas) == 1
        assert criadas[0].inicio_ciclo is None
        assert criadas[0].fim_ciclo is None
        db.session.refresh(fr)
        assert fr.consumo_acumulado == Decimal("4")
        assert fr.inicio_ciclo is None
        assert fr.fim_ciclo is None


def test_alinhamento_nao_propaga_ciclo_invalido_para_nula(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-alinh")
        _vinculo_monetario(conta, inicio=INICIO, fim=INICIO)
        n = alinhar_franquias_sem_ciclo_ao_canonico(conta.id)
        db.session.commit()
        db.session.refresh(fr)
        assert n == 0
        assert fr.inicio_ciclo is None
        assert fr.fim_ciclo is None


def test_periodo_invalido_persistido_nao_e_sobrescrito(app):
    with app.app_context():
        conta, fr = seed_conta_franquia_cliente(slug="f2-ciclo-keep")
        fr.inicio_ciclo = FIM
        fr.fim_ciclo = INICIO
        db.session.commit()
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        assert ciclo.divergente is True
        assert not alinhar_franquia_ao_ciclo_conta(fr, ciclo)
        db.session.refresh(fr)
        assert fr.inicio_ciclo == FIM
        assert fr.fim_ciclo == INICIO



# --- Degradação ---


def test_degradacao_permanece_individual(app):
    with app.app_context():
        conta, fr_a = seed_conta_franquia_cliente(slug="f2-deg")
        fr_a.consumo_acumulado = Decimal("100")
        fr_a.limite_total = Decimal("100")
        fr_a.status = Franquia.STATUS_DEGRADED
        fr_b = _extra_franquia(conta, "f2-deg-b")
        fr_b.consumo_acumulado = Decimal("10")
        fr_b.limite_total = Decimal("100")
        fr_b.status = Franquia.STATUS_ACTIVE
        db.session.commit()
        st_a, _ = classificar_estado_operacional_franquia(fr_a, _plano_mu())
        st_b, _ = classificar_estado_operacional_franquia(fr_b, _plano_mu())
        assert st_a == Franquia.STATUS_DEGRADED
        assert st_b == Franquia.STATUS_ACTIVE
        db.session.refresh(fr_a)
        db.session.refresh(fr_b)
        assert fr_a.status == Franquia.STATUS_DEGRADED
        assert fr_b.status == Franquia.STATUS_ACTIVE


# --- Stripe / IA / jornada pública ---


def test_fase2_nao_chama_stripe(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-stripe")
        user = seed_usuario(fr.id, conta.id, email="f2.stripe@test.com", categoria="free")
        fake_stripe = MagicMock()
        with patch.dict("sys.modules", {"stripe": fake_stripe}):
            atribuir_plano_para_usuario(
                email=user.email, plano_raw="multiuser", quantidade_franquias_raw="5"
            )
            executar_reconciliacao_multiuser_antes_enforcement()
            snapshot_capacidade(conta.id)
        fake_stripe.assert_not_called()
        assert not fake_stripe.method_calls


def test_servicos_fase2_nao_importam_stripe_nem_ia():
    arquivos = [
        ROOT / "app" / "services" / "conta_multiuser_capacidade_service.py",
        ROOT / "app" / "services" / "conta_multiuser_ciclo_service.py",
        ROOT / "app" / "services" / "conta_multiuser_reconciliacao_service.py",
        ROOT / "app" / "services" / "conta_multiuser_errors.py",
        ROOT / "app" / "services" / "user_plan_control_service.py",
    ]
    for path in arquivos:
        src = path.read_text(encoding="utf-8")
        assert "import stripe" not in src
        assert "stripe." not in src or "Não chama Stripe" in src or "nao chama Stripe" in src.lower()
        assert "openai" not in src.lower()
        assert "anthropic" not in src.lower()


def test_fase2_nao_cria_jornada_publica():
    for rel in (
        "app/services/conta_multiuser_capacidade_service.py",
        "app/services/conta_multiuser_ciclo_service.py",
        "app/services/conta_multiuser_reconciliacao_service.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "@app.route" not in src
        assert "Blueprint" not in src
        assert "checkout" not in src.lower() or "Não chama" in src


def test_fase2_sem_hardcodes_comerciais():
    for rel in (
        "app/services/conta_multiuser_capacidade_service.py",
        "app/services/conta_multiuser_ciclo_service.py",
        "app/services/conta_multiuser_reconciliacao_service.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "49.90" not in src
        assert "49,90" not in src
        assert "return 5" not in src
        assert "quantidade_minima = 5" not in src


def test_quantity_externa_persistida_detecta_divergencia(app):
    with app.app_context():
        conta, _fr = seed_conta_franquia_cliente(slug="f2-qext")
        persistir_quantidade_assentos_contratados(conta.id, 5)
        db.session.add(
            ContaMonetizacaoVinculo(
                conta_id=conta.id,
                provider="stripe",
                ativo=True,
                snapshot_normalizado_json='{"quantity": 7}',
            )
        )
        db.session.commit()
        divs = listar_divergencias_conta(conta.id)
        assert any(d.codigo == CODIGO_DIV_QUANTITY_EXTERNA_LOCAL for d in divs)


def test_reducao_quantity_abaixo_de_ativos_e_impeditiva(app):
    with app.app_context():
        _preparar_planos_admin()
        conta, fr = seed_conta_franquia_cliente(slug="f2-red")
        user = seed_usuario(fr.id, conta.id, email="f2.red@test.com", categoria="free")
        atribuir_plano_para_usuario(
            email=user.email, plano_raw="multiuser", quantidade_franquias_raw="3"
        )
        livres = [
            x for x in Franquia.query.filter_by(conta_id=conta.id) if x.id != user.franquia_id
        ]
        for i, frx in enumerate(livres[:2]):
            u = seed_usuario(frx.id, conta.id, email=f"f2.red.{i}@test.com", categoria="multiuser")
            ocupar_assento(conta_id=conta.id, user_id=u.id, franquia_id=frx.id, papel="membro")
        with pytest.raises(DivergenciaImpeditivaError):
            aplicar_atribuicao_plano_multiuser_admin(
                user=db.session.get(User, user.id),
                quantidade_assentos=1,
                limite_referencia=Decimal("200"),
                admin_user_id=None,
            )


def test_backfill_f1_ainda_e_a_rotina_reutilizada():
    src = inspect.getsource(executar_reconciliacao_multiuser_antes_enforcement)
    assert "aplicar_backfill_multiuser_legado" in src
    src_adm = inspect.getsource(aplicar_atribuicao_plano_multiuser_admin)
    assert "executar_reconciliacao_multiuser_antes_enforcement" in src_adm
    assert "exigir_cnpj=nova_ativacao" in src_adm
