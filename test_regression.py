#!/usr/bin/env python3
"""
Regression tests — run before every deploy.
Usage: python test_regression.py
All tests must PASS before you submit.
"""
import os
import json
import sys

os.environ.setdefault("GEMINI_API_KEY", "test_key")
os.environ.setdefault("GROQ_API_KEY", "test_key")

from state import ContextStore, ConversationStore
from composer import _parse, _kind_task, _lang_rule, _build_prompt
from reply_handler import (
    handle_reply, _classify_intent, _is_auto_reply, _action_response
)

PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        print(f"  PASS  {name}")
        PASS += 1
    else:
        print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        FAIL += 1


# ─────────────────────────────────────────────────────────────
print("\n── ContextStore ─────────────────────────────────────────")
ctx = ContextStore()
check("get missing returns None", ctx.get("category", "dentists") is None)
ctx.set("category", "dentists", 1, {"slug": "dentists"})
check("get after set", ctx.get("category", "dentists") == {"slug": "dentists"})
check("version after set", ctx.get_version("category", "dentists") == 1)
ctx.set("category", "dentists", 2, {"slug": "dentists", "v": 2})
check("higher version replaces", ctx.get("category", "dentists").get("v") == 2)
ctx.set("merchant", "m001", 1, {"id": "m001"})
counts = ctx.counts()
check("count category=1", counts["category"] == 1)
check("count merchant=1", counts["merchant"] == 1)
check("count customer=0", counts["customer"] == 0)
all_cats = ctx.all_of_scope("category")
check("all_of_scope returns 1 item", len(all_cats) == 1)
ctx.clear()
check("clear wipes state", ctx.counts() == {"category": 0, "merchant": 0, "customer": 0, "trigger": 0})

# ─────────────────────────────────────────────────────────────
print("\n── ConversationStore ────────────────────────────────────")
cs = ConversationStore()
check("empty turns", cs.get_turns("c1") == [])
cs.add_turn("c1", "bot", "Hello")
cs.add_turn("c1", "merchant", "Yes")
turns = cs.get_turns("c1")
check("2 turns recorded", len(turns) == 2)
check("first turn role", turns[0]["role"] == "bot")
check("second turn body", turns[1]["body"] == "Yes")
check("different conv empty", cs.get_turns("c2") == [])
cs.clear()
check("clear wipes turns", cs.get_turns("c1") == [])

# ─────────────────────────────────────────────────────────────
print("\n── Auto-reply detection ─────────────────────────────────")
check("EN auto-reply 1", _is_auto_reply("Thank you for contacting us! Our team will respond shortly."))
check("EN auto-reply 2", _is_auto_reply("We have received your message and will get back to you soon."))
check("HI auto-reply",   _is_auto_reply("Aapki jaankari ke liye bahut-bahut shukriya. Main aapki"))
check("Real reply not flagged 1", not _is_auto_reply("Yes please send the abstract"))
check("Real reply not flagged 2", not _is_auto_reply("How much does the cleaning cost?"))
check("Real reply not flagged 3", not _is_auto_reply("Okay let's do it"))

# ─────────────────────────────────────────────────────────────
print("\n── Intent classifier ────────────────────────────────────")
check("commit: yes", _classify_intent("Yes") == "commit")
check("commit: go ahead", _classify_intent("Ok go ahead") == "commit")
check("commit: let's do it", _classify_intent("Let's do it. What's next?") == "commit")
check("commit: hindi haan", _classify_intent("Haan karo") == "commit")
check("commit: confirm", _classify_intent("Confirmed, please proceed") == "commit")
check("hostile: stop", _classify_intent("Stop messaging me. This is spam.") == "hostile")
check("hostile: not interested", _classify_intent("Not interested, please stop") == "hostile")
check("hostile: bakwaas", _classify_intent("Bakwaas hai ye sab") == "hostile")
check("wait: later", _classify_intent("Please contact me later") == "wait")
check("wait: not now", _classify_intent("Not now, abhi nahi") == "wait")
check("auto: auto-reply", _classify_intent("Thank you for contacting us! Our team will respond shortly.") == "auto")
check("plain reply", _classify_intent("What is the pricing?") == "reply")

