"""AgentCore Platform v1.0 — CMN-C2-687 Meeting Commitment Ledger & Follow-up Recall Agent."""

# Pure deterministic transforms: input quality gate (S-2 quarantine), NFKC normalization,
# and ISO-8601 time handling / overdue computation (docs/02_design.md §S-2, §Clock seam).
# Framework-free so the core logic is unit-testable without the SDK.

from __future__ import annotations

import unicodedata
from datetime import datetime, timezone

# ── quality-gate reason codes (S-2) ─────────────────────────────────────────────
QG_DISALLOWED_FORMAT = "disallowed_format"    # PDF/ZIP-OOXML/OLE magic bytes
QG_INVALID_ENCODING = "invalid_encoding"      # replacement-char ratio too high (mojibake)
QG_CONTROL_CHARS = "control_chars"            # control-char ratio too high
QG_LOW_PRINTABLE = "low_printable"            # printable ratio too low
QG_TOO_SHORT = "too_short"
QG_TOO_LONG = "too_long"

# Default thresholds mirror config/config.yaml; these are defensive code defaults used
# when a runtime key is absent.
DEFAULT_GATE_CONFIG: dict = {
    "input_min_chars": 20,
    "input_max_chars": 200000,
    "input_max_replacement_char_ratio": 0.02,
    "input_max_control_char_ratio": 0.02,
    "input_min_printable_ratio": 0.90,
}

# Leading-byte signatures of binary document formats a text/CSV export never starts with.
_MAGIC_SIGNATURES: tuple[bytes, ...] = (
    b"%PDF",            # PDF
    b"PK\x03\x04",      # ZIP / OOXML (xlsx/docx)
    b"\xd0\xcf\x11\xe0",  # OLE (legacy .xls/.doc)
)


def quality_gate(text: str, config: dict | None = None) -> str | None:
    """Ordered S-2 input quality gate. Returns a reason code (first failure) or None.

    Order (docs/02 §S-2): magic-byte -> encoding(mojibake) -> control ratio ->
    printable ratio -> length. Operates on the (already PII-masked, decoded) string.
    """
    cfg = {**DEFAULT_GATE_CONFIG, **(config or {})}

    # 1. format allowlist — sniff the leading bytes (a text/CSV export never starts here).
    head_bytes = text.encode("utf-8", errors="replace")[:8]
    for sig in _MAGIC_SIGNATURES:
        if head_bytes.startswith(sig):
            return QG_DISALLOWED_FORMAT

    total = len(text)
    # 4/5. length is checked with the ratios below; guard the zero-length denominator here.
    if total < int(cfg["input_min_chars"]):
        return QG_TOO_SHORT
    if total > int(cfg["input_max_chars"]):
        return QG_TOO_LONG

    # 2. mojibake — U+FFFD replacement-char ratio (a strict decode upstream would reject
    #    outright; a paste of already-broken text is caught here).
    replacement = text.count("�")
    if replacement / total > float(cfg["input_max_replacement_char_ratio"]):
        return QG_INVALID_ENCODING

    # 3. control-char ratio (C0/C1 excluding TAB/LF/CR).
    control = sum(1 for c in text if _is_control(c))
    if control / total > float(cfg["input_max_control_char_ratio"]):
        return QG_CONTROL_CHARS

    # 3b. printable ratio.
    printable = sum(1 for c in text if _is_printable(c))
    if printable / total < float(cfg["input_min_printable_ratio"]):
        return QG_LOW_PRINTABLE

    return None


def _is_control(ch: str) -> bool:
    o = ord(ch)
    if ch in ("\t", "\n", "\r"):
        return False
    return o < 0x20 or 0x7F <= o <= 0x9F


def _is_printable(ch: str) -> bool:
    if ch in ("\t", "\n", "\r", " "):
        return True
    if _is_control(ch):
        return False
    # Unicode categories starting with C (other/control/format/surrogate) are non-printable.
    return not unicodedata.category(ch).startswith("C")


def nfkc(text: str) -> str:
    """NFKC normalization — fold full-width digits/letters so verbatim token checks are
    stable (docs/02 §S-3 (c): `params.due`/`owner` verbatim-within-span after NFKC)."""
    return unicodedata.normalize("NFKC", text or "")


def signature_tokens(text: str) -> set[str]:
    """CJK-aware similarity signature for cross-meeting matching (brief classify / recall).

    Japanese/Chinese text carries no whitespace word boundaries, so a whitespace/alnum
    tokenizer collapses a whole sentence into one token and never overlaps. This emits ASCII
    alnum WORDS plus character BIGRAMS over the alnum stream, so CJK phrases share bigram
    tokens and Jaccard overlap becomes meaningful. Deterministic and pure.
    """
    s = nfkc(text).lower()
    out: set[str] = set()
    word: list[str] = []
    stream: list[str] = []
    for ch in s:
        if ch.isalnum():
            stream.append(ch)
            if ch.isascii():
                word.append(ch)
            elif word:
                out.add("".join(word))
                word = []
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    joined = "".join(stream)
    if len(joined) == 1:
        out.add(joined)
    for i in range(len(joined) - 1):
        out.add(joined[i : i + 2])
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of two token sets (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── ISO-8601 time / overdue (offset-aware; never string-compared) ────────────────
# 🔴 Comparing ISO strings lexically breaks across offsets — "2026-01-01T00:00:00+09:00"
# sorts AFTER "2026-01-01T00:00:00Z" as text but is EARLIER in time
# Always parse to aware datetimes.

def parse_iso(value: str) -> datetime | None:
    """Parse an ISO-8601 string to an aware UTC datetime. Naive input is assumed UTC.
    Accepts a trailing ``Z``. Date-only (``YYYY-MM-DD``) parses to midnight UTC.
    Returns None on unparseable input (caller decides degradation)."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        # date-only fallback
        try:
            dt = datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_overdue(due_iso: str, as_of_iso: str) -> bool:
    """True when ``due`` is strictly before ``as_of`` (deterministic, offset-aware).
    Unparseable due -> not overdue (fail-safe; surfaced as a coverage gap elsewhere)."""
    due = parse_iso(due_iso)
    as_of = parse_iso(as_of_iso)
    if due is None or as_of is None:
        return False
    return due < as_of


def to_utc_iso(value: str) -> str | None:
    """Normalize an ISO-8601 timestamp to canonical ``...Z`` UTC, or None if unparseable."""
    dt = parse_iso(value)
    if dt is None:
        return None
    return dt.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_part(as_of_iso: str) -> str:
    """The ``YYYY-MM-DD`` component of an as_of timestamp (meeting_meta.date default)."""
    dt = parse_iso(as_of_iso)
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%d")


def add_seconds_iso(as_of_iso: str, seconds: int) -> str:
    """Return ``as_of + seconds`` as canonical ``...Z`` UTC (staging expiry basis).
    Empty string if as_of is unparseable (caller treats as immediately expired)."""
    from datetime import timedelta

    dt = parse_iso(as_of_iso)
    if dt is None:
        return ""
    return (dt + timedelta(seconds=int(seconds))).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
