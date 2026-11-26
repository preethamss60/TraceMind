from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import func
from werkzeug.utils import secure_filename

from ..models import RCAReport, LogEntry, Upload, db

api_bp = Blueprint("api", __name__)
ALLOWED_EXTENSIONS = {"log", "txt"}


# ---------------------------------------------------------------------------
# Engine integration helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts_str: Optional[str]) -> Optional[datetime]:
    """Convert an ISO-8601 string from the engine to a tz-aware datetime."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _persist_analysis(upload_id: int, result: dict) -> None:
    """
    Persist engine output (entries + reports) in a single transaction.
    Invalid or missing fields are silently coerced so a partial result
    never blocks the commit.
    """
    for entry in result.get("entries") or []:
        db.session.add(LogEntry(
            upload_id=upload_id,
            timestamp=_parse_iso(entry.get("timestamp")),
            level=str(entry.get("level") or "UNKNOWN")[:16],
            message=str(entry.get("message") or ""),
            is_anomaly=bool(entry.get("is_anomaly", False)),
            cluster_id=(str(entry["cluster_id"])[:100] if entry.get("cluster_id") else None),
        ))

    for report in result.get("reports") or []:
        cluster_id = str(report.get("cluster_id") or "")[:100]
        if not cluster_id:
            continue
        score = report.get("severity_score")
        try:
            score = float(score) if score is not None else None
        except (TypeError, ValueError):
            score = None
        db.session.add(RCAReport(
            upload_id=upload_id,
            cluster_id=cluster_id,
            error_summary=str(report.get("error_summary") or "") or None,
            root_cause=str(report.get("root_cause") or "") or None,
            suggested_fix=str(report.get("suggested_fix") or "") or None,
            severity_score=score,
        ))


def _json_error(message, status):
    return jsonify(success=False, error=message), status


def _allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _upload_or_404(upload_id):
    return db.session.get(Upload, upload_id)


@api_bp.post("/upload")
def upload_file():
    if "file" not in request.files:
        return _json_error("Missing file field.", 400)
    file = request.files["file"]
    if not file.filename:
        return _json_error("Select a log file to upload.", 400)
    if not _allowed_file(file.filename):
        return _json_error("Only .log and .txt files are supported.", 415)

    original_name = secure_filename(file.filename)
    if not original_name:
        return _json_error("Invalid filename.", 400)
    stored_name = f"{uuid4().hex}_{original_name}"
    destination = Path(current_app.config["UPLOAD_FOLDER"]) / stored_name
    try:
        file.save(destination)
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            return _json_error("Empty files cannot be analyzed.", 400)
        with destination.open("rb") as saved_file:
            total_lines = sum(1 for _ in saved_file)
        record = Upload(filename=original_name, stored_filename=stored_name, total_lines=total_lines, status="uploaded")
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        destination.unlink(missing_ok=True)
        current_app.logger.exception("Upload failed")
        return _json_error("Could not store the uploaded file.", 500)
    # ── Run the analysis engine (best-effort; failures keep status "uploaded") ──
    try:
        from engine.pipeline import run_analysis  # noqa: PLC0415
        result = run_analysis(destination)
        _persist_analysis(record.id, result)
        record.status = "analyzed"
        db.session.commit()
        current_app.logger.info("Engine analysis complete for upload %d", record.id)
    except ImportError:
        current_app.logger.info(
            "Engine dependencies not installed; skipping analysis for upload %d. "
            "Install requirements-engine.txt to enable it.",
            record.id,
        )
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "Engine analysis failed for upload %d; upload record is preserved.", record.id
        )

    return jsonify(success=True, upload_id=record.id, filename=record.filename, status=record.status), 201


@api_bp.get("/dashboard/<int:upload_id>")
def dashboard_data(upload_id):
    upload = _upload_or_404(upload_id)
    if not upload:
        return _json_error("Upload not found.", 404)
    entries = LogEntry.query.filter_by(upload_id=upload_id)
    counts = {level: entries.filter(func.upper(LogEntry.level) == level).count() for level in ("INFO", "WARN", "ERROR", "FATAL")}
    # WARNING is accepted from engines but presented as WARN consistently.
    counts["WARN"] += entries.filter(func.upper(LogEntry.level) == "WARNING").count()
    timeline_rows = (db.session.query(LogEntry.timestamp, func.count(LogEntry.id))
        .filter(LogEntry.upload_id == upload_id, func.upper(LogEntry.level).in_(["ERROR", "FATAL"]), LogEntry.timestamp.isnot(None))
        .group_by(LogEntry.timestamp).order_by(LogEntry.timestamp).all())
    timeline = [{"timestamp": stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else stamp.isoformat(), "count": count} for stamp, count in timeline_rows]
    return jsonify(success=True, upload={"id": upload.id, "filename": upload.filename, "upload_time": upload.upload_time.isoformat(), "total_lines": upload.total_lines, "status": upload.status}, statistics={"total_entries": entries.count(), "info": counts["INFO"], "warn": counts["WARN"], "error": counts["ERROR"], "fatal": counts["FATAL"], "anomalies": entries.filter_by(is_anomaly=True).count()}, error_timeline=timeline)


@api_bp.get("/clusters/<int:upload_id>")
def clusters(upload_id):
    upload = _upload_or_404(upload_id)
    if not upload:
        return _json_error("Upload not found.", 404)
    cluster_counts = dict(db.session.query(LogEntry.cluster_id, func.count(LogEntry.id)).filter(LogEntry.upload_id == upload_id, LogEntry.cluster_id.isnot(None)).group_by(LogEntry.cluster_id).all())
    reports = RCAReport.query.filter_by(upload_id=upload_id).all()
    report_map = {report.cluster_id: report for report in reports}
    identifiers = sorted(set(cluster_counts) | set(report_map))
    result = []
    for cluster_id in identifiers:
        report = report_map.get(cluster_id)
        result.append({"cluster_id": cluster_id, "error_summary": report.error_summary if report else None, "occurrences": cluster_counts.get(cluster_id, 0), "severity_score": report.severity_score if report else None, "root_cause": report.root_cause if report else None, "suggested_fix": report.suggested_fix if report else None, "analysis_available": report is not None})
    return jsonify(success=True, clusters=result)


@api_bp.get("/clusters/<int:upload_id>/<cluster_id>")
def cluster_detail(upload_id, cluster_id):
    if not _upload_or_404(upload_id):
        return _json_error("Upload not found.", 404)
    report = RCAReport.query.filter_by(upload_id=upload_id, cluster_id=cluster_id).first()
    evidence = LogEntry.query.filter_by(upload_id=upload_id, cluster_id=cluster_id).order_by(LogEntry.id).limit(50).all()
    if not report and not evidence:
        return _json_error("Cluster not found.", 404)
    return jsonify(success=True, cluster_id=cluster_id, error_summary=report.error_summary if report else None, root_cause=report.root_cause if report else None, suggested_fix=report.suggested_fix if report else None, severity_score=report.severity_score if report else None, entries=[{"timestamp": e.timestamp.isoformat() if e.timestamp else None, "level": e.level, "message": e.message, "is_anomaly": e.is_anomaly} for e in evidence])
