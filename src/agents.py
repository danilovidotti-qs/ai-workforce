"""Agent nodes for the worktree-based autonomous pipeline.

Phases: plan → assign → execute → review (loop) → merge
"""

import json
import logging
import os
import pathlib
import re

from langchain_openai import ChatOpenAI

logger = logging.getLogger("agents")
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.prebuilt import create_react_agent

from src.state import AgentState, Subtask, WorktreeInfo
from src.callbacks import get_langfuse_handler
from src.tools import make_file_tools
from src.worktree import (
    ensure_git_repo,
    async_create_worktree,
    async_get_diff,
    async_commit_worktree,
    async_merge_worktree,
    async_cleanup_worktree,
    async_validate_diff_safety,
    async_reset_worktree,
    async_count_worktree_lines,
)

LITELLM_BASE = os.getenv("LITELLM_BASE_URL", "http://localhost:4000")
LITELLM_KEY = os.getenv("LITELLM_API_KEY", "anything")
MAX_REVIEW_ROUNDS = 3
MAX_WORKER_ITERATIONS = 20

# Role → LiteLLM model alias (must match model_name in litellm_config.yaml)
MODEL_SCANNER = os.getenv("MODEL_SCANNER", "scanner")
MODEL_ARCHITECT = os.getenv("MODEL_ARCHITECT", "architect")
MODEL_WORKER = os.getenv("MODEL_WORKER", "worker")
MODEL_REVIEWER = os.getenv("MODEL_REVIEWER", "reviewer")


def make_llm(model_name: str) -> ChatOpenAI:
    return ChatOpenAI(
        model=model_name,
        base_url=LITELLM_BASE,
        api_key=LITELLM_KEY,
        model_kwargs={"metadata": {"role": model_name}},
    )


def _config(state: AgentState) -> dict:
    cb = get_langfuse_handler(
        project=state["project"],
        task=state["task"],
        run_id=state["run_id"],
    )
    cfg: dict = {}
    if cb:
        cfg["callbacks"] = [cb]
    cfg["tags"] = [state["project"], state["run_id"]]
    cfg["metadata"] = {"project": state["project"], "run_id": state["run_id"]}
    return cfg


# ── Phase 0: Scanner — gather project context with local model ────────────────

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
             ".tox", "dist", "build", ".next", ".nuxt", "target", ".worktrees"}

KEY_FILES = [
    "package.json", "pyproject.toml", "Cargo.toml", "go.mod",
    "requirements.txt", "Makefile", "docker-compose.yml",
    "Dockerfile", "README.md", ".env.example",
]

ENTRY_PATTERNS = [
    "src/__init__.py", "src/main.py", "src/index.ts",
    "app.py", "main.py", "index.ts", "index.js",
]


def _gather_project_files(workspace: str, max_depth: int = 3) -> str:
    """Walk workspace and collect file tree + key config file contents."""
    root = pathlib.Path(workspace)

    # Build file tree
    tree_lines = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        depth = len(rel.parts)
        if depth > max_depth:
            continue
        indent = "  " * (depth - 1)
        name = rel.name + ("/" if path.is_dir() else "")
        tree_lines.append(f"{indent}{name}")

    file_tree = "\n".join(tree_lines[:500])

    # Read key config files
    config_contents = []
    for fname in KEY_FILES:
        fpath = root / fname
        if fpath.is_file():
            try:
                text = fpath.read_text(errors="replace")[:3000]
                config_contents.append(f"### {fname}\n```\n{text}\n```")
            except Exception:
                pass

    # Read entry-point source files
    for pattern in ENTRY_PATTERNS:
        fpath = root / pattern
        if fpath.is_file():
            try:
                text = fpath.read_text(errors="replace")[:3000]
                config_contents.append(f"### {pattern}\n```\n{text}\n```")
            except Exception:
                pass

    configs = "\n\n".join(config_contents) if config_contents else "(no config files found)"
    return f"## File Tree\n```\n{file_tree}\n```\n\n## Key Files\n{configs}"


