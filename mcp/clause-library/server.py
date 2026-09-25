"""clause-library -- the legal team's clause library, as a small local MCP
server bundled with the Contract Reviewer starter. AgentBox scans it, builds it
into its own container and reaches it only through MCP Bridge, under an
app-scoped name such as `<app>__clause-library`.

Three operations, chosen because the bridge treats each differently:

    find_standard_clause(topic)              reads; declared read-only, never held
    save_redline(contract_id, section, text) writes; an ordinary call
    delete_all_redlines(contract_id)         classified DESTRUCTIVE by the
                                             bridge's own classifier, so it is
                                             held for an operator's approval

Records live in a file, seeded on first start from the bundled `clauses.json`
(eight standard clauses). The path is this container's own writable layer,
which survives a restart but not a `docker rm`.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

# host must be 0.0.0.0, not FastMCP's 127.0.0.1 default -- this server runs in
# its own container, reachable from managed-mcp-bridge over the docker
# network, not from a process sharing its own loopback.
mcp = FastMCP("clause-library", host="0.0.0.0")

_STORE = Path(os.environ.get("CLAUSE_STORE_PATH", "/data/clause-library.json"))
_SEED = Path(__file__).resolve().parent / "clauses.json"
_LOCK = threading.Lock()


def _seed() -> dict:
    clauses = json.loads(_SEED.read_text(encoding="utf-8")) if _SEED.is_file() else []
    return {"clauses": clauses, "redlines": []}


def _read() -> dict:
    try:
        raw = _STORE.read_text(encoding="utf-8")
    except OSError:
        return _seed()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # A truncated file (killed mid-write) should not make every later
        # call fail. Start over from the seed rather than raise.
        return _seed()
    if not isinstance(data, dict):
        return _seed()
    data.setdefault("clauses", [])
    data.setdefault("redlines", [])
    return data


def _write(data: dict) -> None:
    _STORE.parent.mkdir(parents=True, exist_ok=True)
    # Write-then-rename: a crash mid-write leaves the previous good file in
    # place instead of a half-written one.
    tmp = _STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(_STORE)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def find_standard_clause(topic: str) -> dict:
    """The company's standard wording on a topic, such as liability, notice, renewal, governing law or ip."""
    needle = (topic or "").strip().lower()
    if not needle:
        return {"matches": [], "count": 0, "error": "topic must not be empty"}
    with _LOCK:
        clauses = _read()["clauses"]
    matches = [
        c
        for c in clauses
        if needle in str(c.get("topic", "")).lower()
        or needle in str(c.get("title", "")).lower()
        or needle in str(c.get("text", "")).lower()
    ]
    return {"matches": matches[:5], "count": len(matches)}


@mcp.tool()
def save_redline(contract_id: str, section: str, text: str) -> dict:
    """A redline saved for one section of a contract; that contract's redline count comes back."""
    contract_id = (contract_id or "").strip()
    section = (section or "").strip()
    text = (text or "").strip()
    if not contract_id or not section or not text:
        return {"saved": False, "error": "contract_id, section and text are all required"}
    with _LOCK:
        data = _read()
        redline = {
            "id": max((r.get("id", 0) for r in data["redlines"]), default=0) + 1,
            "contract_id": contract_id,
            "section": section,
            "text": text[:4000],
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        data["redlines"].append(redline)
        _write(data)
        count = sum(1 for r in data["redlines"] if r["contract_id"] == contract_id)
    return {"saved": True, "id": redline["id"], "contract_id": contract_id, "redlines": count}


@mcp.tool()
def delete_all_redlines(contract_id: str) -> dict:
    """Deletes every redline saved for a contract.

    Destructive on purpose, and named so the bridge can tell: this is the
    starter's example of an action AgentBox holds for a manager rather than
    running on request. Approved in the admin console, the same call is made
    again and the redlines go.
    """
    contract_id = (contract_id or "").strip()
    if not contract_id:
        return {"deleted": 0, "error": "contract_id is required"}
    with _LOCK:
        data = _read()
        keep = [r for r in data["redlines"] if r["contract_id"] != contract_id]
        deleted = len(data["redlines"]) - len(keep)
        data["redlines"] = keep
        _write(data)
    return {"deleted": deleted, "contract_id": contract_id, "remaining": len(keep)}


if __name__ == "__main__":
    # sse, not FastMCP's stdio default: this runs as its own detached
    # container and the bridge reaches it over HTTP at <container>:8000/sse.
    # The retry loop absorbs the first seconds after joining a busy bridge
    # network, when uvicorn's SSE app can exit cleanly on a failed startup.
    for attempt in range(10):
        try:
            mcp.run(transport="sse")
            break
        except BaseException as exc:
            print(f"clause-library: startup attempt {attempt} failed: {exc!r}", flush=True)
            time.sleep(1)
