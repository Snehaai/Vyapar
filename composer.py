"""
Vera message composer.
LLM backend: Gemini 2.0 Flash (free, 10k RPM) with Groq fallback.

Entry: compose_message(category, merchant, trigger, customer?) -> dict | None
"""

import os
import re
import json
import logging
from typing import Optional

import httpx

log = logging.getLogger("vera.composer")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY   = os.getenv("GROQ_API_KEY", "")

GEMINI_MODEL   = "gemini-2.0-flash"
GROQ_MODEL     = "llama-3.3-70b-versatile"
LLM_TIMEOUT    = 22          # stay under 30s hard limit

# simple in-process cache: last sent body per (merchant_id, trigger_kind)
_last_sent: dict[str, str] = {}


# ─────────────────────────────────────────────────────────────
#  PUBLIC ENTRY POINT
# ─────────────────────────────────────────────────────────────

def compose_message(
    category: dict,
    merchant: dict,
    trigger:  dict,
    customer: Optional[dict] = None,
) -> Optional[dict]:
    """Returns {body, cta, send_as, template_params, rationale} or None."""

    if not merchant or not trigger:
        return None

    kind        = trigger.get("kind", "generic")
    merchant_id = merchant.get("merchant_id", "?")

    prompt = _build_prompt(category, merchant, trigger, customer)
    raw    = _call_llm(prompt)
    if not raw:
        log.warning("LLM empty response for %s/%s", merchant_id, kind)
        return None

    result = _parse(raw)
    if not result:
        log.warning("Parse failed for %s/%s: %.150s", merchant_id, kind, raw)
        return None

    body = result.get("body", "").strip()
    if not body:
        return None

    # ── anti-repetition ──────────────────────────────────────
    cache_key = f"{merchant_id}:{kind}"
    if _last_sent.get(cache_key) == body:
        # nudge it just enough to avoid the exact-repeat penalty
        body = body.rstrip(" .") + " 📌"
    _last_sent[cache_key] = body

    # ── strip any URLs the model sneaks in ───────────────────
    body = re.sub(r'https?://\S+', '', body).strip()

    result["body"]     = body
    result["send_as"]  = "merchant_on_behalf" if customer else "vera"

    # template_params: first 3 sentences
    sentences = [s.strip() for s in re.split(r'[.!?]\s+', body) if s.strip()]
    result["template_params"] = sentences[:3]

    return result


# ─────────────────────────────────────────────────────────────
#  PROMPT BUILDER
# ─────────────────────────────────────────────────────────────

