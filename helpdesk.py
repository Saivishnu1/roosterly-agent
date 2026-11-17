"""Async client for the Roosterly helpdesk API.

Every call goes through `_request`, which handles the helpdesk's errors:
  - 503 and 429 are retried. Both mean the request was not saved, so this is safe for POSTs too.
  - A timeout or dropped connection is retried for GETs only. For a POST we can't know whether
    the answer was saved, so we raise UncertainOutcome and let the caller check the ticket.
  - Everything else (400, 401, 404, 409, 422) is raised at once: retrying would not help.
"""
import asyncio
import logging
import random
import time

import httpx

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BACKOFF_START = 0.5  # seconds; doubles each attempt
BACKOFF_MAX = 8.0
# Errors where the request never reached the server, so even a POST is safe to resend.
NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout)
# The helpdesk allows 5 requests per second. We keep 1 in 5 in reserve: one request every 0.25 s = 4 per second,
# shared by all workers, so we stay under the limit instead of being rejected with 429.
MIN_GAP = 0.25


def _backoff(attempt: int) -> float:
    """0.5s, 1s, 2s, 4s, 8s ... with +-20% jitter so parallel retries don't line up."""
    return min(BACKOFF_START * 2 ** (attempt - 1), BACKOFF_MAX) * random.uniform(0.8, 1.2)


class HelpdeskError(Exception):
    """The helpdesk refused a request, or kept failing after all retries."""

    def __init__(self, status: int | None, code: str, message: str = "", problems: list | None = None):
        self.status, self.code, self.problems = status, code, problems or []
        super().__init__(f"{status} {code}: {message}".strip())


class AlreadyHandled(HelpdeskError):
    """409: this ticket already got an answer. Never retry."""


class UncertainOutcome(HelpdeskError):
    """A POST timed out or the connection dropped: the answer may or may not have been saved."""


class HelpdeskClient:
    def __init__(self, base_url: str, key: str, email: str, sleep=asyncio.sleep):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "X-Candidate-Email": email},
            timeout=15.0,
        )
        self._sleep = sleep  # tests pass a fake so they don't actually wait
        self._next_slot = 0.0  # the earliest time the next request may be sent (time.monotonic)
        self._slot_lock = asyncio.Lock()

    async def __aenter__(self) -> "HelpdeskClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def _wait_for_slot(self) -> None:
        """Rate limit: every request, from any worker and including retries, takes the next free time slot
        (0.25 s apart). The lock is held only to hand out the slot; the waiting happens outside it."""
        async with self._slot_lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + MIN_GAP
        if slot > now:
            await self._sleep(slot - now)

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            await self._wait_for_slot()
            # 1. Send. Each failure below decides how long to wait before the next try.
            try:
                resp = await self._http.request(method, path, **kwargs)
            except httpx.TransportError as e:  # timeout or connection problem, no response
                if method == "POST" and not isinstance(e, NEVER_SENT):
                    raise UncertainOutcome(None, type(e).__name__, f"{method} {path}") from e
                problem, wait = type(e).__name__, _backoff(attempt)
            else:
                # 2. We got a response.
                if resp.is_success:
                    return resp.json()
                if resp.status_code == 503:  # nothing was saved
                    problem, wait = "503", _backoff(attempt)
                elif resp.status_code == 429:  # too fast, nothing was saved
                    problem, wait = "429", float(resp.headers.get("Retry-After", 1))
                else:  # 400, 401, 404, 409, 422: retrying would not help
                    raise _error_from(resp)

            # 3. Wait and try again (unless that was the last attempt).
            if attempt < MAX_ATTEMPTS:
                log.warning("%s %s -> %s, retry %d in %.1fs", method, path, problem, attempt, wait)
                await self._sleep(wait)

        raise HelpdeskError(None, "gave_up", f"{method} {path} still failing after {MAX_ATTEMPTS} attempts ({problem})")

    async def get_status(self) -> dict:
        return await self._request("GET", "/status")

    async def get_examples(self) -> dict:
        """The 10 tickets Nordavo labelled, used for the eval."""
        return await self._request("GET", "/examples")

    async def list_tickets(self, limit: int = 10, cursor: str | None = None, status: str = "open") -> dict:
        """One page of tickets (max 10). `status` is "open" (not yet handled) or "all"."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/tickets", params=params)

    async def iter_open_tickets(self):
        """Yield open tickets one by one, fetching the next page only when this one is used up,
        so at most one page is in memory however many tickets are open.
        The cursor is the last ticket id, so tickets we handle meanwhile do not shift the pages."""
        cursor = None
        while True:
            page = await self.list_tickets(cursor=cursor)
            for ticket in page["tickets"]:
                yield ticket
            cursor = page["next_cursor"]
            if cursor is None:
                return

    async def get_ticket(self, ticket_id: str) -> dict:
        """One ticket, plus how it was handled (if it was)."""
        return await self._request("GET", f"/tickets/{ticket_id}")

    async def post_decision(self, ticket_id: str, action: str, decision: dict) -> dict:
        """Send the one decision for a ticket. `action` is reply, handoff or close."""
        if action not in ("reply", "handoff", "close"):
            raise ValueError(f"unknown action: {action}")
        return await self._request("POST", f"/tickets/{ticket_id}/{action}", json=decision)

    async def reset(self) -> dict:
        """Re-open all practice tickets. Changes the helpdesk state."""
        return await self._request("POST", "/practice/reset")


def _error_from(resp: httpx.Response) -> HelpdeskError:
    try:
        body = resp.json()
    except ValueError:
        body = {}
    cls = AlreadyHandled if resp.status_code == 409 else HelpdeskError
    return cls(resp.status_code, body.get("error", "unknown"), body.get("message", ""), body.get("problems"))