async def scanner_scan(state: AgentState) -> dict:
    """Scan the project workspace and produce a context summary for the architect."""
    raw_context = _gather_project_files(state["workspace"])

    try:
        llm = make_llm(MODEL_SCANNER)
        response = await llm.ainvoke(
            [
                SystemMessage(content=(
                    "You are a project scanner. Analyse the project structure below and produce "
                    "a concise summary for another AI that will plan coding tasks.\n\n"
                    "Include:\n"
                    "1. Project type (language, framework)\n"
                    "2. Key directories and their purpose\n"
                    "3. Dependencies and tech stack\n"
                    "4. Entry points and main modules\n"
                    "5. Build/test/run commands if visible\n\n"
                    "Be concise — bullet points, no prose."
                )),
                HumanMessage(content=raw_context),
            ],
            config=_config(state),
        )
        return {"project_context": response.content.strip()}
    except Exception:
        # If Ollama is down, pass raw context — still useful for the architect
        return {"project_context": raw_context}


# ── Phase 1: Architect — decompose task into subtasks ─────────────────────────

async def architect_plan(state: AgentState) -> dict:
    """Break the task into subtasks with predicted file paths."""
    llm = make_llm(MODEL_ARCHITECT)

    response = await llm.ainvoke(
        [
            SystemMessage(content=(
                "You are a senior software architect. Analyse the task and the project workspace.\n"
                "Break it into independent subtasks that can be worked on separately.\n"
                "For each subtask, predict which files will be created or modified.\n\n"
                "IMPORTANT: Respond ONLY with valid JSON, no markdown fences. Use this exact format:\n"
                "[\n"
                '  {"id": "subtask-1", "description": "...", "files_touched": ["path/to/file.py"]},\n'
                '  {"id": "subtask-2", "description": "...", "files_touched": ["other/file.py"]}\n'
                "]\n\n"
                "Keep subtasks focused. If the task is simple, return a single subtask."
            )),
            HumanMessage(content=(
                f"Workspace: {state['workspace']}\n\n"
                f"Project Context:\n{state.get('project_context', '(no scan available)')}\n\n"
                f"Task: {state['task']}"
            )),
        ],
        config=_config(state),
    )

    raw = response.content.strip()
    # Strip markdown fences if the model adds them anyway
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    # Try to extract JSON array even if surrounded by prose
    if not raw.startswith("["):
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            raw = match.group(0)

    if not raw:
        raise ValueError(f"Architect returned empty response")

    subtask_defs = json.loads(raw)

    # Detect overlap: any file appearing in multiple subtasks
    all_files: dict[str, int] = {}
    for s in subtask_defs:
        for f in s.get("files_touched", []):
            all_files[f] = all_files.get(f, 0) + 1
    overlap = any(count > 1 for count in all_files.values())

    subtasks: list[Subtask] = [
        Subtask(
            id=s["id"],
            description=s["description"],
            files_touched=s.get("files_touched", []),
            assigned_worktree="",
            status="pending",
            result="",
        )
        for s in subtask_defs
    ]

    return {
        "subtasks": subtasks,
        "overlap_detected": overlap,
        "review_round": 0,
        "review_feedback": {},
        "merge_results": {},
    }


# ── Phase 2: Lead Dev — assign subtasks to worktrees ─────────────────────────

async def lead_dev_assign(state: AgentState) -> dict:
    """Create worktrees and assign subtasks to them."""
    repo = ensure_git_repo(state["workspace"])
    run_id = state["run_id"]
    subtasks = list(state["subtasks"])
    worktrees: list[WorktreeInfo] = []

    if state["overlap_detected"]:
        # Single shared worktree — all subtasks sequential
        branch = f"wt/{run_id}/shared"
        path = await async_create_worktree(repo, branch, "wt-shared")
        for s in subtasks:
            s["assigned_worktree"] = "wt-shared"
        worktrees.append(WorktreeInfo(
            id="wt-shared",
            branch_name=branch,
            path=path,
            subtask_ids=[s["id"] for s in subtasks],
            status="created",
            diff_summary="",
        ))
    else:
        # One worktree per subtask — parallel execution
        for s in subtasks:
            wt_id = f"wt-{s['id']}"
            branch = f"wt/{run_id}/{s['id']}"
            path = await async_create_worktree(repo, branch, wt_id)
            s["assigned_worktree"] = wt_id
            worktrees.append(WorktreeInfo(
                id=wt_id,
                branch_name=branch,
                path=path,
                subtask_ids=[s["id"]],
                status="created",
                diff_summary="",
            ))

    return {"subtasks": subtasks, "worktrees": worktrees}


