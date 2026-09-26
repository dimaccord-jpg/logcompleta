import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.capability_taxonomy import DESTINATIONS
from app.editorial_metadata import listar_habilidades
from app.funnel_event_service import META_PIXEL_ALLOWED_EVENTS
from app.growth_routes import ALLOWED_STATIC_CTA_IDS, allowed_cta_ids, growth_bp
from app.models import FunnelEvent
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


@pytest.fixture
def client(app, ctx, monkeypatch):
    app.config['SECRET_KEY'] = 'cta-test'
    app.register_blueprint(growth_bp)
    monkeypatch.setattr('flask_login.utils._get_user', lambda: SimpleNamespace(is_authenticated=False))
    monkeypatch.setattr('app.funnel_event_service._is_desktop_access_admin_test_mode', lambda: False)
    return app.test_client()


@pytest.mark.parametrize('cta_id', sorted(allowed_cta_ids()))
def test_allowed_anonymous_persists_only_controlled_metadata(client, cta_id):
    event_id = str(uuid4())
    response = client.post('/api/growth/cta-clicked', json={
        'event_id': event_id, 'cta_id': cta_id, 'user_id': 123,
        'conta_id': 456, 'franquia_id': 789, 'destination': 'private',
        'email': 'private@example.com', 'href': '/private?next=secret',
    })
    assert response.status_code == 200
    row = FunnelEvent.query.filter_by(idempotency_key=event_id).one()
    assert row.event_name == 'cta_clicked'
    assert row.source == 'growth'
    assert row.metadata_json == {'cta_id': cta_id}
    assert (row.user_id, row.conta_id, row.franquia_id) == (None, None, None)
    assert row.event_name not in META_PIXEL_ALLOWED_EVENTS


@pytest.mark.parametrize('cta_id', [None, [], {}, 'arbitrary', 'discovery_handoff_arbitrary', 'article_skill_arbitrary'])
def test_invalid_cta_rejected(client, cta_id):
    assert client.post('/api/growth/cta-clicked', json={'event_id': str(uuid4()), 'cta_id': cta_id}).status_code == 400
    assert FunnelEvent.query.count() == 0


@pytest.mark.parametrize('event_id', [None, '', 'x' * 81])
def test_invalid_event_id(client, event_id):
    assert client.post('/api/growth/cta-clicked', json={'event_id': event_id, 'cta_id': 'fretes_login'}).status_code == 400


def test_backend_identity_and_occurrence_idempotency(client, monkeypatch):
    conta, franquia = seed_conta_franquia_cliente(slug='cta-identity')
    user = seed_usuario(franquia.id, conta.id, email='cta@test.com')
    monkeypatch.setattr('flask_login.utils._get_user', lambda: SimpleNamespace(
        is_authenticated=True, id=user.id, conta_id=conta.id, franquia_id=franquia.id,
    ))
    occurrence = str(uuid4())
    for event_id in [occurrence, occurrence, str(uuid4())]:
        assert client.post('/api/growth/cta-clicked', json={
            'event_id': event_id, 'cta_id': 'profile_view_plans',
            'user_id': 9999, 'conta_id': 9999, 'franquia_id': 9999,
        }).status_code == 200
    rows = FunnelEvent.query.filter_by(event_name='cta_clicked').all()
    assert len(rows) == 2
    assert all((row.user_id, row.conta_id, row.franquia_id) == (user.id, conta.id, franquia.id) for row in rows)


def test_tracking_failure_is_open(client, monkeypatch):
    monkeypatch.setattr('app.growth_routes.try_record_funnel_event', lambda **kwargs: None)
    assert client.post('/api/growth/cta-clicked', json={'event_id': str(uuid4()), 'cta_id': 'fretes_login'}).status_code == 200


def test_allowlist_uses_product_sources():
    assert allowed_cta_ids() == ALLOWED_STATIC_CTA_IDS | {
        'discovery_handoff_' + destination for destination in DESTINATIONS
    } | {'article_skill_' + skill['id'] for skill in listar_habilidades()}


