import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.main import apply_case_assignment
from app.models import CaseStatus


class AssignmentWorkflowTest(unittest.TestCase):
    def case(self, *, status=CaseStatus.UNASSIGNED, latest_version=0, annotator_id=None, reviewer_id=None):
        return SimpleNamespace(
            status=status,
            latest_version=latest_version,
            annotator_id=annotator_id,
            reviewer_id=reviewer_id,
        )

    def test_assigning_annotator_moves_unassigned_case_to_assigned(self):
        item = self.case()
        apply_case_assignment(item, update_annotator=True, annotator_id="annotator-1", update_reviewer=False, reviewer_id=None)
        self.assertEqual(item.annotator_id, "annotator-1")
        self.assertEqual(item.status, CaseStatus.ASSIGNED)

    def test_reviewer_only_batch_keeps_existing_annotator(self):
        item = self.case(status=CaseStatus.ASSIGNED, annotator_id="annotator-1")
        apply_case_assignment(item, update_annotator=False, annotator_id=None, update_reviewer=True, reviewer_id="reviewer-1")
        self.assertEqual(item.annotator_id, "annotator-1")
        self.assertEqual(item.reviewer_id, "reviewer-1")
        self.assertEqual(item.status, CaseStatus.ASSIGNED)

    def test_annotated_case_cannot_change_annotator(self):
        item = self.case(status=CaseStatus.ANNOTATING, latest_version=1, annotator_id="annotator-1")
        with self.assertRaises(HTTPException) as raised:
            apply_case_assignment(item, update_annotator=True, annotator_id="annotator-2", update_reviewer=False, reviewer_id=None)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "病例已有标注版本，不能更换初标医生")

    def test_clearing_unstarted_annotator_returns_case_to_unassigned(self):
        item = self.case(status=CaseStatus.ASSIGNED, annotator_id="annotator-1")
        apply_case_assignment(item, update_annotator=True, annotator_id=None, update_reviewer=False, reviewer_id=None)
        self.assertIsNone(item.annotator_id)
        self.assertEqual(item.status, CaseStatus.UNASSIGNED)

    def test_same_person_cannot_be_both_annotator_and_reviewer(self):
        item = self.case()
        with self.assertRaises(HTTPException) as raised:
            apply_case_assignment(
                item,
                update_annotator=True,
                annotator_id="doctor-1",
                update_reviewer=True,
                reviewer_id="doctor-1",
            )
        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(raised.exception.detail, "初标医生与审核医生不能为同一人")


if __name__ == "__main__":
    unittest.main()
