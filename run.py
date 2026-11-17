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
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from helpdesk import HelpdeskClient, HelpdeskError
from llm import Analysis, Analyzer, ClaudeClient, LLMError, Verdict
from safety import Decision, empty_ticket_decision, gate, is_empty, redact_ticket, rule_hits

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
    r = sub.add_parser("run", help="analyse open tickets with the LLM")
    r.add_argument("--dry-run", action="store_true", help="print decisions, post nothing")
    r.add_argument("--limit", type=int, help="only the first N open tickets")
    return p


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


async def cmd_run(cfg: Config, dry_run: bool, limit: int | None) -> None:
    if not dry_run:
        sys.exit("Posting decisions is not built yet. Use --dry-run.")
    faq = Path("data/faq.md").read_text(encoding="utf-8")
    faq_ids = set(re.findall(r"^## (F\d+)", faq, re.MULTILINE))
    claude = ClaudeClient(cfg.anthropic_api_key, cfg.anthropic_model, timeout=cfg.llm_timeout)
    analyzer = Analyzer(claude, faq)
    async with HelpdeskClient(cfg.helpdesk_url, cfg.helpdesk_key, cfg.candidate_email) as hd:
        tickets = await hd.list_open_tickets()
    tickets = tickets[:limit] if limit else tickets
    print(f"Analysing {len(tickets)} ticket(s) with {cfg.anthropic_model}. DRY RUN: nothing is posted.\n")
    for t in tickets:
        started = time.monotonic()
        try:
            d, a, v = await decide(t, analyzer, faq_ids)
        except LLMError as e:
            print(f"{t['id']}  FAILED (left open): {e}\n")
            continue
        overruled = f"  (LLM said {a.action})" if a and a.action != d.action else ""
        print(f"{t['id']}  {d.language}  {d.category:<15} {d.priority}  {d.action:<7} conf {d.confidence:.2f}"
              f"{overruled}  [{time.monotonic() - started:.1f}s]")
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
        asyncio.run(cmd_run(load_config(need_llm=True), args.dry_run, args.limit))


if __name__ == "__main__":
    try:
        main()
    except HelpdeskError as e:
        sys.exit(f"Helpdesk error: {e}")
