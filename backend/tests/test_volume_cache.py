import tempfile
import unittest
from pathlib import Path

from app.main import volume_file_response


class VolumeCacheTests(unittest.TestCase):
    def test_volume_is_cached_only_in_the_authenticated_browser_session(self):
        with tempfile.NamedTemporaryFile(suffix=".nii.gz") as source:
            source.write(b"medical-image")
            source.flush()
            response = volume_file_response(Path(source.name), "case_w.nii.gz")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["cache-control"],
            "private, max-age=86400, stale-while-revalidate=3600, no-transform",
        )
        self.assertEqual(response.headers["vary"], "Cookie")
        self.assertTrue(response.headers["etag"])

    def test_matching_etag_returns_not_modified(self):
        with tempfile.NamedTemporaryFile(suffix=".nii.gz") as source:
            source.write(b"medical-image")
            source.flush()
            initial = volume_file_response(Path(source.name), "case_w.nii.gz")
            response = volume_file_response(Path(source.name), "case_w.nii.gz", initial.headers["etag"])

        self.assertEqual(response.status_code, 304)
        self.assertEqual(response.headers["etag"], initial.headers["etag"])
        self.assertEqual(response.headers["vary"], "Cookie")


if __name__ == "__main__":
    unittest.main()
