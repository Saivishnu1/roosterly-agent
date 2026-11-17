"""Offline tests for helpdesk.py: the helpdesk is mocked with respx, nothing goes over the network."""
import asyncio

import httpx
import pytest
import respx

from helpdesk import AlreadyHandled, HelpdeskClient, HelpdeskError, UncertainOutcome

BASE = "https://helpdesk.test"


def ok(body=None):
    return httpx.Response(200, json=body if body is not None else {"ok": True})


def err(status, code, **extra):
    return httpx.Response(status, json={"error": code, "message": code, **extra})


@pytest.fixture
async def client():
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async with HelpdeskClient(BASE, "key", "me@example.com", sleep=fake_sleep) as hd:
        hd.sleeps = sleeps
        yield hd


# --- retries ---------------------------------------------------------------

@respx.mock(base_url=BASE)
async def test_get_retries_503_then_succeeds(respx_mock, client):
    route = respx_mock.get("/status").mock(side_effect=[err(503, "service_unavailable")] * 3 + [ok()])
    assert await client.get_status() == {"ok": True}
    assert route.call_count == 4


@respx.mock(base_url=BASE)
async def test_gives_up_after_max_attempts(respx_mock, client):
    route = respx_mock.get("/status").mock(return_value=err(503, "service_unavailable"))
    with pytest.raises(HelpdeskError) as e:
        await client.get_status()
    assert e.value.code == "gave_up"
    assert route.call_count == 5


@respx.mock(base_url=BASE)
async def test_429_waits_retry_after(respx_mock, client):
    limited = httpx.Response(429, headers={"Retry-After": "2"}, json={"error": "rate_limited"})
    respx_mock.get("/status").mock(side_effect=[limited, ok()])
    await client.get_status()
    assert client.sleeps[0] == 2.0  # the first wait is Retry-After (later ones are just rate-limit spacing)


@respx.mock(base_url=BASE)
async def test_post_retries_503_because_nothing_was_saved(respx_mock, client):
    route = respx_mock.post("/tickets/T001/reply").mock(side_effect=[err(503, "service_unavailable"), ok()])
    await client.post_decision("T001", "reply", {})
    assert route.call_count == 2


# --- timeouts: GET retries, POST does not ----------------------------------

@respx.mock(base_url=BASE)
async def test_get_timeout_is_retried(respx_mock, client):
    route = respx_mock.get("/tickets/T001").mock(side_effect=[httpx.ReadTimeout("slow"), ok()])
    await client.get_ticket("T001")
    assert route.call_count == 2


@respx.mock(base_url=BASE)
async def test_post_timeout_is_not_retried(respx_mock, client):
    route = respx_mock.post("/tickets/T001/reply").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(UncertainOutcome):
        await client.post_decision("T001", "reply", {})
    assert route.call_count == 1


@respx.mock(base_url=BASE)
async def test_post_connect_error_is_retried(respx_mock, client):
    # The request never reached the server, so retrying cannot cause a second answer.
    route = respx_mock.post("/tickets/T001/reply").mock(side_effect=[httpx.ConnectError("down"), ok()])
    await client.post_decision("T001", "reply", {})
    assert route.call_count == 2


# --- errors that must never be retried --------------------------------------

@respx.mock(base_url=BASE)
async def test_409_raises_already_handled_once(respx_mock, client):
    route = respx_mock.post("/tickets/T001/handoff").mock(return_value=err(409, "already_handled"))
    with pytest.raises(AlreadyHandled):
        await client.post_decision("T001", "handoff", {})
    assert route.call_count == 1


@respx.mock(base_url=BASE)
async def test_422_carries_problems(respx_mock, client):
    respx_mock.post("/tickets/T001/reply").mock(
        return_value=err(422, "validation_failed", problems=["draft_reply is required"])
    )
    with pytest.raises(HelpdeskError) as e:
        await client.post_decision("T001", "reply", {})
    assert e.value.problems == ["draft_reply is required"]


@respx.mock(base_url=BASE)
async def test_401_is_not_retried(respx_mock, client):
    route = respx_mock.get("/status").mock(return_value=err(401, "unauthorized"))
    with pytest.raises(HelpdeskError) as e:
        await client.get_status()
    assert (e.value.status, e.value.code, route.call_count) == (401, "unauthorized", 1)


# --- pagination -------------------------------------------------------------

@respx.mock(base_url=BASE)
async def test_iter_open_tickets_follows_cursor_one_page_at_a_time(respx_mock, client):
    route = respx_mock.get("/tickets").mock(side_effect=[
        ok({"tickets": [{"id": "T001"}, {"id": "T002"}], "next_cursor": "T002"}),
        ok({"tickets": [{"id": "T003"}], "next_cursor": None}),
    ])
    seen = []
    async for t in client.iter_open_tickets():
        seen.append(t["id"])
        if t["id"] == "T001":
            assert route.call_count == 1  # page 2 is not fetched while page 1 is still being used
    assert seen == ["T001", "T002", "T003"]
    assert route.calls[1].request.url.params["cursor"] == "T002"


# --- rate limit -------------------------------------------------------------

@respx.mock(base_url=BASE)
async def test_requests_from_many_workers_are_spaced_about_3_per_second(respx_mock, client):
    respx_mock.get("/status").mock(return_value=ok())
    await asyncio.gather(*(client.get_status() for _ in range(10)))  # ten workers ask at the same instant
    waits = sorted(client.sleeps)
    assert len(waits) == 9                       # the first goes at once, the other nine each wait their turn
    gaps = [b - a for a, b in zip(waits, waits[1:])]
    assert all(abs(g - 0.3) < 0.02 for g in gaps)   # one request per 0.3 s, about 3 per second
    assert waits[-1] >= 9 * 0.3 - 0.05           # so ten requests take about 2.7 s, never a burst
