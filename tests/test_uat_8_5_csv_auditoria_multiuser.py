"""UAT 8.5: CSV administrativo incorpora diagnóstico oficial Multiuser."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from app.extensions import db
from app.models import utcnow_naive
from app.services.admin_auditoria_clientes_csv_service import gerar_csv_auditoria_clientes
from app.services.conta_multiuser_diagnostico_service import (
    AchadoDiagnostico,
    DiagnosticoContaMultiuser,
)
from app.services.conta_organizacional_rules import (
    CODIGO_DIAG_REDUCAO_PENDENTE,
    STATUS_DIAG_OK,
    STATUS_DIAG_RECONCILIACAO_NECESSARIA,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

DETALHE_UAT_85 = "stripe_na_futura_sem_evidencia_desta_reducao"


def _linhas_csv(payload: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(payload)))


def _ativar_multiuser(conta) -> None:
    conta.multiuser_ativa = True
    db.session.add(conta)
    db.session.commit()


def _diag(
    conta_id: int,
    *,
    status: str,
    codigo: str = "",
    detalhe: str = "",
) -> DiagnosticoContaMultiuser:
    achados: list[AchadoDiagnostico] = []
    if codigo:
        achados.append(
            AchadoDiagnostico(
                codigo=codigo,
                classificacao=status,
                detalhe=detalhe,
            )
        )
    return DiagnosticoContaMultiuser(
        conta_id=int(conta_id),
        status=status,
        stripe_consulta="ok",
        quantity_local=12,
        quantity_stripe=10,
        memberships_ativos=1,
        reservas_validas=0,
        quantity_futura=10,
        customer_id=None,
        subscription_id=None,
        subscription_item_id=None,
        correlation_id=None,
        detectado_em="2026-09-19T12:00:00",
        achados=achados,
    )


def _patch_diagnostico(monkeypatch, factory):
    calls: list[tuple[int, bool]] = []

    def _fake(conta_id: int, *, consultar_stripe: bool = True):
        calls.append((int(conta_id), bool(consultar_stripe)))
        return factory(int(conta_id), consultar_stripe=consultar_stripe)

    monkeypatch.setattr(
        "app.services.admin_auditoria_clientes_csv_service.diagnosticar_conta_multiuser",
        _fake,
    )
    return calls


def test_a_reconciliacao_necessaria_aparece_no_csv_e_eleva_risco(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="uat85-a")
        _ativar_multiuser(conta)
        user = seed_usuario(
            franquia.id, conta.id, email="uat85-a@test.com", categoria="free"
        )
        calls = _patch_diagnostico(
            monkeypatch,
            lambda cid, **_: _diag(
                cid,
                status=STATUS_DIAG_RECONCILIACAO_NECESSARIA,
                codigo=CODIGO_DIAG_REDUCAO_PENDENTE,
                detalhe=DETALHE_UAT_85,
            ),
        )

        payload, total = gerar_csv_auditoria_clientes({})
        row = next(r for r in _linhas_csv(payload) if r["email"] == user.email)

        assert total == 1
        assert calls == [(int(conta.id), True)]
        assert row["multiuser_status_diagnostico"] == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert row["multiuser_achado_codigo"] == CODIGO_DIAG_REDUCAO_PENDENTE
        assert row["multiuser_achado_detalhe"] == DETALHE_UAT_85
        assert row["status_divergencia_severidade"] == "nenhuma"
        assert row["nivel_risco_auditoria"] == "atenção"
        assert row["flag_requer_revisao_manual"] == "true"


def test_b_dois_usuarios_mesma_conta_calculam_diagnostico_uma_vez(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="uat85-b")
        _ativar_multiuser(conta)
        u1 = seed_usuario(franquia.id, conta.id, email="uat85-b1@test.com", categoria="free")
        u2 = seed_usuario(franquia.id, conta.id, email="uat85-b2@test.com", categoria="free")
        calls = _patch_diagnostico(
            monkeypatch,
            lambda cid, **_: _diag(
                cid,
                status=STATUS_DIAG_RECONCILIACAO_NECESSARIA,
                codigo=CODIGO_DIAG_REDUCAO_PENDENTE,
                detalhe=DETALHE_UAT_85,
            ),
        )

        payload, total = gerar_csv_auditoria_clientes({})
        rows = [r for r in _linhas_csv(payload) if r["email"] in {u1.email, u2.email}]

        assert total == 2
        assert calls == [(int(conta.id), True)]
        assert len(rows) == 2
        for row in rows:
            assert row["conta_id"] == str(conta.id)
            assert row["multiuser_status_diagnostico"] == STATUS_DIAG_RECONCILIACAO_NECESSARIA
            assert row["multiuser_achado_codigo"] == CODIGO_DIAG_REDUCAO_PENDENTE
            assert row["multiuser_achado_detalhe"] == DETALHE_UAT_85
            assert row["nivel_risco_auditoria"] == "atenção"
            assert row["flag_requer_revisao_manual"] == "true"


def test_c_conta_multiuser_saudavel_nao_gera_revisao_indevida(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="uat85-c")
        _ativar_multiuser(conta)
        user = seed_usuario(
            franquia.id, conta.id, email="uat85-c@test.com", categoria="free"
        )
        calls = _patch_diagnostico(
            monkeypatch,
            lambda cid, **_: _diag(cid, status=STATUS_DIAG_OK),
        )

        payload, _total = gerar_csv_auditoria_clientes({})
        row = next(r for r in _linhas_csv(payload) if r["email"] == user.email)

        assert calls == [(int(conta.id), True)]
        assert row["multiuser_status_diagnostico"] == STATUS_DIAG_OK
        assert row["multiuser_achado_codigo"] == ""
        assert row["multiuser_achado_detalhe"] == ""
        assert row["status_divergencia_severidade"] == "nenhuma"
        assert row["nivel_risco_auditoria"] == "ok"
        assert row["flag_requer_revisao_manual"] == "false"


def test_d_risco_critico_existente_nao_e_reduzido_pelo_multiuser(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="uat85-d")
        franquia.status = "expired"
        franquia.fim_ciclo = utcnow_naive() + timedelta(days=3)
        db.session.add(franquia)
        _ativar_multiuser(conta)
        user = seed_usuario(
            franquia.id, conta.id, email="uat85-d@test.com", categoria="free"
        )
        _patch_diagnostico(
            monkeypatch,
            lambda cid, **_: _diag(
                cid,
                status=STATUS_DIAG_RECONCILIACAO_NECESSARIA,
                codigo=CODIGO_DIAG_REDUCAO_PENDENTE,
                detalhe=DETALHE_UAT_85,
            ),
        )

        payload, _total = gerar_csv_auditoria_clientes({})
        row = next(r for r in _linhas_csv(payload) if r["email"] == user.email)

        assert row["multiuser_status_diagnostico"] == STATUS_DIAG_RECONCILIACAO_NECESSARIA
        assert row["nivel_risco_auditoria"] == "crítico"
        assert row["flag_requer_revisao_manual"] == "true"


def test_e_conta_nao_multiuser_preserva_comportamento_anterior(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="uat85-e")
        user = seed_usuario(
            franquia.id, conta.id, email="uat85-e@test.com", categoria="free"
        )

        def _nao_deve_chamar(*_a, **_k):
            raise AssertionError("diagnostico Multiuser nao deve rodar em conta nao Multiuser")

        monkeypatch.setattr(
            "app.services.admin_auditoria_clientes_csv_service.diagnosticar_conta_multiuser",
            _nao_deve_chamar,
        )

        payload, total = gerar_csv_auditoria_clientes({})
        row = next(r for r in _linhas_csv(payload) if r["email"] == user.email)

        assert total == 1
        assert conta.multiuser_ativa is False
        assert row["multiuser_status_diagnostico"] == ""
        assert row["multiuser_achado_codigo"] == ""
        assert row["multiuser_achado_detalhe"] == ""
        assert row["status_divergencia_severidade"] == "nenhuma"
        assert row["nivel_risco_auditoria"] == "ok"
        assert row["flag_requer_revisao_manual"] == "false"
