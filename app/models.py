from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def utcnow():
    return datetime.now(timezone.utc)


class Upload(db.Model):
    __tablename__ = "uploads"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(300), nullable=False, unique=True)
    upload_time = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)
    total_lines = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(32), nullable=False, default="uploaded", index=True)
    log_entries = db.relationship("LogEntry", back_populates="upload", cascade="all, delete-orphan", lazy="dynamic")
    rca_reports = db.relationship("RCAReport", back_populates="upload", cascade="all, delete-orphan", lazy="dynamic")


class LogEntry(db.Model):
    __tablename__ = "log_entries"
    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=False, index=True)
    timestamp = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    level = db.Column(db.String(16), nullable=True, index=True)
    message = db.Column(db.Text, nullable=False)
    is_anomaly = db.Column(db.Boolean, nullable=False, default=False, index=True)
    cluster_id = db.Column(db.String(100), nullable=True, index=True)
    upload = db.relationship("Upload", back_populates="log_entries")


class RCAReport(db.Model):
    __tablename__ = "rca_reports"
    __table_args__ = (db.UniqueConstraint("upload_id", "cluster_id", name="uq_report_upload_cluster"),)
    id = db.Column(db.Integer, primary_key=True)
    upload_id = db.Column(db.Integer, db.ForeignKey("uploads.id"), nullable=False, index=True)
    cluster_id = db.Column(db.String(100), nullable=False, index=True)
    error_summary = db.Column(db.Text, nullable=True)
    root_cause = db.Column(db.Text, nullable=True)
    suggested_fix = db.Column(db.Text, nullable=True)
    severity_score = db.Column(db.Float, nullable=True, index=True)
    upload = db.relationship("Upload", back_populates="rca_reports")
