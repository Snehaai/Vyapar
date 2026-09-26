#!/usr/bin/env python3
"""
Vyapar Bot — magicpin AI Challenge
Submitted by: Sneha Manohar NSUT 2023UCA1930 (snehamanohar068@gmail.com)
"""

import os
import time
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from composer import compose_message
from reply_handler import handle_reply
from state import ContextStore, ConversationStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vyapar")

app = FastAPI(title="Vyapar Bot", version="1.0.0")
START_TIME = time.time()

ctx  = ContextStore()
conv = ConversationStore()
fired_suppressions:   set[str]  = set()
active_conversations: dict[str, dict] = {}

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


# ── health ────────────────────────────────────────────────────

@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "contexts_loaded": ctx.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "bot_name":     "Vyapar",
        "submitted_by":  ["Sneha Manohar"],
        "university":      "NSUT",
        "university_id":   "2023UCA1930",
        "university_email": "sneha.manohar.ug23@nsut.ac.in",
        "contact_email": "snehamanohar068@gmail.com",
        "model":         "gemini-2.0-flash",
        "fallback":      "llama-3.3-70b-versatile (groq)",
        "approach":      "Trigger-kind dispatcher → grounded per-kind prompt → Gemini/Groq at temp=0 → CTA validation → intent-aware reply handler",
        "version":       "1.0.0",
        "submitted_at":  datetime.now(timezone.utc).isoformat(),
    }


# ── context ───────────────────────────────────────────────────

class ContextBody(BaseModel):
    scope:        str
    context_id:   str
    version:      int
    payload:      dict[str, Any]
    delivered_at: str


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope"})

    existing = ctx.get_version(body.scope, body.context_id)
    if existing is not None and existing >= body.version:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": existing})

    ctx.set(body.scope, body.context_id, body.version, body.payload)
    log.info("context stored: %s/%s v%d", body.scope, body.context_id, body.version)
    return {
        "accepted":  True,
        "ack_id":    f"ack_{body.context_id}_v{body.version}",
        "stored_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


# ── tick ──────────────────────────────────────────────────────

class TickBody(BaseModel):
    now:                str
    available_triggers: list[str] = Field(default_factory=list)


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions = []

    # sort by urgency descending
    trigger_payloads = []
    for tid in body.available_triggers:
        trg = ctx.get("trigger", tid)
        if trg:
            trigger_payloads.append((tid, trg))
    trigger_payloads.sort(key=lambda x: x[1].get("urgency", 1), reverse=True)

    for tid, trg in trigger_payloads:
        # expiry check
        expires = trg.get("expires_at", "")
        if expires:
            try:
                exp = datetime.fromisoformat(expires.replace("Z", "+00:00"))
                now = datetime.fromisoformat(body.now.replace("Z", "+00:00"))
                if now > exp:
                    continue
            except Exception:
                pass

        sup_key = trg.get("suppression_key", "")
        if sup_key and sup_key in fired_suppressions:
            continue

        merchant_id = trg.get("merchant_id")
        customer_id = trg.get("customer_id")
        if not merchant_id:
            continue

        merchant = ctx.get("merchant", merchant_id)
        if not merchant:
            continue

        category = ctx.get("category", merchant.get("category_slug", "")) or {}
        customer = ctx.get("customer", customer_id) if customer_id else None

        conv_id = f"conv_{merchant_id}_{tid}"
        if conv_id in active_conversations:
            continue

        try:
            result = compose_message(category, merchant, trg, customer)
        except Exception as e:
            log.error("compose failed %s: %s", tid, e, exc_info=True)
            continue

        if not result:
            continue

        if sup_key:
            fired_suppressions.add(sup_key)

        active_conversations[conv_id] = {
            "merchant_id":    merchant_id,
            "customer_id":    customer_id,
            "trigger_id":     tid,
            "last_bot_body":  result["body"],
            "auto_reply_count": 0,
            "merchant_intent":  "neutral",
        }
        conv.add_turn(conv_id, "bot", result["body"])

        actions.append({
            "conversation_id": conv_id,
            "merchant_id":     merchant_id,
            "customer_id":     customer_id,
            "send_as":         result.get("send_as", "vera"),
            "trigger_id":      tid,
            "template_name":   f"vyapar_{trg.get('kind','generic')}_v1",
            "template_params": result.get("template_params", []),
            "body":            result["body"],
            "cta":             result.get("cta", "open_ended"),
            "suppression_key": sup_key,
            "rationale":       result.get("rationale", ""),
        })

        if len(actions) >= 20:
            break

    log.info("tick: %d actions", len(actions))
    return {"actions": actions}


# ── reply ─────────────────────────────────────────────────────

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id:     Optional[str] = None
    customer_id:     Optional[str] = None
    from_role:       str
    message:         str
    received_at:     str
    turn_number:     int


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    conv_id = body.conversation_id
    conv.add_turn(conv_id, body.from_role, body.message)

    state = active_conversations.setdefault(conv_id, {
        "merchant_id": body.merchant_id, "customer_id": body.customer_id,
        "trigger_id": None, "last_bot_body": "",
        "auto_reply_count": 0, "merchant_intent": "neutral",
    })

    merchant = ctx.get("merchant", body.merchant_id) if body.merchant_id else {}
    category = ctx.get("category", (merchant or {}).get("category_slug", "")) or {}
    customer = ctx.get("customer", body.customer_id) if body.customer_id else None
    history  = conv.get_turns(conv_id)

    result = handle_reply(
        message=body.message, conv_state=state,
        history=history, category=category,
        merchant=merchant or {}, customer=customer,
    )

    if result["action"] == "send":
        state["last_bot_body"] = result["body"]
        conv.add_turn(conv_id, "bot", result["body"])
    elif result["action"] in ("end", "wait"):
        state["merchant_intent"] = result["action"]

    log.info("reply %s → %s", conv_id, result["action"])
    return result


# ── teardown ──────────────────────────────────────────────────

@app.post("/v1/teardown")
async def teardown():
    ctx.clear(); conv.clear()
    active_conversations.clear(); fired_suppressions.clear()
    return {"status": "wiped"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("bot:app", host="0.0.0.0", port=int(os.getenv("PORT", 8080)), reload=False)
