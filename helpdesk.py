"""Async client for the Roosterly helpdesk API.

Step 1a: send the right headers and make GET requests. Errors, retries, rate limiting and
pagination are added in the next steps.
"""
import httpx


class HelpdeskClient:
    def __init__(self, base_url: str, key: str, email: str):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {key}", "X-Candidate-Email": email},
            timeout=15.0,
        )

    async def __aenter__(self) -> "HelpdeskClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self._http.aclose()

    async def get_status(self) -> dict:
        resp = await self._http.get("/status")
        return resp.json()

    async def get_examples(self) -> dict:
        """The 10 tickets Nordavo labelled, used for the eval."""
        resp = await self._http.get("/examples")
        return resp.json()

    async def list_tickets(self, limit: int = 10, cursor: str | None = None, status: str = "open") -> dict:
        """One page of tickets (max 10). `status` is "open" (not yet handled) or "all"."""
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        resp = await self._http.get("/tickets", params=params)
        return resp.json()

    async def list_open_tickets(self) -> list[dict]:
        """All open tickets, page by page until next_cursor is null."""
        tickets: list[dict] = []
        cursor = None
        while True:
            page = await self.list_tickets(cursor=cursor)
            tickets += page["tickets"]
            cursor = page["next_cursor"]
            if cursor is None:
                return tickets

    async def get_ticket(self, ticket_id: str) -> dict:
        """One ticket, plus how it was handled (if it was)."""
        resp = await self._http.get(f"/tickets/{ticket_id}")
        return resp.json()
