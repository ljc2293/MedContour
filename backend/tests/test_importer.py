import csv
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.importer import identity_review_required, import_dataset
from app.models import ImagingCase, Project, ProjectCase


class DatasetImporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data_root = root / "data"
        self.manifests_root = root / "manifests"
        self.annotations_root = root / "annotations"
        self.data_root.mkdir()
        self.manifests_root.mkdir()
        self.annotations_root.mkdir()
        self.settings = Settings(
            database_url="sqlite://",
            data_root=self.data_root,
            manifests_root=self.manifests_root,
            annotation_root=self.annotations_root,
        )
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        with self.Session() as db:
            db.add(Project(name="Dixon 四序列器官分割", description="test"))
            db.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def write_csv(self, name, fieldnames, rows):
        with (self.manifests_root / name).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    def prepare_manifest(self):
        self.write_csv(
            "patient_name_zh_patient_id_mapping.csv",
            ["PatientID", "中文姓名", "显示姓名", "映射状态", "需人工复核"],
            [{"PatientID": "P001", "中文姓名": "测试患者", "显示姓名": "测试患者", "映射状态": "唯一", "需人工复核": "否"}],
        )
        rows = []
        for role in ("opp", "in", "f", "w"):
            relative = f"P001/20260101/abd/{role}.nii.gz"
            path = self.data_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"test")
            rows.append({"patient_id": "P001", "normalized_family": "abd", "target_role": role, "nifti_relative_path": relative, "status": "converted"})
        self.write_csv(
            "conversion_manifest.csv",
            ["patient_id", "normalized_family", "target_role", "nifti_relative_path", "status"],
            rows,
        )
        self.write_csv(
            "source_quartet_manifest.csv",
            ["output_acquisition_directory", "study_dates", "normalized_family"],
            [{"output_acquisition_directory": "P001/20260101/abd", "study_dates": "20260101", "normalized_family": "abd"}],
        )

    def test_import_is_idempotent_and_creates_project_task(self):
        self.prepare_manifest()
        with self.Session() as db:
            first = import_dataset(db, self.settings)
            second = import_dataset(db, self.settings)
            self.assertEqual(first["created"], 1)
            self.assertEqual(second["updated"], 1)
            self.assertEqual(db.scalar(select(func.count(ImagingCase.id))), 1)
            self.assertEqual(db.scalar(select(func.count(ProjectCase.id))), 1)
            case = db.scalar(select(ImagingCase))
            self.assertEqual(case.patient_name_zh, "测试患者")
            self.assertEqual(set(case.sequence_paths), {"opp", "in", "f", "w"})

    def test_incomplete_quartet_is_skipped(self):
        self.prepare_manifest()
        (self.data_root / "P001/20260101/abd/w.nii.gz").unlink()
        with self.Session() as db:
            result = import_dataset(db, self.settings)
            self.assertEqual(result["created"], 0)
            self.assertEqual(result["unavailable"], 1)

    def test_repeat_studies_with_unique_identity_do_not_require_review(self):
        identity = {
            "需人工复核": "是",
            "复核原因": "原始病例目录数=2",
            "映射状态": "唯一匹配",
            "源身份状态": "ok",
        }
        self.assertFalse(identity_review_required(identity))

    def test_true_identity_conflict_still_requires_review(self):
        identity = {
            "需人工复核": "是",
            "复核原因": "源身份状态=identity_conflict；原始病例目录数=2",
            "映射状态": "唯一匹配",
            "源身份状态": "identity_conflict",
        }
        self.assertTrue(identity_review_required(identity))

    def test_ambiguous_chinese_name_still_requires_review(self):
        identity = {
            "需人工复核": "是",
            "复核原因": "多中文姓名候选",
            "映射状态": "多中文姓名候选",
            "源身份状态": "ok",
        }
        self.assertTrue(identity_review_required(identity))


if __name__ == "__main__":
    unittest.main()
