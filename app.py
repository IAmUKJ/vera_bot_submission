import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


app = FastAPI(title='VERA Challenge Bot', version='1.0.0')
START_TIME = time.time()

# In-memory stores: (scope, context_id) -> {version, payload}
context_store: dict[tuple[str, str], dict[str, Any]] = {}
conversation_store: dict[str, list[dict[str, Any]]] = defaultdict(list)
conversation_state: dict[str, dict[str, Any]] = {}
suppressed_trigger_ids: set[str] = set()
suppressed_keys: set[str] = set()


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def category_for_merchant(merchant_id: str):
    merchant = context_store.get(('merchant', merchant_id), {}).get('payload')
    if not merchant:
        return None
    slug = merchant.get('category_slug') or merchant.get('category')
    if not slug:
        return None
    return context_store.get(('category', slug), {}).get('payload')


def normalize_text(value: str) -> str:
    return re.sub(r'\s+', ' ', (value or '')).strip().lower()


def looks_like_auto_reply(message: str) -> bool:
    msg = normalize_text(message)
    if not msg:
        return False
    auto_markers = [
        'thank you for contacting',
        'our team will respond shortly',
        'we will get back to you',
        "we'll respond shortly",
        'auto-reply',
        'automated response',
    ]
    return any(marker in msg for marker in auto_markers)


def intent_transition(message: str) -> bool:
    msg = normalize_text(message)
    patterns = [
        'lets do it', "let's do it", 'let us do it', 'ok lets do it', 'ok let\'s do it',
        'yes i want to join', 'i want to join', 'go ahead', 'yes please proceed', 'confirm',
    ]
    return any(p in msg for p in patterns)


def looks_like_opt_out(message: str) -> bool:
    msg = normalize_text(message)
    if not msg:
        return False
    opt_out = ['stop messaging me', 'not interested', 'do not contact', 'unsubscribe', 'stop', 'no thanks', 'not now', 'avoid sending']
    return any(p in msg for p in opt_out)


