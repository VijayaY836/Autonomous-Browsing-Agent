"""
server.py

FastAPI app that:
  - serves the static frontend (../frontend)
  - POST /api/run          -> starts a browser-agent task, returns a task_id
  - WS   /api/ws/{task_id} -> streams JSON step events for that task live
  - POST /api/stop/{id}    -> requests the running task stop early

Run with:  uvicorn server:app --reload --port 8000
"""

import asyncio
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from agent import Agent, OpenRouterClient
from browser_controller import BrowserController

app = FastAPI(title="Browser Agent")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# task_id -> asyncio.Queue of step dicts (None sentinel = stream finished)
_task_queues: dict[str, asyncio.Queue] = {}
_task_stop_flags: dict[str, bool] = {}


class RunRequest(BaseModel):
    api_key: str
    model: str = "openai/gpt-4o-mini"
    task: str
    start_url: Optional[str] = None
    headless: bool = True
    max_steps: int = 15


@app.post("/api/run")
async def run_task(req: RunRequest):
    task_id = uuid.uuid4().hex[:12]
    queue: asyncio.Queue = asyncio.Queue()
    _task_queues[task_id] = queue
    _task_stop_flags[task_id] = False

    asyncio.create_task(_execute(task_id, req, queue))
    return {"task_id": task_id}


@app.post("/api/stop/{task_id}")
async def stop_task(task_id: str):
    _task_stop_flags[task_id] = True
    return {"ok": True}


async def _execute(task_id: str, req: RunRequest, queue: asyncio.Queue):
    controller = BrowserController(headless=req.headless)

    async def on_step(payload: dict):
        payload["task_id"] = task_id
        await queue.put(payload)
        if _task_stop_flags.get(task_id):
            raise asyncio.CancelledError("stopped by user")

    try:
        await controller.start()
        llm = OpenRouterClient(api_key=req.api_key, model=req.model)
        agent = Agent(controller, llm, on_step=on_step, max_steps=req.max_steps)
        result = await agent.run(req.task, req.start_url)
        await queue.put({"type": "done", "task_id": task_id, **result})
    except asyncio.CancelledError:
        await queue.put({"type": "done", "task_id": task_id, "success": False, "result": "Stopped by user."})
    except Exception as e:
        await queue.put({"type": "error", "task_id": task_id, "message": f"Fatal error: {e}"})
        await queue.put({"type": "done", "task_id": task_id, "success": False, "result": f"Fatal error: {e}"})
    finally:
        await controller.stop()
        await queue.put(None)


@app.websocket("/api/ws/{task_id}")
async def ws_stream(websocket: WebSocket, task_id: str):
    await websocket.accept()
    queue = _task_queues.get(task_id)
    if queue is None:
        await websocket.send_json({"type": "error", "message": "unknown task_id"})
        await websocket.close()
        return
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            await websocket.send_json(item)
    except WebSocketDisconnect:
        pass
    finally:
        _task_queues.pop(task_id, None)
        _task_stop_flags.pop(task_id, None)


# ---- static frontend ----

@app.get("/")
async def index():
    return FileResponse(FRONTEND_DIR / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")