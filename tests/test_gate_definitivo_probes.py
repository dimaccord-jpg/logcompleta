"""Provas independentes do gate; não modifica código de produto nem testes anteriores."""
import io

from flask import session
from flask_login import login_user
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.services import conta_multiuser_convite_service as svc
from tests.test_auditoria_independente_f4 import setup_case
from tests.test_fase4_multiuser_convites import _build_client


def _old_cleide_upload_after_transfer(app, monkeypatch, tmp_path):
    import app.cleide_upload_store as store
    from app.cleide_contracts import set_cleide_upload_ref
    from app.cleide_routes import cleide_bp

    monkeypatch.setattr(store, 'get_cleide_upload_tmp_dir', lambda: str(tmp_path))
    conta, owner, user, invite = setup_case()
    client = _build_client(app)
    app.register_blueprint(cleide_bp)
    app.add_url_rule('/', endpoint='index', view_func=lambda: 'index')
    with app.test_request_context('/'):
        login_user(user)
        saved = store.save_cleide_upload_file(
            file_storage=FileStorage(stream=io.BytesIO(b'carrier,value\nACCOUNT_A_SECRET,123\n'), filename='a.csv'),
            safe_filename='a.csv',
        )
        set_cleide_upload_ref(session, saved['upload_ref'])
        old_session = dict(session)
    with client.session_transaction() as sess:
        sess.update(old_session)
    response = client.post(f'/convite/{invite.token}/aceitar', data={
        'csrf_token': svc.gerar_csrf_token_aceite(user.id, invite.convite_id),
    })
    assert response.status_code == 302
    db.session.refresh(user)
    assert user.conta_id == conta.id
    return user, saved, old_session


def test_cleide_raw_upload_old_reference_must_miss(app, ctx, monkeypatch, tmp_path):
    import app.cleide_upload_store as store
    user, saved, old_session = _old_cleide_upload_after_transfer(app, monkeypatch, tmp_path)
    with app.test_request_context('/'):
        session.update(old_session)
        login_user(user)
        resolved = store.resolve_cleide_upload_file(saved['upload_ref'])
        if resolved is not None:
            assert b'ACCOUNT_A_SECRET' in resolved.read_bytes()
        assert resolved is None, 'Upload bruto de A continua legivel no scope autenticado B'


def test_old_session_cannot_clear_account_a_upload_in_b(app, ctx, monkeypatch, tmp_path):
    from pathlib import Path
    user, saved, old_session = _old_cleide_upload_after_transfer(app, monkeypatch, tmp_path)
    second = app.test_client()
    with second.session_transaction() as sess:
        sess.update(old_session)
    # Autorização operacional real da rota, sem mock de autorização/scope.
    response = second.post('/api/cleide/upload/clear', json={})
    assert Path(saved['absolute_path']).exists(), (
        'Sessao antiga em B excluiu upload de A: '
        f'HTTP {response.status_code}, {response.get_json()}'
    )


def test_financial_json_exception_must_block_accept(app, ctx, monkeypatch):
    from datetime import timedelta
    import app.services.cleiton_monetizacao_service as money
    from app.models import Franquia, utcnow_naive
    from tests.test_fase4_multiuser_convites import _monetizacao_paga

    conta, owner, user, invite = setup_case()
    source_id = user.conta_id
    row = _monetizacao_paga(source_id, 'starter')
    money.processar_evento_stripe({
        'id': 'evt_gate_failed', 'type': 'invoice.payment_failed',
        'data': {'object': {
            'id': 'in_gate_failed', 'customer': row.customer_id,
            'subscription': row.subscription_id, 'billing_reason': 'subscription_cycle',
            'metadata': {'conta_id': str(source_id), 'franquia_id': str(user.franquia_id),
                         'usuario_id': str(user.id), 'plano_interno': 'starter'},
        }},
    })
    db.session.refresh(row)
    row.vigencia_externa_fim = utcnow_naive() - timedelta(days=1)
    db.session.get(Franquia, user.franquia_id).fim_ciclo = row.vigencia_externa_fim
    db.session.commit()
    assert svc.decidir_beneficio_pago_para_transferencia(source_id) == svc.BENEFICIO_PAGO_VIGENTE
    real_loads = money.json.loads
    calls = []

    def broken_invoice_json(raw, *args, **kwargs):
        if isinstance(raw, str) and 'in_gate_failed' in raw:
            calls.append(1)
            raise ValueError('AUDIT_FINANCIAL_JSON_FAILURE')
        return real_loads(raw, *args, **kwargs)

    monkeypatch.setattr(money.json, 'loads', broken_invoice_json)
    decision = svc.decidir_beneficio_pago_para_transferencia(source_id)
    try:
        svc.aceitar_convite(user=user, token=invite.token,
                           csrf_token=svc.gerar_csrf_token_aceite(user.id, invite.convite_id))
    except (RuntimeError, svc.ConviteContratoIncompativelError):
        db.session.rollback()
    db.session.refresh(user)
    assert calls, 'Fault injection precisa atingir parser financeiro real'
    assert user.conta_id == source_id, f'Erro financeiro virou {decision}; User transferido para {user.conta_id}'
    assert decision == svc.BENEFICIO_INCONCLUSIVO


def test_roberto_scope_miss_must_not_delete_account_a_upload(app, ctx, monkeypatch, tmp_path):
    from pathlib import Path
    import app.roberto_upload_store as store
    from app.upload_handler import get_dados_upload_cliente

    monkeypatch.setattr(store, '_base_dir', lambda: str(tmp_path))
    conta, owner, user, invite = setup_case()
    client = _build_client(app)
    app.add_url_rule('/', endpoint='index', view_func=lambda: 'index')
    with app.test_request_context('/'):
        login_user(user)
        ref = store.save_upload_data([{'carrier': 'ACCOUNT_A_ROBERTO_SECRET'}])
        session['roberto_upload_ref'] = ref
        old_session = dict(session)
        path = Path(store._file_path(ref))
        assert path.exists()
    with client.session_transaction() as sess:
        sess.update(old_session)
    response = client.post(f'/convite/{invite.token}/aceitar', data={
        'csrf_token': svc.gerar_csrf_token_aceite(user.id, invite.convite_id),
    })
    assert response.status_code == 302
    db.session.refresh(user)
    assert user.conta_id == conta.id
    with app.test_request_context('/'):
        session.update(old_session)
        login_user(user)
        assert get_dados_upload_cliente() is None
        assert path.exists(), 'MISS de scope em B excluiu fisicamente o upload Roberto de A'
