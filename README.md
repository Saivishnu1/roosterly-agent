# Roosterly support-ticket agent

Reads every open helpdesk ticket and decides language, category, priority and one action (reply / handoff / close).
Python 3.11, `uv`, async, Claude Haiku 5.5. **Work in progress:** posting decisions is the next phase (marked below).

## How to run

```
uv sync
cp .env.example .env            # fill in HELPDESK_KEY, CANDIDATE_EMAIL, ANTHROPIC_API_KEY
uv run python run.py status     # open / handled counts (read-only)
uv run python run.py run --dry-run [--limit N]   # decide every open ticket, post nothing, show English translations
uv run python eval.py --tag after_fix            # score the agent on the 10 labelled examples
uv run pytest                   # offline tests (helpdesk and LLM mocked)
```
`run.py run` (posting) is not built yet.

## Decisions

1. **The model proposes, code decides.** One Claude call triages and drafts. Then plain code (`safety.py`) masks
   payment details, applies keyword rules and a gate, and a *second, independent* Claude call reviews every draft
   before it could be sent. Anything that fails a check becomes a handoff with the reason in the note.
2. **Work in Dutch directly, verify in English.** Translating first loses tone and the Dutch menu names. Instead the
   reviewer returns a faithful English translation of every draft, checks each claim against the FAQ, and an
   offline detector (lingua) checks the reply language. That is how I check Dutch replies without reading Dutch.
3. **Small, cheap model with structured output.** Haiku 5.5 with a fixed JSON schema; retries for network errors are
   the SDK's, retries for unusable answers (refusal, cut off, broken rules) are ours, max 3 each.

**AI tools:** built with Claude Code. I set the design and phases, reviewed and tested every change, and kept the
code small enough to explain line by line.

**Assumptions where the brief is open:**
- *Unsure* = any failed gate check: a sensitive topic (keyword or model flag), no or unknown FAQ id, confidence
  < 0.75 (close < 0.9), draft not in the customer's language, payment details in the draft, or a reviewer objection.
- *Priority*: the brief's definitions, plus: "blocked" = no workaround; "money is involved" = a charge, refund or
  failed payment (a price question is P4); "contract" = cancel or change the plan. Security/legal topics are at
  least P1, other client-rule topics at least P2.
- *Client rules*: a disputed charge = the customer says a charge is wrong; a security incident = someone else may
  have access (not a forgotten password); a threat to leave = says they will cancel or switch; a personal data
  request = see, export or delete data (not "where is it stored").
- A reply may explain a cause or say where a request goes (roadmap, status page), but never promise a fix.
- Keyword rules are a backstop written from the client's rule list, not a complete detector.
- Only nl / en / de; other languages are **not handled yet**.

## Reliability

- **503**: retried with exponential backoff and jitter (max 5); safe for POSTs because nothing was saved.
- **429**: wait `Retry-After`, then retry. **409**: never retried. 400/401/404/422: raised at once.
- **POST timeout**: never resent blindly; raised as "uncertain" so the caller checks the ticket first.
- **Crash safety** (state file before each POST, re-check on restart) and the shared rate limit: *next phase*.
- **Security**: keys from environment variables only; card numbers, CVC, expiry and IBANs masked before text reaches
  the model or a note; ticket text wrapped as data; logs hold ids and decisions, never ticket text.

## Eval and fix

`eval.py` runs the real pipeline on the 10 labelled examples (labels only from `GET /examples`, removed before the
agent sees a ticket), 3 runs each because answers vary; nothing is posted.

| 30 decisions | Unsafe replies | Missed replies | Action | Category | Priority | Language |
|---|---|---|---|---|---|---|
| [Baseline](eval_results/baseline.md) | 0 | 6 | 23 | 27 | 26 | 30 |
| [After fix](eval_results/after_fix.md) | 0 | 0 | 30 | 30 | 30 | 30 |

**The fix:** the prompt said "reply only if the help centre *fully* answers", so the agent handed off tickets a FAQ
entry does answer (E01 iOS requirement, E05 roadmap). A reply now also counts when an entry explains the cause or
says where the request goes. While measuring I also removed two contradictions the prompt had (what "money" and
"blocked" mean), which fixed category and priority.
Caveat: these changes were found and measured on the same 10 examples, so I also checked the 30 practice tickets.

**Cost per 100 tickets**: after-fix run, 152,134 input + 13,413 output tokens for 30 decisions (drafts include the
review call). At $0.10 / $0.50 per million tokens: (152,134 × 0.10 + 13,413 × 0.50) / 1,000,000 = $0.022 for 30,
so **≈ $0.07 per 100 tickets**.

## What I would improve

- **Next step:** posting with a crash-safe state file, parallel workers under one rate limit, and one log line per
  ticket; then live and crash tests.
- The reviewer is still strict on "where to find it" replies (it sometimes hands off a roadmap pointer).
- **In the brief I would change** the P2 definition: "one customer blocked, or money is involved" is read very
  differently by people and models (a crashed phone, a price question). Two examples per level would remove most
  of the disagreement.