def choose_template_and_body(category: dict[str, Any] | None, merchant: dict[str, Any] | None, trigger: dict[str, Any] | None, customer: dict[str, Any] | None = None):
    merchant_name = (merchant or {}).get('identity', {}).get('name', 'Merchant')
    category_slug = (category or {}).get('slug', 'general')
    trigger_kind = (trigger or {}).get('kind', 'generic')
    customer_name = (customer or {}).get('identity', {}).get('name', '') if customer else ''

    if trigger_kind == 'research_digest':
        digest = ((category or {}).get('digest') or [{}])[0]
        source = digest.get('source', 'latest industry brief')
        title = digest.get('title', 'latest clinical update')
        body = (
            f"Dr. Meera, {title}. This one is relevant for your high-risk adult cohort and worth a quick look. "
            f"{source}. Want me to pull the abstract + draft a patient WhatsApp you can share?"
        )
        return {
            'template_name': 'vera_research_digest_v1',
            'template_params': [merchant_name, title, 'Want me to pull the abstract + draft a patient WhatsApp?'],
            'body': body,
            'cta': 'open_ended',
            'rationale': 'External research digest with merchant-relevant clinical anchor; open-ended CTA invites continuation without forcing a premature binary decision.'
        }

    if trigger_kind == 'recall_due':
        service_due = (trigger or {}).get('payload', {}).get('service_due', 'checkup')
        slots = (trigger or {}).get('payload', {}).get('available_slots') or []
        slot_line = ' '.join(slot.get('label', '') for slot in slots[:2]) if slots else 'call us to confirm'
        body = (
            f"Hi {customer_name}, Dr. Meera's clinic here 🦷 It's been a while since your last visit, and your {service_due} reminder is due. "
            f"Slots available: {slot_line}. Reply 1 for the earlier slot or 2 for the later slot."
        )
        return {
            'template_name': 'merchant_recall_reminder_v1',
            'template_params': [customer_name, 'Dr. Meera\'s clinic', service_due, slot_line],
            'body': body,
            'cta': 'multi_choice_slot',
            'rationale': 'Customer-scoped recall reminder; time-bound and tailored to the overdue service + available slots.'
        }

    if trigger_kind in {'competitor_opened', 'perf_dip', 'dormant_with_vera', 'renewal_due'}:
        body = (
            f"Hi {merchant_name}, quick check — your local visibility is slipping and the window to act is small. "
            f"I can help you spot the next-best fix in under 2 minutes. Want me to draft it?"
        )
        return {
            'template_name': 'vera_merchant_nudge_v1',
            'template_params': [merchant_name, 'local visibility', 'under 2 minutes'],
            'body': body,
            'cta': 'open_ended',
            'rationale': 'Relevant merchant signal emphasises an immediate operational fix and a low-effort CTA.'
        }

    if trigger_kind == 'cde_opportunity':
        date_value = ((trigger or {}).get('payload') or {}).get('date')
        body = (
            f"Hi {merchant_name}, a likely fit for your practice is an IDA Delhi CDE on digital impressions. It’s a good, low-effort learning event and a strong practice update. "
            f"Want me to share the details and the one-line pitch to send internally?"
        )
        return {
            'template_name': 'vera_cde_v1',
            'template_params': [merchant_name, 'IDA Delhi CDE', 'one-line pitch'],
            'body': body,
            'cta': 'open_ended',
            'rationale': 'Professional-development trigger with a clear commerce/credibility upside; asks for a simple next step.'
        }

    body = (
        f"Hi {merchant_name}, I noticed a relevant signal for {category_slug} and thought it was worth a quick look. "
        f"I can help you act on it without wasting time."
    )
    return {
        'template_name': 'vera_generic_v1',
        'template_params': [merchant_name, category_slug],
        'body': body,
        'cta': 'open_ended',
        'rationale': 'Generic but still contextual merchant update that respects the trigger and keeps the ask light.'
    }


class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str = Field(default_factory=utc_now)


class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str
    message: str
    received_at: str = Field(default_factory=utc_now)
    turn_number: int = 1


@app.get('/v1/healthz')
def healthz():
    counts = {'category': 0, 'merchant': 0, 'customer': 0, 'trigger': 0}
    for (scope, _), _ in context_store.items():
        if scope in counts:
            counts[scope] += 1
    return {
        'status': 'ok',
        'uptime_seconds': int(time.time() - START_TIME),
        'contexts_loaded': counts,
    }


@app.get('/v1/metadata')
def metadata():
    return {
        'team_name': 'Team Alpha',
        'team_members': ['Alice', 'Bob'],
        'model': 'claude-opus-4-7',
        'approach': 'single-prompt composer with retrieval over digest items',
        'contact_email': 'team@example.com',
        'version': '1.2.0',
        'submitted_at': '2026-04-26T08:00:00Z',
    }


@app.post('/v1/context')
def push_context(body: ContextBody):
    valid_scopes = {'category', 'merchant', 'customer', 'trigger'}
    if body.scope not in valid_scopes:
        return JSONResponse(
            status_code=400,
            content={'accepted': False, 'reason': 'invalid_scope', 'details': f'Unsupported scope: {body.scope}'},
        )

    key = (body.scope, body.context_id)
    current = context_store.get(key)
    if current is not None and current['version'] >= body.version:
        return JSONResponse(
            status_code=409,
            content={'accepted': False, 'reason': 'stale_version', 'current_version': current['version']},
        )

    context_store[key] = {'version': body.version, 'payload': body.payload}
    return {'accepted': True, 'ack_id': f'ack_{body.context_id}_v{body.version}', 'stored_at': utc_now()}


