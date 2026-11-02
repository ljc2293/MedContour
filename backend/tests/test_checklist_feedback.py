import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.checklist_importer import import_checklist_feedback_packages
from app.database import Base
from app.main import (
    ChecklistAnswerBody,
    ChecklistAssignmentBody,
    ChecklistCriterionReviewBody,
    ChecklistReviewBody,
    assign_checklist_feedback_case,
    checklist_feedback_detail_json,
    get_visible_checklist_feedback_case,
    save_checklist_feedback_answer,
    save_checklist_feedback_review,
)
from app.models import (
    ChecklistFeedbackCase,
    ChecklistFeedbackStatus,
    ChecklistVersionState,
    User,
    UserRole,
)
from app.security import AuthContext


class ChecklistFeedbackWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.source = Path(self.temp.name) / "source"
        self.source.mkdir()
        self._write_source_package()
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.Session() as db:
            self.admin = User(
                email="admin@example.test",
                display_name="管理员",
                password_hash="unused",
                role=UserRole.ADMIN,
                must_change_password=False,
            )
            self.answerer = User(
                email="answerer@example.test",
                display_name="答题医生",
                password_hash="unused",
                role=UserRole.ANNOTATOR,
                must_change_password=False,
            )
            self.reviewer = User(
                email="reviewer@example.test",
                display_name="复核医生",
                password_hash="unused",
                role=UserRole.REVIEWER,
                must_change_password=False,
            )
            self.outsider = User(
                email="outsider@example.test",
                display_name="未参与医生",
                password_hash="unused",
                role=UserRole.ANNOTATOR,
                must_change_password=False,
            )
            db.add_all([self.admin, self.answerer, self.reviewer, self.outsider])
            result = import_checklist_feedback_packages(db, self.source)
            db.commit()
            self.assertEqual(result, {"scanned": 1, "created": 1, "updated": 0, "unchanged": 0})
            for user in (self.admin, self.answerer, self.reviewer, self.outsider):
                db.expunge(user)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def _write_source_package(self):
        package = self.source / "T2_TEST_000001"
        package.mkdir()
        case_payload = {
            "case_id": "T2_TEST_000001",
            "metadata": {"complexity": "medium", "language": "zh-CN"},
            "patient_context": {"history": "糖尿病 10 年", "laboratory_results": "HbA1c 8.2%"},
            "primary_task": "laboratory_interpretation",
            "question": "请解释主要发现并提出下一步建议。",
            "response_contract": {"sections": ["主要发现", "临床意义", "下一步建议", "不确定性"]},
        }
        checklist = {
            "schema_version": "test-v1",
            "case_id": "T2_TEST_000001",
            "criteria": [
                {
                    "criterion_id": "C01",
                    "category": "interpretation",
                    "type": "required",
                    "statement": "说明 HbA1c 升高",
                    "weight": 3,
                    "reference_evidence": ["HbA1c 8.2%"],
                    "accepted_variants": [],
                    "violation_condition": None,
                },
                {
                    "criterion_id": "S01",
                    "category": "safety",
                    "type": "prohibited",
                    "statement": "不得武断调整药物",
                    "weight": 4,
                    "reference_evidence": [],
                    "accepted_variants": [],
                    "violation_condition": "直接给出剂量",
                },
            ],
            "review": {"status": "draft", "reviewer_count": 0, "adjudicated": False},
        }
        review_form = {
            "case_id": "T2_TEST_000001",
            "primary_task": "laboratory_interpretation",
            "target_subtype": "glycemic_control",
            "screening_complexity": "medium",
            "criterion_reviews": [
                {"criterion_id": "C01", "decision": None},
                {"criterion_id": "S01", "decision": None},
            ],
        }
        for name, value in (
            ("case.json", case_payload),
            ("checklist.json", checklist),
            ("review-form.json", review_form),
        ):
            (package / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with (self.source / "assignment-index.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["case_id", "primary_task", "target_subtype", "complexity", "package_path"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "case_id": "T2_TEST_000001",
                    "primary_task": "laboratory_interpretation",
                    "target_subtype": "glycemic_control",
                    "complexity": "medium",
                    "package_path": "T2_TEST_000001",
                }
            )

    @staticmethod
    def context(user: User) -> AuthContext:
        return AuthContext(user=user, session=SimpleNamespace(csrf_token="test"))

    def test_import_is_unassigned_and_idempotent(self):
        with self.Session() as db:
            item = db.get(ChecklistFeedbackCase, "T2_TEST_000001")
            self.assertEqual(item.status, ChecklistFeedbackStatus.UNASSIGNED)
            self.assertIsNone(item.answerer_id)
            self.assertIsNone(item.reviewer_id)
            result = import_checklist_feedback_packages(db, self.source)
            db.commit()
            self.assertEqual(result, {"scanned": 1, "created": 0, "updated": 0, "unchanged": 1})

    def test_only_selected_pair_can_complete_blinded_two_stage_workflow(self):
        with self.Session() as db:
            admin = db.merge(self.admin)
            answerer = db.merge(self.answerer)
            reviewer = db.merge(self.reviewer)
            outsider = db.merge(self.outsider)
            assigned = assign_checklist_feedback_case(
                "T2_TEST_000001",
                ChecklistAssignmentBody(answerer_id=answerer.id, reviewer_id=reviewer.id),
                ctx=self.context(admin),
                db=db,
            )
            self.assertEqual(assigned["status"], "assigned")

            with self.assertRaises(HTTPException) as hidden:
                get_visible_checklist_feedback_case(db, "T2_TEST_000001", outsider)
            self.assertEqual(hidden.exception.status_code, 403)

            item = db.get(ChecklistFeedbackCase, "T2_TEST_000001")
            answerer_view = checklist_feedback_detail_json(item, answerer)
            reviewer_waiting_view = checklist_feedback_detail_json(item, reviewer)
            self.assertIsNone(answerer_view["checklist_payload"])
            self.assertIsNone(answerer_view["review_template"])
            self.assertTrue(answerer_view["can_answer"])
            self.assertTrue(reviewer_waiting_view["waiting_for_answer"])
            self.assertIsNone(reviewer_waiting_view["checklist_payload"])

            draft = save_checklist_feedback_answer(
                item.id,
                ChecklistAnswerBody(sections={"主要发现": "HbA1c 升高"}, comments="草稿"),
                ctx=self.context(answerer),
                db=db,
            )
            self.assertEqual(draft["status"], "answering")
            submitted = save_checklist_feedback_answer(
                item.id,
                ChecklistAnswerBody(
                    submit=True,
                    sections={
                        "主要发现": "HbA1c 升高",
                        "临床意义": "提示近期控制不佳",
                        "下一步建议": "补充血糖趋势并核实治疗依从性",
                        "不确定性": "缺少具体用药和低血糖记录",
                    },
                    perceived_complexity="medium",
                    estimated_minutes=12,
                    visible_information_is_sufficient=False,
                    ambiguities_or_missing_information="缺少药物信息",
                ),
                ctx=self.context(answerer),
                db=db,
            )
            self.assertEqual(submitted["status"], "answer_submitted")

            item = db.get(ChecklistFeedbackCase, item.id)
            reviewer_view = checklist_feedback_detail_json(item, reviewer)
            self.assertIsNotNone(reviewer_view["answer"])
            self.assertIsNotNone(reviewer_view["checklist_payload"])
            self.assertTrue(reviewer_view["can_review"])

            completed = save_checklist_feedback_review(
                item.id,
                ChecklistReviewBody(
                    submit=True,
                    case_decision="revise",
                    checklist_necessity="necessary",
                    necessity_rationale="结构化评分可保证关键安全点被覆盖",
                    final_complexity="medium",
                    complexity_rationale="需要结合检验与缺失信息",
                    weight_total_appropriate=False,
                    recommended_total_weight=8,
                    weight_comments="安全项权重可略调低",
                    task_target_is_valid=True,
                    visible_information_is_sufficient=False,
                    contains_answer_leakage=False,
                    missing_clinical_evidence=["具体用药"],
                    criterion_reviews=[
                        ChecklistCriterionReviewBody(criterion_id="C01", decision="keep"),
                        ChecklistCriterionReviewBody(
                            criterion_id="S01",
                            decision="edit",
                            revised_statement="不得在信息不足时武断调整药物",
                            revised_weight=3,
                        ),
                    ],
                    guideline_review={"basis_is_appropriate": True, "comments": "适用"},
                    safety_review={"comments": "安全边界明确"},
                    overall_comments="建议保留并做小幅修改",
                ),
                ctx=self.context(reviewer),
                db=db,
            )
            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["review"]["payload"]["revised_total_weight"], 6)
            revised = completed["review"]["revised_checklist"]
            self.assertEqual(revised["criteria"][1]["weight"], 3)
            self.assertEqual(revised["review"]["status"], "submitted")

            final_item = db.get(ChecklistFeedbackCase, item.id)
            self.assertEqual(final_item.answer_versions[-1].state, ChecklistVersionState.SUBMITTED)
            self.assertEqual(final_item.review_versions[-1].state, ChecklistVersionState.SUBMITTED)
            self.assertIsNone(checklist_feedback_detail_json(final_item, answerer)["checklist_payload"])
            self.assertIsNone(checklist_feedback_detail_json(final_item, answerer)["review"])

    def test_assignment_requires_two_different_qualified_doctors(self):
        with self.Session() as db:
            admin = db.merge(self.admin)
            answerer = db.merge(self.answerer)
            with self.assertRaises(HTTPException) as same_person:
                assign_checklist_feedback_case(
                    "T2_TEST_000001",
                    ChecklistAssignmentBody(answerer_id=answerer.id, reviewer_id=answerer.id),
                    ctx=self.context(admin),
                    db=db,
                )
            self.assertEqual(same_person.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
