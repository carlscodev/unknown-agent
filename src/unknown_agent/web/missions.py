"""Mission control surface: DAG state, live telemetry and run control.

The dashboard's job is to make a long-running swarm legible: which agents are
working right now, on what, with which model, at what cost, and what they are
about to change on disk.  This module is the HTTP side of that.

Streaming uses **Server-Sent Events rather than a WebSocket**, deliberately.
Mission telemetry is one-directional and the events are already durable rows in
SQLite, so SSE buys automatic browser reconnection with ``Last-Event-ID``, and
survives the proxies and captive networks that silently drop long-lived
WebSocket upgrades.  Control actions are ordinary POSTs, which keeps them
auditable.

Because the event stream is read from the mission database rather than from
in-process memory, the dashboard works against a mission running in a detached
daemon — start a three-day run from the CLI, close the terminal, and open the
web panel from a different machine to watch it.  A reconnecting client replays
from its last cursor, so nothing is lost across a dropped connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from unknown_agent.core.engine import (
    LongHorizonEngine,
    MissionNode,
    MissionStatus,
    MissionStore,
    NodeStatus,
)
from unknown_agent.observability.logging import get_logger
from unknown_agent.orchestration.roles import ROLE_ORDER, roles_payload
from unknown_agent.orchestration.router import (
    Complexity,
    ModelRouter,
    RoutingError,
    RoutingRequest,
)

log = get_logger("web.missions")

__all__ = ["build_mission_router", "MissionCreate", "MissionControl", "TelemetrySnapshot"]

#: How often the SSE loop polls the ledger. Fast enough to feel live, slow
#: enough that fifty idle dashboards do not become a database benchmark.
POLL_INTERVAL_S = 0.5
#: Emitted when nothing happened, so proxies do not time the connection out.
KEEPALIVE_EVERY_S = 15.0


class MissionCreate(BaseModel):
    goal: str = Field(min_length=4, max_length=8000)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    complexity: int = Field(default=3, ge=1, le=5)


class MissionControl(BaseModel):
    action: str = Field(pattern="^(pause|resume|abandon)$")
    reason: str = ""


class TelemetrySnapshot(BaseModel):
    """Aggregated token and cost usage for one mission."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    #: Models whose tariff is unknown, so ``usd`` understates the true spend.
    unpriced_models: list[str] = Field(default_factory=list)
    by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)


def _store_for(project_root: Path) -> MissionStore:
    return MissionStore(project_root / ".unknown" / "memory" / "memory.db")


def _sse(event: str, data: Any, *, event_id: int | None = None) -> str:
    """One SSE frame. ``id:`` is what lets a browser resume after a drop."""

    lines: list[str] = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(data, default=str, ensure_ascii=False)
    for row in payload.splitlines() or [""]:
        lines.append(f"data: {row}")
    return "\n".join(lines) + "\n\n"


