import json
from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_healthz_and_metadata():
    health = client.get('/v1/healthz')
    assert health.status_code == 200
    data = health.json()
    assert data['status'] == 'ok'
    assert 'uptime_seconds' in data
    assert set(data['contexts_loaded']) == {'category', 'merchant', 'customer', 'trigger'}

    meta = client.get('/v1/metadata')
    assert meta.status_code == 200
    payload = meta.json()
    for key in ['team_name', 'team_members', 'model', 'approach', 'contact_email', 'version', 'submitted_at']:
        assert key in payload


def test_context_is_idempotent_and_replaces_newer_version():
    payload = {
        'scope': 'category',
        'context_id': 'dentists',
        'version': 1,
        'delivered_at': '2026-04-26T09:45:00Z',
        'payload': {'slug': 'dentists', 'digest': []}
    }
    first = client.post('/v1/context', json=payload)
    assert first.status_code == 200
    assert first.json()['accepted'] is True

    second = client.post('/v1/context', json=payload)
    assert second.status_code == 409
    assert second.json()['reason'] == 'stale_version'

    new_payload = {**payload, 'version': 2, 'payload': {'slug': 'dentists', 'digest': [{'id': 'new'}]}}
    third = client.post('/v1/context', json=new_payload)
    assert third.status_code == 200
    assert third.json()['accepted'] is True


def test_tick_creates_action_for_active_trigger():
    category = {
        'slug': 'dentists',
        'offer_catalog': [{'title': 'Dental Cleaning @ ₹299'}],
        'voice': {'tone': 'peer_clinical'},
        'digest': [{'id': 'd_1', 'title': 'JIDA research', 'kind': 'research', 'source': 'JIDA Oct 2026'}],
        'patient_content_library': [],
        'seasonal_beats': [],
        'trend_signals': []
    }
    merchant = {
        'merchant_id': 'm_001_drmeera',
        'category_slug': 'dentists',
        'identity': {'name': "Dr. Meera's Dental Clinic", 'city': 'Delhi', 'locality': 'Lajpat Nagar', 'verified': True, 'languages': ['en', 'hi']},
        'subscription': {'status': 'active', 'plan': 'Pro', 'days_remaining': 82},
        'performance': {'views': 2410, 'calls': 18, 'ctr': 0.021},
        'offers': [{'title': 'Dental Cleaning @ ₹299', 'status': 'active'}],
        'customer_aggregate': {'total_unique_ytd': 540, 'lapsed_180d_plus': 78, 'retention_6mo_pct': 0.38},
        'signals': ['high_risk_adult_cohort'],
        'conversation_history': []
    }
    trigger = {
        'id': 'trg_001_research_digest_dentists',
        'scope': 'merchant',
        'kind': 'research_digest',
        'source': 'external',
        'merchant_id': 'm_001_drmeera',
        'customer_id': None,
        'payload': {'category': 'dentists', 'top_item_id': 'd_1'},
        'urgency': 2,
        'suppression_key': 'research:dentists:2026-W17',
        'expires_at': '2026-05-03T00:00:00Z'
    }
    client.post('/v1/context', json={'scope': 'category', 'context_id': 'dentists', 'version': 1, 'delivered_at': '2026-04-26T09:00:00Z', 'payload': category})
    client.post('/v1/context', json={'scope': 'merchant', 'context_id': 'm_001_drmeera', 'version': 1, 'delivered_at': '2026-04-26T09:00:00Z', 'payload': merchant})
    client.post('/v1/context', json={'scope': 'trigger', 'context_id': 'trg_001_research_digest_dentists', 'version': 1, 'delivered_at': '2026-04-26T09:00:00Z', 'payload': trigger})

    tick = client.post('/v1/tick', json={'now': '2026-04-26T10:35:00Z', 'available_triggers': ['trg_001_research_digest_dentists']})
    assert tick.status_code == 200
    actions = tick.json()['actions']
    assert len(actions) == 1
    assert actions[0]['merchant_id'] == 'm_001_drmeera'
    assert actions[0]['send_as'] == 'vera'
    assert actions[0]['suppression_key'] == 'research:dentists:2026-W17'
    assert 'body' in actions[0]


def test_reply_handles_lets_do_it_and_auto_reply():
    conversation_id = 'conv_test_intent'
    merchant_id = 'm_001_drmeera'

    auto = client.post('/v1/reply', json={
        'conversation_id': conversation_id,
        'merchant_id': merchant_id,
        'customer_id': None,
        'from_role': 'merchant',
        'message': 'Thank you for contacting Dr. Meera\'s Dental Clinic! Our team will respond shortly.',
        'received_at': '2026-04-26T10:42:00Z',
        'turn_number': 1,
    })
    assert auto.status_code == 200
    auto_body = auto.json()
    assert auto_body['action'] in {'wait', 'end', 'send'}

    intent_msg = client.post('/v1/reply', json={
        'conversation_id': 'conv_test_intent_2',
        'merchant_id': merchant_id,
        'customer_id': None,
        'from_role': 'merchant',
        'message': "Okay, let's do it. What's next?",
        'received_at': '2026-04-26T10:42:00Z',
        'turn_number': 1,
    })
    assert intent_msg.status_code == 200
    intent_payload = intent_msg.json()
    assert intent_payload['action'] == 'send'
    assert 'next' in intent_payload['body'].lower() or 'draft' in intent_payload['body'].lower()
