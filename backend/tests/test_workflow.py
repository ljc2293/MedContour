import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.main import (
    resolve_review_transition,
    validate_independent_reviewer,
    validate_review_revision_candidate,
    validate_submission_candidate,
)
from app.models import AnnotationState, CaseStatus, ReviewAction


class SubmissionWorkflowTest(unittest.TestCase):
    def candidate(self, status, latest_version, submitted_version=None, state=AnnotationState.DRAFT):
        item = SimpleNamespace(status=status, submitted_version=submitted_version)
        latest = None if latest_version is None else SimpleNamespace(version=latest_version, state=state)
        return item, latest

    def assert_rejected(self, item, latest, expected_message):
        with self.assertRaises(HTTPException) as raised:
            validate_submission_candidate(item, latest)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, expected_message)

    def test_initial_draft_can_be_submitted(self):
        item, latest = self.candidate(CaseStatus.ANNOTATING, 1)
        validate_submission_candidate(item, latest)

    def test_returned_submission_cannot_be_resubmitted_unchanged(self):
        item, latest = self.candidate(CaseStatus.RETURNED, 1, submitted_version=1, state=AnnotationState.SUPERSEDED)
        self.assert_rejected(item, latest, "退回病例必须修改上一次标注并保存为新版本后才能重新提交")

    def test_returned_case_requires_a_newer_draft_version(self):
        item, latest = self.candidate(CaseStatus.RETURNED, 1, submitted_version=1)
        self.assert_rejected(item, latest, "退回病例必须修改上一次标注并保存为新版本后才能重新提交")

    def test_returned_case_accepts_new_revision_draft(self):
        item, latest = self.candidate(CaseStatus.RETURNED, 2, submitted_version=1)
        validate_submission_candidate(item, latest)

    def test_submitted_case_cannot_be_modified_or_submitted_again(self):
        item, latest = self.candidate(CaseStatus.SUBMITTED, 1, submitted_version=1)
        self.assert_rejected(item, latest, "当前阶段不可提交标注")


class ReviewWorkflowTest(unittest.TestCase):
    def test_pending_submission_can_be_approved_or_returned(self):
        item = SimpleNamespace(status=CaseStatus.SUBMITTED, submitted_version=2, approved_version=None)
        self.assertEqual(resolve_review_transition(item, ReviewAction.APPROVE), (2, False))
        self.assertEqual(resolve_review_transition(item, ReviewAction.RETURN), (2, False))

    def test_review_revision_becomes_the_reviewed_version(self):
        item = SimpleNamespace(status=CaseStatus.IN_REVIEW, submitted_version=2, approved_version=None)
        revision = SimpleNamespace(version=3, state=AnnotationState.DRAFT, kind="review_revision", source_version=2)
        self.assertEqual(resolve_review_transition(item, ReviewAction.APPROVE, revision), (3, False))
        self.assertEqual(resolve_review_transition(item, ReviewAction.RETURN, revision), (3, False))

    def test_review_revision_must_still_be_a_draft(self):
        item = SimpleNamespace(status=CaseStatus.IN_REVIEW, submitted_version=2, approved_version=None)
        revision = SimpleNamespace(version=3, state=AnnotationState.SUPERSEDED, kind="review_revision", source_version=2)
        with self.assertRaises(HTTPException) as raised:
            resolve_review_transition(item, ReviewAction.APPROVE, revision)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "审核微调版本状态异常")

    def test_review_revision_must_reference_the_current_submission(self):
        item = SimpleNamespace(status=CaseStatus.IN_REVIEW, submitted_version=2, approved_version=None)
        revision = SimpleNamespace(version=3, state=AnnotationState.DRAFT, kind="review_revision", source_version=1)
        with self.assertRaises(HTTPException) as raised:
            resolve_review_transition(item, ReviewAction.APPROVE, revision)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "审核微调版本状态异常")

    def test_only_pending_review_cases_accept_direct_revision(self):
        validate_review_revision_candidate(SimpleNamespace(status=CaseStatus.SUBMITTED, submitted_version=2))
        with self.assertRaises(HTTPException) as raised:
            validate_review_revision_candidate(SimpleNamespace(status=CaseStatus.RETURNED, submitted_version=2))
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "只有待审核病例可以由审核医生直接修改")

    def test_approved_case_can_be_revoked_to_rework(self):
        item = SimpleNamespace(status=CaseStatus.APPROVED, submitted_version=2, approved_version=2)
        self.assertEqual(resolve_review_transition(item, ReviewAction.RETURN), (2, True))

    def test_approved_case_cannot_be_approved_again(self):
        item = SimpleNamespace(status=CaseStatus.APPROVED, submitted_version=2, approved_version=2)
        with self.assertRaises(HTTPException) as raised:
            resolve_review_transition(item, ReviewAction.APPROVE)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "已通过病例只能撤回后重新审核")

    def test_approved_case_requires_an_existing_approved_version(self):
        item = SimpleNamespace(status=CaseStatus.APPROVED, submitted_version=2, approved_version=None)
        with self.assertRaises(HTTPException) as raised:
            resolve_review_transition(item, ReviewAction.RETURN)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail, "已通过版本不存在")

    def test_annotator_cannot_review_own_case(self):
        item = SimpleNamespace(annotator_id="doctor-1")
        version = SimpleNamespace(author_id="doctor-1")
        with self.assertRaises(HTTPException) as raised:
            validate_independent_reviewer(item, "doctor-1", version)
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.detail, "本人参与初标的病例必须由其他审核医生审核")

    def test_other_reviewer_can_review_case(self):
        item = SimpleNamespace(annotator_id="doctor-1")
        version = SimpleNamespace(author_id="doctor-1")
        validate_independent_reviewer(item, "doctor-2", version)

    def test_any_prior_annotation_participation_blocks_review(self):
        item = SimpleNamespace(annotator_id="doctor-1")
        version = SimpleNamespace(author_id="doctor-1")
        with self.assertRaises(HTTPException):
            validate_independent_reviewer(item, "doctor-2", version, has_authored_case=True)


if __name__ == "__main__":
    unittest.main()
