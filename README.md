# AI Workforce

An autonomous multi-agent coding system built on LangGraph + LiteLLM. Give it a task and a project — it plans, writes code, reviews its own work, and merges the result. No human in the loop.

---

## Architecture

```
Task submitted via Web UI or API
         │
         ▼
┌─ Scanner (local Ollama) ─────────────────────────────┐
│  Reads project structure, dependencies, entry points │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌─ Architect ──────────────────────────────────────────┐
│  Breaks the task into subtasks, predicts file overlap │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌─ Lead Dev — assign ──────────────────────────────────┐
│  Creates git worktrees:                              │
│  • No overlap → one worktree per subtask (parallel)  │
│  • Overlap    → one shared worktree (sequential)     │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌─ Workers ────────────────────────────────────────────┐
│  Write code using file tools, scoped to worktree     │
│  Each worker operates in filesystem isolation        │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌─ Lead Dev — review ──────────────────────────────────┐
│  Reviews diffs from each worktree                    │
│  • Approved → proceed to merge                       │
│  • Issues   → loop back to workers (up to 3 rounds) │
└────────────────────────┬─────────────────────────────┘
                         ▼
┌─ Lead Dev — merge ───────────────────────────────────┐
│  Merges worktree branches into main, cleans up       │
└────────────────────────┬─────────────────────────────┘
                         ▼
                   Returns result
```

LiteLLM acts as a proxy, routing to any provider (Anthropic, Google, Ollama). Models are swappable via config — no code changes needed.

### Component Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Web UI / API                              │
│                   FastAPI + SSE streaming                        │
│                     (localhost:8000)                              │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     LangGraph Engine                             │
│                                                                  │
│  ┌─────────┐   ┌───────────┐   ┌────────┐   ┌────────┐         │
│  │ Scanner │──▶│ Architect │──▶│ Assign │──▶│ Worker │──┐      │
│  └─────────┘   └───────────┘   └────────┘   └────────┘  │      │
│                      ▲                           │       │      │
│                      │ supervised                ▼       │      │
│                 ┌─────────┐              ┌──────────┐    │      │
│                 │  Human  │              │ Review   │◀───┘      │
│                 │ approval│              │ (≤3 rds) │           │
│                 └─────────┘              └────┬─────┘           │
│                                               │                  │
│                                               ▼                  │
│                                          ┌─────────┐            │
│                                          │  Merge  │            │
│                                          └─────────┘            │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                     LiteLLM Proxy                                │
│              Unified API (localhost:4000)                         │
│         Routing · Fallbacks · Load Balancing                     │
│                                                                  │
│   ┌───────────┐  ┌────────┐  ┌────────┐  ┌───────────────┐     │
│   │ Anthropic │  │ OpenAI │  │ Google │  │ Ollama (local)│     │
│   │  Claude   │  │ Codex  │  │ Gemini │  │ qwen2.5-coder │     │
│   └───────────┘  └────────┘  └────────┘  └───────────────┘     │
└──────────────────────────┬───────────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                   Observability (optional)                        │
│             Langfuse — traces, tokens, cost                      │
│              (cloud or self-hosted)                               │
└──────────────────────────────────────────────────────────────────┘
```

### State Machine (LangGraph)

The pipeline is a compiled LangGraph state machine with conditional edges:

```
scanner_scan ──▶ architect_plan ──▶ lead_dev_assign
                                         │
                          ┌──────────────┤
                          │              │
                     overlap?        no overlap?
                          │              │
                          ▼              ▼
                   execute_sequential  execute_worker (parallel)
                          │              │
                          └──────┬───────┘
                                 ▼
                          lead_dev_review
                                 │
                          ┌──────┴──────┐
                          │             │
                     approved?    feedback + rounds < 3?
                          │             │
                          ▼             ▼
                    lead_dev_merge   back to execute
```

Three graph variants are exposed:
- **`plan_graph`** — Scanner + Architect only (planning phase)
- **`exec_graph`** — Assign + Execute + Review + Merge (execution phase)
- **`full_graph`** — All phases combined (used in Auto mode)

### Overlap Detection & Execution Strategy

The Architect predicts which files each subtask will touch. The Lead Dev checks for overlap:

| Scenario | Strategy | Worktrees | Execution |
| --- | --- | --- | --- |
| No shared files | Parallel | One per subtask | All workers run concurrently |
| Shared files detected | Sequential | Single shared | One worker at a time, in order |

This prevents write conflicts — two agents never modify the same file simultaneously.

### Safety Mechanisms

Multiple safeguards prevent destructive changes:

- **Filesystem sandbox** — Workers can only access files inside their assigned worktree. Path traversal is blocked.
- **Pre-commit validation** — Before committing, diffs are checked:
  - File count drop > 50% → abort
  - Line count drop > 50% → abort
  - Majority of files have only deletions → abort
  - Large deletions (>100 lines) with zero additions → abort
- **Pre-merge validation** — Same checks run again before merging to main
- **Review loop** — An independent reviewer model checks every diff. Up to 3 revision rounds before merge.
- **Worktree cleanup** — Branches and worktree directories are always cleaned up, even on failure or cancellation.

### Persistence & Resumability

Run state is serialized to SQLite after each phase transition:

```
Run started → [scanner] → save → [architect] → save → ... → [merge] → save → done
                                                  ↑
                                            resume from here
