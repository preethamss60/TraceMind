import io
from pathlib import Path
import tempfile
import unittest
from sqlalchemy import inspect

from app import create_app
from app.models import RCAReport, LogEntry, Upload, db


class TraceMindTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": f"sqlite:///{root / 'test.db'}", "UPLOAD_FOLDER": str(root / 'uploads'), "MAX_CONTENT_LENGTH": 1024})
        self.client = self.app.test_client()

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()
            db.engine.dispose()
        self.temp.cleanup()

    def upload(self, content=b"one\ntwo\n", filename="app.log"):
        return self.client.post("/api/upload", data={"file": (io.BytesIO(content), filename)}, content_type="multipart/form-data")

    def test_startup_and_database(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        with self.app.app_context():
            self.assertTrue(inspect(db.engine).has_table("uploads"))
            self.assertTrue(inspect(db.engine).has_table("log_entries"))
            self.assertTrue(inspect(db.engine).has_table("rca_reports"))

    def test_upload_validation_and_duplicate_names(self):
        self.assertEqual(self.upload(b"", "empty.log").status_code, 400)
        self.assertEqual(self.upload(b"x", "bad.pdf").status_code, 415)
        self.assertEqual(self.upload(b"x" * 2048, "big.log").status_code, 413)
        self.assertEqual(self.upload(b"a\n", "same.log").status_code, 201)
        self.assertEqual(self.upload(b"b\n", "same.log").status_code, 201)

    def test_dashboard_clusters_and_missing_upload(self):
        response = self.upload(); upload_id = response.get_json()["upload_id"]
        with self.app.app_context():
            db.session.add_all([LogEntry(upload_id=upload_id, level="ERROR", message="failure", is_anomaly=True, cluster_id="c1"), RCAReport(upload_id=upload_id, cluster_id="c1", error_summary="failure", root_cause="test", suggested_fix="fix", severity_score=8.0)])
            db.session.commit()
        data = self.client.get(f"/api/dashboard/{upload_id}").get_json()
        self.assertEqual(data["statistics"]["error"], 1)
        self.assertEqual(data["statistics"]["anomalies"], 1)
        self.assertEqual(self.client.get(f"/api/clusters/{upload_id}").get_json()["clusters"][0]["cluster_id"], "c1")
        detail = self.client.get(f"/api/clusters/{upload_id}/c1").get_json()
        self.assertEqual(detail["entries"][0]["message"], "failure")
        self.assertEqual(detail["root_cause"], "test")
        self.assertEqual(self.client.get("/api/dashboard/999").status_code, 404)

    def test_empty_cluster_result_and_persistence(self):
        upload_id = self.upload().get_json()["upload_id"]
        self.assertEqual(self.client.get(f"/api/clusters/{upload_id}").get_json()["clusters"], [])
        with self.app.app_context(): self.assertEqual(Upload.query.count(), 1)

    def test_upload_storage_restart_and_frontend_contract(self):
        response = self.upload(b"INFO started\n", "../unsafe name.log")
        self.assertEqual(response.status_code, 201)
        upload_id = response.get_json()["upload_id"]
        with self.app.app_context():
            saved = db.session.get(Upload, upload_id)
            self.assertEqual(saved.filename, "unsafe_name.log")
            self.assertTrue((Path(self.app.config["UPLOAD_FOLDER"]) / saved.stored_filename).is_file())
            database_uri = self.app.config["SQLALCHEMY_DATABASE_URI"]
            upload_folder = self.app.config["UPLOAD_FOLDER"]
            db.session.remove()
            db.engine.dispose()
        restarted = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": database_uri, "UPLOAD_FOLDER": upload_folder})
        with restarted.app_context():
            self.assertEqual(db.session.get(Upload, upload_id).total_lines, 1)
            db.session.remove()
            db.engine.dispose()
        page = self.client.get(f"/dashboard/{upload_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"timeline-chart", page.data)
        dashboard_script = self.client.get("/static/js/dashboard.js")
        self.assertEqual(dashboard_script.status_code, 200)
        self.assertIn(b"/api/dashboard/", dashboard_script.data)
        self.assertIn(b"/api/clusters/", dashboard_script.data)
        dashboard_script.close()


if __name__ == "__main__": unittest.main()
