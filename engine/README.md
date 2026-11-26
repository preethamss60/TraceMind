# TraceMind engine — integration contract & implementation notes

## Entry point

```python
from engine.pipeline import run_analysis

result = run_analysis("/path/to/upload.log")
```

`run_analysis` returns a dict with two keys:

```json
{
  "entries": [
    {
      "timestamp":  "2024-03-15T08:04:00+00:00",
      "level":      "ERROR",
      "message":    "Connection pool exhausted after 5000ms",
      "is_anomaly": true,
      "cluster_id": "cluster-0001"
    }
  ],
  "reports": [
    {
      "cluster_id":    "cluster-0001",
      "error_summary": "Database connection pool exhaustion under load",
      "root_cause":    "Connection pool size too small for peak traffic.",
      "suggested_fix": "Increase pool size; add circuit breaker.",
      "severity_score": 8.5
    }
  ]
}
```

## Pipeline stages

| Stage | Module | Key tech |
|---|---|---|
| 1. Parse | `engine/parser.py` | `re`, `datetime`, `hashlib`, `pandas` |
| 2. Anomaly detect | `engine/anomaly_detector.py` | `TfidfVectorizer`, `IsolationForest`, `joblib` |
| 3. Cluster | `engine/clusterer.py` | `DBSCAN`, `AgglomerativeClustering` |
| 4. RCA | `engine/rca.py` | OpenAI / Gemini / Ollama |
| 5. Serialise | `engine/pipeline.py` | — |

## Supported log formats

- **ISO-8601** timestamps: `2024-03-15 08:00:01`, `2024-03-15T08:00:01.123Z`
- **Syslog**: `Mar 15 08:00:01`
- **Multi-line stack traces**: continuation lines (starting with whitespace,
  `at `, `Caused by:`) are merged into the preceding entry

## Token masking

Dynamic values are replaced with stable placeholders before TF-IDF
vectorisation so that identical error patterns with different IPs/numbers
map to the same cluster template:

| Placeholder | Matches |
|---|---|
| `<REDACTED>` | `password=…`, `token=…`, `api_key=…` |
| `<IP>` | IPv4 addresses (with optional port) |
| `<UUID>` | Standard UUIDs |
| `<URL>` | `http://…` / `https://…` |
| `<EMAIL>` | Email addresses |
| `<HEX>` | `0x…` hex literals |
| `<PATH>` | Unix and Windows file paths |
| `<NUM>` | Bare numbers with optional units (ms, MB, %, …) |

## LLM provider configuration

Set environment variables to enable RCA (first configured provider wins):

```
OPENAI_API_KEY=sk-...        OPENAI_MODEL=gpt-4o-mini   (default)
GEMINI_API_KEY=AIza...       GEMINI_MODEL=gemini-2.0-flash (default)
OLLAMA_HOST=http://...       OLLAMA_MODEL=llama3           (default)
```

No LLM configured → clusters still appear on the dashboard with a static
"Analysis unavailable" notice instead of AI-generated root cause text.

## Model persistence

Trained artefacts are cached under `engine/models/`:

```
engine/models/tfidf_vectorizer.pkl
engine/models/freq_scaler.pkl
engine/models/isolation_forest.pkl
```

Delete these files and re-upload a log to force retraining.

## Flask integration

The Flask upload route (`POST /api/upload`) automatically calls
`run_analysis` after saving the file and persists the returned
`entries` and `reports` in one transaction, then marks the upload
`analyzed`.  If the engine dependencies are not installed the route
logs a notice and leaves the status as `uploaded` — no crash.

Install engine dependencies:
```
pip install -r requirements-engine.txt
```
