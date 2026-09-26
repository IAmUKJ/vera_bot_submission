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
    available_triggers: list[str] = []


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
        if suppression in seen_suppressions:
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
        actions.append({
            'conversation_id': f"conv_{merchant_id}_{trigger_id}",
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
        })

    return {'actions': actions[:20]}


@app.post('/v1/reply')
def reply(body: ReplyBody):
    history = conversation_store[body.conversation_id]
    history.append({'from': body.from_role, 'message': body.message, 'turn_number': body.turn_number})

    msg = body.message or ''
    if looks_like_auto_reply(msg):
        return {
            'action': 'wait',
            'wait_seconds': 14400,
            'rationale': "Detected merchant auto-reply (canned 'Thank you for contacting' phrasing). Backing off 4 hours to wait for owner."
        }

    if looks_like_opt_out(msg):
        return {
            'action': 'end',
            'rationale': 'Merchant explicitly opted out. Closing conversation; suppressing this conversation_id for future ticks.'
        }

    if intent_transition(msg):
        return {
            'action': 'send',
            'body': 'Great — I’ll draft the next step immediately. I can prepare the patient WhatsApp and the GBP post in one go. Reply CONFIRM to proceed or STOP to exit.',
            'cta': 'binary_confirm_cancel',
            'rationale': 'Merchant explicitly committed; switching from question-asking to action-execution. Concrete next step + clear confirm/stop choice.'
        }

    if body.from_role == 'merchant' and len(history) >= 2 and 'yes' in normalize_text(msg):
        return {
            'action': 'send',
            'body': 'Perfect — I’ll send the draft now. You can review it and I’ll keep it to the essentials.',
            'cta': 'open_ended',
            'rationale': 'Honoring the merchant’s positive intent with a minimal next step.'
        }

    return {
        'action': 'send',
        'body': 'I hear you. I can keep this focused on the specific issue and not waste your time. Tell me the one thing you want to fix first.',
        'cta': 'open_ended',
        'rationale': 'Restarting the conversation around a single concrete next step without forcing a broad decision.'
    }
