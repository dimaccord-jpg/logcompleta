"""Testes direcionados do Lote 4B SCRUM-148: Growth paid (cutover observavel)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.extensions import db
from app.funnel_event_service import (
    FUNNEL_EVENT_PAID,
    FUNNEL_SOURCE_GROWTH,
    META_PIXEL_ALLOWED_EVENTS,
    GROWTH_PAID_CUTOVER_AT_ENV,
    is_meta_pixel_allowed,
    try_ensure_growth_paid_for_conta,
)
from app.models import FunnelEvent, MonetizacaoFato
from app.services.cleiton_monetizacao_service import (
    STATUS_TEC_APLICADO,
    registrar_fato_monetizacao,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

CUTOVER = datetime(2026, 9, 25, 12, 0, 0, tzinfo=timezone.utc)
CUTOVER_ISO = "2026-09-25T12:00:00Z"
CUTOVER_NAIVE = CUTOVER.replace(tzinfo=None)


def _ts(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


@pytest.fixture
def cutover_ok(monkeypatch):
    monkeypatch.setenv(GROWTH_PAID_CUTOVER_AT_ENV, CUTOVER_ISO)


def _seed_account(slug: str):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    user = seed_usuario(franquia.id, conta.id, email=f"{slug}@test.com")
    return conta, franquia, user


def _invoice_event(
    *,
    invoice_id: str,
    created: datetime,
    amount_paid: int = 9900,
    billing_reason: str = "subscription_create",
    status: str = "paid",
    subscription_id: str = "sub_growth_1",
    customer_id: str = "cus_growth_1",
    plan: str | None = "starter",
    event_id: str | None = None,
    extra_object: dict | None = None,
):
    obj = {
        "id": invoice_id,
        "object": "invoice",
        "customer": customer_id,
        "subscription": subscription_id,
        "status": status,
        "amount_paid": amount_paid,
        "billing_reason": billing_reason,
        "created": _ts(created),
    }
    if plan:
        obj["metadata"] = {"plano_interno": plan}
    if extra_object:
        obj.update(extra_object)
    return {
        "id": event_id or f"evt_{invoice_id}",
        "type": "invoice.paid",
        "created": _ts(created),
        "data": {"object": obj},
    }


def _persist_invoice_fato(
    *,
    conta_id: int,
    franquia_id: int | None = None,
    usuario_id: int | None = None,
    invoice_id: str,
    created: datetime,
    amount_paid: int = 9900,
    billing_reason: str = "subscription_create",
    status: str = "paid",
    subscription_id: str = "sub_growth_1",
    plan: str | None = "starter",
    snapshot: dict | None = None,
    event_id: str | None = None,
    omit_created: bool = False,
    invalid_created: bool = False,
    extra_object: dict | None = None,
):
    evento = _invoice_event(
        invoice_id=invoice_id,
        created=created,
        amount_paid=amount_paid,
        billing_reason=billing_reason,
        status=status,
        subscription_id=subscription_id,
        plan=plan,
        event_id=event_id,
        extra_object=extra_object,
    )
    if omit_created:
        del evento["data"]["object"]["created"]
    if invalid_created:
        evento["data"]["object"]["created"] = "not-a-timestamp"
    return registrar_fato_monetizacao(
        tipo_fato="stripe_invoice_paid",
        status_tecnico=STATUS_TEC_APLICADO,
        provider="stripe",
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        invoice_id=invoice_id,
        subscription_id=subscription_id,
        customer_id="cus_growth_1",
        external_event_id=evento["id"],
        idempotency_key=f"growth4b:{evento['id']}",
        snapshot_normalizado=snapshot
        if snapshot is not None
        else {"origem": "lote4b_test", "dominio": "recurring_normal"},
        payload_bruto_sanitizado=evento,
    )


def _paid_rows(conta_id: int):
    return (
        FunnelEvent.query.filter_by(
            conta_id=conta_id,
            event_name=FUNNEL_EVENT_PAID,
            source=FUNNEL_SOURCE_GROWTH,
        )
        .order_by(FunnelEvent.id.asc())
        .all()
    )


def test_cutover_ausente_nao_cria_paid(app, monkeypatch):
    monkeypatch.delenv(GROWTH_PAID_CUTOVER_AT_ENV, raising=False)
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-no-cutover")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_no_cutover",
            created=CUTOVER_NAIVE + timedelta(hours=1),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_cutover_invalido_nao_cria_paid(app, monkeypatch):
    monkeypatch.setenv(GROWTH_PAID_CUTOVER_AT_ENV, "25/09/2026 12:00")
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-bad-cutover")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_bad_cutover",
            created=CUTOVER_NAIVE + timedelta(hours=1),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_cutover_sem_timezone_nao_cria_paid(app, monkeypatch):
    monkeypatch.setenv(GROWTH_PAID_CUTOVER_AT_ENV, "2026-09-25T12:00:00")
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-naive-cutover")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_naive_cutover",
            created=CUTOVER_NAIVE + timedelta(hours=1),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_invoice_created_antes_cutover_nao_cria(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-before")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_before",
            created=CUTOVER_NAIVE - timedelta(hours=1),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_invoice_elegivel_pos_cutover_cria_paid(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-ok")
        created = CUTOVER_NAIVE + timedelta(minutes=30)
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_ok_1",
            created=created,
            plan="pro",
        )
        db.session.commit()
        result = try_ensure_growth_paid_for_conta(conta.id)
        assert result is not None
        assert result["created"] is True
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.idempotency_key == f"growth:paid:conta:{conta.id}"
        assert row.correlation_id == "in_ok_1"
        assert row.occurred_at == created
        assert row.user_id == user.id
        assert row.conta_id == conta.id
        assert row.metadata_json == {"plan": "pro"}
        assert "amount" not in (row.metadata_json or {})
        assert "invoice_id" not in (row.metadata_json or {})
        assert "billing_reason" not in (row.metadata_json or {})


def test_amount_paid_zero_nao_cria(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-zero")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_zero",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            amount_paid=0,
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_subscription_cycle_nao_cria(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-cycle")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_cycle",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            billing_reason="subscription_cycle",
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_cobranca_extraordinaria_multiuser_nao_cria(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-extra")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_extra",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            snapshot={
                "dominio": "multiuser_extraordinary",
                "flow_type": "multiuser_extraordinary",
            },
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


@pytest.mark.parametrize(
    "mode",
    ["omit", "invalid"],
)
def test_invoice_created_ausente_ou_invalido_nao_cria(app, cutover_ok, mode):
    with app.app_context():
        conta, franquia, user = _seed_account(f"lote4b-created-{mode}")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id=f"in_created_{mode}",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            omit_created=(mode == "omit"),
            invalid_created=(mode == "invalid"),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_dois_fatos_mesma_invoice_uma_identidade(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-dedup")
        created = CUTOVER_NAIVE + timedelta(hours=2)
        for i in range(2):
            _persist_invoice_fato(
                conta_id=conta.id,
                franquia_id=franquia.id,
                usuario_id=user.id,
                invoice_id="in_same",
                created=created,
                event_id=f"evt_same_{i}",
            )
        db.session.commit()
        try_ensure_growth_paid_for_conta(conta.id)
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        assert rows[0].correlation_id == "in_same"
        assert FunnelEvent.query.filter_by(idempotency_key=f"growth:paid:conta:{conta.id}").count() == 1


def test_duas_invoices_escolhe_primeira_por_created(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-first")
        first_created = CUTOVER_NAIVE + timedelta(hours=1)
        second_created = CUTOVER_NAIVE + timedelta(hours=3)
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_second",
            created=second_created,
            plan="multiuser",
            event_id="evt_second",
        )
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_first",
            created=first_created,
            plan="starter",
            event_id="evt_first",
        )
        db.session.commit()
        try_ensure_growth_paid_for_conta(conta.id)
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        assert rows[0].correlation_id == "in_first"
        assert rows[0].occurred_at == first_created
        assert rows[0].metadata_json == {"plan": "starter"}


def test_paid_ja_existente_nao_duplica(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-idem")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_idem",
            created=CUTOVER_NAIVE + timedelta(hours=1),
        )
        db.session.commit()
        first = try_ensure_growth_paid_for_conta(conta.id)
        second = try_ensure_growth_paid_for_conta(conta.id)
        assert first is not None and first["created"] is True
        assert second is not None and second["created"] is False
        assert len(_paid_rows(conta.id)) == 1


def test_recuperacao_usa_primeira_invoice_ja_persistida(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-recovery")
        first_created = CUTOVER_NAIVE + timedelta(hours=1)
        later_created = CUTOVER_NAIVE + timedelta(hours=5)
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_recover_a",
            created=first_created,
            plan="starter",
            event_id="evt_recover_a",
        )
        db.session.commit()
        # Growth ausente pese invoice A ja existir.
        assert _paid_rows(conta.id) == []

        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_recover_b",
            created=later_created,
            plan="pro",
            event_id="evt_recover_b",
        )
        db.session.commit()
        try_ensure_growth_paid_for_conta(conta.id)
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        assert rows[0].correlation_id == "in_recover_a"
        assert rows[0].occurred_at == first_created
        assert rows[0].metadata_json == {"plan": "starter"}


def test_replay_invoice_pre_cutover_continua_sem_criar(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-replay-pre")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_pre_replay",
            created=CUTOVER_NAIVE - timedelta(days=1),
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_falha_growth_nao_altera_processamento_comercial(app, cutover_ok, monkeypatch):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-failopen")
        fato = _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_failopen",
            created=CUTOVER_NAIVE + timedelta(hours=1),
        )
        db.session.commit()
        fato_id = fato.id
        before = MonetizacaoFato.query.filter_by(id=fato_id).one()
        before_tipo = before.tipo_fato
        before_status = before.status_tecnico

        monkeypatch.setattr(
            "app.funnel_event_service.record_funnel_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("growth boom")),
        )
        result = try_ensure_growth_paid_for_conta(conta.id)
        assert result is None
        assert _paid_rows(conta.id) == []
        after = MonetizacaoFato.query.filter_by(id=fato_id).one()
        assert after.tipo_fato == before_tipo
        assert after.status_tecnico == before_status


def test_pos_commit_hook_chama_avaliador_sem_desfazer_fato(app, cutover_ok, monkeypatch):
    from app.services import cleiton_monetizacao_service as monet

    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-hook")
        called = {"n": 0}

        def _fake_ensure(cid):
            called["n"] += 1
            called["cid"] = cid
            return None

        monkeypatch.setattr(
            "app.funnel_event_service.try_ensure_growth_paid_for_conta",
            _fake_ensure,
        )
        monet._try_growth_paid_pos_commit(conta.id)
        assert called["n"] == 1
        assert called["cid"] == conta.id

        # Falha no hook nao propaga.
        monkeypatch.setattr(
            "app.funnel_event_service.try_ensure_growth_paid_for_conta",
            lambda _cid: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        monet._try_growth_paid_pos_commit(conta.id)


def test_replay_sem_commit_nao_chama_growth(app, cutover_ok, monkeypatch):
    from app.services import cleiton_monetizacao_service as monet
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-stripe-replay")
        created = CUTOVER_NAIVE + timedelta(hours=1)
        evento = _invoice_event(
            invoice_id="in_stripe_replay",
            created=created,
            plan="starter",
            event_id="evt_stripe_replay_ok",
        )
        evento["data"]["object"]["metadata"] = {
            "plano_interno": "starter",
            "conta_id": str(conta.id),
            "franquia_id": str(franquia.id),
            "usuario_id": str(user.id),
        }
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico=STATUS_TEC_APLICADO,
            provider="stripe",
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_stripe_replay",
            subscription_id="sub_growth_1",
            customer_id="cus_growth_1",
            idempotency_key="stripe_event:evt_stripe_replay_ok:invoice.paid",
            external_event_id="evt_stripe_replay_ok",
            snapshot_normalizado={"origem": "preseed", "dominio": "recurring_normal"},
            payload_bruto_sanitizado=evento,
        )
        db.session.commit()
        assert _paid_rows(conta.id) == []

        calls = []
        monkeypatch.setattr(
            monet,
            "_try_growth_paid_pos_commit",
            lambda cid: calls.append(cid),
        )
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out.get("replay") is True
        assert calls == []
        assert _paid_rows(conta.id) == []


def test_early_return_ignorado_nao_chama_growth(app, cutover_ok, monkeypatch):
    from app.services import cleiton_monetizacao_service as monet
    from app.services.cleiton_monetizacao_service import processar_evento_stripe

    with app.app_context():
        calls = []
        monkeypatch.setattr(
            monet,
            "_try_growth_paid_pos_commit",
            lambda cid: calls.append(cid),
        )
        out = processar_evento_stripe(
            {
                "id": "evt_ignored_growth",
                "type": "charge.succeeded",
                "created": _ts(CUTOVER_NAIVE + timedelta(hours=1)),
                "data": {"object": {"id": "ch_1", "object": "charge"}},
            }
        )
        assert out["ok"] is True
        assert out.get("replay") is False
        assert calls == []


def test_pos_commit_final_hooks_ainda_existem():
    import inspect
    from app.services import cleiton_monetizacao_service as monet

    src_evento = inspect.getsource(monet.processar_evento_stripe)
    src_conc = inspect.getsource(monet.processar_fato_stripe_conciliado)
    # Somente o commit final de cada fluxo dispara Growth (sem replay/early-return).
    assert src_evento.count("_try_growth_paid_pos_commit") == 1
    assert src_conc.count("_try_growth_paid_pos_commit") == 1
    assert "db.session.commit()" in src_evento
    assert "db.session.commit()" in src_conc
    # Replay/early-return nao devem chamar o hook.
    assert "replay\": True" not in src_evento.split("_try_growth_paid_pos_commit")[0][-400:]


def test_growth_paid_sem_savepoint_nem_dirty_guard():
    import inspect
    from app.funnel_event_service import try_ensure_growth_paid_for_conta
    from app.services import cleiton_monetizacao_service as monet

    ensure_src = inspect.getsource(try_ensure_growth_paid_for_conta)
    hook_src = inspect.getsource(monet._try_growth_paid_pos_commit)
    assert "begin_nested" not in ensure_src
    assert "SAVEPOINT" not in ensure_src.upper()
    assert "dirty" not in hook_src
    assert "begin_nested" not in hook_src
    assert "sess.new" not in hook_src


def test_meta_pixel_allowlist_paid_fora(app, cutover_ok):
    assert META_PIXEL_ALLOWED_EVENTS == set() or META_PIXEL_ALLOWED_EVENTS == frozenset()
    assert is_meta_pixel_allowed("paid") is False
    assert FUNNEL_EVENT_PAID not in META_PIXEL_ALLOWED_EVENTS
    assert is_meta_pixel_allowed("file_uploaded") is False
    assert is_meta_pixel_allowed("freight_calculated") is False


def test_plan_omitido_quando_nao_comprovavel(app, cutover_ok, monkeypatch):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-noplan")
        monkeypatch.setattr(
            "app.services.plano_service.resolver_plano_por_gateway_price_id_admin",
            lambda **_k: None,
        )
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_noplan",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            plan=None,
        )
        db.session.commit()
        try_ensure_growth_paid_for_conta(conta.id)
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        assert rows[0].metadata_json in (None, {})


def test_invoice_created_divergente_descarta_definitivo(app, cutover_ok):
    """Terceiro fato nao reinsere invoice ambigua; pre-cutover entra na comparacao."""
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-ambig")
        pre = CUTOVER_NAIVE - timedelta(hours=2)
        post_a = CUTOVER_NAIVE + timedelta(hours=1)
        post_b = CUTOVER_NAIVE + timedelta(hours=2)
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_ambig",
            created=pre,
            event_id="evt_ambig_pre",
        )
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_ambig",
            created=post_a,
            event_id="evt_ambig_a",
        )
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_ambig",
            created=post_a,
            event_id="evt_ambig_a2",
        )
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_ok_other",
            created=post_b,
            event_id="evt_ok_other",
            plan="starter",
        )
        db.session.commit()
        try_ensure_growth_paid_for_conta(conta.id)
        rows = _paid_rows(conta.id)
        assert len(rows) == 1
        assert rows[0].correlation_id == "in_ok_other"


def test_subscription_bool_true_nao_elegivel(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-sub-bool")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_sub_bool",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            subscription_id="sub_growth_1",
            extra_object={"subscription": True},
        )
        fato = MonetizacaoFato.query.filter_by(invoice_id="in_sub_bool").one()
        fato.subscription_id = None
        payload = __import__("json").loads(fato.payload_bruto_sanitizado_json)
        payload["data"]["object"]["subscription"] = True
        fato.payload_bruto_sanitizado_json = __import__("json").dumps(payload)
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_amount_paid_infinity_nao_elegivel(app, cutover_ok):
    from app.funnel_event_service import _amount_paid_positive

    assert _amount_paid_positive({"amount_paid": float("inf")}) is False
    assert _amount_paid_positive({"amount_paid": float("nan")}) is False
    assert _amount_paid_positive({"amount_paid": 9900}) is True

    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-inf")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_inf",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            amount_paid=9900,
        )
        fato = MonetizacaoFato.query.filter_by(invoice_id="in_inf").one()
        import app.funnel_event_service as fes

        original = fes._invoice_object_from_fato_payload

        def _inf_obj(payload):
            obj = original(payload)
            if isinstance(obj, dict):
                obj = dict(obj)
                obj["amount_paid"] = float("inf")
            return obj

        fes._invoice_object_from_fato_payload = _inf_obj
        try:
            db.session.commit()
            assert try_ensure_growth_paid_for_conta(conta.id) is None
            assert _paid_rows(conta.id) == []
        finally:
            fes._invoice_object_from_fato_payload = original
        assert fato.id is not None


def test_dominio_recurring_normal_pode_seguir(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-dom-ok")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_dom_ok",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            snapshot={"dominio": "recurring_normal"},
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is not None
        assert len(_paid_rows(conta.id)) == 1


def test_dominio_extraordinary_rejeita(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-dom-extra")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_dom_extra",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            snapshot={"dominio": "multiuser_extraordinary"},
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_dominio_unknown_rejeita(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-dom-unk")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_dom_unk",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            snapshot={"dominio": "unknown"},
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_dominio_ausente_rejeita(app, cutover_ok):
    with app.app_context():
        conta, franquia, user = _seed_account("lote4b-dom-abs")
        _persist_invoice_fato(
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            invoice_id="in_dom_abs",
            created=CUTOVER_NAIVE + timedelta(hours=1),
            snapshot={"origem": "fato_antigo_sem_dominio"},
        )
        db.session.commit()
        assert try_ensure_growth_paid_for_conta(conta.id) is None
        assert _paid_rows(conta.id) == []


def test_classificacao_excecao_vira_unknown(monkeypatch):
    from app.funnel_event_service import (
        BILLING_DOMAIN_UNKNOWN,
        classificar_billing_domain_ingestao,
    )

    def _boom(*_a, **_k):
        raise RuntimeError("classifier down")

    monkeypatch.setattr(
        "app.services.conta_multiuser_cobranca_extraordinaria_service."
        "evento_eh_cobranca_extraordinaria_multiuser",
        _boom,
    )
    dominio = classificar_billing_domain_ingestao(
        {"type": "invoice.paid"},
        {"billing_reason": "subscription_create", "object": "invoice"},
    )
    assert dominio == BILLING_DOMAIN_UNKNOWN


def test_ausencia_extraordinary_nao_implica_recurring_normal():
    from app.funnel_event_service import (
        BILLING_DOMAIN_UNKNOWN,
        classificar_billing_domain_ingestao,
    )

    # Sem billing_reason/subscription e sem extraordinary => unknown (nao recurring).
    dominio = classificar_billing_domain_ingestao(
        {"type": "invoice.paid"},
        {"object": "invoice", "status": "paid", "amount_paid": 10},
    )
    assert dominio == BILLING_DOMAIN_UNKNOWN


def test_sanitizer_nao_preserva_objetos_livres_de_dominio():
    from app.services.stripe_payload_sanitizer import sanitizar_payload_stripe

    bruto = {
        "id": "evt_extra_san",
        "type": "invoice.paid",
        "data": {
            "object": {
                "id": "in_extra_san",
                "object": "invoice",
                "customer": "cus_x",
                "subscription": "sub_x",
                "amount_paid": 100,
                "billing_reason": "subscription_create",
                "status": "paid",
                "created": 1,
                "payment_intent": {"id": {"nested": True}, "charges": {"data": []}},
                "metadata": {
                    "flow_type": {"bad": True},
                    "request_id": {"bad": True},
                    "conta_id": "9",
                    "email": "leak@example.com",
                },
            }
        },
    }
    limpo = sanitizar_payload_stripe(bruto)
    obj = limpo["data"]["object"]
    assert "payment_intent" not in obj
    meta = obj.get("metadata") or {}
    assert "flow_type" not in meta
    assert "request_id" not in meta
    assert meta.get("conta_id") == "9"
    assert "email" not in meta


def test_sanitizer_nao_persiste_payment_intent_em_nenhuma_forma():
    """Growth paid nao depende de payment_intent; campo nunca entra no payload sanitizado."""
    from app.services.stripe_payload_sanitizer import sanitizar_objeto_stripe

    casos = (
        {"payment_intent": "pi_123ABC", "id": "in_1"},
        {"payment_intent": "pi_JoaoSilva", "id": "in_2"},
        {"payment_intent": "pi_JoãoSilva", "id": "in_3"},
        {"payment_intent": "pessoa@example.com", "id": "in_mail"},
        {
            "payment_intent": {"id": "pi_123ABC", "charges": []},
            "id": "in_dict_ok",
        },
        {
            "payment_intent": {
                "id": "pessoa@example.com",
                "receipt_email": "pessoa@example.com",
            },
            "id": "in_mail_obj",
        },
        {
            "payment_intent": {"receipt_email": "pessoa@example.com", "charges": []},
            "id": "in_pii_only",
        },
        {"payment_intent": "pi_123ABC_secret_xyz", "id": "in_secret"},
        {"payment_intent": "pi_", "id": "in_empty_prefix"},
        {"payment_intent": True, "id": "in_bool"},
        {"payment_intent": 123, "id": "in_int"},
        {"payment_intent": ["pi_123ABC"], "id": "in_list"},
    )
    for bruto in casos:
        limpo = sanitizar_objeto_stripe(bruto)
        assert "payment_intent" not in limpo
        assert limpo.get("id") == bruto["id"]
        assert "charges" not in limpo
        assert "receipt_email" not in limpo


def test_sanitizer_origem_metadata_nao_bypass_payment_intent(caplog):
    """Caminho origem+metadata curto nao pode copiar payment_intent integralmente."""
    import logging

    from app.services.stripe_payload_sanitizer import sanitizar_payload_stripe

    rejeitados = (
        "pi_123ABC",
        "pessoa@example.com",
        "pi_expanded_secret",
        "pi_nested_secret",
        "pi_list_secret",
    )

    with caplog.at_level(logging.DEBUG):
        # 1) Stripe-like id
        limpo = sanitizar_payload_stripe(
            {"origem": "x", "metadata": {"payment_intent": "pi_123ABC"}}
        )
        assert "payment_intent" not in (limpo.get("metadata") or {})
        assert limpo.get("origem") == "x"

        # 2) texto livre / email
        limpo = sanitizar_payload_stripe(
            {"origem": "x", "metadata": {"payment_intent": "pessoa@example.com"}}
        )
        assert "payment_intent" not in (limpo.get("metadata") or {})

        # 3) dict expandido
        limpo = sanitizar_payload_stripe(
            {
                "origem": "x",
                "metadata": {
                    "payment_intent": {
                        "id": "pi_expanded_secret",
                        "charges": {"data": []},
                    }
                },
            }
        )
        assert "payment_intent" not in (limpo.get("metadata") or {})

        # 4) aninhado em dict dentro de metadata
        limpo = sanitizar_payload_stripe(
            {
                "origem": "x",
                "metadata": {
                    "nested": {"payment_intent": {"id": "pi_nested_secret"}},
                    "conta_id": "42",
                },
            }
        )
        meta = limpo.get("metadata") or {}
        assert "payment_intent" not in meta
        assert "payment_intent" not in (meta.get("nested") or {})
        assert meta.get("nested") == {}
        assert meta.get("conta_id") == "42"

        # 5) dict em lista dentro de metadata
        limpo = sanitizar_payload_stripe(
            {
                "origem": "x",
                "metadata": {
                    "items": [{"payment_intent": "pi_list_secret", "ok": "keep"}],
                    "fluxo_origem": "checkout",
                },
            }
        )
        meta = limpo.get("metadata") or {}
        assert meta.get("fluxo_origem") == "checkout"
        items = meta.get("items") or []
        assert len(items) == 1
        assert "payment_intent" not in items[0]
        assert items[0].get("ok") == "keep"

        # 6) outras chaves legitimas intactas
        limpo = sanitizar_payload_stripe(
            {
                "origem": "x",
                "metadata": {
                    "conta_id": "9",
                    "correlation_id": "corr-1",
                    "custom_flag": "keep_me",
                },
            }
        )
        meta = limpo.get("metadata") or {}
        assert meta == {
            "conta_id": "9",
            "correlation_id": "corr-1",
            "custom_flag": "keep_me",
        }

    # 7) nenhum valor rejeitado e logado
    log_text = caplog.text
    for valor in rejeitados:
        assert valor not in log_text


def _assert_nenhuma_chave_pii_never(value, *, path="$"):
    """Invariante: nenhuma chave de _PII_NEVER em qualquer profundidade."""
    from app.services.stripe_payload_sanitizer import _PII_NEVER, _norm_key

    if isinstance(value, dict):
        for k, v in value.items():
            assert _norm_key(k) not in _PII_NEVER, (
                f"chave _PII_NEVER '{k}' encontrada em {path}"
            )
            _assert_nenhuma_chave_pii_never(v, path=f"{path}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_nenhuma_chave_pii_never(item, path=f"{path}[{i}]")


def test_sanitizer_invariante_pii_never_recursiva():
    """
    _PII_NEVER é invariante final de sanitizar_payload_stripe:
    remoção por nome de chave em qualquer profundidade (dict/list).
    """
    from app.services.stripe_payload_sanitizer import sanitizar_payload_stripe

    casos = (
        # 1) metadata aninhado
        {
            "bruto": {"origem": "x", "metadata": {"payment_intent": "x", "ok": True}},
            "checks": lambda limpo: (
                limpo.get("origem") == "x"
                and (limpo.get("metadata") or {}).get("ok") is True
                and "payment_intent" not in (limpo.get("metadata") or {})
            ),
        },
        # 2) extra (fora de metadata)
        {
            "bruto": {"origem": "x", "extra": {"payment_intent": "pi_123", "ok": True}},
            "checks": lambda limpo: (
                limpo.get("origem") == "x"
                and (limpo.get("extra") or {}) == {"ok": True}
            ),
        },
        # 3) sob id (cópia allowlist de objeto)
        {
            "bruto": {
                "id": {"payment_intent": "pi_123", "other": "x"},
                "object": "invoice",
                "status": "paid",
            },
            "checks": lambda limpo: (
                limpo.get("status") == "paid"
                and (limpo.get("id") or {}) == {"other": "x"}
            ),
        },
        # 4) profundidade dict + list
        {
            "bruto": {
                "origem": "deep",
                "a": {"b": [{"c": {"payment_intent": "x", "keep": 1}}]},
            },
            "checks": lambda limpo: (
                limpo.get("origem") == "deep"
                and limpo.get("a", {}).get("b", [{}])[0].get("c") == {"keep": 1}
            ),
        },
        # 5) várias chaves legítimas + várias de _PII_NEVER
        {
            "bruto": {
                "origem": "multi",
                "legit": {
                    "amount_paid": 99,
                    "email": "leak@example.com",
                    "receipt_email": "r@example.com",
                    "charges": [{"id": "ch_1"}],
                    "nested": {
                        "payment_intent": "pi_x",
                        "name": "Pessoa",
                        "safe_flag": "yes",
                    },
                },
            },
            "checks": lambda limpo: (
                limpo.get("origem") == "multi"
                and (limpo.get("legit") or {}).get("amount_paid") == 99
                and (limpo.get("legit") or {}).get("nested") == {"safe_flag": "yes"}
                and "email" not in (limpo.get("legit") or {})
                and "receipt_email" not in (limpo.get("legit") or {})
                and "charges" not in (limpo.get("legit") or {})
            ),
        },
    )

    for caso in casos:
        limpo = sanitizar_payload_stripe(caso["bruto"])
        _assert_nenhuma_chave_pii_never(limpo)
        assert caso["checks"](limpo), limpo


def test_anotar_billing_domain_so_enum_controlado(monkeypatch):
    from app.services.cleiton_monetizacao_service import _anotar_billing_domain_no_snapshot
    from app.funnel_event_service import (
        BILLING_DOMAIN_RECURRING_NORMAL,
        BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY,
        BILLING_DOMAIN_UNKNOWN,
        ALLOWED_BILLING_DOMAINS,
    )

    snap: dict = {}
    _anotar_billing_domain_no_snapshot(
        snap,
        evento={"type": "invoice.paid"},
        object_data={
            "id": "in_dom_cls",
            "object": "invoice",
            "billing_reason": "subscription_create",
        },
    )
    assert snap["dominio"] in ALLOWED_BILLING_DOMAINS
    assert snap["dominio"] == BILLING_DOMAIN_RECURRING_NORMAL

    snap2: dict = {}
    monkeypatch.setattr(
        "app.services.conta_multiuser_cobranca_extraordinaria_service."
        "evento_eh_cobranca_extraordinaria_multiuser",
        lambda *_a, **_k: True,
    )
    _anotar_billing_domain_no_snapshot(
        snap2,
        evento={"type": "invoice.paid"},
        object_data={"id": "in_dom_extra", "billing_reason": "subscription_create"},
    )
    assert snap2["dominio"] == BILLING_DOMAIN_MULTIUSER_EXTRAORDINARY

    snap3: dict = {}
    monkeypatch.setattr(
        "app.services.conta_multiuser_cobranca_extraordinaria_service."
        "evento_eh_cobranca_extraordinaria_multiuser",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
    )
    _anotar_billing_domain_no_snapshot(
        snap3,
        evento={"type": "invoice.paid"},
        object_data={"id": "in_dom_boom", "billing_reason": "subscription_create"},
    )
    assert snap3["dominio"] == BILLING_DOMAIN_UNKNOWN
