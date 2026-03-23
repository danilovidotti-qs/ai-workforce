# AI-Assisted Development — Full Session Outline

---

## Part 1: Reflection About Models

---

### What Models Are Bad At

- **Hallucinations** — they invent functions, APIs, and file paths that don't exist
  - "Just import `utils/auth.ts`" — file doesn't exist
  - Confident references to library methods that were never part of the API
- **Long-range consistency** — quality degrades as conversations grow
  - Early decisions get forgotten or contradicted
  - Variable names drift, architectural choices reverse mid-conversation
- **Blind confidence** — they never say "I don't know"
  - Wrong answers delivered with the same tone as correct ones
  - Fabricated error explanations that sound plausible
- **Testing their own code** — they generate code, then generate tests that pass by definition
  - Tests often mirror the implementation instead of challenging it
- **Security awareness** — vulnerable patterns produced without warning
  - SQL injection, missing input validation, hardcoded secrets
- **Context window limits** — even with large windows, attention fades
  - Instructions from early in the conversation get silently dropped

---

### What Models Are Good At

- **Boilerplate and scaffolding** — setup files, configs, repetitive patterns in seconds
- **Pattern recognition** — "make this look like that other component" works surprisingly well
- **Exploring unfamiliar codebases** — faster than grep for understanding intent and flow
- **Code review** — catching inconsistencies, style issues, potential bugs
- **Translating intent to code** — natural language to working code, especially for well-known patterns
- **Breadth of knowledge** — one model knows React, Terraform, SQL, Go, and your CI pipeline
- **Refactoring** — renaming, restructuring, migrating patterns across files
- **Documentation** — summarising code, writing docstrings, generating READMEs

---

### Protecting from the Bad, Potentialising the Good

- Reviewing output — treating it like a PR from a junior dev
- Keeping context small — smaller tasks = fewer hallucinations, better consistency
- Structured prompts — giving the model its role, constraints, and expected output format
- Breaking work into pieces — each piece gets a fresh, focused context window
- Matching model to task — cheap models for scanning, capable models for architecture
- Automated review loops — a second model reviewing the first model's output

---

## Part 2: How Are We Using It

---

### Iterative (Control + Understanding)

- You drive, the model assists
- You read every line, understand every decision
- You learn the codebase as you build

| Pros | Cons |
|------|------|
| High quality output | Slow |
| You understand the code | You are the bottleneck |
| Easy to course-correct | Doesn't scale |

---

### One-Shot, Review Later (Speed + Prototyping)

- Give the model a big task, let it run, review the output
- Great for PoCs, throwaway prototypes, exploring ideas
- Focus shifts from writing to reviewing

| Pros | Cons |
|------|------|
| Fast | Context rot — quality degrades over long generations |
| Great for exploration | Hard to debug when things go wrong |
| Low effort to start | You may not understand what was built |

---

### The Middle Ground — Orchestration

- What if we break the one-shot into **orchestrated steps** with checkpoints?
- Each step is small (good context), specialised (right model), and reviewable

```
One big prompt ──→ breaks into ──→ scan → plan → [review] → execute → review → merge
                                         ↑
                                    checkpoint
```

- The orchestrator controls the flow, not the model
- Checkpoints where you can intervene, approve, or reject
- Each agent gets a **fresh context window** dedicated to its task

> This is what we built.

---

## Part 3: Orchestration

---

### Customisable

- Choose which model handles each role (scanner, architect, worker, reviewer)
- Define the pipeline steps — add, remove, or reorder
- Write the prompts — each agent's behaviour and constraints
- Configure review rounds, parallelism thresholds, merge strategies

---

### Human in the Loop

- **Supervised mode** — the pipeline pauses after planning for review
- Edit subtasks before execution — add, remove, change scope
- Stop mid-run and resume later — work is preserved

---

### No Provider Lock-In

- **LiteLLM** sits between the code and every provider
- One endpoint — LiteLLM routes to the right model
- Swap providers by changing one line of YAML — zero code changes
- No vendor SDK in application code

```yaml
# Today: OpenAI Codex for workers
- model_name: worker
  litellm_params:
    model: codex-5.2

# Tomorrow: swap to Claude Sonnet — one line change
- model_name: worker
  litellm_params:
    model: anthropic/claude-sonnet-4-20250514
```

---

### Local Models

- Run models on your own machine via **Ollama**
- Zero cost, zero latency to cold start, zero data leaving your machine
- The scanner in this project runs locally — project analysis never hits an API
- The gap between local and cloud models is closing fast

---

## Part 4: Hands-On

> **Full details:** [SESSION.md](SESSION.md)

### What We'll Do

1. Start the stack (Docker Compose — orchestrator + LiteLLM)
2. Open the Web UI at `localhost:8000`
3. Select a project and describe a task
4. Run in **Supervised mode** — inspect the plan before execution
5. Watch the pipeline live: scanner → architect → assign → workers → review → merge
6. Check Langfuse for traces, token counts, and costs
7. Inspect git worktrees — see the isolated branches

---

## Part 5: What's Next

---

### Parallel Agents

- Multiple agents working on the same codebase simultaneously
- Git worktrees provide filesystem isolation — no conflicts
- Each agent gets its own branch, its own workspace
- A Lead Dev agent merges everything back

---

### Potential for SDLC Integration

- **Planning** — AI architect breaks epics into stories, stories into tasks
- **Coding** — parallel agents execute tasks in isolated branches
- **Review** — AI reviewer catches issues before human review
- **Testing** — AI generates tests, runs them, fixes failures
- **Documentation** — auto-generated from code changes
- A pipeline like this could plug into any stage

---

### BMAD — Breakthrough Method for Agile AI-Driven Development

- Defines **12+ specialised AI roles** as Markdown files (PM, Architect, Developer, Scrum Master...)
- Each role has a structured system prompt — repeatable, versionable, auditable
- Alternative to unstructured "vibe coding" — persona-driven workflow
- Compatible with orchestration pipelines — personas as agent prompts

> [github.com/bmad-code-org/BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD)

---

### Gas Town

- Steve Yegge's framework: **20-30 AI agents** working in parallel
- Often described as **"Kubernetes for AI coding agents"**
- Merge queues so parallel work doesn't collide
- Three-tier watchdog chain for reliability
- Full provenance — every action attributed, every agent tracked

> [github.com/steveyegge/gastown](https://github.com/steveyegge/gastown)

---

### GSD — Get Shit Done

- Solves **context rot** — each sub-agent gets a fresh 200K context window
- 4 parallel research agents → planner + checker loop → parallel executors → verifier
- Git branch management, cost tracking, stuck loop detection, crash recovery

> [github.com/gsd-build/get-shit-done](https://github.com/gsd-build/get-shit-done)

---

## Closing

> Models are powerful but flawed. Orchestration is one way to work with both — break work into small pieces, match model to task, keep humans in the loop, own the infrastructure.
