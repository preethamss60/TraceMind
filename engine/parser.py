"""
engine/parser.py

Parses raw log files into a structured pandas DataFrame.

Responsibilities:
  - Multi-format timestamp extraction (ISO-8601, syslog).
  - Log level normalisation (WARNING → WARN, SEVERE → ERROR).
  - Multi-line stack trace grouping (lines starting with whitespace,
    "at ", "Caused by:", etc. are merged into the previous entry).
  - Dynamic token masking (<IP>, <NUM>, <UUID>, <URL>, <PATH>,
    <EMAIL>, <HEX>, <REDACTED>) for stable clustering templates.
  - MD5 template hashing for cache-friendly deduplication.
"""

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

# ---------------------------------------------------------------------------
# Timestamp patterns (tried in declaration order; first match wins)
# ---------------------------------------------------------------------------
_TS_SPECS: list[tuple[re.Pattern, list[str]]] = [
    (
        # ISO-8601: 2024-03-15T08:00:01.123Z  /  2024-03-15 08:00:01
        re.compile(
            r"\b(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
            r"(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"
        ),
        [
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
        ],
    ),
    (
        # Syslog: Mar 15 08:00:01
        re.compile(r"\b(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\b"),
        ["%b %d %H:%M:%S", "%b  %d %H:%M:%S"],
    ),
]

# ---------------------------------------------------------------------------
# Log-level extraction
# ---------------------------------------------------------------------------
_LEVEL_RE = re.compile(
    r"\b(DEBUG|TRACE|VERBOSE|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL|SEVERE)\b",
    re.IGNORECASE,
)
_LEVEL_MAP = {
    "WARNING": "WARN",
    "SEVERE": "ERROR",
    "CRITICAL": "FATAL",
    "VERBOSE": "DEBUG",
}

# Lines that are continuations of the previous log entry (stack traces, etc.)
_CONTINUATION_RE = re.compile(
    r"^\s+at\s"              # Java / Kotlin "at …"
    r"|^Caused by:"          # Java chained exceptions
    r"|^\s+\.\.\."           # "... N more" truncation
    r"|^\s+\["               # bracket-indented detail
    r"|^\s{4,}"              # deeply indented (Python tracebacks, etc.)
)

# ---------------------------------------------------------------------------
# Masking patterns – applied in this exact order to build log templates
# ---------------------------------------------------------------------------
_MASKS: list[tuple[re.Pattern, str]] = [
    # Credentials must come first to avoid number-masking eating the value
    (
        re.compile(
            r"(?:password|passwd|secret|token|api[_\-]?key|authorization|bearer)"
            r"\s*[=:]\s*\S+",
            re.IGNORECASE,
        ),
        "<REDACTED>",
    ),
    # IPv4 with optional port
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d{1,5})?\b"), "<IP>"),
    # UUID
    (
        re.compile(
            r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
            re.IGNORECASE,
        ),
        "<UUID>",
    ),
    # URLs (must precede PATH)
    (re.compile(r"https?://\S+"), "<URL>"),
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "<EMAIL>",
    ),
    # Hex literals
    (re.compile(r"\b0x[0-9A-Fa-f]+\b"), "<HEX>"),
    # Unix-style file paths (>= 2 path components)
    (re.compile(r"(?<![:\w])(?:/[\w.\-]+){2,}"), "<PATH>"),
    # Windows paths
    (re.compile(r"[A-Za-z]:\\(?:[\w.\- ]+\\)*[\w.\- ]+"), "<PATH>"),
    # Numbers (with optional trailing units) — must come last
    (
        re.compile(r"\b\d+(?:[.,]\d+)?(?:ms|s|ns|us|MB|GB|KB|%|px|k)?\b"),
        "<NUM>",
    ),
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(raw: str) -> Optional[datetime]:
    """Return the first parseable datetime found in *raw*, or None."""
    for pattern, fmts in _TS_SPECS:
        m = pattern.search(raw)
        if not m:
            continue
        ts_str = m.group("ts")
        for fmt in fmts:
            try:
                dt = datetime.strptime(ts_str, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                continue
    return None


def _parse_level(text: str) -> str:
    """Return normalised log level string, or 'UNKNOWN'."""
    m = _LEVEL_RE.search(text)
    if not m:
        return "UNKNOWN"
    lvl = m.group(0).upper()
    return _LEVEL_MAP.get(lvl, lvl)


def mask(text: str) -> str:
    """Replace dynamic tokens with stable placeholders."""
    for pattern, replacement in _MASKS:
        text = pattern.sub(replacement, text)
    return text


def template_hash(template: str) -> str:
    """Short, stable MD5 hex digest of a masked template string."""
    return hashlib.md5(template.encode("utf-8")).hexdigest()[:12]


def _flush(pending: list[str], rows: list[dict]) -> None:
    """Merge *pending* lines into one log entry and append to *rows*."""
    if not pending:
        return
    raw = "\n".join(pending)
    message = raw.strip()
    templ = mask(message)
    rows.append(
        {
            "line_no": len(rows) + 1,
            "raw": raw,
            "timestamp": _parse_timestamp(raw),
            "level": _parse_level(raw),
            "message": message,
            "template": templ,
            "template_hash": template_hash(templ),
        }
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def parse_file(filepath: "str | Path") -> pd.DataFrame:
    """
    Parse a log file and return a tidy DataFrame with columns:

    - ``line_no``       - 1-based entry number (after grouping)
    - ``raw``           - original text (may span multiple lines)
    - ``timestamp``     - tz-aware datetime or NaT
    - ``level``         - normalised level string
    - ``message``       - stripped full text of the entry
    - ``template``      - masked version of *message*
    - ``template_hash`` - 12-char hex digest of *template*
    """
    path = Path(filepath)
    rows: list[dict] = []
    pending: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for raw_line in fh:
            line = raw_line.rstrip("\n\r")
            if _CONTINUATION_RE.match(line) and pending:
                pending.append(line)
            else:
                _flush(pending, rows)
                pending = [line]
        _flush(pending, rows)

    if not rows:
        return pd.DataFrame(
            columns=[
                "line_no", "raw", "timestamp", "level",
                "message", "template", "template_hash",
            ]
        )

    df = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df
