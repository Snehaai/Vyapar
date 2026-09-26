# Vyapar — AI Customer Assistant Bot

**Submitted by:** Sneha Manohar NSUT 2023UCA1930 (snehamanohar068@gmail.com)

**Deployed Link:** https://vyapar-x4se.onrender.com

## Approach

Trigger-kind dispatcher → per-kind grounded prompt → Gemini 2.0 Flash (free, temp=0) → post-LLM validation → intent-aware reply handler.

Groq Llama-3.3-70B kicks in automatically if Gemini fails.

## How each scoring dimension is addressed

| Dimension | What Vyapar does |
|---|---|
| Decision quality | 17 trigger kinds each get a separate instruction block that forces ONE primary signal selection before composing. No shared generic prompt. |
| Specificity | Real numbers (views, CTR vs peer benchmark, customer counts, offer prices, digest trial N) injected directly. Model instructed to use them or fail. URLs stripped post-generation. |
| Category fit | `voice.tone`, `vocab_taboos`, `offer_catalog`, `peer_stats`, `trend_signals` all injected per category. Taboo words listed in hard-rules. |
| Merchant fit | Owner first name, locality, subscription status, active offers, customer aggregate, account signals — all grounded in the message. Language (hi/en/mix) honoured. |
| Engagement compulsion | Each kind-task mandates one compulsion lever (curiosity, social proof, loss aversion, effort-externalisation). Single CTA enforced by prompt + post-parse validation. |

## Multi-turn replay handling

- **Auto-reply:** regex detection → nudge owner on 1st → wait 24h on 2nd → end on 3rd
- **Intent commit** (yes/go ahead/haan karo): switches immediately to execution mode — confirms action, stops qualifying
- **Hostile/opt-out:** graceful `end`, suppresses future sends
- **Wait:** backs off 1h before next contact

## Model

Gemini 2.0 Flash — free tier, 10k RPM, `temperature=0` for determinism. 
Groq Llama-3.3-70B as automatic fallback.

## Tradeoffs

In-memory state — fast, zero infra, resets on restart. Fine for a single judge test window. Would use Redis for multi-instance production.

No retrieval layer — full context passed per prompt. Works within the 500KB payload cap. Would switch to RAG at higher context volume.

Regex intent detection for replies — saves ~200ms per turn vs. LLM classification, keeps well under the 30s timeout.
