import unittest

from fastapi import HTTPException

from app.main import release_checklist_feedback_assignment, release_user_assignments, validate_user_deletion
from app.models import ChecklistFeedbackCase, ChecklistFeedbackStatus, CaseStatus, ProjectCase, User, UserRole


class UserDeletionTests(unittest.TestCase):
    def test_admin_cannot_be_deleted(self):
        user = User(role=UserRole.ADMIN)

        with self.assertRaises(HTTPException) as raised:
            validate_user_deletion(user, annotation_count=0, review_count=0)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(raised.exception.detail, "管理员账户不能删除")

    def test_historical_user_must_be_deactivated_instead(self):
        user = User(role=UserRole.ANNOTATOR)

        with self.assertRaises(HTTPException) as raised:
            validate_user_deletion(user, annotation_count=1, review_count=0)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("请改为停用账户", raised.exception.detail)

    def test_unstarted_annotation_assignment_is_released(self):
        item = ProjectCase(
            annotator_id="doctor-1",
            reviewer_id="doctor-2",
            latest_version=0,
            status=CaseStatus.ANNOTATING,
        )

        changed = release_user_assignments(item, "doctor-1")

        self.assertTrue(changed)
        self.assertIsNone(item.annotator_id)
        self.assertEqual(item.reviewer_id, "doctor-2")
        self.assertEqual(item.status, CaseStatus.UNASSIGNED)

    def test_reviewer_assignment_is_released_without_changing_workflow(self):
        item = ProjectCase(
            annotator_id="doctor-1",
            reviewer_id="doctor-2",
            latest_version=1,
            submitted_version=1,
            status=CaseStatus.SUBMITTED,
        )

        changed = release_user_assignments(item, "doctor-2")

        self.assertTrue(changed)
        self.assertIsNone(item.reviewer_id)
        self.assertEqual(item.annotator_id, "doctor-1")
        self.assertEqual(item.status, CaseStatus.SUBMITTED)

    def test_unstarted_checklist_pair_is_released_together(self):
        item = ChecklistFeedbackCase(
            id="T2_TEST",
            primary_task="test",
            screening_complexity="low",
            case_payload={},
            checklist_payload={},
            review_template={},
            source_hash="0" * 64,
            answerer_id="doctor-1",
            reviewer_id="doctor-2",
            status=ChecklistFeedbackStatus.ASSIGNED,
        )

        changed = release_checklist_feedback_assignment(item, "doctor-1")

        self.assertTrue(changed)
        self.assertIsNone(item.answerer_id)
        self.assertIsNone(item.reviewer_id)
        self.assertEqual(item.status, ChecklistFeedbackStatus.UNASSIGNED)

    def test_checklist_history_prevents_user_deletion(self):
        user = User(role=UserRole.REVIEWER)

        with self.assertRaises(HTTPException) as raised:
            validate_user_deletion(user, 0, 0, checklist_history_count=1)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("Checklist", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
