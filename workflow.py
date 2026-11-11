import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from langsmith import tracing_context

from storage import ApprovalError, TicketStore


class ApprovalState(TypedDict, total=False):
    run_id: str
    ticket_id: str
    revision: int
    draft: str
    approved: bool
    outcome: str


def build_graph(store: TicketStore, checkpointer: SqliteSaver):
    def prepare(state: ApprovalState):
        ticket = store.get(state["ticket_id"])
        return {"revision": ticket["revision"], "draft": ticket["draft"]}

    def review(state: ApprovalState):
        decision = interrupt({
            "ticket_id": state["ticket_id"],
            "revision": state["revision"],
            "draft": state["draft"],
        })
        return {"approved": decision is True}

    def apply(state: ApprovalState):
        outcome = store.apply(
            state["run_id"], state["ticket_id"], state["revision"], state["draft"]
        )
        return {"outcome": outcome}

    def reject(_state: ApprovalState):
        return {"outcome": "rejected"}

    graph = StateGraph(ApprovalState)
    graph.add_node("prepare", prepare)
    graph.add_node("review", review)
    graph.add_node("apply", apply)
    graph.add_node("reject", reject)
    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "review")
    graph.add_conditional_edges(
        "review", lambda state: "apply" if state["approved"] else "reject",
        {"apply": "apply", "reject": "reject"},
    )
    graph.add_edge("apply", END)
    graph.add_edge("reject", END)
    return graph.compile(checkpointer=checkpointer)


class ApprovalWorkflow:
    def __init__(self, directory: Path, store: TicketStore):
        self.directory = directory
        self.store = store

    @contextmanager
    def _open_graph(self, run_id: str):
        # Serialize graph commands across CLI processes without locking ticket edits.
        lock_path = self.directory / "workflow-lock.sqlite3"
        checkpoint_path = self.directory / "checkpoints.sqlite3"
        with closing(sqlite3.connect(lock_path, timeout=10)) as lock, lock:
            lock.execute("BEGIN IMMEDIATE")
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                with tracing_context(enabled=False):
                    config = {"configurable": {"thread_id": run_id}}
                    yield build_graph(self.store, saver), config

    @staticmethod
    def _snapshot(graph, config):
        snapshot = graph.get_state(config)
        if not snapshot.values.get("run_id"):
            raise ApprovalError(f"Unknown run: {config['configurable']['thread_id']}")
        return snapshot

    @staticmethod
    def _view(snapshot):
        state = dict(snapshot.values)
        if snapshot.interrupts:
            state["status"] = "awaiting_review"
        else:
            state["status"] = state.get("outcome", "needs_recovery")
        return state

    def request(self, ticket_id: str, run_id: str):
        self.store.get(ticket_id)
        with self._open_graph(run_id) as (graph, config):
            snapshot = graph.get_state(config)
            if snapshot.values:
                if snapshot.values["ticket_id"] != ticket_id:
                    raise ApprovalError(f"Run ID already belongs to another ticket: {run_id}")
            else:
                graph.invoke({"run_id": run_id, "ticket_id": ticket_id}, config, durability="sync")
            return self._view(self._snapshot(graph, config))

    def status(self, run_id: str):
        with self._open_graph(run_id) as (graph, config):
            return self._view(self._snapshot(graph, config))

    def decide(self, run_id: str, revision: int, approved: bool):
        with self._open_graph(run_id) as (graph, config):
            snapshot = self._snapshot(graph, config)
            if snapshot.values.get("revision") != revision:
                raise ApprovalError("Decision revision does not match the reviewed revision.")
            if snapshot.interrupts:
                graph.invoke(Command(resume=approved), config, durability="sync")
            elif snapshot.next:
                raise ApprovalError(f"Run already has a decision or needs recovery. Use recover {run_id}.")
            elif snapshot.values.get("approved") is not approved:
                raise ApprovalError("Run already has a different decision.")
            return self._view(self._snapshot(graph, config))

    def recover(self, run_id: str):
        with self._open_graph(run_id) as (graph, config):
            snapshot = self._snapshot(graph, config)
            if snapshot.next and not snapshot.interrupts:
                graph.invoke(None, config, durability="sync")
            return self._view(self._snapshot(graph, config))
