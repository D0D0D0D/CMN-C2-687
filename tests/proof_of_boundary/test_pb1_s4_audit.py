"""PB-1 / TC-05 (domain): every FunctionNode module emits at least one domain S-4 event.

Patches each node module's emit_trace_event binding and asserts a DOMAIN event fires from
execute() (not the framework-automatic node_start/complete). Mutation check: delete the
domain emit and this test goes red. GraphNode (main) is excluded — see PB-6.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil


def _discover():
    from framework.nodes.base_node import BaseNode
    from framework.nodes.graph_node import GraphNode

    pkg = importlib.import_module("src.nodes")
    out = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes.", onerror=lambda _n: None):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and not issubclass(attr, GraphNode)
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                out.append((modname, module, attr))
    return out


def test_every_node_module_emits_a_domain_event(monkeypatch):
    nodes = _discover()
    assert len(nodes) >= 5
    failures = []
    for modname, module, cls in nodes:
        captured = []
        monkeypatch.setattr(module, "emit_trace_event", lambda et, p, s, *a, _c=captured, **k: _c.append(et))
        state = {
            "correlation_id": "pb1",
            "mode": "invoke",
            "input_context": {"mode": "invoke", "tenant_id": "t1", "org_id": "o1"},
            "tenant_id": "t1",
            "org_id": "o1",
            "as_of_utc": "2026-08-05T00:00:00Z",
            "user_input": "{}",
            "normalized_transcript": "",
            "meeting_meta": {"meeting_id": "M1"},
            "extracted_items": [],
            "match_result": {},
            "fetched_open_rows": [],
            "node_history": [],
            "error_log": [],
        }
        cls().execute(state)
        if not captured:
            failures.append(modname)
    assert not failures, f"nodes with no domain S-4 event: {failures}"
