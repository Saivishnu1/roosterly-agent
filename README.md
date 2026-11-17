# Roosterly support-ticket agent

Reads every open helpdesk ticket and posts one decision each: language, category, priority, and reply / handoff / close.
Python 3.11, `uv`, async, Claude Haiku 5.5.

## How to run

```
uv sync
cp .env.example .env               # fill in HELPDESK_KEY, CANDIDATE_EMAIL, ANTHROPIC_API_KEY
uv run python run.py run           # decide and post one decision per open ticket (--workers 3, --limit N)
uv run python run.py run --dry-run # posts nothing; prints an English translation of every draft
uv run python eval.py --tag after_fix   # score the 10 labelled examples
uv run pytest                      # offline tests; also: run.py status | reset
```

## Decisions

1. **The model proposes, code decides.** One Claude call triages and drafts. Plain code (`safety.py`) masks payment
   details, applies keyword rules and a gate, and a second, independent Claude call reviews every draft. Any failed
   check turns the ticket into a handoff, with the reason in the note.
2. **Dutch directly, verified in English.** Translating first loses tone and the Dutch menu names. The reviewer
   returns an English translation of each draft and checks every claim against the FAQ; an offline detector
   (lingua) checks the reply language. That is how I check Dutch replies without reading Dutch.
3. **Small model, strict schema, streaming.** Haiku 5.5 with structured output; tickets stream page by page through a
   bounded queue to parallel workers, so memory stays flat however many are open.

**AI tools:** built with Claude Code: I set the design and phases, reviewed and ran every change, and kept the code small.

**Assumptions where the brief is open** (all in the prompt): *unsure* = any failed check (sensitive topic, no or unknown
FAQ id, confidence < 0.75, wrong language, payment details, reviewer objection) → handoff. "Blocked" = no workaround;
"money is involved" = a charge, refund or failed payment (a price question is P4); "contract" = cancel or change the
plan. A reply may say where a request goes (roadmap, status page) but never promise a fix. Only nl / en / de.
Keyword rules are a backstop written from the client's rules, not a complete detector.

## Reliability

- **503**: backoff with jitter, max 5. **429**: all workers share one limiter (one request per 0.3 s, about 3 per
  second, under the limit of 5); if a 429 still comes back, wait `Retry-After`. **409**: never retried. A POST that
  times out is checked against the ticket before it is ever resent.
- **Crash**: each decision is saved as "posting" *before* the POST; the next run asks the helpdesk whether it went
  through and re-sends only the saved decision, with no new LLM call. One run at a time (file lock). Tested live: a run
  hard-killed after 6 tickets, the next run did the other 24, none answered twice. A ticket stuck over 180 s is left open.
- **Security**: keys from environment variables only; card numbers, CVC, expiry and IBANs are masked before text
  reaches the model, a note or a log; ticket text is data. `logs/` has one line per ticket (ids and decision only).

## Eval and fix

`eval.py` runs the real pipeline on the 10 labelled examples (labels only from `GET /examples`, removed before the
agent sees a ticket), 3 runs each; nothing is posted.

| 30 decisions | Unsafe replies | Missed replies | Action | Category | Priority | Language |
|---|---|---|---|---|---|---|
| [Baseline](eval_results/baseline.md) | 0 | 6 | 23 | 27 | 26 | 30 |
| [After fix](eval_results/after_fix.md) | 0 | 0 | 30 | 30 | 30 | 30 |

**The fix:** the prompt said "reply only if the help centre *fully* answers", so tickets a FAQ entry does answer
(E01, E05) were handed off. A reply now also counts when an entry explains the cause or says where the request goes;
I also defined the vague words ("blocked", "money", the client-rule terms). Caveat: found and measured on the same
10 examples. Final run after `reset`: 30 tickets, 18 handoff, 10 reply, 2 close, no 409.

**Cost per 100 tickets:** 30 decisions used 152,134 input and 13,413 output tokens, reviews included. At $0.10 / $0.50
per million: (152,134 × 0.10 + 13,413 × 0.50) / 1,000,000 = $0.022 for 30 → **≈ $0.07 per 100**.

## What I would improve

- **Next:** hand off tickets in languages other than nl / en / de, and try more workers against the rate limiter.
- The reviewer is strict on "where to find it" replies, and some feature requests are still handed off (T020).
- **In the brief** I would change the priority table: "one customer blocked" and "money is involved" read very
  differently to people and models; two examples per level would remove most disagreement.
