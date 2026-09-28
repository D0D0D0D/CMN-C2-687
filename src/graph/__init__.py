"""AgentCore Platform v1.0"""

# Re-export the agent class so the manifest's dotted entry point resolves.
from src.graph.graph import MeetingCommitmentLedgerAgent

__all__ = ["MeetingCommitmentLedgerAgent"]