# ── Phase 3: Worker — execute subtasks with file tools ────────────────────────

async def execute_worker(state: AgentState) -> dict:
    """Execute all subtasks assigned to the worktree indicated by _current_worktree_id."""
    wt_id = state.get("_current_worktree_id", "")

    # Find the worktree and its subtasks
    worktree = next(wt for wt in state["worktrees"] if wt["id"] == wt_id)
    subtasks = [s for s in state["subtasks"] if s["assigned_worktree"] == wt_id]

    # Check for review feedback from previous round
    feedback = state.get("review_feedback", {}).get(wt_id, "")

    tools = make_file_tools(worktree["path"])
    llm = make_llm(MODEL_WORKER)

    worker = create_react_agent(
        llm,
        tools,
        prompt=(
            "You are a senior software engineer. You MUST use your file tools to complete tasks.\n\n"
            "Available tools:\n"
            "- write_file: Create or overwrite a file\n"
            "- read_file: Read a file's contents\n"
            "- list_directory: List files in a directory\n"
            "- copy_file: Copy a file\n"
            "- move_file: Move/rename a file\n\n"
            "IMPORTANT: Do NOT just describe code in text. You MUST call write_file for every file "
            "you need to create or modify. File paths are relative to the working directory.\n\n"
            "After writing all files, respond with a brief summary of what you created."
        ),
    )

    # Build the task description for the worker
    task_parts = []
    for s in subtasks:
        task_parts.append(f"## {s['id']}: {s['description']}\nFiles to create/modify: {', '.join(s['files_touched'])}")

    task_description = (
        f"Working directory: {worktree['path']}\n\n"
        "Use the write_file tool to create each file listed below.\n\n"
        + "\n\n".join(task_parts)
    )

    # Always snapshot worktree BEFORE worker runs
    pre_files, pre_lines = await async_count_worktree_lines(worktree["path"])
    logger.info(f"[{wt_id}] PRE-WORKER snapshot: {pre_files} files, {pre_lines} lines (feedback={'yes' if feedback else 'no'})")

    if feedback:
        existing_diff = await async_get_diff(worktree["path"])
        task_description += (
            f"\n\n## REVISION REQUIRED\n"
            f"Your previous attempt was reviewed and needs changes:\n{feedback}\n\n"
            f"## EXISTING CODE (do NOT delete this)\n"
            f"Below is the diff of code already written. You must PRESERVE all of it "
            f"and only make the specific changes requested above.\n"
            f"```\n{existing_diff[:8000]}\n```\n\n"
            f"CRITICAL RULES FOR REVISIONS:\n"
            f"1. First use read_file to read EVERY existing file before modifying\n"
            f"2. Only modify the specific parts mentioned in the feedback\n"
            f"3. NEVER delete files unless the feedback explicitly asks for deletion\n"
            f"4. NEVER rewrite entire files — make targeted edits only\n"
            f"5. When using write_file, include ALL existing content plus your changes\n"
            f"6. If you're unsure, read the file first and make minimal changes"
        )

    result = await worker.ainvoke(
        {"messages": [HumanMessage(content=task_description)]},
        config={**_config(state), "recursion_limit": 100},
    )

    # Extract the final message as the worker output
    output = result["messages"][-1].content if result["messages"] else ""

    # Post-worker snapshot
    post_files, post_lines = await async_count_worktree_lines(worktree["path"])
    logger.info(f"[{wt_id}] POST-WORKER snapshot: {post_files} files, {post_lines} lines")

    # Safety check: validate diff before committing
    safe, reason = await async_validate_diff_safety(worktree["path"], pre_files, pre_lines)
    logger.info(f"[{wt_id}] Safety check: safe={safe}, reason={reason}")
    if not safe:
        await async_reset_worktree(worktree["path"])
        output = f"SAFETY ABORT: {reason}. Worktree reset to last good state."
        logger.warning(f"[{wt_id}] SAFETY ABORT — worktree reset")

    # Commit changes in the worktree
    await async_commit_worktree(
        worktree["path"],
        f"feat({wt_id}): {state['task'][:50]}",
    )

    # Update subtask statuses
    updated_subtasks = list(state["subtasks"])
    for s in updated_subtasks:
        if s["assigned_worktree"] == wt_id:
            s["status"] = "done"
            s["result"] = output

    # Get diff for review
    diff = await async_get_diff(worktree["path"])
    updated_worktrees = list(state["worktrees"])
    for wt in updated_worktrees:
        if wt["id"] == wt_id:
            wt["status"] = "review"
            wt["diff_summary"] = diff

    return {"subtasks": updated_subtasks, "worktrees": updated_worktrees}


