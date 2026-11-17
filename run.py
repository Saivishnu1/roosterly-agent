"""Entry point for the Roosterly support-ticket agent.

Phase 0: config loading and the CLI skeleton. Commands are added phase by phase.
"""
import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from helpdesk import AlreadyHandled, HelpdeskClient, HelpdeskError, UncertainOutcome
from llm import Analysis, Analyzer, ClaudeClient, LLMError, Verdict
from safety import Decision, empty_ticket_decision, gate, is_empty, redact_ticket, rule_hits
from state import AlreadyRunning, StateFile, TicketLog, single_run

log = logging.getLogger("run")

# Most tickets take 3-10 s. The limit stops one slow ticket (e.g. an API having a bad day, with all its
# retries) from holding a worker for many minutes; it is left open and picked up by the next run.
TICKET_TIMEOUT = 180

REQUIRED = ("HELPDESK_URL", "HELPDESK_KEY", "CANDIDATE_EMAIL")


@dataclass(frozen=True)
class Config:
    helpdesk_url: str
    helpdesk_key: str
    candidate_email: str
    anthropic_api_key: str
    anthropic_model: str
    llm_timeout: float

    def __repr__(self) -> str:  # never print secrets
        return (
            f"Config(helpdesk_url={self.helpdesk_url!r}, helpdesk_key={mask(self.helpdesk_key)}, "
            f"candidate_email={self.candidate_email!r}, anthropic_api_key={mask(self.anthropic_api_key)}, "
            f"anthropic_model={self.anthropic_model!r}, llm_timeout={self.llm_timeout})"
        )


def mask(secret: str) -> str:
    if not secret:
        return "<not set>"
    return secret[:4] + "..." + f"({len(secret)} chars)"


def load_config(need_llm: bool = False) -> Config:
    """Read settings from environment variables (a local .env is loaded if present)."""
    load_dotenv()
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if need_llm and not os.getenv("ANTHROPIC_API_KEY"):
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    return Config(
        helpdesk_url=os.environ["HELPDESK_URL"].rstrip("/"),
        helpdesk_key=os.environ["HELPDESK_KEY"],
        candidate_email=os.environ["CANDIDATE_EMAIL"],
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-haiku-5-5"),
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "60")),
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="Roosterly support-ticket agent")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="show loaded configuration (secrets masked)")
    sub.add_parser("status", help="show how many tickets are open and handled (read-only)")
    sub.add_parser("examples", help="save the 10 labelled examples to eval_results/examples.json (read-only)")
    t = sub.add_parser("tickets", help="list one page of open tickets (read-only)")
    t.add_argument("--cursor", help="next_cursor printed by the previous page")
    sub.add_parser("reset", help="re-open all 30 practice tickets (CHANGES helpdesk state)")
    r = sub.add_parser("run", help="decide and post one decision per open ticket (CHANGES helpdesk state)")
    r.add_argument("--dry-run", action="store_true", help="print decisions, post nothing")
    r.add_argument("--limit", type=int, help="only the first N open tickets")
    r.add_argument("--workers", type=int, default=3, help="tickets handled in parallel (default 3)")
    return p


def load_faq(path: str = "data/faq.md") -> tuple[str, set[str]]:
    """The help-centre text and the FAQ ids in it (F01, F02, ...)."""
    text = Path(path).read_text(encoding="utf-8")
    return text, set(re.findall(r"^## (F\d+)", text, re.MULTILINE))


async def decide(ticket: dict, analyzer: Analyzer, faq_ids: set[str]) -> tuple[Decision, Analysis | None, Verdict | None]:
    """One ticket -> final decision. Raises LLMError if Claude gives no usable answer (the ticket stays open)."""
    if is_empty(ticket):
        return empty_ticket_decision(), None, None
    safe = redact_ticket(ticket)  # the LLM never sees card numbers or IBANs
    analysis = await analyzer.analyze(safe)
    hits = rule_hits(safe)
    decision = gate(safe, analysis, hits, faq_ids)
    if decision.action != "reply":
        return decision, analysis, None
    # The code checks passed: now a separate reviewer reads the draft before anything can be sent.
    verdict = await analyzer.verify(safe, decision.draft_reply)
    return gate(safe, analysis, hits, faq_ids, verdict), analysis, verdict


