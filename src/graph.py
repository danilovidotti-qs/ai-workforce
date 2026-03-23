"""LangGraph pipelines: plan + execute, with optional pause between them."""

from langgraph.graph import StateGraph, END
from langgraph.types import Send

from src.state import AgentState
from src.agents import (
    scanner_scan,
    architect_plan,
    lead_dev_assign,
    execute_worker,
    execute_sequential,
    lead_dev_review,
    lead_dev_merge,
)

MAX_REVIEW_ROUNDS = 3


# ── Routing functions ────────────────────────────────────────────────────────

def route_execution(state: AgentState):
    """After assignment, decide parallel (Send per worktree) or sequential."""
    if state["overlap_detected"]:
        return "execute_sequential"
    return [
        Send("execute_worker", {**state, "_current_worktree_id": wt["id"]})
        for wt in state["worktrees"]
    ]


def route_after_review(state: AgentState) -> str:
    """After review, either loop back for fixes or proceed to merge."""
    has_feedback = bool(state.get("review_feedback"))
    at_max_rounds = state.get("review_round", 0) >= MAX_REVIEW_ROUNDS

    if has_feedback and not at_max_rounds:
        if state["overlap_detected"]:
            return "execute_sequential"
        return "re_execute_parallel"

    return "lead_dev_merge"


# ── Plan graph: architect breaks down the task ───────────────────────────────

plan_builder = StateGraph(AgentState)
plan_builder.add_node("scanner_scan", scanner_scan)
plan_builder.add_node("architect_plan", architect_plan)
plan_builder.add_edge("__start__", "scanner_scan")
plan_builder.add_edge("scanner_scan", "architect_plan")
plan_builder.add_edge("architect_plan", END)

plan_graph = plan_builder.compile()


# ── Execute graph: assign → execute → review loop → merge ────────────────────

exec_builder = StateGraph(AgentState)
exec_builder.add_node("lead_dev_assign", lead_dev_assign)
exec_builder.add_node("execute_worker", execute_worker)
exec_builder.add_node("execute_sequential", execute_sequential)
exec_builder.add_node("lead_dev_review", lead_dev_review)
exec_builder.add_node("lead_dev_merge", lead_dev_merge)

exec_builder.add_edge("__start__", "lead_dev_assign")

exec_builder.add_conditional_edges(
    "lead_dev_assign",
    route_execution,
    {"execute_sequential": "execute_sequential"},
)

exec_builder.add_edge("execute_worker", "lead_dev_review")
exec_builder.add_edge("execute_sequential", "lead_dev_review")

exec_builder.add_conditional_edges(
    "lead_dev_review",
    route_after_review,
    {
        "execute_sequential": "execute_sequential",
        "re_execute_parallel": "execute_worker",
        "lead_dev_merge": "lead_dev_merge",
    },
)

exec_builder.add_edge("lead_dev_merge", END)

exec_graph = exec_builder.compile()


# ── Full graph (auto mode): plan + execute in one shot ───────────────────────

full_builder = StateGraph(AgentState)
full_builder.add_node("scanner_scan", scanner_scan)
full_builder.add_node("architect_plan", architect_plan)
full_builder.add_node("lead_dev_assign", lead_dev_assign)
full_builder.add_node("execute_worker", execute_worker)
full_builder.add_node("execute_sequential", execute_sequential)
full_builder.add_node("lead_dev_review", lead_dev_review)
full_builder.add_node("lead_dev_merge", lead_dev_merge)

full_builder.add_edge("__start__", "scanner_scan")
full_builder.add_edge("scanner_scan", "architect_plan")
full_builder.add_edge("architect_plan", "lead_dev_assign")

full_builder.add_conditional_edges(
    "lead_dev_assign",
    route_execution,
    {"execute_sequential": "execute_sequential"},
)

full_builder.add_edge("execute_worker", "lead_dev_review")
full_builder.add_edge("execute_sequential", "lead_dev_review")

full_builder.add_conditional_edges(
    "lead_dev_review",
    route_after_review,
    {
        "execute_sequential": "execute_sequential",
        "re_execute_parallel": "execute_worker",
        "lead_dev_merge": "lead_dev_merge",
    },
)

full_builder.add_edge("lead_dev_merge", END)

full_graph = full_builder.compile()

# Default export for LangGraph Studio
graph = full_graph
