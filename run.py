"""Entry point for the Roosterly support-ticket agent.

Phase 0: config loading and the CLI skeleton. Commands are added phase by phase.
"""
import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from helpdesk import HelpdeskClient, HelpdeskError

REQUIRED = ("HELPDESK_URL", "HELPDESK_KEY", "CANDIDATE_EMAIL")


@dataclass(frozen=True)
class Config:
    helpdesk_url: str
    helpdesk_key: str
    candidate_email: str
    gemini_api_key: str
    gemini_model: str

    def __repr__(self) -> str:  # never print secrets
        return (
            f"Config(helpdesk_url={self.helpdesk_url!r}, helpdesk_key={mask(self.helpdesk_key)}, "
            f"candidate_email={self.candidate_email!r}, gemini_api_key={mask(self.gemini_api_key)}, "
            f"gemini_model={self.gemini_model!r})"
        )


def mask(secret: str) -> str:
    if not secret:
        return "<not set>"
    return secret[:4] + "..." + f"({len(secret)} chars)"


def load_config(need_llm: bool = False) -> Config:
    """Read settings from environment variables (a local .env is loaded if present)."""
    load_dotenv()
    missing = [k for k in REQUIRED if not os.getenv(k)]
    if need_llm and not os.getenv("GEMINI_API_KEY"):
        missing.append("GEMINI_API_KEY")
    if missing:
        sys.exit(f"Missing environment variables: {', '.join(missing)} (see .env.example)")
    return Config(
        helpdesk_url=os.environ["HELPDESK_URL"].rstrip("/"),
        helpdesk_key=os.environ["HELPDESK_KEY"],
        candidate_email=os.environ["CANDIDATE_EMAIL"],
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
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
    # Later phases: run
    return p


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


def main() -> None:
    args = build_parser().parse_args()
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


if __name__ == "__main__":
    try:
        main()
    except HelpdeskError as e:
        sys.exit(f"Helpdesk error: {e}")
