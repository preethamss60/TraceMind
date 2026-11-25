"""TraceMind application factory."""
from pathlib import Path
import os

from flask import Flask, jsonify

from .models import db


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    instance_path = Path(app.instance_path)
    upload_path = Path(app.root_path).parent / "uploads"
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("TRACEMIND_SECRET_KEY", "change-this-for-production"),
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{instance_path / 'tracemind.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        UPLOAD_FOLDER=str(upload_path),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    db.init_app(app)

    from .routes.api import api_bp
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.get("/")
    def index():
        from flask import render_template
        return render_template("index.html")

    @app.get("/dashboard/<int:upload_id>")
    def dashboard(upload_id):
        from flask import render_template
        return render_template("dashboard.html", upload_id=upload_id)

    @app.errorhandler(413)
    def request_too_large(_error):
        return jsonify(success=False, error="File is too large. Maximum size is 10 MB."), 413

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify(success=False, error="Resource not found."), 404

    with app.app_context():
        db.create_all()
    return app
