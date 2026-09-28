"""Framework-free unit tests for src.services.extraction (prompt + response parsing)."""

from __future__ import annotations

import json

from src.services import extraction


def test_prompt_embeds_schema_and_transcript():
    p = extraction.build_extract_prompt("会議本文", {"meeting_id": "M1", "title": "定例"})
    # 🔴 the schema MUST be embedded (else the LLM returns {} — MunicipalContractVariationEvidenceBriefAgent defect).
    assert '"items"' in p and "evidence_span" in p and "会議本文" in p and "M1" in p
    assert "NEVER infer, normalize, translate, or convert `owner` or `due`" in p
    assert "do NOT convert it to ISO-8601" in p


def test_parse_valid_items():
    raw = json.dumps(
        {
            "items": [
                {
                    "type": "action",
                    "summary": "在庫を確認する",
                    "owner": "担当A",
                    "due": "2026-08-14",
                    "evidence_span": "担当Aが在庫を確認する",
                    "params": {"qty": "10"},
                }
            ]
        },
        ensure_ascii=False,
    )
    items = extraction.parse_extraction(raw)
    assert len(items) == 1 and items[0]["type"] == "action" and items[0]["owner"] == "担当A"


def test_parse_drops_short_span():
    raw = json.dumps({"items": [{"type": "action", "summary": "x", "evidence_span": "短"}]}, ensure_ascii=False)
    assert extraction.parse_extraction(raw) == []


def test_parse_drops_unknown_type():
    raw = json.dumps({"items": [{"type": "rumor", "summary": "x", "evidence_span": "long enough span"}]}, ensure_ascii=False)
    assert extraction.parse_extraction(raw) == []


def test_parse_empty_object_returns_empty():
    # the MunicipalContractVariationEvidenceBriefAgent failure shape — {} must coerce to [] (not a valid-but-empty item).
    assert extraction.parse_extraction("{}") == []


def test_parse_garbage_returns_empty():
    assert extraction.parse_extraction("not json at all") == []


def test_parse_fenced_json_block():
    raw = "```json\n" + json.dumps({"items": []}) + "\n```"
    assert extraction.parse_extraction(raw) == []


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK extraction: {len(_fns)} tests")
