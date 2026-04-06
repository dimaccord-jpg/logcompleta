"""Testes unitários de classificação de status operacional (plano + vigência + limite + manual)."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models import Franquia, utcnow_naive
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
)
from app.services.cleiton_plano_resolver import (
    CODIGO_AVULSO,
    CODIGO_FREE,
    CODIGO_INTERNA,
    CODIGO_MULTIUSER,
    CODIGO_PRO,
    CODIGO_STARTER,
    CODIGO_UNKNOWN,
    FONT_USER_CATEGORIA,
    PlanoResolvidoCleiton,
)


def _plano(codigo: str) -> PlanoResolvidoCleiton:
    return PlanoResolvidoCleiton(
        codigo=codigo,
        fonte=FONT_USER_CATEGORIA,
        usuario_referencia_id=1,
        categoria_raw="x",
        pendencias=(),
    )


def _fr(**kwargs):
    base = dict(
        limite_total=Decimal("100"),
        consumo_acumulado=Decimal("0"),
        fim_ciclo=None,
        bloqueio_manual=False,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_bloqueio_manual_prevalece():
    fr = _fr(
        bloqueio_manual=True,
        consumo_acumulado=Decimal("5"),
        limite_total=Decimal("100"),
    )
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_FREE))
    assert st == Franquia.STATUS_BLOCKED
    assert m == "bloqueio_manual"


def test_interna_ignora_limite_salvo_manual():
    fr = _fr(
        limite_total=Decimal("1"),
        consumo_acumulado=Decimal("999"),
        bloqueio_manual=False,
    )
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_INTERNA))
    assert st == Franquia.STATUS_ACTIVE
    assert m == "operacional_ok_interna"


def test_interna_bloqueio_manual():
    fr = _fr(bloqueio_manual=True)
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_INTERNA))
    assert st == Franquia.STATUS_BLOCKED
    assert m == "bloqueio_manual"


def test_free_no_limite_active():
    fr = _fr(consumo_acumulado=Decimal("50"), limite_total=Decimal("100"))
    st, _ = classificar_estado_operacional_franquia(fr, _plano(CODIGO_FREE))
    assert st == Franquia.STATUS_ACTIVE


def test_free_no_limite_blocked():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_FREE))
    assert st == Franquia.STATUS_BLOCKED
    assert m == "limite_atingido_free"


def test_starter_degraded():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_STARTER))
    assert st == Franquia.STATUS_DEGRADED
    assert m == "limite_atingido_starter_pro"


def test_pro_degraded():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_PRO))
    assert st == Franquia.STATUS_DEGRADED
    assert m == "limite_atingido_starter_pro"


def test_multiuser_degraded():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_MULTIUSER))
    assert st == Franquia.STATUS_DEGRADED
    assert m == "limite_atingido_starter_pro"


def test_avulso_expired_por_limite():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_AVULSO))
    assert st == Franquia.STATUS_EXPIRED
    assert m == "limite_ou_vigencia_avulso"


def test_unknown_blocked_por_limite():
    fr = _fr(consumo_acumulado=Decimal("100"), limite_total=Decimal("100"))
    st, m = classificar_estado_operacional_franquia(fr, _plano(CODIGO_UNKNOWN))
    assert st == Franquia.STATUS_BLOCKED
    assert m == "limite_atingido_plano_indefinido"


def test_vigencia_expirada_prevalece_sobre_limite():
    agora = utcnow_naive()
    fr = _fr(
        fim_ciclo=agora - timedelta(days=1),
        consumo_acumulado=Decimal("1000"),
        limite_total=Decimal("10"),
    )
    st, m = classificar_estado_operacional_franquia(
        fr, _plano(CODIGO_FREE), agora=agora
    )
    assert st == Franquia.STATUS_EXPIRED
    assert m == "vigencia_expirada"


@pytest.mark.parametrize(
    "codigo,esperado",
    [
        (CODIGO_FREE, Franquia.STATUS_BLOCKED),
        (CODIGO_STARTER, Franquia.STATUS_DEGRADED),
        (CODIGO_PRO, Franquia.STATUS_DEGRADED),
        (CODIGO_MULTIUSER, Franquia.STATUS_DEGRADED),
        (CODIGO_AVULSO, Franquia.STATUS_EXPIRED),
    ],
)
def test_no_limite_sem_efeito(codigo, esperado):
    fr = _fr(limite_total=None, consumo_acumulado=Decimal("999999"))
    st, _ = classificar_estado_operacional_franquia(fr, _plano(codigo))
    assert st == Franquia.STATUS_ACTIVE
