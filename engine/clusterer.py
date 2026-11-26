"""
engine/clusterer.py

Groups similar error/warning log entries into incident clusters.

Algorithm:
  1. Filter to actionable levels (WARN, ERROR, FATAL, …).
  2. De-duplicate by template_hash to get a compact set of unique templates.
  3. TF-IDF-encode those templates, L2-normalise, compute cosine distance matrix.
  4. Run DBSCAN with the precomputed distance matrix.
  5. If DBSCAN produces all-noise (no clusters found) and there are ≥ 3
     distinct templates, fall back to AgglomerativeClustering.
  6. Map cluster labels back to every original row:
       - DBSCAN/Agglomerative label ≥ 0  → "cluster-NNNN"
       - Noise (label == -1)              → "noise-<template_hash>"
         (preserved as individual incidents rather than discarded)
  7. Rows outside the target levels receive None.
"""

import logging

import numpy as np
import pandas as pd
from sklearn.cluster import DBSCAN, AgglomerativeClustering
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_distances
from sklearn.preprocessing import normalize

logger = logging.getLogger(__name__)

_TARGET_LEVELS = {"WARN", "WARNING", "ERROR", "FATAL", "CRITICAL", "SEVERE"}


def _cluster_label_to_id(label: int, template_hash: str) -> str:
    if label >= 0:
        return f"cluster-{label:04d}"
    return f"noise-{template_hash}"


def assign_clusters(
    df: pd.DataFrame,
    *,
    eps: float = 0.25,
    min_samples: int = 2,
    max_tfidf_features: int = 1500,
    fallback_agglomerative: bool = True,
    max_agglomerative_clusters: int = 10,
) -> pd.Series:
    """
    Return a Series (same index as *df*) of cluster_id strings.
    Rows outside *_TARGET_LEVELS* receive ``None``.

    Parameters
    ----------
    df:
        Parsed log DataFrame from ``engine.parser.parse_file``.
        Must contain columns ``level``, ``template``, ``template_hash``.
    eps:
        DBSCAN neighbourhood radius in cosine-distance space (0–2).
    min_samples:
        DBSCAN minimum cluster size.
    max_tfidf_features:
        Vocabulary cap for the TF-IDF vectoriser.
    fallback_agglomerative:
        When DBSCAN finds no clusters, try AgglomerativeClustering.
    max_agglomerative_clusters:
        Upper bound on the number of Agglomerative clusters.
    """
    result = pd.Series(None, index=df.index, dtype=object)

    target_mask = df["level"].str.upper().isin(_TARGET_LEVELS)
    target_df = df[target_mask].copy()
    if target_df.empty:
        return result

    # ── Unique templates ──────────────────────────────────────────────────────
    unique_tmpl = target_df.drop_duplicates(subset="template_hash").copy()
    n_unique = len(unique_tmpl)
    logger.info("Clustering %d unique error templates (%d total entries)",
                n_unique, len(target_df))

    # ── TF-IDF vectorisation ──────────────────────────────────────────────────
    vec = TfidfVectorizer(
        max_features=max_tfidf_features,
        ngram_range=(1, 2),
        sublinear_tf=True,
    )
    try:
        X = vec.fit_transform(unique_tmpl["template"].fillna(""))
    except ValueError:
        # Empty vocabulary (e.g. all templates are identical after masking)
        for idx, row in target_df.iterrows():
            result[idx] = f"noise-{row['template_hash']}"
        return result

    X = normalize(X, norm="l2")
    dist = cosine_distances(X)
    # Clip floating-point noise to valid range [0, 1]
    dist = np.clip(dist, 0.0, 1.0)

    # ── DBSCAN ───────────────────────────────────────────────────────────────
    effective_min_samples = min(min_samples, n_unique)
    labels = DBSCAN(
        eps=eps,
        min_samples=effective_min_samples,
        metric="precomputed",
        n_jobs=-1,
    ).fit_predict(dist)

    n_clusters = len(set(labels) - {-1})
    n_noise = int((labels == -1).sum())
    logger.info("DBSCAN → %d clusters, %d noise points", n_clusters, n_noise)

    # ── Agglomerative fallback ────────────────────────────────────────────────
    if n_clusters == 0 and fallback_agglomerative and n_unique >= 3:
        n_wanted = max(2, min(n_unique // 2, max_agglomerative_clusters))
        logger.info(
            "DBSCAN all-noise; falling back to AgglomerativeClustering(n=%d)", n_wanted
        )
        labels = AgglomerativeClustering(
            n_clusters=n_wanted,
            metric="precomputed",
            linkage="average",
        ).fit_predict(dist)
        n_clusters = len(set(labels))
        logger.info("AgglomerativeClustering → %d clusters", n_clusters)

    # ── Map labels back to original rows ──────────────────────────────────────
    hash_to_cluster_id: dict[str, str] = {}
    for i, row in enumerate(unique_tmpl.itertuples(index=False)):
        hash_to_cluster_id[row.template_hash] = _cluster_label_to_id(
            int(labels[i]), row.template_hash
        )

    for idx, row in target_df.iterrows():
        result[idx] = hash_to_cluster_id.get(
            row["template_hash"], f"noise-{row['template_hash']}"
        )

    logger.info(
        "Cluster assignment complete: %d distinct cluster IDs assigned",
        result.nunique(),
    )
    return result
