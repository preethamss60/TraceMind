"""
engine/rca.py

Root Cause Analysis via Large Language Model.

Supported providers (tried in order; first available wins):
  1. OpenAI   – set OPENAI_API_KEY  (optionally OPENAI_MODEL, default gpt-4o-mini)
  2. Gemini   – set GEMINI_API_KEY  (optionally GEMINI_MODEL, default gemini-2.0-flash)
  3. Ollama   – set OLLAMA_HOST     (optionally OLLAMA_MODEL, default llama3)

If none is configured the analysis falls back to a static "unavailable" notice
so the rest of the pipeline always completes successfully.

Each provider is asked to reply with a strict JSON object:
  {
    "error_summary":  "<one-sentence description of the error pattern>",
    "root_cause":     "<likely root cause, 1–3 sentences>",
    "suggested_fix":  "<concrete remediation steps, 1–3 sentences>",
    "severity_score": <float 0.0–10.0>
  }

Secrets are never logged.  API keys are read from environment variables only.
"""

import json
import logging
import os
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "You are a senior Site Reliability Engineer performing root cause analysis "
    "on production application logs.\n\n"
    "You will receive a cluster of similar log error messages. "
    "Respond ONLY with a valid JSON object — no markdown fences, no extra keys — "
    "matching this exact schema:\n"
    "{\n"
    '  "error_summary":  "<one-sentence description of the error pattern>",\n'
    '  "root_cause":     "<the most likely technical root cause, 1–3 sentences>",\n'
    '  "suggested_fix":  "<concrete remediation steps, 1–3 sentences>",\n'
    '  "severity_score": <float 0.0–10.0, where 10 is complete service outage>\n'
    "}"
)

_FALLBACK: dict = {
    "error_summary": "Analysis unavailable — no LLM provider configured.",
    "root_cause": (
        "Set OPENAI_API_KEY, GEMINI_API_KEY, or OLLAMA_HOST to enable "
        "AI-powered root cause analysis."
    ),
    "suggested_fix": "Review the evidence entries in the detail panel manually.",
    "severity_score": None,
}

# Maximum representative entries sent to the LLM per cluster
_MAX_EVIDENCE = 20


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _build_user_prompt(cluster_id: str, entries: list[dict]) -> str:
    lines = "\n".join(
        f"[{e.get('level', 'UNKNOWN')}] {e.get('message', '')}"
        for e in entries[:_MAX_EVIDENCE]
    )
    return f"Cluster: {cluster_id}\n\nRepresentative log entries:\n{lines}"


def _validate(raw: dict) -> dict:
    """Ensure the LLM response conforms to the expected schema."""
    result = {**_FALLBACK, **raw}
    for key in ("error_summary", "root_cause", "suggested_fix"):
        if not isinstance(result.get(key), str):
            result[key] = str(result.get(key) or "")
    score = result.get("severity_score")
    try:
        result["severity_score"] = round(float(score), 2) if score is not None else None
    except (TypeError, ValueError):
        result["severity_score"] = None
    return result


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------

def _call_openai(prompt: str) -> Optional[dict]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI  # type: ignore

        client = OpenAI(api_key=api_key)
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return json.loads(response.choices[0].message.content)
    except Exception as exc:
        logger.warning("OpenAI RCA call failed: %s", exc)
        return None


def _call_gemini(prompt: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return None
    try:
        from google import genai  # type: ignore
        from google.genai import types as genai_types  # type: ignore

        client = genai.Client(api_key=api_key)
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        response = client.models.generate_content(
            model=model,
            contents=f"{_SYSTEM_PROMPT}\n\n{prompt}",
            config=genai_types.GenerateContentConfig(
                temperature=0.2,
                max_output_tokens=512,
                response_mime_type="application/json",
            ),
        )
        return json.loads(response.text)
    except Exception as exc:
        logger.warning("Gemini RCA call failed: %s", exc)
        return None


def _call_ollama(prompt: str) -> Optional[dict]:
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    model = os.getenv("OLLAMA_MODEL", "llama3")
    try:
        import requests  # type: ignore

        payload = {
            "model": model,
            "prompt": f"{_SYSTEM_PROMPT}\n\n{prompt}",
            "stream": False,
            "format": "json",
        }
        resp = requests.post(f"{host}/api/generate", json=payload, timeout=60)
        resp.raise_for_status()
        return json.loads(resp.json()["response"])
    except Exception as exc:
        logger.warning("Ollama RCA call failed (%s/%s): %s", host, model, exc)
        return None


_PROVIDERS = [_call_openai, _call_gemini, _call_ollama]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_cluster(cluster_id: str, entries: list[dict]) -> dict:
    """
    Run LLM RCA on a single cluster.

    Tries each configured provider in order (OpenAI → Gemini → Ollama).
    Returns a validated dict with keys:
    ``error_summary``, ``root_cause``, ``suggested_fix``, ``severity_score``.
    """
    prompt = _build_user_prompt(cluster_id, entries)
    for provider in _PROVIDERS:
        result = provider(prompt)
        if result:
            validated = _validate(result)
            logger.info(
                "RCA success for cluster %s via %s (severity=%.1f)",
                cluster_id,
                provider.__name__.lstrip("_call_"),
                validated.get("severity_score") or 0.0,
            )
            return validated
    logger.info("No LLM provider available; using static fallback for %s", cluster_id)
    return dict(_FALLBACK)


def analyze_all_clusters(
    df: pd.DataFrame,
    min_error_entries: int = 1,
) -> dict[str, dict]:
    """
    Run ``analyze_cluster`` for every cluster in *df* that contains at least
    *min_error_entries* rows at ERROR / FATAL level.

    Returns:
        ``{cluster_id: rca_dict}``
    """
    results: dict[str, dict] = {}
    if "cluster_id" not in df.columns:
        return results

    error_df = df[
        df["level"].str.upper().isin({"ERROR", "FATAL", "CRITICAL"})
        & df["cluster_id"].notna()
    ]

    for cluster_id, group in error_df.groupby("cluster_id"):
        if len(group) < min_error_entries:
            continue
        entries = group[["level", "message"]].to_dict("records")
        logger.info("Analysing cluster %s (%d entries)", cluster_id, len(entries))
        results[str(cluster_id)] = analyze_cluster(str(cluster_id), entries)

    return results
