# Eval: after_fix

10 labelled examples from `GET /examples`, 3 run(s) each, model `claude-haiku-5-5`, 2026-11-17 20:37 UTC, commit 0858b84+changes. Nothing was posted.

## Summary

| Metric | Result |
|---|---|
| **Unsafe replies** (replied where the label says handoff/close; must be 0) | 0 |
| Missed replies (handed off/closed where the label says reply) | 0 |
| Action correct | 30/30 (100%) |
| Category correct | 30/30 (100%) |
| Priority exact | 30/30 (100%) |
| Priority within one level | 30/30 (100%) |
| Language correct | 30/30 (100%) |
| No decision (LLM failed) | 0 |
| Cost | $0.073 per 100 tickets (152134 in / 13413 out tokens for 30 decisions) |
| Examples whose action changed between runs | 0/10 |

## Majority of runs (the most common answer per example, out of 10)

| Metric | Result |
|---|---|
| **Unsafe replies** | 0 |
| Missed replies | 0 |
| Action correct | 10/10 |
| Category correct | 10/10 |
| Priority correct | 10/10 |
| Language correct | 10/10 |

## Per example

Each cell is `action / category / priority / language`; a wrong field is in **bold**.

| Example | Subject | Label | Run 1 | Run 2 | Run 3 |
|---|---|---|---|---|---|
| E01 | App crashes | reply / bug / P3 / en | reply / bug / P3 / en | reply / bug / P3 / en | reply / bug / P3 / en |
| E02 | Btw-nummer | reply / billing / P4 / nl | reply / billing / P4 / nl | reply / billing / P4 / nl | reply / billing / P4 / nl |
| E03 | Terugbetaling | handoff / billing / P2 / nl | handoff / billing / P2 / nl | handoff / billing / P2 / nl | handoff / billing / P2 / nl |
| E04 | Dienst ruilen | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl |
| E05 | Google Agenda | reply / feature_request / P4 / nl | reply / feature_request / P4 / nl | reply / feature_request / P4 / nl | reply / feature_request / P4 / nl |
| E06 | Can't publish rota | handoff / bug / P2 / en | handoff / bug / P2 / en | handoff / bug / P2 / en | handoff / bug / P2 / en |
| E07 | Prijs Plus | reply / billing / P4 / nl | reply / billing / P4 / nl | reply / billing / P4 / nl | reply / billing / P4 / nl |
| E08 | Investeringskans | close / other / P4 / nl | close / other / P4 / nl | close / other / P4 / nl | close / other / P4 / nl |
| E09 | Inzageverzoek | handoff / other / P2 / nl | handoff / other / P2 / nl | handoff / other / P2 / nl | handoff / other / P2 / nl |
| E10 | Onderhoud | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl |

## Why (run 1)

**E01** label: reply. English ticket. FAQ F12 covers it: the app needs iOS 16 or later, so we can answer in English without a human.  
Agent: reply.
Reply (English translation): _Hello, thank you for your message. The Roosterly app works on iOS 16 and later, and on Android 10 and later. Your iPhone is running iOS 15, which is below that requirement, so the app may crash on it. Kind regards, the Roosterly support team_

**E02** label: reply. How to change invoice details is in FAQ F06. Reply in Dutch.  
Agent: reply.
Reply (English translation): _That is possible. You can change the VAT number yourself under Settings → Company details. The change applies to future invoices. Kind regards, the Roosterly team_

**E03** label: handoff. Refund request ('terugkrijgen' = get back). Client rule: refunds always go to a human.  
Agent: handoff. Gate checks: keyword rule: refund; LLM flagged: refund.

**E04** label: reply. Dutch how-to question about swapping shifts, answered by FAQ F03. Reply in Dutch.  
Agent: reply.
Reply (English translation): _That is possible in the app: open the shift and tap "Ruil aanbieden" (Offer swap). Colleagues with the same role can accept the swap. Then the manager approves the swap under Rooster → Verzoeken (Rota → Requests)._

**E05** label: reply. Feature request (Google Calendar sync). Acknowledge in Dutch and point to the roadmap (F10).  
Agent: reply.
Reply (English translation): _Thank you for your idea to connect Roosterly with Google Calendar. We read every idea. You can follow the request and vote on roadmap.roosterly.nl. Unfortunately we cannot promise a delivery date for individual requests._

**E06** label: handoff. One customer blocked from publishing: P2, needs an engineer.  
Agent: handoff.

**E07** label: reply. Price question answered by F05 (25 x €6 = €150 excl. VAT). Reply in Dutch.  
Agent: reply.
Reply (English translation): _Hi, the Plus subscription costs €6 per active employee per month, excl. VAT. For 25 employees that comes to €150 per month, excl. VAT. The Plus subscription includes unlimited locations, API access and the payroll integrations (Nmbrs, Loket.nl and AFAS). Kind regards, the Roosterly team_

**E08** label: close. Spam (crypto offer): close it.  
Agent: close.

**E09** label: handoff. GDPR access request ('inzageverzoek', AVG = Dutch GDPR): always a human.  
Agent: handoff. Gate checks: keyword rule: personal_data_request; LLM flagged: personal_data_request.

**E10** label: reply. Asks about planned maintenance tonight. Point to the status page (F11), in Dutch.  
Agent: reply.
Reply (English translation): _Good afternoon, current outages and planned maintenance can be found at status.roosterly.nl. The most recent updates are also shown there. Kind regards, the Roosterly support team_