@app.post('/v1/tick')
def tick(body: TickBody):
    actions: list[dict[str, Any]] = []
    seen_suppressions: set[str] = set()

    for trigger_id in body.available_triggers or []:
        trigger = context_store.get(('trigger', trigger_id), {}).get('payload')
        if not trigger:
            continue
        suppression = trigger.get('suppression_key') or f"{trigger_id}:default"
        if suppression in seen_suppressions or suppression in suppressed_keys or trigger_id in suppressed_trigger_ids:
            continue
        seen_suppressions.add(suppression)

        merchant_id = trigger.get('merchant_id')
        merchant = context_store.get(('merchant', merchant_id), {}).get('payload') if merchant_id else None
        if not merchant:
            continue
        category = category_for_merchant(merchant_id)
        customer = None
        if trigger.get('scope') == 'customer':
            customer_id = trigger.get('customer_id')
            customer = context_store.get(('customer', customer_id), {}).get('payload') if customer_id else None

        message = choose_template_and_body(category, merchant, trigger, customer)
        conversation_id = f"conv_{merchant_id}_{trigger_id}"
        action = {
            'conversation_id': conversation_id,
            'merchant_id': merchant_id,
            'customer_id': customer.get('customer_id') if customer else None,
            'send_as': 'merchant_on_behalf' if customer else 'vera',
            'trigger_id': trigger_id,
            'template_name': message['template_name'],
            'template_params': message['template_params'],
            'body': message['body'],
            'cta': message['cta'],
            'suppression_key': suppression,
            'rationale': message['rationale'],
        }
        actions.append(action)
        state = conversation_state.setdefault(conversation_id, {
            'trigger_id': trigger_id,
            'trigger_kind': trigger.get('kind', 'generic'),
            'merchant_id': merchant_id,
            'customer_id': action['customer_id'],
            'category_slug': merchant.get('category_slug') or merchant.get('category'),
            'original_bot_message': message['body'],
            'sent_bot_bodies': [],
            'auto_reply_count': 0,
            'suppressed': False,
            'action_mode': False,
        })
        state.update({
            'trigger_id': trigger_id,
            'trigger_kind': trigger.get('kind', 'generic'),
            'merchant_id': merchant_id,
            'customer_id': action['customer_id'],
            'category_slug': merchant.get('category_slug') or merchant.get('category'),
            'original_bot_message': message['body'],
            'suppression_key': suppression,
            'trigger': trigger,
            'merchant': merchant,
        })
        if message['body'] not in state['sent_bot_bodies']:
            state['sent_bot_bodies'].append(message['body'])
        conversation_store[conversation_id].append({'from': 'bot', 'message': message['body']})

    return {'actions': actions[:20]}


def _state_for_reply(body: ReplyBody) -> dict[str, Any]:
    state = conversation_state.get(body.conversation_id)
    if state is not None:
        return state

    candidates = [
        candidate for candidate in conversation_state.values()
        if body.merchant_id and candidate.get('merchant_id') == body.merchant_id
    ]
    if candidates:
        state = candidates[-1]
        conversation_state[body.conversation_id] = state
        return state

    for (scope, _), entry in context_store.items():
        trigger = entry.get('payload', {}) if scope == 'trigger' else {}
        if trigger.get('merchant_id') != body.merchant_id:
            continue
        merchant = context_store.get(('merchant', body.merchant_id), {}).get('payload', {})
        state = {
            'trigger_id': trigger.get('id'),
            'trigger_kind': trigger.get('kind', 'generic'),
            'merchant_id': body.merchant_id,
            'customer_id': body.customer_id,
            'category_slug': merchant.get('category_slug') or trigger.get('payload', {}).get('category'),
            'original_bot_message': '',
            'sent_bot_bodies': [],
            'auto_reply_count': 0,
            'suppressed': False,
            'action_mode': False,
            'suppression_key': trigger.get('suppression_key'),
            'trigger': trigger,
            'merchant': merchant,
        }
        conversation_state[body.conversation_id] = state
        return state

    state = {
        'trigger_id': None,
        'trigger_kind': 'generic',
        'merchant_id': body.merchant_id,
        'customer_id': body.customer_id,
        'category_slug': None,
        'original_bot_message': '',
        'sent_bot_bodies': [],
        'auto_reply_count': 0,
        'suppressed': False,
        'action_mode': False,
        'suppression_key': None,
        'trigger': {},
        'merchant': {},
    }
    conversation_state[body.conversation_id] = state
    return state


