# Eval: baseline

10 labelled examples from `GET /examples`, 3 runs each, model `claude-haiku-5-5`, 2026-11-17 19:21 UTC. Nothing was posted.

## Summary

| Metric | Result |
|---|---|
| **Unsafe replies** (replied where the label says handoff/close; must be 0) | 0 |
| Missed replies (handed off/closed where the label says reply) | 6 |
| Action correct | 23/30 (77%) |
| Category correct | 27/30 (90%) |
| Priority exact | 26/30 (87%) |
| Priority within one level | 30/30 (100%) |
| Language correct | 30/30 (100%) |
| Examples whose action changed between runs | 1/10 |
| No decision (LLM failed) | 0 |
| Cost | $0.062 per 100 tickets (112572 in / 14684 out tokens for 30 decisions) |

## Per example

Each cell is `action / category / priority / language`; a wrong field is in **bold**.

| Example | Subject | Label | Run 1 | Run 2 | Run 3 |
|---|---|---|---|---|---|
| E01 | App crashes | reply / bug / P3 / en | **handoff** / bug / **P2** / en | **handoff** / bug / **P2** / en | **handoff** / bug / P3 / en |
| E02 | Btw-nummer | reply / billing / P4 / nl | reply / billing / **P3** / nl | reply / billing / P4 / nl | reply / billing / **P3** / nl |
| E03 | Terugbetaling | handoff / billing / P2 / nl | handoff / billing / P2 / nl | handoff / billing / P2 / nl | handoff / billing / P2 / nl |
| E04 | Dienst ruilen | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl |
| E05 | Google Agenda | reply / feature_request / P4 / nl | **handoff** / feature_request / P4 / nl | **handoff** / feature_request / P4 / nl | **handoff** / feature_request / P4 / nl |
| E06 | Can't publish rota | handoff / bug / P2 / en | handoff / bug / P2 / en | handoff / bug / P2 / en | handoff / bug / P2 / en |
| E07 | Prijs Plus | reply / billing / P4 / nl | reply / **question** / P4 / nl | reply / **question** / P4 / nl | reply / **question** / P4 / nl |
| E08 | Investeringskans | close / other / P4 / nl | close / other / P4 / nl | **handoff** / other / P4 / nl | close / other / P4 / nl |
| E09 | Inzageverzoek | handoff / other / P2 / nl | handoff / other / P2 / nl | handoff / other / P2 / nl | handoff / other / P2 / nl |
| E10 | Onderhoud | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl | reply / question / P4 / nl |

## Why (run 1)

**E01** label: reply. English ticket. FAQ F12 covers it: the app needs iOS 16 or later, so we can answer in English without a human.  
Agent: handoff.

**E02** label: reply. How to change invoice details is in FAQ F06. Reply in Dutch.  
Agent: reply.
Reply (English translation): _Good day, you can change the VAT number yourself under Settings → Company details (Instellingen → Bedrijfsgegevens). The new VAT number will then be used on future invoices. Kind regards, Roosterly support_

**E03** label: handoff. Refund request ('terugkrijgen' = get back). Client rule: refunds always go to a human.  
Agent: handoff. Gate checks: keyword rule: refund; LLM flagged: refund.

**E04** label: reply. Dutch how-to question about swapping shifts, answered by FAQ F03. Reply in Dutch.  
Agent: reply.
Reply (English translation): _Hi! This is how you swap a shift with a colleague: open the shift in the app and tap "Ruil aanbieden" (Offer swap). Colleagues with the same role can accept the swap. Then your manager approves the swap under Rooster → Verzoeken (Rota → Requests). Good luck!_

**E05** label: reply. Feature request (Google Calendar sync). Acknowledge in Dutch and point to the roadmap (F10).  
Agent: handoff.

**E06** label: handoff. One customer blocked from publishing: P2, needs an engineer.  
Agent: handoff.

**E07** label: reply. Price question answered by F05 (25 x €6 = €150 excl. VAT). Reply in Dutch.  
Agent: reply.
Reply (English translation): _Hi, Plus costs €6 per active employee per month, excluding VAT. For 25 employees that comes to €150 per month excluding VAT. You only pay for active employees. If you want to upgrade, you can do so at any time via Settings → Subscription; the new price applies from the next invoice. Kind regards, the Roosterly team_

**E08** label: close. Spam (crypto offer): close it.  
Agent: close.

**E09** label: handoff. GDPR access request ('inzageverzoek', AVG = Dutch GDPR): always a human.  
Agent: handoff. Gate checks: keyword rule: personal_data_request; LLM flagged: personal_data_request.

**E10** label: reply. Asks about planned maintenance tonight. Point to the status page (F11), in Dutch.  
Agent: reply.
Reply (English translation): _Thank you for your message! Current information about outages and planned maintenance can be found at status.roosterly.nl. There you can see right away whether maintenance is planned for this evening._