# ─────────────────────────────────────────────────────────────
print("\n── Output parser ────────────────────────────────────────")
good = json.dumps({"body": "Hello Dr. Meera", "cta": "binary_yes_no", "rationale": "test"})
r = _parse(good)
check("valid json parsed", r is not None and r["body"] == "Hello Dr. Meera")
check("cta preserved", r and r["cta"] == "binary_yes_no")

fenced = "```json\n" + json.dumps({"body": "Hi", "cta": "none", "rationale": "x"}) + "\n```"
r2 = _parse(fenced)
check("fenced json parsed", r2 is not None and r2["body"] == "Hi")

bad_cta = json.dumps({"body": "Hi", "cta": "invalid_cta", "rationale": "x"})
r3 = _parse(bad_cta)
check("bad cta normalised to open_ended", r3 and r3["cta"] == "open_ended")

empty_body = json.dumps({"body": "", "cta": "none", "rationale": "x"})
r4 = _parse(empty_body)
check("empty body returns None", r4 is None)

garbage = "not json at all lol"
r5 = _parse(garbage)
check("garbage returns None", r5 is None)

embedded = 'Some text before {"body": "Hi!", "cta": "open_ended", "rationale": "ok"} and after'
r6 = _parse(embedded)
check("json embedded in prose extracted", r6 is not None and r6["body"] == "Hi!")

# ─────────────────────────────────────────────────────────────
MERCHANT = {
    "merchant_id": "m_001_drmeera",
    "category_slug": "dentists",
    "identity": {
        "name": "Dr. Meera's Dental Clinic",
        "owner_first_name": "Meera",
        "city": "Delhi",
        "locality": "Lajpat Nagar",
        "languages": ["en", "hi"],
    },
    "subscription": {"status": "active", "plan": "Pro", "days_remaining": 82},
    "performance": {
        "views": 2410, "calls": 18, "ctr": 0.021,
        "delta_7d": {"views_pct": 0.18, "calls_pct": -0.05},
    },
    "offers": [{"id": "o1", "title": "Dental Cleaning @ Rs 299", "status": "active"}],
    "customer_aggregate": {"total_unique_ytd": 540, "high_risk_adult_count": 124, "lapsed_180d_plus": 78},
    "signals": ["ctr_below_peer_median", "stale_posts"],
    "conversation_history": [],
}

CATEGORY = {
    "slug": "dentists",
    "voice": {"tone": "peer_clinical", "taboos": ["guaranteed", "cure"]},
    "peer_stats": {"avg_ctr": 0.030, "scope": "delhi_solo_practices"},
    "offer_catalog": [{"id": "d1", "title": "Dental Cleaning @ Rs 299", "audience": "new_user"}],
    "digest": [
        {"id": "d_2026W17_jida", "kind": "research",
         "title": "3-month fluoride recall cuts caries 38% better",
         "source": "JIDA Oct 2026, p.14", "trial_n": 2100, "patient_segment": "high_risk_adults"}
    ],
    "seasonal_beats": [{"month_range": "Nov-Feb", "note": "exam-stress bruxism spike"}],
    "trend_signals": [{"query": "clear aligners delhi", "delta_yoy": 0.62}],
}

TRIGGER = {
    "id": "trg_001",
    "scope": "merchant",
    "kind": "research_digest",
    "source": "external",
    "merchant_id": "m_001_drmeera",
    "customer_id": None,
    "payload": {"category": "dentists", "top_item_id": "d_2026W17_jida"},
    "urgency": 2,
    "suppression_key": "research:dentists:2026-W17",
    "expires_at": "2026-05-03T00:00:00Z",
}

# ─────────────────────────────────────────────────────────────
print("\n── Prompt builder ───────────────────────────────────────")
prompt = _build_prompt(CATEGORY, MERCHANT, TRIGGER, None)
check("prompt contains merchant name", "Dr. Meera" in prompt)
check("prompt contains peer CTR", "3.0%" in prompt or "0.030" in prompt or "3%" in prompt)
check("prompt contains taboos", "guaranteed" in prompt)
check("prompt contains digest title", "fluoride" in prompt or "caries" in prompt)
check("prompt contains trigger kind", "research_digest" in prompt)
check("prompt contains no-URL rule", "URL" in prompt or "http" in prompt.lower())
check("prompt requests JSON output", '"body"' in prompt and '"cta"' in prompt)

