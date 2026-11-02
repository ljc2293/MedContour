import tempfile
import unittest
from pathlib import Path

import nibabel as nib
import numpy as np
from fastapi import HTTPException

from app.main import validate_mask


class MaskValidationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.reference_path = self.root / "reference.nii.gz"
        self.mask_path = self.root / "mask.nii.gz"
        self.affine = np.eye(4, dtype=np.float64)
        nib.save(
            nib.Nifti1Image(np.zeros((4, 5, 6), dtype=np.float32), self.affine),
            self.reference_path,
        )

    def tearDown(self):
        self.temp.cleanup()

    def save_mask(self, data: np.ndarray, affine: np.ndarray | None = None):
        nib.save(nib.Nifti1Image(data, self.affine if affine is None else affine), self.mask_path)

    def assert_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            validate_mask(self.mask_path, self.reference_path, {1})
        self.assertEqual(raised.exception.status_code, 422)

    def test_matching_binary_mask_is_accepted(self):
        data = np.zeros((4, 5, 6), dtype=np.uint8)
        data[1:3, 2:4, 3:5] = 1
        self.save_mask(data)
        validate_mask(self.mask_path, self.reference_path, {1})

    def test_wrong_shape_is_rejected(self):
        self.save_mask(np.zeros((4, 5, 5), dtype=np.uint8))
        self.assert_rejected()

    def test_wrong_affine_is_rejected(self):
        shifted = self.affine.copy()
        shifted[0, 3] = 2.0
        self.save_mask(np.zeros((4, 5, 6), dtype=np.uint8), shifted)
        self.assert_rejected()

    def test_undefined_label_value_is_rejected(self):
        data = np.zeros((4, 5, 6), dtype=np.uint8)
        data[0, 0, 0] = 2
        self.save_mask(data)
        self.assert_rejected()


if __name__ == "__main__":
    unittest.main()