```

If a run fails or is stopped, it can be resumed from the last saved checkpoint. Stale worktrees are cleaned up automatically on resume.

---

## Execution Modes

| Mode           | Behaviour                                                                                          |
| -------------- | -------------------------------------------------------------------------------------------------- |
| **Auto**       | Runs the full pipeline end-to-end without pausing.                                                 |
| **Supervised** | Pauses after planning so you can review, edit, add, or remove subtasks before approving execution. |

Both modes stream live progress via SSE to the web UI.

---

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Ollama](https://ollama.com) installed and running natively (optional, for local models)
- API keys for at least one provider (Anthropic, Google, or OpenAI)

---

## Setup

### 1. Clone the project

```bash
git clone <repo-url> ai-workforce
cd ai-workforce
```

### 2. Configure environment

Copy and edit `.env`:

```bash
# Required — at least one provider key
ANTHROPIC_API_KEY=sk-ant-...
GEMINI_API_KEY=AIza...
OPENAI_API_KEY=sk-...

# Projects directory — each subfolder becomes a selectable project in the UI
PROJECTS_DIR=/path/to/your/projects

# Optional — Langfuse for observability (free tier works)
# LANGFUSE_PUBLIC_KEY=pk-lf-...
# LANGFUSE_SECRET_KEY=sk-lf-...
# LANGFUSE_HOST=https://cloud.langfuse.com
```

Your `PROJECTS_DIR` should contain one subfolder per project, each being a git repo:

```
/path/to/your/projects/
    ├── my-app/            ← appears as "my-app" in the UI dropdown
    ├── backend-api/       ← appears as "backend-api"
    └── landing-page/      ← appears as "landing-page"
```

When a task runs, agents create git worktrees inside the project to write code in isolation, then merge the branches back to main:

```
/path/to/your/projects/my-app/
    ├── src/                        ← your existing code (untouched during work)
    ├── package.json
    └── .worktrees/
        ├── wt-subtask-1/           ← agent 1 writes here (isolated branch)
        └── wt-subtask-2/           ← agent 2 writes here (isolated branch)
```

### 3. Pull the Ollama model (optional)

```bash
ollama pull qwen2.5-coder:7b
OLLAMA_HOST=0.0.0.0 ollama serve
```

### 4. Start the services

```bash
docker compose up -d --build
```

Verify:

```bash
curl http://localhost:4000/health   # LiteLLM proxy
curl http://localhost:8000/health   # Orchestrator
```

---

## Usage

### Web UI (recommended)

Open **http://localhost:8000** in your browser.

1. Select a project from the dropdown (auto-discovered from your `PROJECTS_DIR`)
2. Describe the task
3. Choose **Auto** or **Supervised** mode
4. Click **Run**

The UI shows live progress with expandable accordion phases. In supervised mode, you can edit the plan before approving. A **Stop** button lets you cancel a running task at any time — stopped runs can be resumed.

### API

```bash
# Auto mode — runs full pipeline
curl -X POST http://localhost:8000/api/run/stream \
  -H "Content-Type: application/json" \
  -d '{"project": "my-project", "task": "Add JWT auth", "mode": "auto"}'

# Supervised mode — pauses after planning
curl -X POST http://localhost:8000/api/run/stream \
  -H "Content-Type: application/json" \
  -d '{"project": "my-project", "task": "Add JWT auth", "mode": "supervised"}'

# Approve a supervised plan
curl -X POST http://localhost:8000/api/runs/{run_id}/approve \
  -H "Content-Type: application/json" -d '{}'

# Resume a failed/stopped run
curl -X POST http://localhost:8000/api/runs/{run_id}/resume

# Stop a running task
curl -X POST http://localhost:8000/api/runs/{run_id}/stop

# List runs
curl http://localhost:8000/api/runs
```

All streaming endpoints return **Server-Sent Events** (SSE) with phase progress.

---

## How Worktrees Work

Git worktrees give each agent an isolated copy of the repo on a separate branch:

```
/projects/my-project/                  ← main (untouched during work)
/projects/my-project/.worktrees/
    ├── wt-subtask-1/                  ← branch: wt/<run-id>/subtask-1
    └── wt-subtask-2/                  ← branch: wt/<run-id>/subtask-2
