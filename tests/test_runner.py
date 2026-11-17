"""Offline tests for posting (run.post_ticket): one answer per customer, even across crashes and errors.
The helpdesk is mocked with respx; decide() is replaced, so no LLM is called."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
import respx

import run
from helpdesk import HelpdeskClient
from llm import LLMError
from safety import Decision
from state import AlreadyRunning, StateFile, single_run

BASE = "https://helpdesk.test"
TICKET = {"id": "T001", "subject": "Wachtwoord", "body": "Ik ben mijn wachtwoord vergeten."}
DECISION = Decision("reply", "nl", "question", "P4", "F01 answers it.", 0.9, draft_reply="Ga naar de loginpagina...")
OPEN = {**TICKET, "handled": None}
HANDLED = {**TICKET, "handled": {"action": "reply"}}


@pytest.fixture
def decide_calls(monkeypatch):
    """Replace decide() with a stub that returns DECISION and counts its calls."""
    calls = []

    async def fake_decide(ticket, analyzer, faq_ids):
        calls.append(ticket["id"])
        return DECISION, None, None

    monkeypatch.setattr(run, "decide", fake_decide)
    return calls


async def post(state, ticket=TICKET):
    async def no_wait(_):
        pass
    async with HelpdeskClient(BASE, "key", "me@test", sleep=no_wait) as hd:
        return await run.post_ticket(ticket, hd, analyzer=None, faq_ids=set(), state=state)


@respx.mock
async def test_posts_once_and_records_done(tmp_path, decide_calls):
    route = respx.post(f"{BASE}/tickets/T001/reply").mock(return_value=httpx.Response(200, json={"ok": True}))
    state = StateFile(tmp_path / "s.json")
    assert await post(state) == "posted reply"
    assert route.call_count == 1
    assert state.get("T001")["status"] == "done"
    assert route.calls[0].request.content and b"draft_reply" in route.calls[0].request.content


@respx.mock
async def test_crash_during_post_resends_the_saved_decision_without_deciding_again(tmp_path, decide_calls):
    # The previous run saved "posting" and died before the POST got through; the ticket is still open.
    state = StateFile(tmp_path / "s.json")
    await state.save("T001", status="posting", action="reply", body=DECISION.body())
    respx.get(f"{BASE}/tickets/T001").mock(return_value=httpx.Response(200, json=OPEN))
    route = respx.post(f"{BASE}/tickets/T001/reply").mock(return_value=httpx.Response(200, json={}))
    assert await post(state) == "posted reply"
    assert route.call_count == 1 and decide_calls == []  # same decision, no new LLM call


@respx.mock
async def test_crash_after_post_got_through_is_not_sent_again(tmp_path, decide_calls):
    state = StateFile(tmp_path / "s.json")
    await state.save("T001", status="posting", action="reply", body=DECISION.body())
    respx.get(f"{BASE}/tickets/T001").mock(return_value=httpx.Response(200, json=HANDLED))
    route = respx.post(f"{BASE}/tickets/T001/reply")
    assert await post(state) == "already posted before a crash"
    assert route.call_count == 0 and state.get("T001")["status"] == "done"


@respx.mock
async def test_409_is_never_retried(tmp_path, decide_calls):
    route = respx.post(f"{BASE}/tickets/T001/reply").mock(
        return_value=httpx.Response(409, json={"error": "already_handled"}))
    state = StateFile(tmp_path / "s.json")
    assert await post(state) == "already handled (409)"
    assert route.call_count == 1 and state.get("T001")["status"] == "done"


@respx.mock
async def test_timeout_after_sending_checks_before_resending(tmp_path, decide_calls):
    route = respx.post(f"{BASE}/tickets/T001/reply").mock(side_effect=httpx.ReadTimeout("slow"))
    respx.get(f"{BASE}/tickets/T001").mock(return_value=httpx.Response(200, json=HANDLED))  # it did get through
    state = StateFile(tmp_path / "s.json")
    assert await post(state) == "posted reply"
    assert route.call_count == 1  # not sent a second time


@respx.mock
async def test_422_is_not_resent_and_ticket_is_decided_again_next_run(tmp_path, decide_calls):
    respx.post(f"{BASE}/tickets/T001/reply").mock(return_value=httpx.Response(
        422, json={"error": "validation_failed", "problems": ["draft_reply missing"]}))
    state = StateFile(tmp_path / "s.json")
    assert (await post(state)).startswith("failed: rejected")
    assert state.get("T001")["status"] == "rejected"  # not "posting", so the next run decides again


@respx.mock
async def test_no_decision_posts_nothing(tmp_path, monkeypatch):
    async def failing_decide(*_):
        raise LLMError("API down")
    monkeypatch.setattr(run, "decide", failing_decide)
    route = respx.post(f"{BASE}/tickets/T001/reply")
    state = StateFile(tmp_path / "s.json")
    assert (await post(state)).startswith("failed: no decision")
    assert route.call_count == 0 and state.get("T001") is None


@respx.mock
async def test_ticket_reopened_by_reset_is_decided_again(tmp_path, decide_calls):
    # The state says "done", but the helpdesk lists the ticket as open again (practice reset): decide afresh.
    state = StateFile(tmp_path / "s.json")
    await state.save("T001", status="done", action="reply", body=DECISION.body())
    respx.post(f"{BASE}/tickets/T001/reply").mock(return_value=httpx.Response(200, json={}))
    assert await post(state) == "posted reply"
    assert decide_calls == ["T001"]


async def test_state_file_survives_a_restart(tmp_path):
    path = tmp_path / "s.json"
    await StateFile(path).save("T001", status="posting", action="reply", body={"x": 1})
    assert StateFile(path).get("T001")["status"] == "posting"  # a new process reads what the old one wrote


# --- streaming, parallel workers, single run ----------------------------------

@respx.mock
async def test_all_pages_posted_exactly_once_by_parallel_workers(tmp_path, monkeypatch, decide_calls):
    monkeypatch.chdir(tmp_path)  # the state file is written under the current folder
    ids = [f"T{i:03d}" for i in range(1, 26)]  # 25 tickets = 3 pages
    pages = [ids[0:10], ids[10:20], ids[20:25]]
    respx.get(f"{BASE}/tickets").mock(side_effect=[
        httpx.Response(200, json={"tickets": [{"id": i, "body": "x"} for i in page],
                                  "next_cursor": page[-1] if n < 2 else None})
        for n, page in enumerate(pages)
    ])
    posts = respx.post(url__regex=rf"{BASE}/tickets/T\d+/reply").mock(return_value=httpx.Response(200, json={}))

    biggest_queue = 0
    real_put = asyncio.Queue.put

    async def watched_put(self, item):
        nonlocal biggest_queue
        await real_put(self, item)
        biggest_queue = max(biggest_queue, self.qsize())
    monkeypatch.setattr(asyncio.Queue, "put", watched_put)

    async def no_wait(_):
        pass
    async with HelpdeskClient(BASE, "key", "me@test", sleep=no_wait) as hd:
        await run.post_all(hd, analyzer=SimpleNamespace(tokens_in=0, tokens_out=0), faq_ids=set(),
                           workers=3, limit=None)

    posted = sorted(str(c.request.url).split("/")[-2] for c in posts.calls)
    assert posted == ids                      # every ticket once, none twice
    assert biggest_queue <= 6                 # never more than workers * 2 tickets waiting in memory


def test_second_run_is_refused_while_one_is_running(tmp_path):
    lock = tmp_path / "run.lock"
    with single_run(lock):
        with pytest.raises(AlreadyRunning):
            with single_run(lock):
                pass
    with single_run(lock):  # released again once the first run ends
        pass


@respx.mock
async def test_slow_ticket_is_abandoned_and_others_still_get_posted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def decide(ticket, analyzer, faq_ids):
        if ticket["id"] == "T002":
            await asyncio.sleep(10)  # stuck far longer than the limit
        return DECISION, None, None
    monkeypatch.setattr(run, "decide", decide)
    respx.get(f"{BASE}/tickets").mock(return_value=httpx.Response(200, json={
        "tickets": [{"id": f"T00{i}", "body": "x"} for i in (1, 2, 3, 4)], "next_cursor": None}))
    posts = respx.post(url__regex=rf"{BASE}/tickets/T\d+/reply").mock(return_value=httpx.Response(200, json={}))

    async def no_wait(_):
        pass
    async with HelpdeskClient(BASE, "key", "me@test", sleep=no_wait) as hd:
        with pytest.raises(SystemExit):  # exit code 1: a ticket is still open, run again
            await run.post_all(hd, analyzer=SimpleNamespace(tokens_in=0, tokens_out=0), faq_ids=set(),
                               workers=2, limit=None, ticket_timeout=0.2)

    posted = sorted(str(c.request.url).split("/")[-2] for c in posts.calls)
    assert posted == ["T001", "T003", "T004"]  # the slow one is left open, the rest went through
