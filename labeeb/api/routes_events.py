"""Server-Sent Events (SSE) streaming API for real-time UI updates."""
from __future__ import annotations

import asyncio
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from labeeb.config import Config
from labeeb.core.controller import LabeebController
from labeeb.core.events import global_event_bus, read_domain_events
from labeeb.models import utc_now

router = APIRouter(prefix="/api/goals", tags=["events"])


@router.get("/{goal_id}/events")
async def goal_events_stream(goal_id: str, request: Request):
    """Stream live events and phase changes for a goal via Server-Sent Events."""
    config: Config = request.app.state.config
    ctl = LabeebController(config, goal_id)
    if not ctl.paths.state.exists() and goal_id != "*":
        raise HTTPException(status_code=404, detail=f"Goal {goal_id} not found")

    async def event_generator():
        queue = await global_event_bus.subscribe(goal_id)
        try:
            # Yield initial sync event with current state
            initial_state = ctl.status() if ctl.paths.state.exists() else {}
            init_data = json.dumps(
                {
                    "event_type": "goal.synced",
                    "goal_id": goal_id,
                    "timestamp": utc_now(),
                    "data": initial_state,
                }
            )
            yield f"event: goal.synced\ndata: {init_data}\n\n"

            events_file = ctl.paths.events
            seen_ids: set[str] = set()
            last_line = 0

            # Replay historical journal events on connect/reconnect
            if events_file.exists():
                hist_events, last_line = read_domain_events(events_file, after_line=0)
                for ev in hist_events:
                    eid = ev.get("event_id")
                    if eid:
                        seen_ids.add(eid)
                    ev_type = ev.get("event_type", "event")
                    yield f"event: {ev_type}\ndata: {json.dumps(ev)}\n\n"

            idle_ticks = 0
            while True:
                if await request.is_disconnected():
                    break

                got_event = False
                # 1. Drain new events from file (cross-process support)
                if events_file.exists():
                    new_events, last_line = read_domain_events(events_file, after_line=last_line)
                    for ev in new_events:
                        eid = ev.get("event_id")
                        if eid and eid in seen_ids:
                            continue
                        if eid:
                            seen_ids.add(eid)
                        ev_type = ev.get("event_type", "event")
                        yield f"event: {ev_type}\ndata: {json.dumps(ev)}\n\n"
                        got_event = True

                # 2. Drain any live in-memory event from queue with short timeout
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    eid = event.event_id
                    if not (eid and eid in seen_ids):
                        if eid:
                            seen_ids.add(eid)
                        payload = json.dumps(event.to_dict())
                        yield f"event: {event.event_type}\ndata: {payload}\n\n"
                        got_event = True
                except asyncio.TimeoutError:
                    pass

                if len(seen_ids) > 1000:
                    seen_ids.clear()

                if got_event:
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                    if idle_ticks >= 15:
                        yield ": keepalive\n\n"
                        idle_ticks = 0
        finally:
            await global_event_bus.unsubscribe(goal_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
