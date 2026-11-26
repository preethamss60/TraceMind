"""
engine/anomaly_detector.py

Anomaly detection for parsed log DataFrames.

Strategy:
  1. Build a combined feature matrix:
       a. TF-IDF of masked message templates        (content signal)
       b. Per-minute error-rate + event-rate pair   (time-series signal)
  2. Train an Isolation Forest on the "normal" baseline rows
     (INFO / DEBUG / TRACE) so that unusual spikes or error bursts score low.
  3. Unconditionally flag FATAL / CRITICAL entries as anomalies regardless
     of the model score.

Models (vectorizer + scaler + isolation forest) are persisted with joblib
so that repeated calls on the same host skip retraining.
"""

import logging
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix, hstack
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import MaxAbsScaler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths for persisted model artefacts
# ---------------------------------------------------------------------------
_MODEL_DIR = Path(__file__).parent / "models"
_VEC_PATH = _MODEL_DIR / "tfidf_vectorizer.pkl"
_SCALER_PATH = _MODEL_DIR / "freq_scaler.pkl"
_IF_PATH = _MODEL_DIR / "isolation_forest.pkl"

# ---------------------------------------------------------------------------
# Level buckets
# ---------------------------------------------------------------------------
_NORMAL_LEVELS = {"INFO", "DEBUG", "TRACE", "VERBOSE"}
_HARD_ANOMALY_LEVELS = {"FATAL", "CRITICAL"}


# ---------------------------------------------------------------------------
# Feature builders
# ---------------------------------------------------------------------------

def _time_features(df: pd.DataFrame) -> np.ndarray:
    """
    Return an (n_rows x 2) float32 array:
      col 0 - total events in the entry's 1-minute window
      col 1 - error-event rate in that same window

    Rows with no timestamp receive zeros.
    """
    n = len(df)
    out = np.zeros((n, 2), dtype=np.float32)

    timed = df.dropna(subset=["timestamp"])
    if timed.empty:
        return out

    idx_sorted = timed.set_index("timestamp").sort_index()
    total_per_min: pd.Series = idx_sorted.resample("1min")["level"].count()
    error_per_min: pd.Series = idx_sorted.resample("1min")["level"].apply(
        lambda s: s.str.upper().isin({"ERROR", "FATAL", "CRITICAL"}).sum()
    )
    rate_per_min: pd.Series = (
        error_per_min / total_per_min.clip(lower=1)
    ).fillna(0.0)

    for i, (_, row) in enumerate(df.iterrows()):
        if pd.isna(row["timestamp"]):
            continue
        minute = row["timestamp"].floor("1min")
        out[i, 0] = float(total_per_min.get(minute, 1))
        out[i, 1] = float(rate_per_min.get(minute, 0.0))

    return out


def _build_X(df: pd.DataFrame, vectorizer: TfidfVectorizer, scaler: MaxAbsScaler):
    """Return a sparse combined feature matrix ready for Isolation Forest."""
    tfidf = vectorizer.transform(df["template"].fillna(""))
    time_arr = _time_features(df)
    time_scaled = scaler.transform(time_arr)
    return hstack([tfidf, csr_matrix(time_scaled)])


# ---------------------------------------------------------------------------
# Model load / train helpers
# ---------------------------------------------------------------------------

def _get_vectorizer(templates: pd.Series) -> TfidfVectorizer:
    if _VEC_PATH.exists():
        logger.debug("Loading TF-IDF vectorizer from %s", _VEC_PATH)
        return joblib.load(_VEC_PATH)
    vec = TfidfVectorizer(
        max_features=2000,
        ngram_range=(1, 2),
        sublinear_tf=True,
        min_df=1,
    )
    vec.fit(templates.fillna(""))
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(vec, _VEC_PATH)
    logger.info("Saved TF-IDF vectorizer -> %s", _VEC_PATH)
    return vec


def _get_scaler(time_arr: np.ndarray) -> MaxAbsScaler:
    if _SCALER_PATH.exists():
        logger.debug("Loading frequency scaler from %s", _SCALER_PATH)
        return joblib.load(_SCALER_PATH)
    scaler = MaxAbsScaler()
    scaler.fit(time_arr)
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(scaler, _SCALER_PATH)
    logger.info("Saved frequency scaler -> %s", _SCALER_PATH)
    return scaler


def _get_model(X, contamination: float) -> IsolationForest:
    if _IF_PATH.exists():
        logger.debug("Loading Isolation Forest from %s", _IF_PATH)
        return joblib.load(_IF_PATH)
    model = IsolationForest(
        n_estimators=200,
        contamination=contamination,
        random_state=42,
        n_jobs=-1,
    )
    model.fit(X)
    _MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, _IF_PATH)
    logger.info("Saved Isolation Forest -> %s", _IF_PATH)
    return model


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect(df: pd.DataFrame, force_retrain: bool = False) -> pd.Series:
    """
    Return a boolean Series (same index as *df*) where True = anomaly.

    Parameters
    ----------
    df:
        Output of ``engine.parser.parse_file``.  Must have columns
        ``template``, ``level``, ``timestamp``.
    force_retrain:
        Delete cached models and retrain from scratch.
    """
    if df.empty:
        return pd.Series(False, index=df.index, dtype=bool)

    if force_retrain:
        for p in (_VEC_PATH, _SCALER_PATH, _IF_PATH):
            p.unlink(missing_ok=True)
        logger.info("Model cache cleared; retraining from scratch.")

    # Estimate contamination from observed error rate (clamp 1%-49%)
    error_rate = df["level"].str.upper().isin({"ERROR", "FATAL", "CRITICAL"}).mean()
    contamination = float(np.clip(error_rate + 0.05, 0.01, 0.49))
    logger.debug("Contamination estimate: %.3f", contamination)

    # Train on the "normal" subset; fall back to full data if too few rows
    normal_mask = df["level"].str.upper().isin(_NORMAL_LEVELS)
    train_df = df[normal_mask] if normal_mask.sum() >= 5 else df

    # 1. Vectorizer (fit on all templates so the vocabulary is complete)
    vectorizer = _get_vectorizer(df["template"])

    # 2. Scaler (fit on training time features)
    time_arr_train = _time_features(train_df)
    scaler = _get_scaler(time_arr_train)

    # 3. Build training matrix & fit model
    tfidf_train = vectorizer.transform(train_df["template"].fillna(""))
    time_scaled_train = scaler.transform(time_arr_train)
    X_train = hstack([tfidf_train, csr_matrix(time_scaled_train)])
    model = _get_model(X_train, contamination)

    # 4. Score every entry
    X_all = _build_X(df, vectorizer, scaler)
    preds = model.predict(X_all)       # +1 = normal, -1 = anomaly
    is_anomaly = pd.Series(preds == -1, index=df.index, dtype=bool)

    # 5. Hard-flag FATAL / CRITICAL unconditionally
    is_anomaly |= df["level"].str.upper().isin(_HARD_ANOMALY_LEVELS)

    logger.info(
        "Anomaly detection done: %d / %d entries flagged",
        is_anomaly.sum(), len(df),
    )
    return is_anomaly