async def post_ticket(ticket: dict, hd: HelpdeskClient, analyzer: Analyzer, faq_ids: set[str],
                      state: StateFile) -> str:
    """Decide and post one ticket so the customer gets exactly one answer, even across crashes.
    Returns a short outcome for the summary."""
    tid = ticket["id"]
    record = state.get(tid)
    if record and record["status"] == "posting":
        # A previous run stopped during the POST. Did the answer get through?
        if (await hd.get_ticket(tid)).get("handled"):
            await state.save(tid, **{**record, "status": "done", "outcome": "posted before a crash"})
            return "already posted before a crash"
        action, body = record["action"], record["body"]  # re-send exactly what we decided then
        log.warning("%s: resending the decision saved before a crash", tid)
    else:
        try:
            d, _, _ = await decide(ticket, analyzer, faq_ids)
        except LLMError as e:
            log.error("%s: no decision, left open: %s", tid, e)
            return "failed: no decision (left open)"
        action, body = d.action, d.body()
        await state.save(tid, status="posting", action=action, body=body, checks=d.failed_checks)  # BEFORE the POST
    return await send(hd, state, tid, action, body)


async def send(hd: HelpdeskClient, state: StateFile, tid: str, action: str, body: dict) -> str:
    """POST a saved decision and record the result. Handles each error as api_reference.md says."""
    try:
        await hd.post_decision(tid, action, body)
        outcome = f"posted {action}"
    except AlreadyHandled:
        # 409: "Do not retry. Your agent should not have sent this." The state file should prevent it.
        log.error("%s: already handled (409); not sent again. Our crash protection should have caught this", tid)
        outcome = "already handled (409)"
    except UncertainOutcome:
        # The POST went out but no answer came back: check before sending again.
        if not (await hd.get_ticket(tid)).get("handled"):
            await hd.post_decision(tid, action, body)  # if uncertain again, it stays "posting" for the next run
        outcome = f"posted {action}"
    except HelpdeskError as e:
        if e.status == 401:  # wrong key: stop the whole run
            raise
        if e.status in (400, 422):  # "Fix the body": decide again next run instead of resending this one
            log.error("%s: helpdesk rejected the decision: %s %s", tid, e, e.problems)
            await state.save(tid, **{**(state.get(tid) or {}), "status": "rejected", "error": str(e)})
            return f"failed: rejected ({e.status}), left open"
        if e.status == 404:  # "a ticket that is not in your current set"
            log.error("%s: not in the current ticket set (404)", tid)
            return "failed: unknown ticket (404)"
        # 503/429 still failing after all retries: nothing was saved; the next run checks and resends
        log.error("%s: could not post, will check again next run: %s", tid, e)
        return "failed: could not post (left as posting)"
    await state.save(tid, **{**(state.get(tid) or {}), "status": "done", "outcome": outcome})
    return outcome


async def produce(hd: HelpdeskClient, queue: asyncio.Queue, workers: int, limit: int | None) -> None:
    """Stream open tickets into the queue page by page; the queue is bounded, so this waits while it is full."""
    count = 0
    async for ticket in hd.iter_open_tickets():
        if limit and count >= limit:
            break
        await queue.put(ticket)
        count += 1
    for _ in range(workers):
        await queue.put(None)  # one stop signal per worker


async def post_all(hd: HelpdeskClient, analyzer: Analyzer, faq_ids: set[str], workers: int, limit: int | None,
                   ticket_timeout: float = TICKET_TIMEOUT) -> None:
    """Post one decision per open ticket. A producer streams tickets into a small queue and `workers` tasks
    handle them in parallel, so only a few tickets are in memory however many are open."""
    state = StateFile()
    ticket_log = TicketLog()
    queue: asyncio.Queue = asyncio.Queue(maxsize=workers * 2)
    outcomes: Counter = Counter()

    async def worker() -> None:
        while (ticket := await queue.get()) is not None:
            started = time.monotonic()
            try:
                outcome = await asyncio.wait_for(post_ticket(ticket, hd, analyzer, faq_ids, state), ticket_timeout)
            except TimeoutError:
                # Give up on this ticket so the worker can move on. Safe even mid-POST: the decision was saved
                # as "posting" first, so the next run checks the helpdesk before sending anything.
                log.error("%s: took longer than %ss, left for the next run", ticket["id"], ticket_timeout)
                outcome = f"failed: took longer than {ticket_timeout:g}s (left open)"
            except HelpdeskError as e:
                if e.status == 401:
                    raise  # wrong key: stop everything
                outcome = f"failed: {e}"
            except Exception as e:  # one broken ticket must not stop the others
                log.exception("%s: unexpected error", ticket["id"])
                outcome = f"failed: unexpected {type(e).__name__}"
            print(f"{ticket['id']}  {outcome}")
            ticket_log.write(ticket["id"], outcome, state.get(ticket["id"]), time.monotonic() - started)
            outcomes[outcome] += 1

    await asyncio.gather(produce(hd, queue, workers, limit), *(worker() for _ in range(workers)))
    print(f"\nLog: {ticket_log.path}")
    print("Summary: " + (", ".join(f"{n} {o}" for o, n in sorted(outcomes.items())) or "no open tickets"))
    print(f"Tokens: {analyzer.tokens_in} in, {analyzer.tokens_out} out")
    if any(o.startswith("failed") for o in outcomes):
        sys.exit(1)  # some tickets are still open: run again


