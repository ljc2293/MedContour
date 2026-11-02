import json
import math
import tempfile
import unittest
from pathlib import Path

import nibabel as nib
import numpy as np
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.importer import PDFF_PROJECT_NAME, SEGMENTATION_PROJECT_NAME, sync_pdff_project_cases
from app.main import PdffPlaneBody, PdffRoiBody, calculate_pdff_plane_measurements, calculate_pdff_roi_metrics
from app.models import CaseStatus, ImagingCase, Project, ProjectCase, User, UserRole


class PdffMetricTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        affine = np.diag([1.0, 1.0, 3.0, 1.0])
        fat = np.full((96, 48, 6), 20.0, dtype=np.float32)
        water = np.full((96, 48, 6), 80.0, dtype=np.float32)
        in_phase = np.full((96, 48, 6), 100.0, dtype=np.float32)
        opposed_phase = np.full((96, 48, 6), 70.0, dtype=np.float32)
        self.fat_path = self.root / "fat.nii.gz"
        self.water_path = self.root / "water.nii.gz"
        self.in_phase_path = self.root / "in.nii.gz"
        self.opposed_phase_path = self.root / "opp.nii.gz"
        nib.save(nib.Nifti1Image(fat, affine), self.fat_path)
        nib.save(nib.Nifti1Image(water, affine), self.water_path)
        nib.save(nib.Nifti1Image(in_phase, affine), self.in_phase_path)
        nib.save(nib.Nifti1Image(opposed_phase, affine), self.opposed_phase_path)

    def tearDown(self):
        self.temp.cleanup()

    def rois(self):
        return self.rois_on_slice(2)

    def rois_on_slice(self, slice_index):
        return [
            PdffRoiBody(region="head", center_voxel=(15, 20, slice_index), radius_mm=10),
            PdffRoiBody(region="body", center_voxel=(45, 20, slice_index), radius_mm=10),
            PdffRoiBody(region="tail", center_voxel=(75, 20, slice_index), radius_mm=10),
        ]

    def test_calculates_paired_three_point_pdff(self):
        normalized, metrics = calculate_pdff_roi_metrics(
            self.fat_path,
            self.water_path,
            self.in_phase_path,
            self.opposed_phase_path,
            self.rois(),
        )
        self.assertEqual([item["region"] for item in normalized], ["head", "body", "tail"])
        self.assertEqual(len(metrics), 3)
        for metric in metrics:
            self.assertAlmostEqual(metric["fat"]["mean"], 20.0)
            self.assertAlmostEqual(metric["water"]["mean"], 80.0)
            self.assertAlmostEqual(metric["in_phase"]["mean"], 100.0)
            self.assertAlmostEqual(metric["opposed_phase"]["mean"], 70.0)
            self.assertAlmostEqual(metric["pdff"]["mean"], 20.0)
            self.assertAlmostEqual(metric["pdff_ratio_of_means"], 20.0)
            self.assertAlmostEqual(metric["pdff_in_opposed"]["mean"], 15.0)
            self.assertAlmostEqual(metric["pdff_in_opposed_ratio_of_means"], 15.0)
            self.assertEqual(metric["voxel_count"], metric["pdff_voxel_count"])
            self.assertEqual(metric["voxel_count"], metric["pdff_in_opposed_voxel_count"])

    def test_calculates_partial_roi_preview(self):
        normalized, metrics = calculate_pdff_roi_metrics(
            self.fat_path,
            self.water_path,
            self.in_phase_path,
            self.opposed_phase_path,
            self.rois()[:1],
            require_complete=False,
        )
        self.assertEqual([item["region"] for item in normalized], ["head"])
        self.assertEqual(len(metrics), 1)
        self.assertAlmostEqual(metrics[0]["pdff_ratio_of_means"], 20.0)
        self.assertAlmostEqual(metrics[0]["pdff_in_opposed_ratio_of_means"], 15.0)

    def test_in_opposed_signal_fraction_uses_magnitude_difference(self):
        brighter_opposed_path = self.root / "brighter-opp.nii.gz"
        nib.save(
            nib.Nifti1Image(np.full((96, 48, 6), 130.0, dtype=np.float32), np.diag([1.0, 1.0, 3.0, 1.0])),
            brighter_opposed_path,
        )
        _, metrics = calculate_pdff_roi_metrics(
            self.fat_path,
            self.water_path,
            self.in_phase_path,
            brighter_opposed_path,
            self.rois()[:1],
            require_complete=False,
        )
        self.assertAlmostEqual(metrics[0]["pdff_in_opposed_ratio_of_means"], 15.0)
        self.assertAlmostEqual(metrics[0]["pdff_in_opposed"]["mean"], 15.0)

    def test_enforces_minimum_area_of_half_a_square_centimeter(self):
        minimum_radius = math.sqrt(50.0 / math.pi)
        PdffRoiBody(region="head", center_voxel=(15, 20, 2), radius_mm=minimum_radius)
        PdffRoiBody(region="head", center_voxel=(15, 20, 2), radius_mm=4.0)
        PdffRoiBody(region="head", center_voxel=(15, 20, 2), radius_mm=25)
        self.assertGreaterEqual(math.pi * minimum_radius**2 / 100.0, 0.5)
        for invalid_radius in (3.9, math.nextafter(minimum_radius, -math.inf)):
            with self.assertRaises(ValueError):
                PdffRoiBody(region="head", center_voxel=(15, 20, 2), radius_mm=invalid_radius)

    def test_rejects_rois_on_different_slices(self):
        rois = self.rois()
        rois[-1] = PdffRoiBody(region="tail", center_voxel=(75, 20, 3), radius_mm=10)
        with self.assertRaises(HTTPException) as raised:
            calculate_pdff_roi_metrics(
                self.fat_path,
                self.water_path,
                self.in_phase_path,
                self.opposed_phase_path,
                rois,
            )
        self.assertEqual(raised.exception.detail, "胰头、胰体、胰尾三个 ROI 必须位于同一轴位层面")

    def test_rejects_unregistered_fourth_phase(self):
        shifted_path = self.root / "shifted-opp.nii.gz"
        shifted_affine = np.diag([1.0, 1.0, 3.0, 1.0])
        shifted_affine[0, 3] = 2.0
        nib.save(nib.Nifti1Image(np.full((96, 48, 6), 70.0, dtype=np.float32), shifted_affine), shifted_path)
        with self.assertRaises(HTTPException) as raised:
            calculate_pdff_roi_metrics(
                self.fat_path,
                self.water_path,
                self.in_phase_path,
                shifted_path,
                self.rois(),
            )
        self.assertIn("反相位与脂相空间坐标不一致", raised.exception.detail)

    def test_aggregates_three_distinct_measurement_planes(self):
        planes, summary = calculate_pdff_plane_measurements(
            self.fat_path,
            self.water_path,
            self.in_phase_path,
            self.opposed_phase_path,
            [
                PdffPlaneBody(plane_index=1, rois=self.rois_on_slice(1)),
                PdffPlaneBody(plane_index=2, rois=self.rois_on_slice(2)),
                PdffPlaneBody(plane_index=3, rois=self.rois_on_slice(3)),
            ],
        )
        self.assertEqual([plane["slice_index"] for plane in planes], [1, 2, 3])
        self.assertEqual(summary["plane_count"], 3)
        self.assertEqual(summary["roi_count"], 9)
        self.assertAlmostEqual(summary["fat_water"]["mean"], 20.0)
        self.assertAlmostEqual(summary["fat_water"]["sd"], 0.0)
        self.assertAlmostEqual(summary["fat_water"]["cv_percent"], 0.0)
        self.assertAlmostEqual(summary["in_opposed"]["mean"], 15.0)
        self.assertGreater(summary["total_area_mm2"], 900.0)
        self.assertGreater(summary["total_voxel_count"], 900)

    def test_saves_partial_multi_plane_progress(self):
        planes, summary = calculate_pdff_plane_measurements(
            self.fat_path,
            self.water_path,
            self.in_phase_path,
            self.opposed_phase_path,
            [PdffPlaneBody(plane_index=2, rois=self.rois_on_slice(2))],
        )
        self.assertEqual(len(planes), 1)
        self.assertEqual(summary["plane_count"], 1)
        self.assertEqual(summary["fat_water"]["count"], 1)
        self.assertEqual(summary["fat_water"]["sd"], 0.0)

    def test_rejects_reusing_the_same_slice_for_two_planes(self):
        with self.assertRaises(HTTPException) as raised:
            calculate_pdff_plane_measurements(
                self.fat_path,
                self.water_path,
                self.in_phase_path,
                self.opposed_phase_path,
                [
                    PdffPlaneBody(plane_index=1, rois=self.rois_on_slice(2)),
                    PdffPlaneBody(plane_index=2, rois=self.rois_on_slice(2)),
                ],
            )
        self.assertEqual(raised.exception.detail, "不同测量平面必须位于不同的轴位层面")


class PdffPublicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data"
        self.prediction_root = self.root / "predictions"
        self.data_root.mkdir()
        self.prediction_root.mkdir()
        self.settings = Settings(
            data_root=self.data_root,
            pancreas_prediction_root=self.prediction_root,
        )
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def add_prediction(self, patient_id: str):
        affine = np.diag([1.0, 1.0, 3.0, 1.0])
        shape = (16, 16, 4)
        nib.save(nib.Nifti1Image(np.zeros(shape, dtype=np.float32), affine), self.data_root / "w.nii.gz")
        case_root = self.prediction_root / f"case-{patient_id}"
        case_root.mkdir()
        prediction_path = case_root / "pancreas_pred.nii.gz"
        nib.save(nib.Nifti1Image(np.ones(shape, dtype=np.uint8), affine), prediction_path)
        (case_root / "inference_metadata.json").write_text(json.dumps({
            "status": "completed",
            "patient_id": patient_id,
            "native_shape": list(shape),
            "native_affine": affine.tolist(),
            "prediction_filename": prediction_path.name,
            "model_method_id": "test-model",
        }), encoding="utf-8")

    def test_reuses_case_but_not_doctor_assignment_or_status(self):
        with self.Session() as db:
            source = Project(name=SEGMENTATION_PROJECT_NAME, workflow_type="segmentation")
            target = Project(name=PDFF_PROJECT_NAME, workflow_type="pdff_roi")
            doctor = User(
                email="annotator@example.test",
                display_name="原任务医生",
                password_hash="not-used",
                role=UserRole.ANNOTATOR,
                must_change_password=False,
            )
            case = ImagingCase(
                source_key="same-source",
                case_code="DX-TEST",
                patient_id="P001",
                patient_name_zh="测试病例",
                sequence_paths={"f": "f.nii.gz", "w": "w.nii.gz", "in": "in.nii.gz", "opp": "opp.nii.gz"},
            )
            db.add_all([source, target, doctor, case])
            db.flush()
            db.add(ProjectCase(
                project_id=source.id,
                case_id=case.id,
                status=CaseStatus.ANNOTATING,
                annotator_id=doctor.id,
                latest_version=4,
            ))
            db.flush()

            self.add_prediction(case.patient_id)

            self.assertEqual(sync_pdff_project_cases(db, self.settings), 1)
            pdff_task = db.scalar(select(ProjectCase).where(ProjectCase.project_id == target.id))
            self.assertIsNotNone(pdff_task)
            self.assertEqual(pdff_task.case_id, case.id)
            self.assertEqual(pdff_task.status, CaseStatus.UNASSIGNED)
            self.assertIsNone(pdff_task.annotator_id)
            self.assertIsNone(pdff_task.reviewer_id)
            self.assertEqual(pdff_task.latest_version, 0)

    def test_does_not_publish_a_case_without_a_unique_reference(self):
        with self.Session() as db:
            source = Project(name=SEGMENTATION_PROJECT_NAME, workflow_type="segmentation")
            target = Project(name=PDFF_PROJECT_NAME, workflow_type="pdff_roi")
            case = ImagingCase(
                source_key="missing-reference",
                case_code="DX-MISSING",
                patient_id="P002",
                patient_name_zh="无参考病例",
                sequence_paths={"f": "f.nii.gz", "w": "w.nii.gz", "in": "in.nii.gz", "opp": "opp.nii.gz"},
            )
            db.add_all([source, target, case])
            db.flush()
            db.add(ProjectCase(project_id=source.id, case_id=case.id))
            db.flush()

            self.assertEqual(sync_pdff_project_cases(db, self.settings), 0)
            self.assertIsNone(db.scalar(select(ProjectCase).where(ProjectCase.project_id == target.id)))


if __name__ == "__main__":
    unittest.main()