def test_approved_surfaces_and_exclusions():
    templates = Path('app/templates')
    expected = {
        'base.html': ['nav_login_cadastro', 'shell_regularize_payment'],
        'user_area.html': ['profile_view_plans'],
        'roberto_bi.html': ['fretes_login'],
        'cleide_auditoria_frete.html': ['cleide_bi_login'],
    }
    for filename, identifiers in expected.items():
        source = (templates / filename).read_text(encoding='utf-8')
        for identifier in identifiers:
            assert f'data-growth-cta="{identifier}"' in source
    home = (templates / 'index.html').read_text(encoding='utf-8')
    assert 'data-growth-cta="home_skill_{{ skill.id }}"' in home
    article = (templates / 'noticia_interna.html').read_text(encoding='utf-8')
    assert 'data-growth-cta="article_skill_{{ seo.habilidade_id }}"' in article
    assert 'growth_allowed_cta_ids' in home and 'growth_allowed_cta_ids' in article
    for path in templates.rglob('*.html'):
        if any(part in path.name for part in ('contrate_plano', 'login', 'cadastro', 'acesso_desktop')):
            assert 'data-growth-cta' not in path.read_text(encoding='utf-8')
    chat = Path('app/static/js/chat_behavior.js').read_text(encoding='utf-8')
    assert "link.setAttribute('data-growth-cta', 'limit_upgrade_view_plans')" in chat
    block = chat[chat.index('if (DISCOVERY_MODE && window.AFGrowth)'):]
    block = block[:block.index("if (actionItem.kind === 'limit')")]
    assert "'discovery_handoff_' + actionItem.destination" in block
    assert "'discovery_continue_free'" in block
    assert 'isAllowedCta(growthCtaId)' in block
    assert 'actionItem.label' not in block and 'actionItem.url' not in block


def test_helper_runtime_fail_open_and_occurrences():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for the focused helper runtime test')
    source = Path('app/templates/base.html').read_text(encoding='utf-8')
    script = next(block for block in re.findall(r'<script>(.*?)</script>', source, re.S) if 'function trackCta(' in block)
    script = re.sub(r'{{ growth_allowed_cta_ids.*?}}', json.dumps(sorted(allowed_cta_ids())), script)
    script = re.sub(r'{{ url_for\("growth.growth_cta_clicked"\).*?}}', '"/api/growth/cta-clicked"', script)
    harness = """
const assert = require('node:assert/strict');
let sequence = 0;
let listener;
const calls = [];
global.window = {crypto: {randomUUID: () => 'uuid-' + (++sequence)}};
global.document = {addEventListener: (name, callback, capture) => {
  assert.equal(name, 'click'); assert.equal(capture, true); listener = callback;
}};
global.fetch = (url, options) => { calls.push({url, options}); return Promise.resolve({}); };
""" + script + """
const event = {target: {closest: () => ({getAttribute: () => 'fretes_login'})},
  preventDefault: () => { throw Error('Navigation blocked'); }};
listener(event); listener(event);
assert.equal(calls.length, 2);
assert.notEqual(JSON.parse(calls[0].options.body).event_id, JSON.parse(calls[1].options.body).event_id);
window.AFGrowth.trackCta('fretes_login', 'retry-id');
assert.deepEqual(JSON.parse(calls[2].options.body), {event_id: 'retry-id', cta_id: 'fretes_login'});
assert.equal(calls[2].options.credentials, 'same-origin');
assert.equal(calls[2].options.keepalive, true);
window.AFGrowth.trackCta('article_skill_arbitrary');
assert.equal(calls.length, 3);
global.fetch = () => { throw Error('sync failure'); };
assert.doesNotThrow(() => listener(event));
global.fetch = () => Promise.reject(Error('async failure'));
assert.doesNotThrow(() => listener(event));
window.crypto = undefined;
assert.doesNotThrow(() => listener(event));
"""
    result = subprocess.run([node, '-e', harness], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    for forbidden in ['preventDefault', 'stopPropagation', 'localStorage', 'sessionStorage', 'document.cookie', 'location.', 'fbq', 'gtag']:
        assert forbidden not in script
