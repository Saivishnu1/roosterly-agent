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