```

- Agents can only write inside their assigned worktree (sandboxed)
- No file conflicts — even when running in parallel
- On success, branches are merged back to main
- Worktrees and branches are always cleaned up, even on failure

---

## Run Persistence

Run state is stored in SQLite (`/app/data/runs.db` inside the container, persisted via Docker volume). This means:

- **Failed runs can be resumed** — picks up from the last saved state
- **Stopped runs can be resumed** — use the Stop button or API, then resume later
- **Run history** is visible in the UI and via `GET /api/runs`

---

## Model Configuration

Each pipeline role uses a LiteLLM model alias. Defaults:

| Role                       | Env var           | Default alias | Default model                |
| -------------------------- | ----------------- | ------------- | ---------------------------- |
| Scanner (project analysis) | `MODEL_SCANNER`   | `scanner`     | Ollama qwen2.5-coder (local) |
| Architect (planner)        | `MODEL_ARCHITECT` | `architect`   | Codex 5.2                    |
| Worker (coder)             | `MODEL_WORKER`    | `worker`      | Claude Opus 4.5              |
| Reviewer                   | `MODEL_REVIEWER`  | `reviewer`    | Gemini 2.5 Flash             |

### Switching a model

Set the env var in `.env` to any alias defined in `litellm_config.yaml`:

```bash
MODEL_WORKER=ollama-local     # use local Ollama for coding
MODEL_REVIEWER=gemini-flash   # use Gemini for reviews
```

Then restart: `docker compose up -d --build`

### Available model aliases

| Alias           | Provider  | Model             |
| --------------- | --------- | ----------------- |
| `claude-opus`   | Anthropic | claude-opus-4-5   |
| `claude-sonnet` | Anthropic | claude-sonnet-4-5 |
| `codex`         | OpenAI    | gpt-5.2-codex     |
| `gpt-4o`        | OpenAI    | gpt-4o            |
| `gemini-flash`  | Google    | gemini-2.5-flash  |
| `ollama-local`  | Ollama    | qwen2.5-coder:7b  |

### Adding a new model

Add an entry to `litellm_config.yaml`:

```yaml
- model_name: my-model
  litellm_params:
    model: provider/model-name
    api_key: os.environ/MY_API_KEY
```

Then use it: `MODEL_WORKER=my-model` in `.env`. No Python changes needed.

---

## Monitoring

| Tool             | URL                              | What it shows                               |
| ---------------- | -------------------------------- | ------------------------------------------- |
| Web UI           | http://localhost:8000            | Live progress, run history, plan editing    |
| LangGraph Studio | Local app                        | Live graph, node states, parallel execution |
| Langfuse         | cloud.langfuse.com               | Full traces, token counts, cost per run     |
| LiteLLM Logs     | `docker compose logs -f litellm` | Raw API calls, latency, fallbacks           |

Langfuse is optional — if no keys are set, the app runs without tracing.

### Setting up Langfuse (cloud)

1. Create a free account at [cloud.langfuse.com](https://cloud.langfuse.com)
2. Create a new project (e.g. "ai-workforce")
3. Go to **Settings → API Keys** and create a new key pair
4. Add the keys to your `.env`:

```bash
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

5. Restart: `docker compose up -d --build`

LiteLLM sends all LLM calls to Langfuse automatically — no code changes needed. Traces appear under your project in the Langfuse dashboard.

### Self-hosting Langfuse

To run Langfuse locally instead of using the cloud:

```bash
docker compose --profile observability up -d
```

This starts Langfuse + PostgreSQL alongside the main services. Open [localhost:3001](http://localhost:3001), create an account, create a project, and grab the API keys from **Settings → API Keys**.

---

## Troubleshooting

**Ollama not reachable from LiteLLM container**
Make sure Ollama is running with `OLLAMA_HOST=0.0.0.0` and the `extra_hosts` entry in `docker-compose.yml` is present.

**LiteLLM returns 404 for a model**
Check the `model_name` in `litellm_config.yaml` matches exactly what `agents.py` passes to `make_llm()`.

**LangGraph Studio doesn't see the graph**
Confirm `langgraph.json` points to the correct path (`./src/graph.py:graph`) and that `graph` is the compiled variable name.

**Worktrees not cleaning up**
Stale worktrees are automatically cleaned at the start of each new run and on resume. If needed, clean manually:

```bash
cd /projects/my-project
rm -rf .worktrees
git worktree prune
git branch | grep 'wt/' | xargs git branch -D
```

**`'str' object` errors on resume**
This was fixed — the orchestrator now resets serialized messages and worktrees before resuming a run.

---

## Project Structure

```
ai-workforce/
├── .env                  API keys and config
├── Dockerfile            Orchestrator container (Python + git)
├── docker-compose.yml    LiteLLM proxy + orchestrator + optional Langfuse
├── litellm_config.yaml   Model definitions and provider routing
├── langgraph.json        LangGraph Studio entrypoint
├── pyproject.toml        Python dependencies
└── src/
    ├── __init__.py       Package marker
    ├── agents.py         Pipeline nodes: plan, assign, execute, review, merge
    ├── callbacks.py      Langfuse tracing (optional, reads from env)
    ├── graph.py          LangGraph pipeline with conditional routing
    ├── server.py         FastAPI server, SSE streaming, stop/resume endpoints
    ├── state.py          AgentState, Subtask, WorktreeInfo schemas
    ├── store.py          SQLite persistence for run state
    ├── tools.py          Scoped file tools per worktree
    ├── ui.html           Web UI (single-page, dark theme, accordion phases)
    └── worktree.py       Git worktree lifecycle management
```