# ── Phase 3b: Sequential execution (when subtasks overlap) ───────────────────

async def execute_sequential(state: AgentState) -> dict:
    """Execute all subtasks in the single shared worktree, one at a time."""
    # There's only one worktree in the overlap case
    worktree = state["worktrees"][0]
    subtasks = list(state["subtasks"])

    feedback = state.get("review_feedback", {}).get(worktree["id"], "")

    tools = make_file_tools(worktree["path"])
    llm = make_llm(MODEL_WORKER)

    worker = create_react_agent(
        llm,
        tools,
        prompt=(
            "You are a senior software engineer. You MUST use your file tools to complete tasks.\n\n"
            "Available tools:\n"
            "- write_file: Create or overwrite a file\n"
            "- read_file: Read a file's contents\n"
            "- list_directory: List files in a directory\n"
            "- copy_file: Copy a file\n"
            "- move_file: Move/rename a file\n\n"
            "IMPORTANT: Do NOT just describe code in text. You MUST call write_file for every file "
            "you need to create or modify. File paths are relative to the working directory.\n\n"
            "Complete each subtask IN ORDER. Be careful — subtasks may touch the same files, "
            "so ensure consistency. After writing all files, respond with a brief summary."
        ),
    )

    task_parts = []
    for s in subtasks:
        task_parts.append(f"## {s['id']}: {s['description']}\nFiles to create/modify: {', '.join(s['files_touched'])}")

    task_description = (
        f"Working directory: {worktree['path']}\n\n"
        "Use the write_file tool to create each file listed below.\n\n"
        + "\n\n".join(task_parts)
    )

    # Always snapshot worktree BEFORE worker runs
    pre_files, pre_lines = await async_count_worktree_lines(worktree["path"])
    logger.info(f"[{worktree['id']}] PRE-WORKER snapshot: {pre_files} files, {pre_lines} lines (feedback={'yes' if feedback else 'no'})")

    if feedback:
        existing_diff = await async_get_diff(worktree["path"])
        task_description += (
            f"\n\n## REVISION REQUIRED\n"
            f"Your previous attempt was reviewed and needs changes:\n{feedback}\n\n"
            f"## EXISTING CODE (do NOT delete this)\n"
            f"Below is the diff of code already written. You must PRESERVE all of it "
            f"and only make the specific changes requested above.\n"
            f"```\n{existing_diff[:8000]}\n```\n\n"
            f"CRITICAL RULES FOR REVISIONS:\n"
            f"1. First use read_file to read EVERY existing file before modifying\n"
            f"2. Only modify the specific parts mentioned in the feedback\n"
            f"3. NEVER delete files unless the feedback explicitly asks for deletion\n"
            f"4. NEVER rewrite entire files — make targeted edits only\n"
            f"5. When using write_file, include ALL existing content plus your changes\n"
            f"6. If you're unsure, read the file first and make minimal changes"
        )

    result = await worker.ainvoke(
        {"messages": [HumanMessage(content=task_description)]},
        config={**_config(state), "recursion_limit": 100},
    )

    output = result["messages"][-1].content if result["messages"] else ""

    # Post-worker snapshot
    post_files, post_lines = await async_count_worktree_lines(worktree["path"])
    logger.info(f"[{worktree['id']}] POST-WORKER snapshot: {post_files} files, {post_lines} lines")

    # Safety check: validate diff before committing
    safe, reason = await async_validate_diff_safety(worktree["path"], pre_files, pre_lines)
    logger.info(f"[{worktree['id']}] Safety check: safe={safe}, reason={reason}")
    if not safe:
        await async_reset_worktree(worktree["path"])
        output = f"SAFETY ABORT: {reason}. Worktree reset to last good state."
        logger.warning(f"[{worktree['id']}] SAFETY ABORT — worktree reset")

    await async_commit_worktree(
        worktree["path"],
        f"feat({worktree['id']}): {state['task'][:50]}",
    )

    for s in subtasks:
        s["status"] = "done"
        s["result"] = output

    diff = await async_get_diff(worktree["path"])
    updated_worktrees = list(state["worktrees"])
    updated_worktrees[0]["status"] = "review"
    updated_worktrees[0]["diff_summary"] = diff

    return {"subtasks": subtasks, "worktrees": updated_worktrees}


