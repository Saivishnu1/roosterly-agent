"""Eval: run the agent on the 10 labelled examples and score it against Nordavo's labels.

- Labels come only from GET /examples (saved by `run.py examples` to eval_results/examples.json).
  The label is removed before the agent sees a ticket; the model under test never sees or writes labels.
- The agent runs exactly as in production (decide(): redact -> LLM -> rules -> gate -> reviewer). Nothing is posted.
- Each of the 10 examples runs 3 times by default: the model's answers vary between runs, and with only 10
  examples a single run would make a before/after difference of 1-2 look like a change when it is chance.

    uv run python eval.py --tag after_fix
    uv run python eval.py --tag quick --runs 1

Every run is stored in eval_results/runs/ (git-ignored): one report per run, and one line per run in
history.jsonl with the scores and the git commit it ran on. Only eval_results/baseline.md is committed.
"""
import argparse
import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from llm import Analyzer, ClaudeClient, LLMError
from run import decide, load_config, load_faq, setup_logging

EXAMPLES = Path("eval_results/examples.json")
RUNS_DIR = Path("eval_results/runs")
CONCURRENCY = 4
FIELDS = ("action", "category", "priority", "language")
PRICE_IN, PRICE_OUT = 0.10, 0.50  # USD per 1M tokens for claude-haiku-5-5


async def run_once(example: dict, analyzer: Analyzer, faq_ids: set[str], limit: asyncio.Semaphore) -> dict:
    ticket = {k: v for k, v in example.items() if k != "label"}  # the agent never sees the label
    async with limit:
        try:
            d, _, verdict = await decide(ticket, analyzer, faq_ids)
        except LLMError as e:  # no decision = nothing would be posted
            return {"error": str(e)}
    return {"action": d.action, "category": d.category, "priority": d.priority, "language": d.language,
            "checks": d.failed_checks, "draft": d.draft_reply,
            "english": verdict.english_translation if verdict else ""}


