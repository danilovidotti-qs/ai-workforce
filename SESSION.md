# Agent Orchestration — Hands-On Session

> **Goal:** Understand how multi-agent orchestration works under the hood, escape vendor lock-in, and learn to build agents that behave the way we want.
>
> This repo isn't meant to be perfect — it's a tool for us to see how sausages are made.

---

## Session Index

- [Agent Orchestration — Hands-On Session](#agent-orchestration--hands-on-session)
  - [Session Index](#session-index)
  - [1. Why Agent Orchestration Matters](#1-why-agent-orchestration-matters)
  - [2. The Infrastructure Stack](#2-the-infrastructure-stack)
  - [3. LiteLLM — The Model Router](#3-litellm--the-model-router)
  - [4. LangGraph — The Pipeline Engine](#4-langgraph--the-pipeline-engine)
  - [5. Langfuse — Observability \& Cost Tracking](#5-langfuse--observability--cost-tracking)
  - [6. The Pipeline: Roles \& How They Work](#6-the-pipeline-roles--how-they-work)
    - [Scanner (Ollama — local, free)](#scanner-ollama--local-free)
    - [Architect (Codex 5.4)](#architect-codex-54)
    - [Worker (Codex 5.2)](#worker-codex-52)
    - [Reviewer (Gemini 2.5 Flash)](#reviewer-gemini-25-flash)
    - [Merge](#merge)
  - [7. Hands-On: Running a Task](#7-hands-on-running-a-task)
    - [Prerequisites](#prerequisites)
    - [Setup](#setup)
    - [Run a task](#run-a-task)
    - [What to observe](#what-to-observe)
  - [8. Frameworks Worth Knowing](#8-frameworks-worth-knowing)
    - [BMAD — Breakthrough Method for Agile AI-Driven Development](#bmad--breakthrough-method-for-agile-ai-driven-development)
    - [GSD — Get Shit Done](#gsd--get-shit-done)
    - [Gas Town — The Ultimate Agent Orchestration](#gas-town--the-ultimate-agent-orchestration)
  - [9. Local Models — The Great Equaliser](#9-local-models--the-great-equaliser)
  - [10. Key Takeaways](#10-key-takeaways)
  - [Appendix: File Map](#appendix-file-map)

---

## 1. Why Agent Orchestration Matters

The industry is shifting from **single-prompt AI** to **multi-agent systems** — pipelines where specialised agents collaborate to complete complex tasks autonomously.

**The problem with today's tools:**

- Cursor, Copilot, Windsurf — all locked to specific providers and models
- You can't see what's happening inside (black box)
- You can't swap models, control costs, or customise behaviour
- You're renting someone else's workflow

**What orchestration gives us:**

- Full control over which model does what
- Visibility into every decision, every token, every dollar spent
- The ability to use the **best model for each job** at the **best price**
- Freedom to run locally, in the cloud, or hybrid

> "The goal isn't to replace these tools — it's to understand the machinery so we can build agents that work for us, not the other way around."

---

## 2. The Infrastructure Stack

```
┌─────────────────────────────────────────────────────┐
│                     Web UI (:8000)                   │
│              Submit tasks, live progress             │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│              FastAPI + LangGraph                     │
│         Orchestrator — the brain                     │
│   Manages pipeline, state, worktrees, streaming      │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                  LiteLLM Proxy (:4000)               │
│     Routes requests to any provider by alias         │
│   Anthropic · OpenAI · Google · Ollama (local)       │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│                    Langfuse                           │
│     Traces, token counts, cost per role per run      │
└─────────────────────────────────────────────────────┘
```

Everything runs in Docker. Two containers (orchestrator + LiteLLM), optional Langfuse.

---

## 3. LiteLLM — The Model Router

**What it does:** Acts as a proxy between your code and any LLM provider. Your code calls one endpoint; LiteLLM routes to the right provider.

**Why this matters — no lock-in:**

```yaml
# litellm_config.yaml — this is ALL you change to swap models

- model_name: architect # Role alias
  litellm_params:
    model: codex-5.4 # OpenAI Codex 5.4
    api_key: os.environ/OPENAI_API_KEY

- model_name: worker
  litellm_params:
    model: codex-5.2 # OpenAI Codex 5.2
    api_key: os.environ/OPENAI_API_KEY

- model_name: reviewer
  litellm_params:
    model: gemini/gemini-2.5-flash # Google Gemini
    api_key: os.environ/GEMINI_API_KEY

- model_name: scanner
  litellm_params:
    model: ollama/qwen2.5-coder:7b # Local model, free
    api_base: http://host.docker.internal:11434
```

**Key points to demonstrate:**

- One pipeline, four different providers
- Swap a model by changing one line in ENV — zero code changes
- Fallback chains: if Codex is down, fall back to Claude Sonnet automatically
- Cost tracking per model via Langfuse

```python
# In Python, the code is completely provider-agnostic:
llm = ChatOpenAI(model="architect", base_url="http://litellm:4000")
# LiteLLM resolves "architect" → codex-5.4 → OpenAI API
```

---

## 4. LangGraph — The Pipeline Engine

**What it does:** Defines a directed graph where each node is an agent step, with conditional edges for routing.

```
scanner_scan → architect_plan → lead_dev_assign
                                      │
                         ┌────────────┴────────────┐
                         │                         │
                    (parallel)              (sequential)
                   execute_worker        execute_sequential
                         │                         │
                         └────────────┬────────────┘
                                      │
                              lead_dev_review
                                      │
                         ┌────────────┴────────────┐
                         │                         │
                    (approved)              (needs changes)
                   lead_dev_merge          → back to workers
                                           (up to 3 rounds)
```

**Key concepts:**

- **StateGraph** — shared state flows through every node
- **Conditional edges** — `if review_feedback: loop back to workers`
- **Fan-out with Send** — parallel execution across worktrees
- **Plan graph vs Exec graph** — supervised mode splits planning from execution

```python
# graph.py — the full pipeline in ~30 lines
full_builder = StateGraph(AgentState)
full_builder.add_node("scanner_scan", scanner_scan)
full_builder.add_node("architect_plan", architect_plan)
full_builder.add_node("lead_dev_assign", lead_dev_assign)
# ... conditional routing for review loop
full_graph = full_builder.compile()
```

---

## 5. Langfuse — Observability & Cost Tracking

**What it does:** Captures every LLM call as a trace — which model, how many tokens, how long, how much it cost.

**Setup:**

```bash
# .env — just three vars, free tier works
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com
```

**What you see in Langfuse:**

- Full trace per run — scanner → architect → workers → reviewer → merge
- Token counts and cost per step
- Which model handled each role
- Latency breakdown — where is the pipeline spending time?
- Error traces when things go wrong

**Why this matters:**

- You can actually **measure** the cost of different model choices
- Compare: "What if we used Gemini Flash for review instead of Claude?"
- Identify bottlenecks — is the scanner slow? Is the worker burning tokens?
- Compliance: full audit trail of every AI decision

**LiteLLM native integration:**

```yaml
# litellm_config.yaml — Langfuse gets ALL calls automatically
litellm_settings:
  success_callback: ["langfuse"]
  failure_callback: ["langfuse"]
```

---

## 6. The Pipeline: Roles & How They Work

### Scanner (Ollama — local, free)

- Reads the project: file tree, package.json, entry points
- Produces a structured summary for the architect
- Runs locally via Ollama — no API cost, no data leaving your machine

### Architect (Codex 5.4)

- Receives the scanner's context + the user's task
- Breaks the task into independent subtasks
- Predicts which files each subtask will touch
- Detects overlap to decide parallel vs sequential execution

### Worker (Codex 5.2)

- Gets assigned a git worktree (isolated branch)
- Has file tools: `write_file`, `read_file`, `list_directory`, `copy_file`, `move_file`
- Writes actual code to the filesystem
- Commits when done

### Reviewer (Gemini 2.5 Flash)

- Reviews the git diff from each worktree
- Either approves (`APPROVED`) or provides specific fix instructions
- Up to 3 review rounds before auto-proceeding

### Merge

- Merges approved worktree branches back to main
- Cleans up branches and worktree directories

**Git worktrees — the secret weapon:**

```
/projects/my-project/                  ← main (untouched)
/projects/my-project/.worktrees/
    ├── wt-subtask-1/                  ← isolated branch
    └── wt-subtask-2/                  ← isolated branch
```

Each worker gets its own copy. No file conflicts, even in parallel.

---

## 7. Hands-On: Running a Task

### Prerequisites

```bash
# Install Docker Desktop + Ollama
ollama pull qwen2.5-coder:7b
OLLAMA_HOST=0.0.0.0 ollama serve
```

### Setup

```bash
git clone <repo-url> ai-workforce && cd ai-workforce

# Configure .env with your API keys
cp .env.example .env
# Edit: ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY
# Set: PROJECTS_DIR=/path/to/your/projects

# Start
docker compose up -d --build

# Verify
curl http://localhost:4000/health   # LiteLLM
curl http://localhost:8000/health   # Orchestrator
```

### Run a task

1. Open http://localhost:8000
2. Select a project from the dropdown
3. Describe the task: _"Add a login page with email/password"_
4. Choose **Supervised** mode (so we can inspect the plan)
5. Click **Run**
6. Review the plan — edit subtasks, add/remove as needed
7. Click **Approve & Execute**
8. Watch the pipeline: scanner → architect → assign → workers → review → merge

### What to observe

- **Web UI:** Live accordion with phase progress
- **Langfuse:** Open cloud.langfuse.com — see traces flowing in real-time
- **Worktrees:** `ls /projects/your-project/.worktrees/` — see isolated branches
- **Git log:** After merge, `git log --oneline` shows the merged branches

---

## 8. Frameworks Worth Knowing

### BMAD — Breakthrough Method for Agile AI-Driven Development

A persona-driven framework that defines **12+ specialised AI roles** as Markdown files — Product Manager, Architect, Developer, Scrum Master, UX Designer, etc.

**How it works:**

1. **Analysis** — Capture problem/constraints in a one-page PRD
2. **Planning** — Break PRD into user stories with acceptance criteria
3. **Solutioning** — Architect produces minimal design, Developer proposes steps
4. **Implementation** — Iterative small stories, clear criteria

**Why it matters for us:**

- Each LLM interaction gets a structured system prompt (the agent Markdown file)
- Replaces unstructured "vibe coding" with a repeatable, persona-driven workflow
- **Can be mixed with our orchestration** — use BMAD personas as the prompts for each pipeline role, and LiteLLM to route each persona to the best model/price for the job
- Documentation as source of truth, not just code

> Reference: [github.com/bmad-code-org/BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD)

---

### GSD — Get Shit Done

A meta-prompting and spec-driven development system that solves **context rot** — the degradation of LLM output quality as conversation history grows.

**How it works:**

1. **Research** — 4 parallel agents investigate stack, features, architecture, pitfalls
2. **Planning** — Planner creates tasks, checker verifies, loops until pass
3. **Execution** — Parallel executors, each with a **fresh 200K context window**
4. **Verification** — Verifier checks goals, debuggers diagnose failures

**Key insight:** Each sub-agent gets a clean context window dedicated exclusively to its task. No context pollution from previous steps.

**What it manages:** Git branches, cost/token tracking, stuck loop detection, crash recovery, automatic parallelism for independent tasks.

> Reference: [github.com/gsd-build/get-shit-done](https://github.com/gsd-build/get-shit-done)

---

### Gas Town — The Ultimate Agent Orchestration

Created by Steve Yegge, Gas Town coordinates **20-30 AI coding agents** (Claude Code, Copilot, Codex, Gemini) working in parallel on the same codebase. Often described as **"Kubernetes for AI coding agents."**

**Seven specialised roles:**
| Role | What it does |
|------|-------------|
| **Mayor** | Primary user interface, dispatches work |
| **Polecats** | Ephemeral workers that execute tasks and produce merge requests |
| **Refinery** | Manages merge queue so parallel work doesn't collide |
| **Witness** | Monitors worker health |
| **Deacon** | Runs continuous patrol loops |
| **Dogs** | Maintenance tasks |
| **Crew** | Persistent agents for collaborative design |

**Why it's the ultimate:**

- Three-tier watchdog chain for reliability (Daemon → Boot → Deacon)
- State persistence via git-backed hooks — context survives agent restarts
- Every action attributed, every agent has a track record, full provenance
- Treats AI agent work as **structured data** with full orchestration

> This is where the industry is heading. Our repo is a stepping stone toward this level of orchestration.
>
> Reference: [github.com/steveyegge/gastown](https://github.com/steveyegge/gastown)

---

## 9. Local Models — The Great Equaliser

Local models are becoming surprisingly capable and are a key part of the no-lock-in strategy.

**What's available today:**
| Model | Size | What it's good for |
|-------|------|-------------------|
| Qwen 2.5 Coder 7B | 4GB | Code analysis, scanning, light coding |
| DeepSeek Coder V2 | 16B | Solid coding, reviews |
| CodeLlama 34B | 20GB | Complex coding tasks |
| Llama 3.3 70B | 40GB | General reasoning, planning |

**Why this matters:**

- **Zero cost** — run as many tokens as you want
- **Zero latency to cold start** — model is always loaded
- **Data privacy** — nothing leaves your machine
- **Offline capability** — works on a plane

**In our pipeline:**

```yaml
# Scanner runs on local Ollama — project analysis never hits an API
- model_name: scanner
  litellm_params:
    model: ollama/qwen2.5-coder:7b
    api_base: http://host.docker.internal:11434
```

**The trend:** Every few months, local models close the gap. What required GPT-4 last year runs on a laptop today. The architecture we're building is ready for when local models can handle the full pipeline.

---

## 10. Key Takeaways

1. **No lock-in** — LiteLLM lets you swap providers in one line of YAML. Your code never touches a vendor SDK directly.

2. **Right model for the job** — Cheap local models for scanning, mid-tier for coding, premium for planning. Match model capability to task complexity.

3. **Observability is non-negotiable** — Langfuse shows you exactly where tokens and dollars go. Without it, you're flying blind.

4. **Agents are just graphs** — LangGraph makes it clear: nodes are functions, edges are conditions. There's no magic. Once you see it, you can build anything.

5. **Git worktrees solve the parallel problem** — Filesystem isolation without the overhead of containers or VMs. Each agent gets its own branch.

6. **The industry is converging** — BMAD (structured personas), GSD (context management), Gas Town (fleet orchestration), and this repo (model-agnostic pipeline) are all solving pieces of the same puzzle.

7. **Local models are the escape hatch** — When you can run a capable model on your laptop, the power dynamic shifts from providers to builders.

> **The point isn't this specific repo. The point is understanding the patterns — so when you build your own agents, you know exactly what's happening and why.**

---

## Appendix: File Map

```
ai-workforce/
├── .env                  ← API keys and model config
├── docker-compose.yml    ← LiteLLM + orchestrator containers
├── litellm_config.yaml   ← Model definitions (THE file for swapping providers)
├── langgraph.json        ← LangGraph Studio entrypoint
└── src/
    ├── agents.py         ← Pipeline nodes: scan, plan, assign, execute, review, merge
    ├── graph.py           ← LangGraph pipeline definition with conditional routing
    ├── server.py          ← FastAPI server, SSE streaming, stop/resume
    ├── state.py           ← AgentState schema (shared state between all nodes)
    ├── store.py           ← SQLite persistence for run state
    ├── tools.py           ← File tools scoped per worktree (read, write, list, copy, move)
    ├── worktree.py        ← Git worktree lifecycle (create, diff, commit, merge, cleanup)
    ├── callbacks.py       ← Langfuse tracing integration
    └── ui.html            ← Web UI (single-page, dark theme, live progress)
```
