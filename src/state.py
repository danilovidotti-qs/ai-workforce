from operator import or_
from typing import Annotated, Literal
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages


class Subtask(TypedDict):
    id: str                          # e.g. "subtask-1"
    description: str                 # what to do
    files_touched: list[str]         # predicted file paths (relative to workspace)
    assigned_worktree: str           # worktree id
    status: Literal["pending", "in_progress", "done", "failed"]
    result: str                      # agent output / summary


class WorktreeInfo(TypedDict):
    id: str                          # e.g. "wt-0"
    branch_name: str                 # e.g. "wt/run-abc/subtask-1"
    path: str                        # absolute path to worktree directory
    subtask_ids: list[str]           # which subtasks are assigned here
    status: Literal["created", "working", "review", "merged", "cleaned"]
    diff_summary: str                # populated after work is done


def _replace_by_id(existing: list[dict], updates: list[dict]) -> list[dict]:
    """Reducer: merge two lists of dicts by 'id', updates overwrite existing."""
    merged = {item["id"]: item for item in existing}
    for item in updates:
        merged[item["id"]] = item
    return list(merged.values())


class AgentState(TypedDict):
    # ── Core task info ──
    project: str
    task: str
    workspace: str                   # main repo path (not a worktree)
    project_context: str             # scanner output: project structure summary

    # ── Tracing ──
    run_id: str
    session_id: str

    # ── LangGraph internals ──
    messages: Annotated[list, add_messages]

    # ── Execution mode ──
    mode: Literal["auto", "supervised"]  # auto = run through, supervised = pause after plan

    # ── Pipeline state (reducers allow concurrent updates from parallel workers) ──
    subtasks: Annotated[list[Subtask], _replace_by_id]
    worktrees: Annotated[list[WorktreeInfo], _replace_by_id]
    overlap_detected: bool           # True = subtasks share files → sequential

    # ── Review loop ──
    review_feedback: Annotated[dict[str, str], or_]  # worktree_id -> feedback
    review_round: int                # current review iteration (max 3)

    # ── Results ──
    merge_results: Annotated[dict[str, str], or_]  # worktree_id -> "success" | error
    final_output: str
