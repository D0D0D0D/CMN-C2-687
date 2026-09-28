"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Inner domain workflow (docs/02_design.md §Inner subgraph). Fixed linear pipeline
# extract -> match -> brief; MODE selects behaviour inside the nodes, not the node set.
# Instantiated by LedgerWorkflowGraphNode.get_subgraph() in src/nodes/main_node.py.
#
# 🔴 route()/get_output() are annotated with LedgerWorkflowState (this module's own schema),
# not the base AgentState — annotating with the base drops custom fields (mode, etc.) under
# state projection and mis-routes.

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.nodes.inner.brief_node import BriefNode
from src.nodes.inner.extract_node import ExtractNode
from src.nodes.inner.match_node import MatchNode
from src.schemas.state import State


class LedgerWorkflowState(State):
    """Inner graph schema — declares the same field contract as its own schema so route()/
    get_output() annotations resolve against the real fields."""


class LedgerWorkflowGraph(BaseGraph):
    """Inner extract -> match -> brief workflow (mode-parameterized inside the nodes)."""

    def __init__(self, config: dict | None = None, llm: Any = None, ledger: Any = None, recall: Any = None) -> None:
        self._llm = llm
        self._ledger = ledger
        self._recall = recall
        super().__init__(config=config or {})

    @property
    def name(self) -> str:
        return "ledger_workflow"

    @property
    def state_schema(self) -> type:
        return LedgerWorkflowState

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes is abstract. Do NOT register initialize/finalize.
        self._nodes["extract"] = ExtractNode(llm=self._llm)
        self._nodes["match"] = MatchNode(ledger=self._ledger, recall=self._recall, config=self.config)
        self._nodes["brief"] = BriefNode(ledger=self._ledger, config=self.config)

    def add_edges(self) -> None:
        self._sg.add_edge(START, "extract")
        # skip downstream nodes once a node sets status=error (fail-closed short-circuit).
        self._sg.add_conditional_edges("extract", self._route_on_error, {"continue": "match", "error": END})
        self._sg.add_conditional_edges("match", self._route_on_error, {"continue": "brief", "error": END})
        self._sg.add_edge("brief", END)

    def _route_on_error(self, state: LedgerWorkflowState) -> str:
        return "error" if state.get("status") == AgentStatus.ERROR.value else "continue"

    def route(self, state: LedgerWorkflowState) -> str:
        # conditional routing handled by add_conditional_edges; required by the ABC.
        return END

    def get_output(self, state: LedgerWorkflowState) -> dict:
        out: dict[str, Any] = {"status": state.get("status", AgentStatus.SUCCESS.value)}
        if state.get("degradation_reason"):
            out["degradation_reason"] = state["degradation_reason"]
        if state.get("error_log"):
            out["error_log"] = state["error_log"]
        # extracted_items surfaced so the outer post_process can build the minutes draft.
        if "extracted_items" in state:
            out["extracted_items"] = state.get("extracted_items", [])
        mode = state.get("mode")
        if mode == "invoke":
            if "commitment_brief" in state:
                out["commitment_brief"] = state["commitment_brief"]
            if state.get("brief_id"):
                out["brief_id"] = state["brief_id"]
        elif mode == "ingest":
            out["ingest_summary"] = state.get("ingest_summary", {})
        elif mode == "feedback":
            out["write_result"] = state.get("write_result", {})
        return out
