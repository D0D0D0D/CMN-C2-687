"""Framework-free unit tests for src.services.normalize (quality gate + ISO time)."""

from __future__ import annotations

from src.services import normalize as n


def test_quality_gate_accepts_normal_text():
    assert n.quality_gate("これは十分な長さの正常な議事録テキストです。" * 2) is None


def test_quality_gate_rejects_pdf_magic():
    assert n.quality_gate("%PDF-1.7 binary content here and more padding text") == n.QG_DISALLOWED_FORMAT


def test_quality_gate_rejects_zip_magic():
    assert n.quality_gate("PK\x03\x04rest of a zip/xlsx file padding padding") == n.QG_DISALLOWED_FORMAT


def test_quality_gate_rejects_too_short():
    assert n.quality_gate("short") == n.QG_TOO_SHORT


def test_quality_gate_rejects_too_long():
    assert n.quality_gate("a" * 10, {"input_min_chars": 1, "input_max_chars": 5}) == n.QG_TOO_LONG


def test_quality_gate_rejects_mojibake():
    text = "�" * 30 + "some readable tail padding here now"
    assert n.quality_gate(text) == n.QG_INVALID_ENCODING


def test_quality_gate_rejects_control_chars():
    text = "\x00\x01\x02\x03\x04" * 10 + "readable tail padding here now ok"
    assert n.quality_gate(text) == n.QG_CONTROL_CHARS


def test_iso_string_compare_offsets_not_lexical():
    # +09:00 midnight is EARLIER than the same wall-clock UTC — lexical compare gets this wrong.
    assert n.is_overdue("2026-01-01T00:00:00+09:00", "2026-01-01T00:00:00Z") is True


def test_overdue_false_when_due_after_as_of():
    assert n.is_overdue("2026-12-31T00:00:00Z", "2026-08-05T00:00:00Z") is False


def test_overdue_unparseable_is_false():
    assert n.is_overdue("next friday", "2026-08-05T00:00:00Z") is False


def test_to_utc_iso_normalizes_offset():
    assert n.to_utc_iso("2026-08-05T09:00:00+09:00") == "2026-08-05T00:00:00Z"


def test_date_part():
    assert n.date_part("2026-08-05T23:59:00Z") == "2026-08-05"


def test_add_seconds_iso():
    assert n.add_seconds_iso("2026-08-05T00:00:00Z", 86400) == "2026-08-06T00:00:00Z"


def test_nfkc_folds_fullwidth():
    assert n.nfkc("ＡＢＣ１２３") == "ABC123"


if __name__ == "__main__":
    _fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for _fn in _fns:
        _fn()
    print(f"OK normalize: {len(_fns)} tests")
