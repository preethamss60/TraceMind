# TraceMind engine integration contract

The Flask upload route deliberately does not call an engine until Person 1 provides one.

The eventual adapter should accept a saved log file or iterable of lines and return only validated data in this shape:

```json
{
  "entries": [{"timestamp": "ISO-8601 or null", "level": "INFO|WARN|WARNING|ERROR|FATAL", "message": "...", "is_anomaly": false, "cluster_id": "optional"}],
  "reports": [{"cluster_id": "...", "error_summary": "...", "root_cause": "...", "suggested_fix": "...", "severity_score": 0.0}]
}
```

The backend will persist the returned entries/reports in one transaction and set the upload status to `analyzed`. No mock analysis is executed in the production upload flow, so uploaded files correctly remain `uploaded` until a real engine is integrated.
