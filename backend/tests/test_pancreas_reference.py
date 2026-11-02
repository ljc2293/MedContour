import json
import tempfile
import unittest
from pathlib import Path

import nibabel as nib
import numpy as np

from app.pancreas_reference import (
    _edge_segments,
    pancreas_slice_segments,
    pancreas_volume_segments,
    resolve_pancreas_reference,
)


class PancreasReferenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.prediction_root = self.root / "predictions"
        self.prediction_root.mkdir()
        self.affine = np.diag([1.2, 1.2, 3.0, 1.0])
        self.shape = (8, 7, 4)
        self.reference_path = self.root / "water.nii.gz"
        nib.save(
            nib.Nifti1Image(np.zeros(self.shape, dtype=np.float32), self.affine),
            self.reference_path,
        )

    def tearDown(self):
        self.temp.cleanup()

    def add_prediction(
        self,
        folder_name: str,
        patient_id: str = "P001",
        *,
        affine: np.ndarray | None = None,
        shape: tuple[int, int, int] | None = None,
    ) -> Path:
        case_root = self.prediction_root / folder_name
        case_root.mkdir()
        candidate_affine = self.affine if affine is None else affine
        candidate_shape = self.shape if shape is None else shape
        data = np.zeros(candidate_shape, dtype=np.uint8)
        data[2:5, 2:5, 1] = 1
        prediction_path = case_root / "pancreas_pred.nii.gz"
        nib.save(nib.Nifti1Image(data, candidate_affine), prediction_path)
        (case_root / "inference_metadata.json").write_text(
            json.dumps(
                {
                    "status": "completed",
                    "patient_id": patient_id,
                    "native_shape": list(candidate_shape),
                    "native_affine": candidate_affine.tolist(),
                    "prediction_filename": prediction_path.name,
                    "model_method_id": "IPMR-APR-FULL-400E-S42",
                }
            ),
            encoding="utf-8",
        )
        return prediction_path

    def test_matches_patient_and_native_water_geometry(self):
        prediction_path = self.add_prediction("patient-one")
        result = resolve_pancreas_reference(self.prediction_root, " p001 ", self.reference_path)
        self.assertTrue(result.available)
        self.assertEqual(result.status, "available")
        self.assertEqual(result.prediction.prediction_path, prediction_path.resolve())
        self.assertEqual(result.slice_count, self.shape[2])

    def test_does_not_guess_when_two_predictions_have_the_same_geometry(self):
        self.add_prediction("patient-one-a")
        self.add_prediction("patient-one-b")
        result = resolve_pancreas_reference(self.prediction_root, "P001", self.reference_path)
        self.assertFalse(result.available)
        self.assertEqual(result.status, "ambiguous")

    def test_rejects_a_prediction_from_a_different_examination_space(self):
        shifted_affine = self.affine.copy()
        shifted_affine[0, 3] = 12.0
        self.add_prediction("patient-one", affine=shifted_affine)
        result = resolve_pancreas_reference(self.prediction_root, "P001", self.reference_path)
        self.assertFalse(result.available)
        self.assertEqual(result.status, "geometry_mismatch")

    def test_returns_only_exposed_voxel_edges(self):
        mask = np.zeros((4, 4), dtype=bool)
        mask[1, 1] = True
        self.assertEqual(len(_edge_segments(mask)), 4)
        mask[2, 1] = True
        self.assertEqual(len(_edge_segments(mask)), 6)

    def test_extracts_the_requested_axial_slice(self):
        prediction_path = self.add_prediction("patient-one")
        self.assertGreater(len(pancreas_slice_segments(prediction_path, 1)), 0)
        self.assertEqual(pancreas_slice_segments(prediction_path, 0), ())
        with self.assertRaises(IndexError):
            pancreas_slice_segments(prediction_path, self.shape[2])

    def test_extracts_all_nonempty_axial_contours_for_preloading(self):
        prediction_path = self.add_prediction("patient-one")
        contours = pancreas_volume_segments(prediction_path)
        self.assertEqual([slice_index for slice_index, _ in contours], [1])
        self.assertGreater(len(contours[0][1]), 0)


if __name__ == "__main__":
    unittest.main()
