# TraceMind

TraceMind is a log-investigation interface for application logs. Person 2's application layer securely stores uploads, exposes dashboard data from SQLite, and provides a responsive Flask/Chart.js interface. Person 1 owns parsing, anomaly detection, clustering, and AI root-cause analysis.

## Features

- Secure `.log` / `.txt` upload with size, extension, empty-file, and filename validation.
- SQLite models for uploads, parsed log entries, and root-cause reports.
- Data-backed JSON dashboard and cluster APIs with no fabricated analysis output.
- Responsive dark upload and investigation dashboard, including Chart.js timeline/severity charts and raw-evidence detail panel.
- A documented, deliberately decoupled analysis-engine contract.

## Setup and run

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Python 3.10+ is required. If the Windows Python launcher is unavailable, use the path to your installed interpreter instead (for example, `C:\Python312\python.exe -m pip install -r requirements.txt`).

Open `http://127.0.0.1:5000`. In production, set a non-default `TRACEMIND_SECRET_KEY` environment variable and run behind a production WSGI server.

## Structure

```
app/                 Flask factory, models, API routes, templates and static UI
engine/              reserved for Person 1; contains the integration contract
instance/            generated SQLite database (ignored by Git)
uploads/             saved uploads (ignored by Git)
tests/               backend/API integration tests
run.py               development entry point
```

## API

| Endpoint | Description |
| --- | --- |
| `POST /api/upload` | Multipart upload; field is `file`. Returns `upload_id`. |
| `GET /api/dashboard/<upload_id>` | Upload metadata, database-derived counts, and timestamped error distribution. |
| `GET /api/clusters/<upload_id>` | Clusters and associated RCA data, if produced by the engine. |
| `GET /api/clusters/<upload_id>/<cluster_id>` | RCA values and up to 50 stored raw log evidence entries for the detail panel. |

`POST /api/upload` accepts `.log` and `.txt` files up to 10 MB. It saves the original filename only for display; a UUID-prefixed secure filename is used on disk. APIs do not disclose server paths.

## Database

`Upload` records each file and processing state. `LogEntry` is related to an upload and stores normalized log level, optional timestamp, anomaly flag, and optional cluster id. `RCAReport` is unique per `(upload_id, cluster_id)` and holds only results delivered by the analysis engine. Indexed foreign keys and query fields support dashboard reads.

## Engine handoff

No analysis is faked. Until the real engine is connected, uploads remain `uploaded`, and the dashboard accurately shows zero parsed results. The exact schema contract and persistence expectations are in [engine/README.md](engine/README.md). The currently present `engine/pipeline.py` is only a zero-result stub and its parser/detector files are empty, so it is intentionally not invoked. Once Person 1 supplies a non-stub adapter conforming to that contract, validate its output, store `LogEntry` and `RCAReport` rows in a single transaction, then set the upload status to `analyzed`.

Before sending log content to any external LLM, the integration adapter must redact secrets, tokens, credentials, and personally identifiable values. Keep provider credentials in environment variables only.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The suite covers app/database startup, allowed uploads, empty/invalid/oversized files, duplicate original names, file storage, restart persistence, dashboard metrics, cluster and detail evidence APIs, missing IDs, empty cluster data, and rendered frontend/API wiring. It uses the Flask test client; perform a visual browser review after changing UI styles or third-party CDN settings.

## Team responsibility

Person 1 delivers the engine and agreed output data. Person 2 delivers the Flask app, database, storage, REST APIs, UI, charts, secure integration boundary, documentation, and tests.
