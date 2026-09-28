"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# MeetingCommitmentLedgerAgent — Cat 2 outer graph (docs/02_design.md §Architecture Overview).
# Fixed backbone: initialize -> pre_process -> main -> {route} -> post_process -> finalize.
# Domain complexity (3 modes, extract/match/brief) is hidden inside the inner
# LedgerWorkflowGraph, wrapped by LedgerWorkflowGraphNode in the `main` slot.
# add_edges()/route() are NOT overridden — backbone wiring belongs to the framework.

from __future__ import annotations

from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.main_node import LedgerWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State


class MeetingCommitmentLedgerAgent(AgentBaseGraph):
    """CMN-C2-687 — Meeting Commitment Ledger & Follow-up Recall Agent.

    config keys (docs/02 §Executable-Contract Completeness / config/config.yaml):
      recall_score_threshold / recall_top_k / staging_ttl_seconds / input_* quality bounds.

    Injected seams (constructor / config):
      llm     — BaseLLM injection seam (config["llm"]) or None -> deterministic degradation.
      ledger  — LedgerStore (BaseDatabase-shaped); None -> InMemoryLedgerStore (dev/CI seam).
      recall  — RecallIndex (BaseVectorStore-shaped); None -> InMemoryRecallIndex (dev/CI seam).
    """

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        llm: Any = None,
        ledger: Any = None,
        recall: Any = None,
        kb: Any = None,
    ) -> None:
        # live-LLM harness compatibility: Cls(config=..., llm=..., kb=...) is accepted.
        cfg = dict(config or {})
        self._llm = llm if llm is not None else cfg.pop("llm", None)
        self._ledger = ledger if ledger is not None else cfg.pop("ledger", None)
        recall_arg = recall if recall is not None else (kb if kb is not None else cfg.pop("recall", None) or cfg.pop("kb", None))
        super().__init__(config=cfg)
        # resolve dev/CI seams ONCE so staging persists across invoke/feedback in one process.
        if self._ledger is None:
            from src.services.stores import InMemoryLedgerStore

            self._ledger = InMemoryLedgerStore()
        self._recall = recall_arg
        if self._recall is None:
            from src.services.stores import InMemoryRecallIndex

            self._recall = InMemoryRecallIndex()

    @property
    def name(self) -> str:
        return "meeting_commitment_ledger"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # non-LLM config validation is inherited from AgentBaseGraph (max_retry/hitl/etc.).
        pass

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode(config=self.config)
        self._nodes["main"] = LedgerWorkflowGraphNode(
            config=self.config, llm=self._llm, ledger=self._ledger, recall=self._recall
        )
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — the framework wires the fixed backbone.