# ── Phase 4: Lead Dev — autonomous review ─────────────────────────────────────

async def lead_dev_review(state: AgentState) -> dict:
    """Review diffs from all worktrees. Approve or provide fix instructions."""
    llm = make_llm(MODEL_REVIEWER)
    review_feedback: dict[str, str] = {}

    for wt in state["worktrees"]:
        if wt["status"] != "review":
            continue

        response = await llm.ainvoke(
            [
                SystemMessage(content=(
                    "You are a tech lead reviewing a code diff.\n"
                    "Check for: correctness, bugs, missing edge cases, style issues, "
                    "and whether it fully addresses the subtask requirements.\n\n"
                    "CRITICAL: If the diff shows files being deleted or large sections of code "
                    "being removed, this is almost certainly a bug. Flag it immediately.\n\n"
                    "If the code is good, respond with exactly: APPROVED\n"
                    "If changes are needed, describe what needs to be fixed. Be specific — "
                    "reference file names and line numbers.\n\n"
                    "IMPORTANT: Your feedback will be sent to a worker agent for revision. "
                    "Be precise about what to change. Never suggest rewriting entire files — "
                    "only request targeted fixes."
                )),
                HumanMessage(content=(
                    f"Task: {state['task']}\n\n"
                    f"Subtasks in this worktree: "
                    f"{[s['description'] for s in state['subtasks'] if s['assigned_worktree'] == wt['id']]}\n\n"
                    f"Diff:\n```\n{wt['diff_summary']}\n```"
                )),
            ],
            config=_config(state),
        )

        verdict = response.content.strip()
        if "APPROVED" not in verdict.upper():
            review_feedback[wt["id"]] = verdict

    return {
        "review_feedback": review_feedback,
        "review_round": state.get("review_round", 0) + 1,
    }


# ── Phase 5: Lead Dev — merge approved worktrees ─────────────────────────────

async def lead_dev_merge(state: AgentState) -> dict:
    """Merge all worktree branches back to main and cleanup."""
    repo = ensure_git_repo(state["workspace"])
    results: dict[str, str] = {}
    updated_worktrees = list(state["worktrees"])

    for wt in updated_worktrees:
        # Final safety gate: validate the worktree diff before merging to main
        safe, reason = await async_validate_diff_safety(wt["path"])
        if not safe:
            results[wt["id"]] = f"blocked: {reason}"
            await async_cleanup_worktree(repo, wt["path"], wt["branch_name"])
            wt["status"] = "blocked"
            continue

        success, msg = await async_merge_worktree(repo, wt["branch_name"])
        results[wt["id"]] = "success" if success else f"conflict: {msg}"
        await async_cleanup_worktree(repo, wt["path"], wt["branch_name"])
        wt["status"] = "merged" if success else "cleaned"

    # Build final summary
    summary_parts = []
    for wt in updated_worktrees:
        subtask_descs = [
            s["description"] for s in state["subtasks"]
            if s["assigned_worktree"] == wt["id"]
        ]
        status = results[wt["id"]]
        summary_parts.append(f"- {wt['id']} ({status}): {', '.join(subtask_descs)}")

    final_output = f"Task: {state['task']}\n\nResults:\n" + "\n".join(summary_parts)

    return {
        "merge_results": results,
        "worktrees": updated_worktrees,
        "final_output": final_output,
    }