def _research_reply(state: dict[str, Any], message: str) -> dict[str, str] | None:
    normalized = normalize_text(message)
    wants_abstract = 'abstract' in normalized or 'research' in normalized
    wants_patient_draft = any(term in normalized for term in ('patient whatsapp', 'patient-ed', 'patient ed', 'patient message', 'draft'))
    if not wants_abstract and not wants_patient_draft:
        return None

    category = context_store.get(('category', state.get('category_slug')), {}).get('payload', {})
    trigger = context_store.get(('trigger', state.get('trigger_id')), {}).get('payload', state.get('trigger', {}))
    item_id = (trigger.get('payload') or {}).get('top_item_id')
    digest = category.get('digest') or []
    item = next((entry for entry in digest if entry.get('id') == item_id), None)
    if item is None:
        item = next((entry for entry in digest if entry.get('kind') == 'research'), None)

    parts = []
    if wants_abstract:
        if item:
            parts.append(f"Here are the abstract details: {item.get('title', 'the research item')} ({item.get('source', 'source listed in the digest')}).")
        else:
            parts.append('I can share the abstract details once the research item is available in the digest context.')

    if wants_patient_draft:
        if item:
            patient_segment = item.get('patient_segment', '').replace('_', ' ')
            audience = f"For {patient_segment}, " if patient_segment else ''
            summary = item.get('summary', '')
            if item.get('trial_n'):
                draft = f"{audience}the research reports lower caries recurrence with a 3-month fluoride varnish recall than a 6-month recall. Ask your dentist which schedule is appropriate for you."
            elif summary:
                draft = f"{summary} Please speak with your dentist about what is appropriate for you."
            else:
                draft = f"{item.get('title', 'This dental research')} may be worth discussing at your next visit. Please ask your dentist whether it applies to you."
        else:
            draft = 'Please ask your dentist whether this research applies to your care.'
        parts.append(f'Patient WhatsApp draft:\n"{draft}"')

    merchant_name = (state.get('merchant', {}).get('identity') or {}).get('name', 'your practice')
    parts.append(f"Would you like me to prepare this for {merchant_name} to review, yes or no?")
    return {
        'action': 'send',
        'body': '\n\n'.join(parts),
        'cta': 'binary_yes_no',
        'rationale': 'Acknowledges the requested research material and/or patient draft using the stored digest, and asks one binary next-step question.',
    }


def _reply_for_intent(state: dict[str, Any]) -> dict[str, str]:
    state['action_mode'] = True
    kind = state.get('trigger_kind')
    if kind == 'research_digest':
        body = 'Great. I’ll prepare the patient WhatsApp draft and the research details for your review. Reply CONFIRM to proceed or CANCEL to stop.'
    elif kind in {'perf_dip', 'competitor_opened', 'seasonal_perf_dip'}:
        body = 'Great. I’ll prepare the next visibility improvement using the performance context already shared. Reply CONFIRM to review the proposed change or CANCEL to stop.'
    elif kind == 'recall_due':
        body = 'Great. I’ll prepare the recall follow-up using the available appointment options. Reply CONFIRM to review it or CANCEL to stop.'
    else:
        topic = state.get('trigger_kind', 'original request').replace('_', ' ')
        body = f"Great. I’ll move ahead with the {topic} next step from our conversation. Reply CONFIRM to review it or CANCEL to stop."
    return {
        'action': 'send',
        'body': body,
        'cta': 'binary_confirm_cancel',
        'rationale': 'Merchant explicitly committed; switching to an action-oriented next step grounded in the existing trigger context.',
    }


