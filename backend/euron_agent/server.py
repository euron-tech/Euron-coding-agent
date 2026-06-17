"""FastAPI server: WebSocket (streaming + approvals) and a REST fallback.

Cloud-ready: optional bearer-token auth (`--token` / $EURON_AGENT_TOKEN), bind to
any host, dynamic port, cancel/undo, auto-approve, and cross-restart persistence.

WebSocket protocol (JSON both directions)
-----------------------------------------
Client -> server:
  {"type":"init","workspace_path":"...","provider":"euri"?,"model":"..."?,
   "api_key":"..."?,"base_url":"..."?,"auto_approve":bool?,"persist":bool?,"token":"..."?}
  {"type":"run","task":"..."}
  {"type":"approval","id":"...","approved":bool,"feedback":"..."?}
  {"type":"cancel"}            # stop the running task
  {"type":"undo"}              # revert the last turn's file changes
  {"type":"ping"}

Server -> client: see euron_agent.events.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import events as ev
from .config import Config, load_config
from .events import AgentIO, ApprovalDecision
from .loop import AgentSession

app = FastAPI(title="Euron Agent", version="0.2.0")
app.state.token = None  # set by serve(); None disables auth (local dev/tests)


def _auth_ok(provided: Optional[str]) -> bool:
    expected = app.state.token
    return expected is None or (provided is not None and secrets.compare_digest(provided, expected))


# --------------------------------------------------------------------------- #
# WebSocket IO
# --------------------------------------------------------------------------- #
class WebSocketIO(AgentIO):
    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.loop = loop
        self.pending: dict[str, asyncio.Future] = {}

    def emit_sync(self, event: dict) -> None:
        asyncio.run_coroutine_threadsafe(self._safe_send(event), self.loop)

    async def _safe_send(self, event: dict) -> None:
        try:
            await self.ws.send_json(event)
        except Exception:
            pass

    async def emit(self, event: dict) -> None:
        await self._safe_send(event)

    async def request_approval(self, request: dict) -> ApprovalDecision:
        fut: asyncio.Future = self.loop.create_future()
        self.pending[request["id"]] = fut
        await self.ws.send_json(request)
        return await fut

    def resolve_approval(self, data: dict) -> None:
        fut = self.pending.pop(data.get("id"), None)
        if fut and not fut.done():
            fut.set_result(
                ApprovalDecision(bool(data.get("approved", False)), data.get("feedback"))
            )


def _config_for(data: dict) -> Config:
    return load_config(
        provider=data.get("provider"),
        model=data.get("model"),
        api_key=data.get("api_key"),
        base_url=data.get("base_url"),
    )


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    io = WebSocketIO(ws, loop)
    session: Optional[AgentSession] = None
    running: Optional[asyncio.Task] = None

    try:
        while True:
            data = await ws.receive_json()
            kind = data.get("type")

            if kind == "init":
                if not _auth_ok(data.get("token")):
                    await ws.send_json(ev.error("Unauthorized: bad or missing token."))
                    await ws.close()
                    return
                cfg = _config_for(data)
                if data.get("auto_approve"):
                    cfg.agent.auto_approve_writes = True
                    cfg.agent.auto_approve_commands = True
                session = AgentSession(
                    data["workspace_path"], cfg, io, persist=bool(data.get("persist"))
                )
                await ws.send_json(
                    ev.status(f"ready · {cfg.provider.name} · {cfg.provider.model}")
                )

            elif kind == "run":
                if session is None:
                    await ws.send_json(ev.error("Send an 'init' message first."))
                    continue
                if running and not running.done():
                    await ws.send_json(ev.error("A task is already running."))
                    continue
                running = asyncio.create_task(session.run(data["task"]))

            elif kind == "approval":
                io.resolve_approval(data)

            elif kind == "cancel":
                if session:
                    session.cancel()

            elif kind == "undo":
                if session:
                    reverted = session.undo()
                    await ws.send_json(
                        ev.info(
                            f"reverted {len(reverted)} file(s): {', '.join(reverted)}"
                            if reverted
                            else "nothing to undo"
                        )
                    )

            elif kind == "ping":
                await ws.send_json({"type": "pong"})

            else:
                await ws.send_json(ev.error(f"Unknown message type: {kind}"))

    except WebSocketDisconnect:
        if running and not running.done():
            if session:
                session.cancel()
            running.cancel()


# --------------------------------------------------------------------------- #
# REST fallback (one-shot, non-interactive)
# --------------------------------------------------------------------------- #
class BufferIO(AgentIO):
    def __init__(self, auto_approve: bool):
        self.events: list[dict] = []
        self.auto_approve = auto_approve

    def emit_sync(self, event: dict) -> None:
        self.events.append(event)

    async def emit(self, event: dict) -> None:
        self.events.append(event)

    async def request_approval(self, request: dict) -> ApprovalDecision:
        self.events.append(request)
        return ApprovalDecision(approved=self.auto_approve, feedback=None)


class RunRequest(BaseModel):
    task: str
    workspace_path: str
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    auto_approve: bool = False


@app.post("/agent/run")
async def agent_run(req: RunRequest, authorization: Optional[str] = Header(default=None)):
    token = authorization.replace("Bearer ", "") if authorization else None
    if not _auth_ok(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    cfg = load_config(
        provider=req.provider, model=req.model, api_key=req.api_key, base_url=req.base_url
    )
    if req.auto_approve:
        cfg.agent.auto_approve_writes = True
        cfg.agent.auto_approve_commands = True
    io = BufferIO(auto_approve=req.auto_approve)
    session = AgentSession(req.workspace_path, cfg, io)
    await session.run(req.task)
    final = next(
        (e["text"] for e in reversed(io.events) if e["type"] == "assistant_message"), ""
    )
    return {
        "status": "ok",
        "provider": cfg.provider.name,
        "model": cfg.provider.model,
        "final": final,
        "session_tokens": session.session_tokens,
        "events": io.events,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/providers")
async def providers():
    cfg = load_config()
    return {
        "active": cfg.provider.name,
        "providers": {
            name: {"type": p.type, "model": p.model, "base_url": p.base_url}
            for name, p in cfg.all_providers.items()
        },
    }


# --------------------------------------------------------------------------- #
# Server entry
# --------------------------------------------------------------------------- #
def _free_port(host: str) -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host if host != "0.0.0.0" else "", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = False,
    token: Optional[str] = None,
    auth: bool = True,
) -> None:
    import uvicorn

    if port == 0:
        port = _free_port(host)

    # Resolve the auth token: explicit > env > generated (unless auth disabled).
    resolved = token or os.getenv("EURON_AGENT_TOKEN")
    if auth and not resolved:
        resolved = secrets.token_urlsafe(24)
    app.state.token = resolved if auth else None

    # Announce port (and token) on stdout so a parent process can discover them.
    print(f"EURON_AGENT_LISTENING http://{host}:{port}", flush=True)
    if app.state.token:
        print(f"EURON_AGENT_TOKEN {app.state.token}", flush=True)

    uvicorn.run(
        "euron_agent.server:app" if reload else app, host=host, port=port, reload=reload
    )
