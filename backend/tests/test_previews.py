import os
import tempfile
import unittest
from pathlib import Path

import nibabel as nib
import numpy as np

from app.previews import (
    display_cache_path,
    ensure_display_volume,
    ensure_preview,
    generate_display_and_preview,
    preview_cache_path,
)


class PreviewTests(unittest.TestCase):
    def test_preview_is_smaller_same_extent_and_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            preview_root = root / "previews"
            source = data_root / "case" / "w.nii.gz"
            source.parent.mkdir(parents=True)
            affine = np.diag([1.2, 1.3, 2.0, 1.0])
            values = np.arange(10 * 8 * 6, dtype=np.uint16).reshape(10, 8, 6)
            nib.save(nib.Nifti1Image(values, affine), source)

            first = ensure_preview(source, data_root, preview_root)
            first_mtime = first.stat().st_mtime_ns
            second = ensure_preview(source, data_root, preview_root)
            preview = nib.load(first)

            self.assertEqual(first, second)
            self.assertEqual(first_mtime, second.stat().st_mtime_ns)
            self.assertEqual(preview.shape, (5, 4, 6))
            self.assertEqual(preview.get_data_dtype(), np.dtype(np.uint8))
            np.testing.assert_allclose(preview.affine, affine @ np.diag([2, 2, 1, 1]))
            self.assertLess(first.stat().st_size, source.stat().st_size)

    def test_source_change_uses_a_new_cache_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            preview_root = root / "previews"
            source = data_root / "w.nii.gz"
            data_root.mkdir(parents=True)
            affine = np.eye(4)
            nib.save(nib.Nifti1Image(np.zeros((8, 8, 4), dtype=np.uint16), affine), source)
            first = ensure_preview(source, data_root, preview_root)

            nib.save(nib.Nifti1Image(np.ones((8, 8, 4), dtype=np.uint16), affine), source)
            os.utime(source, None)
            second = ensure_preview(source, data_root, preview_root)

            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())

    def test_display_volume_preserves_geometry_and_approximate_intensity_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            preview_root = root / "previews"
            source = data_root / "case" / "w.nii.gz"
            source.parent.mkdir(parents=True)
            affine = np.diag([1.2, 1.3, 2.0, 1.0])
            values = np.arange(32 * 24 * 8, dtype=np.uint16).reshape(32, 24, 8)
            nib.save(nib.Nifti1Image(values, affine), source)

            first = ensure_display_volume(source, data_root, preview_root)
            first_mtime = first.stat().st_mtime_ns
            second = ensure_display_volume(source, data_root, preview_root)
            display = nib.load(first)
            restored = np.asarray(display.dataobj)

            self.assertEqual(first, second)
            self.assertEqual(first_mtime, second.stat().st_mtime_ns)
            self.assertEqual(display.shape, values.shape)
            self.assertEqual(display.get_data_dtype(), np.dtype(np.uint8))
            np.testing.assert_allclose(display.affine, affine)
            self.assertIsNotNone(display.dataobj.slope)
            self.assertLess(abs(float(np.median(restored)) - float(np.median(values))), 30)

    def test_display_and_preview_can_be_generated_from_one_decode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_root = root / "data"
            preview_root = root / "previews"
            source = data_root / "case" / "w.nii.gz"
            source.parent.mkdir(parents=True)
            affine = np.diag([1.1, 1.2, 2.5, 1.0])
            values = np.arange(20 * 16 * 5, dtype=np.uint16).reshape(20, 16, 5)
            nib.save(nib.Nifti1Image(values, affine), source)
            display_path = display_cache_path(source, data_root, preview_root)
            preview_path = preview_cache_path(source, data_root, preview_root)

            generated_display, generated_preview = generate_display_and_preview(source, display_path, preview_path)

            self.assertEqual(generated_display, display_path)
            self.assertEqual(generated_preview, preview_path)
            self.assertEqual(nib.load(display_path).shape, values.shape)
            self.assertEqual(nib.load(preview_path).shape, (10, 8, 5))
            np.testing.assert_allclose(nib.load(preview_path).affine, affine @ np.diag([2, 2, 1, 1]))


if __name__ == "__main__":
    unittest.main()