# ─────────────────────────────────────────────────────────────
print("\n── Reply handler: hostile ───────────────────────────────")
state = {"auto_reply_count": 0, "merchant_intent": "neutral", "last_bot_body": ""}
r = handle_reply("Stop messaging me. Useless spam.", state, [], CATEGORY, MERCHANT, None)
check("hostile → end", r["action"] == "end")

# ─────────────────────────────────────────────────────────────
print("\n── Reply handler: auto-reply ladder ─────────────────────")
state = {"auto_reply_count": 0, "merchant_intent": "neutral", "last_bot_body": ""}
auto_msg = "Thank you for contacting us! Our team will respond shortly."

r1 = handle_reply(auto_msg, state, [], CATEGORY, MERCHANT, None)
check("auto-reply #1 → send", r1["action"] == "send")
check("auto-reply count = 1", state["auto_reply_count"] == 1)
check("body not empty", len(r1.get("body", "")) > 5)

r2 = handle_reply(auto_msg, state, [], CATEGORY, MERCHANT, None)
check("auto-reply #2 → wait", r2["action"] == "wait")
check("auto-reply count = 2", state["auto_reply_count"] == 2)
check("wait ≥ 3600s", r2.get("wait_seconds", 0) >= 3600)

r3 = handle_reply(auto_msg, state, [], CATEGORY, MERCHANT, None)
check("auto-reply #3 → end", r3["action"] == "end")

# ─────────────────────────────────────────────────────────────
print("\n── Reply handler: commit transition ─────────────────────")
state = {"auto_reply_count": 0, "merchant_intent": "neutral", "last_bot_body": "earlier message"}
r = handle_reply("Yes let's do it. Go ahead.", state, [], CATEGORY, MERCHANT, None)
check("commit → send", r["action"] == "send")
check("state updated to committed", state["merchant_intent"] == "committed")
check("body has CTA", "CONFIRM" in r.get("body", "") or "confirm" in r.get("body", "").lower())
# Must NOT keep qualifying
body_lower = r.get("body", "").lower()
still_qualifying = any(w in body_lower for w in ["would you", "do you have", "can you tell", "what if"])
check("NOT still qualifying after commit", not still_qualifying)

# ─────────────────────────────────────────────────────────────
print("\n── Reply handler: wait ──────────────────────────────────")
state = {"auto_reply_count": 0, "merchant_intent": "neutral", "last_bot_body": ""}
r = handle_reply("Not now, baad mein baat karte hain", state, [], CATEGORY, MERCHANT, None)
check("wait → wait action", r["action"] == "wait")

# ─────────────────────────────────────────────────────────────
print("\n── Kind task coverage ───────────────────────────────────")
all_kinds = [
    "research_digest", "perf_dip", "perf_spike", "milestone_reached",
    "dormant_with_vera", "review_theme_emerged", "competitor_opened",
    "festival_upcoming", "recall_due", "customer_lapsed_soft",
    "customer_lapsed_hard", "appointment_tomorrow", "chronic_refill_due",
    "trial_followup", "renewal_due", "curious_ask_due", "supply_alert",
    "unknown_kind_xyz",  # fallback
]
for kind in all_kinds:
    t = {"kind": kind, "payload": {}, "urgency": 2}
    task_str = _kind_task(kind, t, MERCHANT, CATEGORY)
    check(f"task defined for {kind}", isinstance(task_str, str) and len(task_str) > 10)

# ─────────────────────────────────────────────────────────────
print("\n── Language rule ────────────────────────────────────────")
check("hi+en → mix",     "mix" in _lang_rule(["en", "hi"], None).lower())
check("hi only → hindi", "hindi" in _lang_rule(["hi"], None).lower())
check("en only → english","english" in _lang_rule(["en"], None).lower())
customer_hi = {"identity": {"language_pref": "hi-en mix"}}
check("customer hi-en mix", "mix" in _lang_rule(["en"], customer_hi).lower())

# ─────────────────────────────────────────────────────────────
print("\n── Bot endpoint imports ─────────────────────────────────")
import importlib.util, subprocess, sys
spec = importlib.util.spec_from_file_location("bot", "bot.py")
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    check("bot.py imports cleanly", True)
except Exception as e:
    check("bot.py imports cleanly", False, str(e))

# ─────────────────────────────────────────────────────────────
print(f"\n{'='*55}")
print(f"  TOTAL: {PASS} passed  |  {FAIL} failed")
print(f"{'='*55}")
if FAIL > 0:
    sys.exit(1)