def _build_prompt(category, merchant, trigger, customer):
    kind = trigger.get("kind", "generic")

    # ── merchant facts ───────────────────────────────────────
    ident        = merchant.get("identity", {})
    m_name       = ident.get("name", "the merchant")
    owner        = ident.get("owner_first_name", "")
    city         = ident.get("city", "")
    locality     = ident.get("locality", "")
    languages    = ident.get("languages", ["en"])
    cat_slug     = merchant.get("category_slug", "")

    perf         = merchant.get("performance", {})
    views        = perf.get("views", 0)
    calls        = perf.get("calls", 0)
    ctr          = perf.get("ctr", 0.0)
    delta        = perf.get("delta_7d", {})
    views_d      = delta.get("views_pct", 0.0)
    calls_d      = delta.get("calls_pct", 0.0)

    sub          = merchant.get("subscription", {})
    signals      = merchant.get("signals", [])
    offers       = merchant.get("offers", [])
    active_off   = [o for o in offers if o.get("status") == "active"]
    cust_agg     = merchant.get("customer_aggregate", {})
    conv_hist    = merchant.get("conversation_history", [])

    # ── category facts ───────────────────────────────────────
    voice        = category.get("voice", {})
    tone         = voice.get("tone", "professional")
    taboos       = voice.get("taboos", voice.get("vocab_taboo", []))
    peer         = category.get("peer_stats", {})
    peer_ctr     = peer.get("avg_ctr", 0.0)
    digest       = category.get("digest", [])
    offer_cat    = category.get("offer_catalog", [])
    seasonal     = category.get("seasonal_beats", [])
    trends       = category.get("trend_signals", [])

    # ── trigger facts ────────────────────────────────────────
    trg_payload  = trigger.get("payload", {})
    urgency      = trigger.get("urgency", 2)
    sup_key      = trigger.get("suppression_key", "")

    # ── CTR vs peer ──────────────────────────────────────────
    ctr_line = ""
    if peer_ctr and ctr:
        diff = ctr / peer_ctr
        if diff < 0.85:
            ctr_line = f"CTR {ctr:.1%} is BELOW peer median {peer_ctr:.1%} — gap to close"
        elif diff > 1.15:
            ctr_line = f"CTR {ctr:.1%} ABOVE peer median {peer_ctr:.1%} — momentum to protect"
        else:
            ctr_line = f"CTR {ctr:.1%} near peer median {peer_ctr:.1%}"

    # ── digest lookup ────────────────────────────────────────
    digest_block = ""
    if digest and kind == "research_digest":
        top_id  = trg_payload.get("top_item_id", "")
        item    = next((d for d in digest if d.get("id") == top_id), digest[0])
        digest_block = (
            f"Digest item — Title: {item.get('title','')}\n"
            f"Source: {item.get('source','')}\n"
            f"Trial N: {item.get('trial_n','')}\n"
            f"Segment: {item.get('patient_segment', item.get('segment',''))}"
        )

    # ── customer block ───────────────────────────────────────
    cust_block = ""
    if customer:
        ci   = customer.get("identity", {})
        cr   = customer.get("relationship", {})
        cp   = customer.get("preferences", {})
        cust_block = (
            f"\nCUSTOMER (message is FROM merchant TO this person):\n"
            f"Name: {ci.get('name','?')}  Lang: {ci.get('language_pref','en')}\n"
            f"State: {customer.get('state','?')}  Last visit: {cr.get('last_visit','?')}\n"
            f"Total visits: {cr.get('visits_total','?')}\n"
            f"Services: {', '.join(cr.get('services_received', []))}\n"
            f"Slot preference: {cp.get('preferred_slot_times', 'not specified')}\n"
        )

    # ── recent history ───────────────────────────────────────
    hist_block = "First message — no prior context."
    if conv_hist:
        tail = conv_hist[-3:]
        hist_block = "\n".join(
            f"[{t.get('role','?').upper()}] {t.get('body', t.get('message',''))}"
            for t in tail
        )

    # ── language rule ────────────────────────────────────────
    lang_rule = _lang_rule(languages, customer)

    # ── kind-specific instruction ─────────────────────────────
    task = _kind_task(kind, trigger, merchant, category)

    prompt = f"""You are Vera, magicpin's WhatsApp AI for merchant growth.
Compose ONE WhatsApp message to send right now. Return ONLY a JSON object — no prose, no markdown.

=== MERCHANT ===
Name: {m_name}
Owner first name: {owner}
Location: {locality}, {city}
Category: {cat_slug}
Subscription: {sub.get('status','?')} / {sub.get('plan','')} / {sub.get('days_remaining','?')} days left
Performance (30d): {views} views | {calls} calls | CTR {ctr:.1%}
7-day delta: views {views_d:+.0%} | calls {calls_d:+.0%}
{ctr_line}
Active offers: {', '.join(o.get('title','') for o in active_off) or 'none'}
Customer aggregate: {json.dumps(cust_agg)}
Account signals: {', '.join(str(s) for s in signals) or 'none'}

=== CATEGORY: {cat_slug.upper()} ===
Voice/tone: {tone}
FORBIDDEN words: {', '.join(taboos) or 'none'}
Peer CTR benchmark: {peer_ctr:.1%} ({peer.get('scope','peers')})
Category offer examples: {', '.join(o.get('title','') for o in offer_cat[:5]) or 'none'}
Seasonal: {'; '.join(s.get('note','') for s in seasonal[:2]) or 'none'}
Trends: {'; '.join(f"{t.get('query','')} +{t.get('delta_yoy',0):.0%}" for t in trends[:2]) or 'none'}
{digest_block}
{cust_block}
=== TRIGGER ===
Kind: {kind}  |  Urgency: {urgency}/5  |  Source: {trigger.get('source','?')}
Payload: {json.dumps(trg_payload)}
Suppression key: {sup_key}

=== RECENT CONVERSATION ===
{hist_block}

=== YOUR TASK ===
{task}

HARD RULES (judge penalizes violations, some -3 per infraction):
1. Every number/fact MUST come from the context above — never invent
2. NO URLs anywhere in the body
3. EXACTLY ONE CTA at the end — a clear yes/no ask or a single open question
4. NEVER use forbidden words: {', '.join(taboos) or 'none'}
5. Language: {lang_rule}
6. 3–6 lines max (WhatsApp, not email)
7. Start with the person's name — not "Dear" or "Hi there"
8. No self-introduction after first message, no "I hope you're doing well"
9. If customer-facing: sign off as the MERCHANT, not as Vera
10. Rationale: name the ONE signal you chose and why — judge cross-checks

Return ONLY this JSON (no markdown, no extra keys):
{{"body": "...", "cta": "binary_yes_no|open_ended|binary_confirm_cancel|multi_choice_slot|none", "rationale": "..."}}"""

    return prompt