def git_commit() -> str:
    """The commit the eval ran on, with '+changes' if there were uncommitted edits (so a result is traceable)."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"
    return f"{sha}+changes" if dirty else sha or "unknown"


def priority_distance(a: str, b: str) -> int:
    return abs(int(a[1]) - int(b[1]))


def score(examples: list[dict], results: dict[str, list[dict]]) -> dict[str, int]:
    """Counts over every (example, run) pair."""
    s = {k: 0 for k in ("total", "errors", "unsafe_replies", "missed_replies", "action", "category",
                        "priority", "priority_within_1", "language", "unstable_examples")}
    for ex in examples:
        label, runs = ex["label"], results[ex["id"]]
        if len({r.get("action") for r in runs}) > 1:
            s["unstable_examples"] += 1
        for r in runs:
            s["total"] += 1
            if "error" in r:
                s["errors"] += 1
                continue
            if r["action"] == "reply" and label["action"] != "reply":
                s["unsafe_replies"] += 1  # we answered a customer who needed a human (or a close)
            if label["action"] == "reply" and r["action"] != "reply":
                s["missed_replies"] += 1  # a human gets a ticket the help centre could have answered
            for f in ("action", "category", "priority", "language"):
                s[f] += r[f] == label[f]
            s["priority_within_1"] += priority_distance(r["priority"], label["priority"]) <= 1
    return s


def report(tag: str, model: str, runs: int, examples: list[dict], results: dict[str, list[dict]], s: dict, cost: dict,
           now: datetime) -> str:
    n = s["total"]
    pct = lambda k: f"{s[k]}/{n} ({100 * s[k] / n:.0f}%)"  # noqa: E731
    lines = [
        f"# Eval: {tag}", "",
        f"{len(examples)} labelled examples from `GET /examples`, {runs} run(s) each, model `{model}`, "
        f"{now:%Y-%m-%d %H:%M} UTC, commit {git_commit()}. Nothing was posted.", "",
        "## Summary", "",
        "| Metric | Result |", "|---|---|",
        f"| **Unsafe replies** (replied where the label says handoff/close; must be 0) | {s['unsafe_replies']} |",
        f"| Missed replies (handed off/closed where the label says reply) | {s['missed_replies']} |",
        f"| Action correct | {pct('action')} |",
        f"| Category correct | {pct('category')} |",
        f"| Priority exact | {pct('priority')} |",
        f"| Priority within one level | {pct('priority_within_1')} |",
        f"| Language correct | {pct('language')} |",
        f"| No decision (LLM failed) | {s['errors']} |",
        f"| Cost | ${cost['per_100']:.3f} per 100 tickets ({cost['tokens_in']} in / {cost['tokens_out']} out tokens "
        f"for {n} decisions) |",
    ]
    if runs > 1:
        lines.append(f"| Examples whose action changed between runs | {s['unstable_examples']}/{len(examples)} |")
    lines += [
        "", "## Per example", "",
        "Each cell is `action / category / priority / language`; a wrong field is in **bold**.", "",
        "| Example | Subject | Label | " + " | ".join(f"Run {i + 1}" for i in range(runs)) + " |",
        "|---|---|---|" + "---|" * runs,
    ]
    for ex in examples:
        label = ex["label"]
        cells = []
        for r in results[ex["id"]]:
            if "error" in r:
                cells.append("error")
            else:
                cells.append(" / ".join(r[f] if r[f] == label[f] else f"**{r[f]}**" for f in FIELDS))
        lines.append(f"| {ex['id']} | {ex['subject']} | {' / '.join(label[f] for f in FIELDS)} | {' | '.join(cells)} |")

    lines += ["", "## Why (run 1)", ""]
    for ex in examples:
        r = results[ex["id"]][0]
        lines.append(f"**{ex['id']}** label: {ex['label']['action']}. {ex['label']['reason']}  ")
        if "error" in r:
            lines.append(f"Agent: no decision ({r['error']})")
        else:
            lines.append(f"Agent: {r['action']}." + (f" Gate checks: {'; '.join(r['checks'])}." if r["checks"] else ""))
            if r["english"]:
                lines.append(f"Reply (English translation): _{r['english']}_")
        lines.append("")
    return "\n".join(lines)


async def main(tag: str, runs: int) -> None:
    cfg = load_config(need_llm=True)
    faq, faq_ids = load_faq()
    analyzer = Analyzer(ClaudeClient(cfg.anthropic_api_key, cfg.anthropic_model, timeout=cfg.llm_timeout), faq)
    examples = json.loads(EXAMPLES.read_text(encoding="utf-8"))
    limit = asyncio.Semaphore(CONCURRENCY)

    jobs = [run_once(ex, analyzer, faq_ids, limit) for ex in examples for _ in range(runs)]
    flat = await asyncio.gather(*jobs)
    results = {ex["id"]: flat[i * runs:(i + 1) * runs] for i, ex in enumerate(examples)}

    s = score(examples, results)
    usd = (analyzer.tokens_in * PRICE_IN + analyzer.tokens_out * PRICE_OUT) / 1_000_000
    cost = {"tokens_in": analyzer.tokens_in, "tokens_out": analyzer.tokens_out, "per_100": usd / s["total"] * 100}
    # Every run is kept locally (git-ignored): a full report per run, plus one line per run in history.jsonl.
    now = datetime.now(timezone.utc)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNS_DIR / f"{now:%Y%m%dT%H%M%SZ}_{tag}.md"
    path.write_text(report(tag, cfg.anthropic_model, runs, examples, results, s, cost, now), encoding="utf-8")
    entry = {"time": now.isoformat(timespec="seconds"), "tag": tag, "model": cfg.anthropic_model,
             "commit": git_commit(), "runs_per_example": runs, "report": path.name,
             **s, "usd_per_100_tickets": round(cost["per_100"], 4)}
    with (RUNS_DIR / "history.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    print(f"Wrote {path} (history: {RUNS_DIR / 'history.jsonl'})")
    print(f"  unsafe replies {s['unsafe_replies']}, missed replies {s['missed_replies']}, "
          f"action {s['action']}/{s['total']}, category {s['category']}/{s['total']}, "
          f"priority {s['priority']}/{s['total']} (within 1: {s['priority_within_1']}), "
          f"language {s['language']}/{s['total']}, unstable {s['unstable_examples']}, errors {s['errors']}, "
          f"${cost['per_100']:.3f} per 100 tickets")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Score the agent on the labelled examples")
    p.add_argument("--tag", default="run", help="label for this run, e.g. baseline or after_fix")
    p.add_argument("--runs", type=int, default=3, help="runs per example (default 3)")
    args = p.parse_args()
    setup_logging(os.getenv("LOG_LEVEL", "WARNING"))
    asyncio.run(main(args.tag, args.runs))