def print_decision(t: dict, d: Decision, a: Analysis | None, v: Verdict | None, seconds: float) -> None:
    """Dry run: show everything about one decision, with English for every Dutch text."""
    overruled = f"  (LLM said {a.action})" if a and a.action != d.action else ""
    print(f"{t['id']}  {d.language}  {d.category:<15} {d.priority}  {d.action:<7} conf {d.confidence:.2f}"
          f"{overruled}  [{seconds:.1f}s]")
    if a:
        print(f"  summary: {a.english_summary}")
    print(f"  reason:  {d.reason}")
    if d.failed_checks:
        print(f"  checks:  {'; '.join(d.failed_checks)}")
    if d.action == "reply":
        print(f"  draft ({d.language}): {d.draft_reply}")
    elif d.action == "handoff":
        print(f"  note:    {d.handoff_note}")
    if v:  # the reviewer's English translation, so a non-Dutch reader can check every draft
        print(f"  english: {v.english_translation}")
        print(f"  review:  {v.explanation}")
    print()


async def cmd_run(cfg: Config, dry_run: bool, limit: int | None, workers: int) -> None:
    faq, faq_ids = load_faq()
    claude = ClaudeClient(cfg.anthropic_api_key, cfg.anthropic_model, timeout=cfg.llm_timeout)
    analyzer = Analyzer(claude, faq)
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        if not dry_run:
            try:
                with single_run():
                    print(f"Posting one decision per open ticket with {cfg.anthropic_model}, {workers} workers.\n")
                    await post_all(hd, analyzer, faq_ids, workers, limit)
            except AlreadyRunning as e:
                sys.exit(f"Not started: {e}")
            return
        print(f"Analysing open tickets with {cfg.anthropic_model}. DRY RUN: nothing is posted.\n")
        count = 0
        async for t in hd.iter_open_tickets():  # streamed page by page, like the real run
            if limit and count >= limit:
                break
            count += 1
            started = time.monotonic()
            try:
                d, a, v = await decide(t, analyzer, faq_ids)
            except LLMError as e:
                print(f"{t['id']}  FAILED (left open): {e}\n")
                continue
            print_decision(t, d, a, v, time.monotonic() - started)
    print(f"Tokens: {analyzer.tokens_in} in, {analyzer.tokens_out} out")


async def cmd_status(cfg: Config) -> None:
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        print(json.dumps(await hd.get_status(), indent=2))


async def cmd_tickets(cfg: Config, cursor: str | None) -> None:
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        page = await hd.list_tickets(cursor=cursor)
    for t in page["tickets"]:
        print(f"{t['id']}  {t['customer_plan']:<6} {t['subject']}")
    print(f"next_cursor: {page['next_cursor']}")


async def cmd_reset(cfg: Config) -> None:
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        print(json.dumps(await hd.reset(), indent=2))
    StateFile().clear()  # the helpdesk re-opened every ticket, so old records no longer apply
    print("Cleared state/decisions.json")


async def cmd_examples(cfg: Config) -> None:
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        examples = (await hd.get_examples())["examples"]
    path = Path("eval_results/examples.json")
    path.write_text(json.dumps(examples, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(examples)} examples to {path}")
    for ex in examples:
        lab = ex["label"]
        print(f"  {ex['id']}  {lab['language']}  {lab['category']:<16} {lab['priority']}  {lab['action']}")


def setup_logging(level: str) -> None:
    """One format for every log line (helpdesk retries, LLM calls, ...), on stderr."""
    logging.basicConfig(level=level.upper(), format="%(asctime)s %(levelname)-7s %(name)-17s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stderr)
    for noisy in ("httpx", "httpx2"):  # they log every HTTP request at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    args = build_parser().parse_args()
    load_dotenv()
    setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    if args.command == "config":
        print(load_config())
    elif args.command == "status":
        asyncio.run(cmd_status(load_config()))
    elif args.command == "tickets":
        asyncio.run(cmd_tickets(load_config(), args.cursor))
    elif args.command == "examples":
        asyncio.run(cmd_examples(load_config()))
    elif args.command == "reset":
        asyncio.run(cmd_reset(load_config()))
    elif args.command == "run":
        asyncio.run(cmd_run(load_config(need_llm=True), args.dry_run, args.limit, args.workers))


if __name__ == "__main__":
    try:
        main()
    except HelpdeskError as e:
        sys.exit(f"Helpdesk error: {e}")