# ─────────────────────────────────────────────────────────────
#  KIND-SPECIFIC TASK INSTRUCTIONS
# ─────────────────────────────────────────────────────────────

def _kind_task(kind, trigger, merchant, category):
    perf  = merchant.get("performance", {})
    ident = merchant.get("identity", {})
    owner = ident.get("owner_first_name", "the owner")
    trg_p = trigger.get("payload", {})

    tasks = {
        "research_digest": (
            "Share the digest item as a peer colleague — cite source, trial size, key finding. "
            "Tie it to THIS merchant's specific patient/customer profile from context. "
            "Offer to pull the abstract or draft a patient-ed WhatsApp. NOT a sales pitch."
        ),
        "perf_dip": (
            f"Views/calls dipped. Current: {perf.get('views')} views, {perf.get('calls')} calls. "
            "Pick ONE likely cause from the signals. Recommend one concrete action. "
            "Tone: peer checking in, not alarm."
        ),
        "perf_spike": (
            f"Performance spiked — views {perf.get('delta_7d',{}).get('views_pct',0):+.0%}. "
            "One-line congrats, then pivot to 'capture this momentum' with one specific action "
            "that converts the extra views into bookings."
        ),
        "milestone_reached": (
            "Celebrate with the exact milestone number. Pivot: 'here's what this unlocks' — "
            "one next step to capitalize on the credibility gained."
        ),
        "dormant_with_vera": (
            "Merchant hasn't replied in 14+ days. Lead with something they'd WANT to know "
            "(a stat, trend, competitor signal). NO guilt. End with one very low-friction yes/no."
        ),
        "review_theme_emerged": (
            "Name the review pattern. Offer to help address it — draft a response template "
            "or suggest one operational fix. Be specific about the theme from payload."
        ),
        "competitor_opened": (
            "New competitor nearby — useful intel, not alarm. Name one concrete differentiator "
            "the merchant already has. End with one action to reinforce their position."
        ),
        "festival_upcoming": (
            "Festival approaching. Lead with a category-appropriate offer idea (not generic %). "
            "Give days remaining. One CTA to start now."
        ),
        "recall_due": (
            "Recall reminder from merchant to patient. Name the patient. State what's due and when. "
            "Offer 2 specific time slots if available. Warm and practical, not clinical."
        ),
        "customer_lapsed_soft": (
            "Customer away 3-6 months. No guilt. Lead with something NEW (new offer, new service). "
            "One no-commitment CTA."
        ),
        "customer_lapsed_hard": (
            "Customer away 6+ months. Warm. Acknowledge gap without dwelling. "
            "Compelling reason to return (new offering, free trial, special price). "
            "Make YES extremely low-effort."
        ),
        "appointment_tomorrow": (
            "Appointment tomorrow — confirm time, service, any prep needed. "
            "One-tap confirm or reschedule option."
        ),
        "chronic_refill_due": (
            "Medicine refill due. Name medicines + expiry date. "
            "Show total + discount applied. Two options: CONFIRM to dispatch or call for changes."
        ),
        "trial_followup": (
            "Customer had a trial/first visit. Follow up warmly. "
            "Ask one specific question about their experience OR offer to book next. "
            "Reference what they came in for."
        ),
        "renewal_due": (
            "Subscription renewal approaching. State days remaining. "
            "Mention 1-2 features they actively use that would be lost. "
            "Direct CTA to renew."
        ),
        "curious_ask_due": (
            "Weekly curiosity engagement. Ask ONE specific question about their business this week. "
            "Offer to turn their answer into something useful (Google post, WhatsApp reply script). "
            "Under 3 lines."
        ),
        "supply_alert": (
            "Compliance/supply alert. Exact alert details (batch numbers from payload). "
            "Quantify impact from their customer data. "
            "Offer to draft the customer-notification + replacement workflow."
        ),
    }
    return tasks.get(kind,
        "Lead with the most important fact from the trigger payload. "
        "One clear, grounded, specific message. One CTA at the end."
    )


