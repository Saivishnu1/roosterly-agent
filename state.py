"""The state file: what we decided for each ticket and whether it was sent.

state/decisions.json maps ticket id -> {"status": "posting" | "done", "action": ..., "body": {...}, ...}.

"posting" is written BEFORE the POST. If the program dies during the POST, the next run finds "posting",
asks the helpdesk whether the ticket was handled, and if not re-sends exactly this saved decision (no new
LLM call). So a crash can never produce a second, different answer.

The helpdesk stays the source of truth: a run only ever looks at tickets the helpdesk says are open.
"""
import asyncio
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


class AlreadyRunning(Exception):
    """Another run holds the lock."""


@contextmanager
def single_run(path: str | Path = "state/run.lock"):
    """Allow only one run at a time, so two runs can never answer the same ticket.
    It is an operating-system file lock: if the process crashes, the OS releases it, so it never gets stuck."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise AlreadyRunning(f"another run is already in progress ({path})") from None
    try:
        yield
    finally:
        f.close()  # closing the file releases the lock


class StateFile:
    def __init__(self, path: str | Path = "state/decisions.json"):
        self.path = Path(path)
        self._lock = asyncio.Lock()  # one writer at a time once tickets run in parallel
        self._data: dict[str, dict] = (
            json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        )

    def get(self, ticket_id: str) -> dict | None:
        return self._data.get(ticket_id)

    async def save(self, ticket_id: str, **record) -> None:
        """Replace this ticket's record and write the whole file atomically."""
        async with self._lock:
            self._data[ticket_id] = {**record, "updated": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, ensure_ascii=False), encoding="utf-8")
            for attempt in range(10):
                try:
                    os.replace(tmp, self.path)  # atomic: the file is always either the old or the new version
                    break
                except PermissionError:  # Windows: antivirus/indexer may hold the file for a moment
                    if attempt == 9:
                        raise
                    await asyncio.sleep(0.05)

    def clear(self) -> None:
        self._data = {}
        self.path.unlink(missing_ok=True)