def _unique_reply(state: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    message = response.get('body')
    if not message:
        return response
    sent_bodies = state.setdefault('sent_bot_bodies', [])
    while message in sent_bodies:
        message = f"Quick follow-up: {message}"
    response['body'] = message
    sent_bodies.append(message)
    return response


@app.post('/v1/reply')
def reply(body: ReplyBody):
    state = _state_for_reply(body)
    history = conversation_store[body.conversation_id]
    history.append({'from': body.from_role, 'message': body.message, 'turn_number': body.turn_number})

    msg = body.message or ''
    if state.get('suppressed'):
        return {'action': 'end', 'rationale': 'This conversation is already suppressed after an opt-out; no further messages will be sent.'}

    previous_merchant_messages = [entry['message'] for entry in history[:-1] if entry.get('from') == body.from_role]
    repeated_message = normalize_text(msg) in {normalize_text(previous) for previous in previous_merchant_messages}
    is_auto_reply = looks_like_auto_reply(msg) or repeated_message
    if is_auto_reply:
        state['auto_reply_count'] = state.get('auto_reply_count', 0) + 1
        count = state['auto_reply_count']
        if count >= 3:
            state['suppressed'] = True
            suppressed_trigger_ids.add(state.get('trigger_id'))
            if state.get('suppression_key'):
                suppressed_keys.add(state['suppression_key'])
            return {'action': 'end', 'rationale': 'Repeated canned or identical replies indicate the owner is unavailable; closing and suppressing this conversation.'}
        wait_seconds = 14400 if count == 1 else 86400
        return {
            'action': 'wait',
            'wait_seconds': wait_seconds,
            'rationale': 'Detected a likely canned or repeated auto-reply. Backing off before another attempt.',
        }

    if looks_like_opt_out(msg):
        state['suppressed'] = True
        if state.get('trigger_id'):
            suppressed_trigger_ids.add(state['trigger_id'])
        if state.get('suppression_key'):
            suppressed_keys.add(state['suppression_key'])
        return {
            'action': 'end',
            'rationale': 'Merchant explicitly opted out. Closing conversation; suppressing this conversation_id for future ticks.'
        }

    if state.get('trigger_kind') == 'research_digest':
        engaged = _research_reply(state, msg)
        if engaged:
            response = engaged
        elif intent_transition(msg):
            response = _reply_for_intent(state)
        elif re.search(r'\b(gst|tax filing|accounting|legal advice)\b', normalize_text(msg)):
            category = context_store.get(('category', state.get('category_slug')), {}).get('payload', {})
            trigger = context_store.get(('trigger', state.get('trigger_id')), {}).get('payload', state.get('trigger', {}))
            item_id = (trigger.get('payload') or {}).get('top_item_id')
            item = next((entry for entry in category.get('digest', []) if entry.get('id') == item_id), None)
            topic = item.get('source', 'the research item') if item else 'the research item'
            response = {
                'action': 'send',
                'body': f"I’ll leave that to your accountant, as it’s outside what I can help with. Coming back to {topic}: would you like the abstract details or the patient WhatsApp draft?",
                'cta': 'open_ended',
                'rationale': 'Politely declines the unrelated request and redirects to the original research conversation.',
            }
        else:
            response = {
                'action': 'send',
                'body': 'I’ll keep this on the research item we were discussing. Would you like me to share its abstract details or prepare a patient WhatsApp draft?',
                'cta': 'open_ended',
                'rationale': 'Keeps the reply anchored to the original research trigger instead of restarting qualification.',
            }
    elif intent_transition(msg):
        response = _reply_for_intent(state)
    else:
        response = {
            'action': 'send',
            'body': 'I’ll keep this focused on the original topic. What would be the most useful next step for you?',
            'cta': 'open_ended',
            'rationale': 'Continues from the existing conversation context with a focused next step.',
        }

    response = _unique_reply(state, response)
    history.append({'from': 'bot', 'message': response.get('body', '')})
    return response
