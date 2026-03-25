import asyncio
import json
import os
import uuid
from pathlib import Path

# Disable OpenTelemetry SDK — it crashes when LangGraph runs parallel async nodes
# because ContextVar tokens get created/reset across different async contexts.
# Langfuse has its own tracing and does not need the OTEL SDK.
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

# Defensive patch: suppress the ContextVar ValueError if OTEL is still active
# (e.g. imported before the env var takes effect)
try:
    from opentelemetry.context.contextvars_context import ContextVarsRuntimeContext

    _original_detach = ContextVarsRuntimeContext.detach

    def _safe_detach(self, token):
        try:
            _original_detach(self, token)
        except ValueError:
            pass

    ContextVarsRuntimeContext.detach = _safe_detach
except ImportError:
    pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from src.graph import plan_graph, exec_graph, full_graph
from src.worktree import cleanup_all_worktrees
from src.store import save_run, get_run, list_runs
from src.agents import MODEL_SCANNER, MODEL_ARCHITECT, MODEL_WORKER, MODEL_REVIEWER

PROJECTS_ROOT = "/projects"

app = FastAPI(title="AI Workforce Orchestrator")

# Track active runs so they can be cancelled
_active_runs: dict[str, bool] = {}  # run_id -> cancelled flag


# ── Models ───────────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    project: str
    task: str
    mode: str = "auto"  # "auto" or "supervised"


class SubtaskResult(BaseModel):
    id: str
    description: str
    status: str


def _initial_state(project: str, task: str, workspace: str,
                    run_id: str, mode: str = "auto") -> dict:
    return {
        "project": project,
        "task": task,
        "workspace": workspace,
        "project_context": "",
        "run_id": run_id,
        "session_id": project,
        "mode": mode,
        "messages": [],
        "subtasks": [],
        "worktrees": [],
        "overlap_detected": False,
        "review_feedback": {},
        "review_round": 0,
        "merge_results": {},
        "final_output": "",
    }


# ── SSE helper ───────────────────────────────────────────────────────────────

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _stream_graph_events(graph, state: dict, run_id: str,
                                project: str, task: str, workspace: str,
                                final_status: str = "completed"):
    """Yield SSE events from a graph execution, persisting state after each phase."""
    last_state = state
    _active_runs[run_id] = False  # not cancelled

    try:
        async for event in graph.astream_events(
            state, version="v2", config={"recursion_limit": 150},
        ):
            # Check if run was cancelled
            if _active_runs.get(run_id):
                save_run(run_id, project, task, workspace, "stopped", last_state)
                # Do NOT cleanup worktrees — code is preserved for resume
                yield _sse("stopped", {"run_id": run_id, "message": "Run stopped by user"})
                return

            kind = event.get("event", "")

            if kind == "on_chain_start" and event.get("name"):
                yield _sse("phase", {"node": event["name"], "status": "started"})

            elif kind == "on_chain_end" and event.get("name"):
                node = event["name"]
                output = event.get("data", {}).get("output", {})

                # astream_events can return non-dict outputs — skip them
                if not isinstance(output, dict):
                    continue

                payload = {"node": node, "status": "completed"}

                if "subtasks" in output:
                    payload["subtasks"] = [
                        {"id": s["id"], "description": s["description"],
                         "files_touched": s.get("files_touched", []),
                         "status": s["status"]}
                        for s in output["subtasks"]
                    ]
                if "overlap_detected" in output:
                    payload["overlap_detected"] = output["overlap_detected"]
                if "worktrees" in output:
                    payload["worktrees"] = [
                        {"id": wt["id"], "status": wt["status"]}
                        for wt in output["worktrees"]
                    ]
                if "review_feedback" in output:
                    payload["review_feedback"] = output["review_feedback"]
                if "review_round" in output:
                    payload["review_round"] = output["review_round"]
                if "merge_results" in output:
                    payload["merge_results"] = output["merge_results"]
                if "final_output" in output:
                    payload["final_output"] = output["final_output"]

                yield _sse("phase", payload)

                # Merge output into state, but skip messages (not JSON-serializable)
                safe_output = {k: v for k, v in output.items() if k != "messages"} if isinstance(output, dict) else {}
                last_state = {**last_state, **safe_output}
                save_run(run_id, project, task, workspace, "running", last_state)

        save_run(run_id, project, task, workspace, final_status, last_state)
        _active_runs.pop(run_id, None)

    except asyncio.CancelledError:
        # Client disconnected (e.g. abort from stop button)
        save_run(run_id, project, task, workspace, "stopped", last_state)
        _active_runs.pop(run_id, None)
    except Exception as e:
        save_run(run_id, project, task, workspace, "failed", last_state)
        _active_runs.pop(run_id, None)
        # Do NOT cleanup worktrees — code is preserved for resume
        yield _sse("error", {"message": str(e), "run_id": run_id})


# ── API Endpoints ────────────────────────────────────────────────────────────

