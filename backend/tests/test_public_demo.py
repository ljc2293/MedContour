"""Verify the published case and exercise its actual HTTP annotation workflow."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import nibabel as nib
import numpy as np
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main as api
from app.config import Settings
from app.database import Base, get_db
from app.importer import import_dataset
from app.models import ImagingCase, Project, ProjectCase
from app.seed_demo_annotation import seed_demo_annotation

DEMO = Path(__file__).resolve().parents[2] / "examples" / "demo"


class PublicDemoTest(unittest.TestCase):
    def test_image_integrity_geometry_and_clean_headers(self):
        report = json.loads((DEMO / "validation.json").read_text())
        reference = nib.load(DEMO / "data/DEMO001/W.nii.gz")
        for relative, expected in report["files"].items():
            path = DEMO / relative
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected["sha256"])
            image = nib.load(path)
            self.assertEqual(image.shape, reference.shape)
            np.testing.assert_allclose(image.affine, reference.affine, atol=1e-4, rtol=0)
            self.assertEqual(len(image.header.extensions), 0)
            for field in ("aux_file", "intent_name", "db_name"):
                self.assertEqual(image.header[field].tobytes().strip(b"\0"), b"")
            self.assertEqual(image.header["descrip"].tobytes().strip(b"\0"), b"DEMO001 de-identified example")
        mask = np.asanyarray(nib.load(DEMO / "data/DEMO001/pancreas_mask.nii.gz").dataobj)
        self.assertEqual(set(np.unique(mask)), {0, 1})
        self.assertEqual(int(mask.sum()), report["mask_voxels"])

    def test_import_login_save_submit_and_independent_review(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            temp = Path(directory)
            settings = Settings(_env_file=None, database_url="sqlite://",
                data_root=DEMO / "data", manifests_root=DEMO / "manifests",
                pancreas_prediction_root=DEMO / "references", annotation_root=temp / "annotations",
                preview_root=temp / "previews", task_file_root=temp / "tasks",
                initial_admin_email="admin@example.com", initial_admin_password="DemoTest-Only-12345")
            engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            Base.metadata.create_all(engine)
            stack.enter_context(patch.object(api, "settings", settings))
            stack.enter_context(patch("app.security.get_settings", return_value=settings))

            def test_db():
                with factory() as db:
                    yield db

            api.app.dependency_overrides[get_db] = test_db
            stack.callback(api.app.dependency_overrides.pop, get_db)
            stack.callback(engine.dispose)
            with factory() as db:
                api.seed_defaults(db)
                first = import_dataset(db, settings)
                self.assertEqual(first["created"], 1)
                self.assertEqual(first["pdff_tasks_created"], 1)
                self.assertEqual(import_dataset(db, settings)["created"], 0)
                self.assertFalse(asyncio.run(seed_demo_annotation(db))["skipped"])
                self.assertTrue(asyncio.run(seed_demo_annotation(db))["skipped"])
                item = db.scalar(select(ProjectCase).join(Project).join(ImagingCase)
                    .where(Project.workflow_type == "segmentation", ImagingCase.patient_id == "DEMO001"))
                case_id = item.id
            client = TestClient(api.app)  # No lifespan: isolated test DB is seeded above.
            reviewer = TestClient(api.app)
            stack.callback(client.close)
            stack.callback(reviewer.close)

            def login_and_change(session, email):
                response = session.post("/api/auth/login", json={"email": email, "password": "DemoTest-Only-12345"})
                self.assertEqual(response.status_code, 200, response.text)
                headers = {"X-CSRF-Token": response.json()["csrf_token"]}
                response = session.post("/api/auth/change-password", headers=headers,
                    json={"current_password": "DemoTest-Only-12345", "new_password": "DemoChanged-Only-12345"})
                self.assertEqual(response.status_code, 200, response.text)
                return headers

            headers = login_and_change(client, "admin@example.com")
            base = f"/api/cases/{case_id}"
            self.assertEqual(client.get(base + "/volume/w.nii.gz").status_code, 200)
            self.assertEqual(client.get(base + "/annotation.nii.gz").status_code, 200)
            mask = (DEMO / "data/DEMO001/pancreas_mask.nii.gz").read_bytes()
            response = client.post(base + "/annotation", headers=headers,
                files={"mask": ("mask.nii.gz", mask, "application/gzip")}, data={"comment": "Demo HTTP test"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["version"], 2)
            response = client.post(base + "/submit", headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "submitted")
            response = client.post(base + "/review", headers=headers, json={"action": "approve", "comment": ""})
            self.assertEqual(response.status_code, 403, response.text)
            response = client.post("/api/admin/users", headers=headers, json={"email": "reviewer@example.com",
                "display_name": "Demo Reviewer", "roles": ["reviewer"], "temporary_password": "DemoTest-Only-12345"})
            self.assertEqual(response.status_code, 201, response.text)
            reviewer_headers = login_and_change(reviewer, "reviewer@example.com")
            response = reviewer.post(base + "/review", headers=reviewer_headers, json={"action": "approve", "comment": "Demo test approval"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["status"], "approved")
            self.assertEqual(response.json()["approved_version"], 2)