def _aggregate_telemetry(store: MissionStore, mission_id: str, router: ModelRouter) -> TelemetrySnapshot:
    """Sum usage across every recorded model turn, and price what we can."""

    snapshot = TelemetrySnapshot()
    cursor = 0
    while True:
        batch = store.events(mission_id, after_id=cursor, limit=1000)
        if not batch:
            break
        cursor = batch[-1]["id"]
        for event in batch:
            payload = event.get("payload") or {}
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                continue
            model = str(payload.get("model") or usage.get("model") or "unknown")
            prompt = int(usage.get("prompt_tokens", 0) or 0)
            completion = int(usage.get("completion_tokens", 0) or 0)
            cache_read = int(usage.get("cache_read_tokens", 0) or 0)
            cache_write = int(usage.get("cache_write_tokens", 0) or 0)

            snapshot.input_tokens += prompt
            snapshot.output_tokens += completion
            snapshot.cache_read_tokens += cache_read
            snapshot.cache_write_tokens += cache_write

            bucket = snapshot.by_model.setdefault(
                model,
                {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
                 "cache_write_tokens": 0, "usd": 0.0, "priced": True, "turns": 0},
            )
            bucket["input_tokens"] += prompt
            bucket["output_tokens"] += completion
            bucket["cache_read_tokens"] += cache_read
            bucket["cache_write_tokens"] += cache_write
            bucket["turns"] += 1

            from unknown_agent.orchestration.router import card_for

            card = card_for(model)
            if card is None:
                bucket["priced"] = False
                continue
            estimate = router.estimate_cost(
                card,
                input_tokens=prompt,
                output_tokens=completion,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            if estimate.priced and estimate.usd is not None:
                bucket["usd"] = round(float(bucket["usd"]) + estimate.usd, 6)
                snapshot.usd = round(snapshot.usd + estimate.usd, 6)
            else:
                bucket["priced"] = False

    snapshot.unpriced_models = sorted(
        name for name, bucket in snapshot.by_model.items() if not bucket["priced"]
    )
    return snapshot


def _dag_payload(mission_dict: dict[str, Any]) -> dict[str, Any]:
    """Shape the DAG for a graph renderer: nodes plus explicit edges."""

    nodes = mission_dict.get("nodes", [])
    edges = [
        {"from": dep, "to": node["key"]}
        for node in nodes
        for dep in node.get("depends_on", [])
    ]
    lanes: dict[str, list[str]] = {}
    for node in nodes:
        lanes.setdefault(node.get("role", "unknown"), []).append(node["key"])
    active = [node["key"] for node in nodes if node.get("status") == NodeStatus.RUNNING.value]
    return {
        "nodes": nodes,
        "edges": edges,
        "lanes": lanes,
        "active": active,
        "counts": {
            status.value: sum(1 for node in nodes if node.get("status") == status.value)
            for status in NodeStatus
        },
    }


def build_mission_router(
    *,
    project_root: Path,
    guard: Callable[[Request], None] | None = None,
) -> APIRouter:
    """Mission endpoints, mounted under ``/api``.

    ``guard`` is the host's authentication check; it receives the request and
    raises if the caller is not allowed.  Injecting it keeps this module free of
    any opinion about how the panel authenticates.
    """

    router = APIRouter(prefix="/api/missions", tags=["missions"])

    def _check(request: Request) -> None:
        if guard is not None:
            guard(request)

    def _engine() -> LongHorizonEngine:
        return LongHorizonEngine(
            project_root=project_root, install_signal_handlers=False
        )

    # -- listing and detail ------------------------------------------------
    @router.get("")
    async def list_missions(request: Request, limit: int = 50) -> JSONResponse:
        _check(request)
        store = _store_for(project_root)
        try:
            return JSONResponse({"missions": store.list_missions(limit=min(200, max(1, limit)))})
        finally:
            store.close()

    @router.post("")
    async def create_mission(request: Request, body: MissionCreate) -> JSONResponse:
        _check(request)
        engine = _engine()
        try:
            nodes: list[MissionNode] = []
            for index, raw in enumerate(body.nodes):
                key = str(raw.get("key") or f"n{index + 1}")
                nodes.append(
                    MissionNode(
                        key=key,
                        title=str(raw.get("title") or key),
                        role=str(raw.get("role") or "coder"),
                        depends_on=tuple(str(item) for item in raw.get("depends_on", [])),
                        max_attempts=int(raw.get("max_attempts", 3)),
                        complexity=int(raw.get("complexity", body.complexity)),
                    )
                )
            if not nodes:
                nodes = [MissionNode(key="n1", title=body.goal[:120], role="planner")]
            mission = engine.start_mission(body.goal, nodes, meta={"origin": "web"})
            return JSONResponse({"mission": mission.to_dict()}, status_code=201)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller as 400
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            engine.close()

    @router.get("/routing")
    async def routing_plan(request: Request, complexity: int = 3) -> JSONResponse:
        """Which model each role would get right now, and why."""

        _check(request)
        model_router = ModelRouter()
        level = Complexity.coerce(complexity, Complexity.MEDIUM)
        plan: dict[str, Any] = {}
        for role_id in ROLE_ORDER:
            try:
                decision = model_router.route(RoutingRequest(role_id=role_id, complexity=level))
            except RoutingError as exc:
                plan[role_id] = {"error": str(exc)}
                continue
            plan[role_id] = decision.to_dict()
        return JSONResponse(
            {
                "complexity": level.value,
                "plan": plan,
                "roles": roles_payload(),
                "available_models": [card.id for card in model_router.available_cards()],
            }
        )

    @router.get("/{mission_id}")
    async def get_mission(request: Request, mission_id: str) -> JSONResponse:
        _check(request)
        store = _store_for(project_root)
        try:
            mission = store.load_mission(mission_id)
            if mission is None:
                raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
            payload = mission.to_dict()
            model_router = ModelRouter()
            return JSONResponse(
                {
                    "mission": payload,
                    "dag": _dag_payload(payload),
                    "telemetry": _aggregate_telemetry(store, mission_id, model_router).model_dump(),
                    "heartbeat": store.last_beat(mission_id),
                    "checkpoints": store.checkpoints(mission_id, limit=10),
                }
            )
        finally:
            store.close()

    @router.get("/{mission_id}/events")
    async def mission_events(request: Request, mission_id: str, after: int = 0) -> StreamingResponse:
        """Live telemetry as SSE.

        The browser's ``Last-Event-ID`` header takes precedence over ``after``,
        so an automatic reconnection resumes exactly where it left off without
        the client having to track anything.
        """

        _check(request)
        resume_header = request.headers.get("last-event-id", "")
        cursor = after
        if resume_header.isdigit():
            cursor = int(resume_header)

        async def stream() -> AsyncIterator[str]:
            store = _store_for(project_root)
            model_router = ModelRouter()
            local_cursor = cursor
            last_emit = time.monotonic()
            last_status: str | None = None
            try:
                mission = store.load_mission(mission_id)
                if mission is None:
                    yield _sse("error", {"detail": f"no mission {mission_id!r}"})
                    return

                payload = mission.to_dict()
                yield _sse("snapshot", {"mission": payload, "dag": _dag_payload(payload)})
                last_status = payload["status"]

                while True:
                    if await request.is_disconnected():
                        break

                    batch = store.events(mission_id, after_id=local_cursor, limit=200)
                    if batch:
                        local_cursor = batch[-1]["id"]
                        for event in batch:
                            yield _sse(
                                event["kind"],
                                {
                                    "node": event["node_key"],
                                    "ts": event["ts"],
                                    "payload": event["payload"],
                                },
                                event_id=event["id"],
                            )
                        # The DAG only needs re-sending when something moved.
                        current = store.load_mission(mission_id)
                        if current is not None:
                            fresh = current.to_dict()
                            yield _sse("dag", _dag_payload(fresh))
                            yield _sse(
                                "telemetry",
                                _aggregate_telemetry(store, mission_id, model_router).model_dump(),
                            )
                            if fresh["status"] != last_status:
                                last_status = fresh["status"]
                                yield _sse("status", {"status": last_status})
                            if MissionStatus(fresh["status"]).terminal:
                                yield _sse("done", {"status": last_status})
                                break
                        last_emit = time.monotonic()
                    elif time.monotonic() - last_emit > KEEPALIVE_EVERY_S:
                        # A comment frame keeps proxies from closing an idle
                        # stream without being delivered to the app as an event.
                        yield ": keepalive\n\n"
                        last_emit = time.monotonic()

                    await asyncio.sleep(POLL_INTERVAL_S)
            except asyncio.CancelledError:  # pragma: no cover - client vanished
                raise
            except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
                log.warning("mission stream %s failed: %s", mission_id, exc)
                with contextlib.suppress(Exception):
                    yield _sse("error", {"detail": "stream interrupted"})
            finally:
                store.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                # Nginx buffers SSE into uselessness without this.
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/{mission_id}/telemetry")
    async def mission_telemetry(request: Request, mission_id: str) -> JSONResponse:
        _check(request)
        store = _store_for(project_root)
        try:
            if store.load_mission(mission_id) is None:
                raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")
            snapshot = _aggregate_telemetry(store, mission_id, ModelRouter())
            return JSONResponse(snapshot.model_dump())
        finally:
            store.close()

    @router.post("/{mission_id}/control")
    async def control_mission(
        request: Request, mission_id: str, body: MissionControl
    ) -> JSONResponse:
        """Pause, resume or abandon a mission — including a detached one.

        Control is expressed as a durable status change rather than a signal,
        so it reaches a worker in another process on its next checkpoint.
        """

        _check(request)
        store = _store_for(project_root)
        try:
            mission = store.load_mission(mission_id)
            if mission is None:
                raise HTTPException(status_code=404, detail=f"no mission {mission_id!r}")

            if body.action == "pause":
                target = MissionStatus.PAUSED
            elif body.action == "resume":
                target = MissionStatus.RUNNING
            else:
                target = MissionStatus.ABANDONED

            if mission.status.terminal and body.action != "resume":
                raise HTTPException(
                    status_code=409,
                    detail=f"mission is already {mission.status.value}",
                )

            store.set_mission_status(mission_id, target)
            store.record_event(
                mission_id, f"mission.{body.action}", reason=body.reason, source="web"
            )
            return JSONResponse({"mission_id": mission_id, "status": target.value})
        finally:
            store.close()

    @router.get("/{mission_id}/nodes/{node_key}")
    async def node_detail(request: Request, mission_id: str, node_key: str) -> JSONResponse:
        _check(request)
        store = _store_for(project_root)
        try:
            mission = store.load_mission(mission_id)
            if mission is None or node_key not in mission.nodes:
                raise HTTPException(status_code=404, detail="no such node")
            node = mission.nodes[node_key]
            events = [
                event
                for event in store.events(mission_id, limit=1000)
                if event["node_key"] == node_key
            ]
            return JSONResponse(
                {
                    "node": node.to_dict(),
                    "events": events[-100:],
                    "fingerprints": [
                        item.to_dict() for item in store.fingerprints(mission_id, node_key=node_key)
                    ],
                }
            )
        finally:
            store.close()

    return router