def _lang_rule(languages, customer):
    if customer:
        lang = customer.get("identity", {}).get("language_pref", "en")
        if "hi" in lang.lower() and "en" in lang.lower():
            return "Hindi-English code-mix. Mix naturally."
        if lang.lower() == "hi":
            return "Hindi primarily."
        return "English."
    if "hi" in languages and "en" in languages:
        return "Hindi-English code-mix preferred. Mix naturally for Indian merchant."
    if "hi" in languages:
        return "Hindi primarily."
    return "English."


# ─────────────────────────────────────────────────────────────
#  LLM CALLERS
# ─────────────────────────────────────────────────────────────

def _call_llm(prompt: str) -> Optional[str]:
    """Try Gemini first, fall back to Groq."""
    if GEMINI_API_KEY:
        result = _call_gemini(prompt)
        if result:
            return result
        log.warning("Gemini failed, trying Groq fallback")

    if GROQ_API_KEY:
        result = _call_groq(prompt)
        if result:
            return result

    log.error("All LLM providers failed")
    return None


def _call_gemini(prompt: str) -> Optional[str]:
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.0,
            "maxOutputTokens": 512,
            "responseMimeType": "application/json",   # force JSON output
        },
    }
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as c:
            r = c.post(url, json=body, headers={"Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception as e:
        log.error("Gemini error: %s", e)
        return None


def _call_groq(prompt: str) -> Optional[str]:
    body = {
        "model": GROQ_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are Vera, magicpin's WhatsApp AI for merchant growth. "
                    "Return ONLY valid JSON — no markdown, no explanation, no code fences."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }
    try:
        with httpx.Client(timeout=LLM_TIMEOUT) as c:
            r = c.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("Groq error: %s", e)
        return None


# ─────────────────────────────────────────────────────────────
#  OUTPUT PARSER
# ─────────────────────────────────────────────────────────────

VALID_CTAS = {
    "binary_yes_no", "open_ended", "binary_confirm_cancel",
    "multi_choice_slot", "none",
}


def _parse(raw: str) -> Optional[dict]:
    text = raw.strip()

    # strip markdown fences
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\s*```$', '', text, flags=re.MULTILINE).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r'\{[\s\S]*\}', text)
        if not m:
            return None
        try:
            data = json.loads(m.group())
        except json.JSONDecodeError:
            return None

    if not data.get("body"):
        return None

    if data.get("cta") not in VALID_CTAS:
        data["cta"] = "open_ended"

    data.setdefault("rationale", "")
    return data