@app.get("/api/projects")
def api_list_projects():
    if not os.path.isdir(PROJECTS_ROOT):
        return {"projects": []}
    projects = [
        d for d in sorted(os.listdir(PROJECTS_ROOT))
        if os.path.isdir(os.path.join(PROJECTS_ROOT, d)) and not d.startswith(".")
    ]
    return {"projects": projects}


@app.get("/api/runs")
def api_list_runs(project: str | None = None):
    return {"runs": list_runs(project=project)}


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.post("/api/run/stream")
async def run_task_stream(req: TaskRequest):
    """Submit a new task. In supervised mode, pauses after planning."""
    run_id = str(uuid.uuid4())
    workspace = os.path.join(PROJECTS_ROOT, req.project)
    mode = req.mode if req.mode in ("auto", "supervised") else "auto"

    cleanup_all_worktrees(workspace)

    state = _initial_state(req.project, req.task, workspace, run_id, mode)
    save_run(run_id, req.project, req.task, workspace, "running", state)

    if mode == "auto":
        # Run the full pipeline
        async def stream():
            yield _sse("started", {"run_id": run_id, "project": req.project, "mode": "auto"})
            async for chunk in _stream_graph_events(
                full_graph, state, run_id, req.project, req.task, workspace
            ):
                yield chunk
            yield _sse("done", {"run_id": run_id})
        return StreamingResponse(stream(), media_type="text/event-stream")
    else:
        # Supervised: only run the plan graph, then pause
        async def stream():
            yield _sse("started", {"run_id": run_id, "project": req.project, "mode": "supervised"})
            async for chunk in _stream_graph_events(
                plan_graph, state, run_id, req.project, req.task, workspace,
                final_status="awaiting_approval",
            ):
                yield chunk
            yield _sse("awaiting_approval", {"run_id": run_id})
        return StreamingResponse(stream(), media_type="text/event-stream")


class ApproveRequest(BaseModel):
    subtasks: list[dict] | None = None  # optional modified plan


@app.post("/api/runs/{run_id}/approve")
async def approve_run(run_id: str, req: ApproveRequest | None = None):
    """Approve a supervised run's plan and continue execution.
    Optionally pass modified subtasks to override the plan."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run["status"] != "awaiting_approval":
        raise HTTPException(status_code=400,
                            detail=f"Run status is '{run['status']}', not awaiting_approval")

    state = run["state"]
    state["messages"] = []  # Reset messages for clean graph execution

    # Apply modified plan if provided
    if req and req.subtasks:
        state["subtasks"] = req.subtasks
        # Recalculate overlap
        all_files: dict[str, int] = {}
        for s in state["subtasks"]:
            for f in s.get("files_touched", []):
                all_files[f] = all_files.get(f, 0) + 1
        state["overlap_detected"] = any(c > 1 for c in all_files.values())

    save_run(run_id, run["project"], run["task"], run["workspace"], "running", state)

    async def stream():
        yield _sse("started", {"run_id": run_id, "project": run["project"], "mode": "executing"})
        async for chunk in _stream_graph_events(
            exec_graph, state, run_id, run["project"], run["task"], run["workspace"]
        ):
            yield chunk
        yield _sse("done", {"run_id": run_id})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/resume")
async def resume_run(run_id: str):
    """Resume a failed run from its last saved state."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    if run["status"] == "completed":
        raise HTTPException(status_code=400, detail="Run already completed")

    # Clean up stale worktrees/branches from the failed attempt
    cleanup_all_worktrees(run["workspace"])

    state = run["state"]
    state["review_feedback"] = {}
    state["review_round"] = 0
    state["worktrees"] = []  # Force re-creation of worktrees
    state["messages"] = []   # Reset messages — serialized messages don't deserialize back

    save_run(run_id, run["project"], run["task"], run["workspace"], "running", state)

    async def stream():
        yield _sse("started", {"run_id": run_id, "project": run["project"], "mode": "resuming"})
        async for chunk in _stream_graph_events(
            exec_graph, state, run_id, run["project"], run["task"], run["workspace"]
        ):
            yield chunk
        yield _sse("done", {"run_id": run_id})

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    """Signal a running task to stop at the next checkpoint."""
    if run_id in _active_runs:
        _active_runs[run_id] = True  # set cancel flag
        return {"status": "stopping", "run_id": run_id}
    # If not in memory, mark as stopped in DB
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if run["status"] == "running":
        save_run(run_id, run["project"], run["task"], run["workspace"], "stopped", run["state"])
    return {"status": "stopped", "run_id": run_id}


@app.get("/api/config")
def api_config():
    """Return current role → model mapping."""
    return {
        "roles": {
            "scanner": MODEL_SCANNER,
            "architect": MODEL_ARCHITECT,
            "worker": MODEL_WORKER,
            "reviewer": MODEL_REVIEWER,
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Web UI ───────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def ui():
    html_path = Path(__file__).parent / "ui.html"
    return html_path.read_text()
