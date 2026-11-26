"""
engine/pipeline.py

Orchestrates the full TraceMind analysis pipeline:

  1. parser           - tokenise & structure raw log lines into a DataFrame
  2. anomaly_detector - flag unusual entries via Isolation Forest + TF-IDF
  3. clusterer        - group similar errors with DBSCAN (or Agglomerative fallback)
  4. rca              - call an LLM for root cause + remediation per cluster

``run_analysis(filepath)`` is the single entry point expected by the Flask
application layer (see engine/README.md for the exact return contract).
"""

import logging
from pathlib import Path
from typing import Union

import pandas as pd

from .parser import parse_file
from .anomaly_detector import detect
from .clusterer import assign_clusters
from .rca import analyze_all_clusters

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _safe_isoformat(ts) -> Union[str, None]:
    """Return ISO-8601 string or None, handling NaT / None gracefully."""
    try:
        if pd.isna(ts):
            return None
    except (TypeError, ValueError):
        pass
    try:
        return ts.isoformat()
    except AttributeError:
        return None


def run_analysis(filepath: Union[str, Path]) -> dict:
    """
    Full analysis pipeline for one log file.

    Parameters
    ----------
    filepath : str | Path
        Absolute or relative path to the log file to analyse.

    Returns
    -------
    dict with two keys:

    ``entries``
        List of dicts, one per parsed log entry::

            {
              "timestamp":  "2024-03-15T08:04:00+00:00" | null,
              "level":      "INFO" | "WARN" | "ERROR" | "FATAL" | ...,
              "message":    "<full log line text>",
              "is_anomaly": true | false,
              "cluster_id": "cluster-0001" | "noise-abc123def456" | null
            }

    ``reports``
        List of dicts, one per error cluster that the LLM analysed::

            {
              "cluster_id":    "cluster-0001",
              "error_summary": "...",
              "root_cause":    "...",
              "suggested_fix": "...",
              "severity_score": 7.5 | null
            }
    """
    path = Path(filepath)
    logger.info("Starting analysis for '%s'", path.name)

    # 1. Parse
    try:
        df = parse_file(path)
    except Exception as exc:
        logger.error("Parse failed for '%s': %s", path.name, exc, exc_info=True)
        return {"entries": [], "reports": []}

    if df.empty:
        logger.warning("No log entries parsed from '%s'", path.name)
        return {"entries": [], "reports": []}

    logger.info("Parsed %d log entries", len(df))

    # 2. Anomaly detection
    try:
        df["is_anomaly"] = detect(df)
    except Exception as exc:
        logger.error("Anomaly detection failed: %s", exc, exc_info=True)
        df["is_anomaly"] = df["level"].str.upper().isin({"ERROR", "FATAL", "CRITICAL"})

    # 3. Cluster similar errors
    try:
        df["cluster_id"] = assign_clusters(df)
    except Exception as exc:
        logger.error("Clustering failed: %s", exc, exc_info=True)
        df["cluster_id"] = None

    # 4. LLM root cause analysis
    try:
        rca_map = analyze_all_clusters(df)
    except Exception as exc:
        logger.error("RCA failed: %s", exc, exc_info=True)
        rca_map = {}

    # 5. Serialise to contract format
    entries = [
        {
            "timestamp":  _safe_isoformat(row["timestamp"]),
            "level":      row["level"],
            "message":    row["message"],
            "is_anomaly": bool(row["is_anomaly"]),
            "cluster_id": (
                row["cluster_id"]
                if pd.notna(row.get("cluster_id"))
                else None
            ),
        }
        for _, row in df.iterrows()
    ]

    reports = [
        {
            "cluster_id":    cluster_id,
            "error_summary": rca.get("error_summary"),
            "root_cause":    rca.get("root_cause"),
            "suggested_fix": rca.get("suggested_fix"),
            "severity_score": rca.get("severity_score"),
        }
        for cluster_id, rca in rca_map.items()
    ]

    logger.info(
        "Analysis complete: %d entries | %d anomalies | %d clusters | %d RCA reports",
        len(entries),
        sum(e["is_anomaly"] for e in entries),
        df["cluster_id"].nunique(dropna=True),
        len(reports),
    )
    return {"entries": entries, "reports": reports}
