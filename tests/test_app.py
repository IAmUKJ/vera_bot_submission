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


def seed_research_conversation(suffix):
    category_id = f'dentists_{suffix}'
    merchant_id = f'merchant_{suffix}'
    trigger_id = f'trigger_{suffix}'
    category = {
        'slug': category_id,
        'digest': [{
            'id': f'digest_{suffix}',
            'kind': 'research',
            'title': '3-month fluoride varnish recall outperforms 6-month for high-risk adult caries',
            'source': 'JIDA Oct 2026, p.14',
            'trial_n': 2100,
            'patient_segment': 'high_risk_adults',
            'summary': 'Multi-center Indian trial shows 38% lower caries recurrence with 3-month vs 6-month recall in adults with active decay history.',
        }],
    }
    merchant = {
        'merchant_id': merchant_id,
        'category_slug': category_id,
        'identity': {'name': "Dr. Meera's Dental Clinic"},
    }
    trigger = {
        'id': trigger_id,
        'kind': 'research_digest',
        'scope': 'merchant',
        'merchant_id': merchant_id,
        'payload': {'category': category_id, 'top_item_id': f'digest_{suffix}'},
        'suppression_key': f'research:{suffix}',
    }
    for scope, context_id, payload in (
        ('category', category_id, category),
        ('merchant', merchant_id, merchant),
        ('trigger', trigger_id, trigger),
    ):
        result = client.post('/v1/context', json={
            'scope': scope,
            'context_id': context_id,
            'version': 1,
            'payload': payload,
        })
        assert result.status_code == 200

    result = client.post('/v1/tick', json={'now': '2026-09-26T10:00:00Z', 'available_triggers': [trigger_id]})
    assert result.status_code == 200
    return result.json()['actions'][0]['conversation_id'], merchant_id, trigger_id


def post_merchant_reply(conversation_id, merchant_id, message, turn_number):
    return client.post('/v1/reply', json={
        'conversation_id': conversation_id,
        'merchant_id': merchant_id,
        'customer_id': None,
        'from_role': 'merchant',
        'message': message,
        'received_at': '2026-09-26T10:42:00Z',
        'turn_number': turn_number,
    })


def test_research_engaged_reply_honors_abstract_and_patient_whatsapp():
    conversation_id, merchant_id, _ = seed_research_conversation('engaged')

    response = post_merchant_reply(
        conversation_id,
        merchant_id,
        'Yes please send the abstract. Also draft the patient WhatsApp.',
        2,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload['action'] == 'send'
    assert payload['cta'] == 'binary_yes_no'
    assert 'abstract' in payload['body'].lower()
    assert 'patient whatsapp draft' in payload['body'].lower()
    assert 'JIDA Oct 2026, p.14' in payload['body']
    assert '3-month fluoride varnish recall' in payload['body']
    assert 'yes or no?' in payload['body'].lower()


def test_auto_reply_waits_then_backs_off_and_ends():
    conversation_id, merchant_id, _ = seed_research_conversation('auto')
    auto_message = 'Thank you for contacting Dr. Meera. Our team will respond shortly.'

    first = post_merchant_reply(conversation_id, merchant_id, auto_message, 2).json()
    second = post_merchant_reply(conversation_id, merchant_id, auto_message, 3).json()
    third = post_merchant_reply(conversation_id, merchant_id, auto_message, 4).json()

    assert first['action'] == 'wait'
    assert first['wait_seconds'] == 14400
    assert second['action'] == 'wait'
    assert second['wait_seconds'] == 86400
    assert third['action'] == 'end'


def test_hard_no_suppresses_trigger_on_future_ticks():
    conversation_id, merchant_id, trigger_id = seed_research_conversation('optout')

    response = post_merchant_reply(conversation_id, merchant_id, 'Not interested. Stop messaging me.', 2)
    tick = client.post('/v1/tick', json={'now': '2026-09-26T11:00:00Z', 'available_triggers': [trigger_id]})

    assert response.json()['action'] == 'end'
    assert tick.status_code == 200
    assert tick.json()['actions'] == []


def test_intent_transition_uses_action_mode_without_qualifying():
    conversation_id, merchant_id, _ = seed_research_conversation('intent')

    payload = post_merchant_reply(conversation_id, merchant_id, "Let's do it", 2).json()

    assert payload['action'] == 'send'
    assert payload['cta'] == 'binary_confirm_cancel'
    assert 'confirm' in payload['body'].lower()
    assert 'what would be useful' not in payload['body'].lower()


def test_gst_curveball_redirects_to_original_research_context():
    conversation_id, merchant_id, _ = seed_research_conversation('curveball')

    payload = post_merchant_reply(conversation_id, merchant_id, 'Can you also help me with my GST filing?', 2).json()

    assert payload['action'] == 'send'
    assert 'accountant' in payload['body'].lower()
    assert 'JIDA Oct 2026, p.14' in payload['body']
    assert 'abstract' in payload['body'].lower()


def test_bot_does_not_repeat_exact_body_in_conversation():
    conversation_id, merchant_id, _ = seed_research_conversation('repeat')

    first = post_merchant_reply(conversation_id, merchant_id, 'Tell me more about that.', 2).json()
    second = post_merchant_reply(conversation_id, merchant_id, 'What else should I know?', 3).json()

    assert first['action'] == 'send'
    assert second['action'] == 'send'
    assert first['body'] != second['body']
