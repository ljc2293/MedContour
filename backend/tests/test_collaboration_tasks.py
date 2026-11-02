import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException, UploadFile
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.main import (
    CodexTaskSessionCreateBody,
    TaskCommentBody,
    TaskProgressBody,
    add_collaboration_task_comment,
    codex_asset_mentions,
    collaboration_task_json,
    create_codex_task_session,
    download_collaboration_task_attachment,
    download_collaboration_task_submission_file,
    get_visible_collaboration_task,
    initial_task_draft,
    organize_codex_task_draft,
    publish_codex_session_revision,
    publish_collaboration_task,
    send_codex_task_message,
    upload_collaboration_task_submission,
    update_collaboration_task_progress,
    visible_collaboration_submissions,
)
from app.codex_publisher import CodexWorkerResult
from app.models import (
    CodexTaskMessage,
    CodexTaskSession,
    CollaborationTask,
    CollaborationTaskAttachment,
    CollaborationTaskComment,
    CollaborationTaskParticipant,
    CollaborationTaskRevision,
    CollaborationTaskSubmission,
    CollaborationTaskStatus,
    TaskPriority,
    TaskProgress,
    User,
    UserRole,
)
from app.security import AuthContext


class CollaborationTaskTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            database_url="sqlite://",
            data_root=root / "data",
            task_file_root=root / "task-files",
            preview_root=root / "previews",
            manifests_root=root / "manifests",
            annotation_root=root / "annotations",
        )
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
            self.participant = User(
                email="doctor@example.test",
                display_name="协作者",
                password_hash="unused",
                role=UserRole.ANNOTATOR,
                must_change_password=False,
            )
            self.outsider = User(
                email="other@example.test",
                display_name="无关人员",
                password_hash="unused",
                role=UserRole.REVIEWER,
                must_change_password=False,
            )
            db.add_all([self.admin, self.participant, self.outsider])
            db.commit()
            for user in (self.admin, self.participant, self.outsider):
                db.expunge(user)

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    @staticmethod
    def context(user: User) -> AuthContext:
        return AuthContext(user=user, session=SimpleNamespace(csrf_token="test"))

    async def publish(self, db):
        upload = UploadFile(filename="协作资料.txt", file=BytesIO(b"local collaboration file"))
        with patch("app.main.settings", self.settings):
            return await publish_collaboration_task(
                title="整理研究资料",
                description="多人共同核对并汇总结果",
                priority=TaskPriority.HIGH,
                due_date="2026-09-01",
                participant_ids=json.dumps([self.participant.id]),
                attachments=[upload],
                ctx=self.context(db.merge(self.admin)),
                db=db,
            )

    async def test_admin_can_publish_arbitrary_local_task_with_attachment(self):
        with self.Session() as db:
            result = await self.publish(db)
            task = db.scalar(select(CollaborationTask))
            self.assertEqual(result["title"], "整理研究资料")
            self.assertEqual(task.priority, TaskPriority.HIGH)
            self.assertEqual(task.status, CollaborationTaskStatus.PUBLISHED)
            self.assertEqual(task.participants[0].user_id, self.participant.id)
            self.assertEqual(len(task.attachments), 1)
            stored = self.settings.task_file_root / task.attachments[0].file_path
            self.assertEqual(stored.read_bytes(), b"local collaboration file")

    async def test_participant_progress_completes_task_and_comment_is_saved(self):
        with self.Session() as db:
            result = await self.publish(db)
            participant = db.merge(self.participant)
            update = update_collaboration_task_progress(
                result["id"],
                TaskProgressBody(progress=TaskProgress.DONE),
                ctx=self.context(participant),
                db=db,
            )
            self.assertEqual(update["status"], CollaborationTaskStatus.COMPLETED.value)
            add_collaboration_task_comment(
                result["id"],
                TaskCommentBody(body="资料已核对完成"),
                ctx=self.context(participant),
                db=db,
            )
            comment = db.scalar(select(CollaborationTaskComment))
            self.assertEqual(comment.body, "资料已核对完成")

    async def test_participant_submission_is_versioned_and_saved_locally(self):
        with self.Session() as db:
            task = await self.publish(db)
            actor = db.merge(self.participant)
            with patch("app.main.settings", self.settings):
                first = await upload_collaboration_task_submission(
                    task["id"],
                    note="第一版结果",
                    files=[UploadFile(filename="结果.csv", file=BytesIO(b"row,value\n1,alpha\n"))],
                    ctx=self.context(actor),
                    db=db,
                )
                second = await upload_collaboration_task_submission(
                    task["id"],
                    note="修订后结果",
                    files=[UploadFile(filename="结果.csv", file=BytesIO(b"row,value\n1,beta\n"))],
                    ctx=self.context(actor),
                    db=db,
                )

            self.assertEqual((first["version"], second["version"]), (1, 2))
            self.assertEqual(first["task_revision_number"], 1)
            submissions = db.scalars(
                select(CollaborationTaskSubmission).order_by(CollaborationTaskSubmission.version)
            ).all()
            self.assertEqual([item.note for item in submissions], ["第一版结果", "修订后结果"])
            self.assertEqual(
                (self.settings.task_file_root / submissions[0].files[0].file_path).read_bytes(),
                b"row,value\n1,alpha\n",
            )
            stored_task = db.get(CollaborationTask, task["id"])
            self.assertEqual(stored_task.participants[0].progress, TaskProgress.IN_PROGRESS)

            with patch("app.main.settings", self.settings):
                response = download_collaboration_task_submission_file(
                    task["id"],
                    submissions[0].id,
                    submissions[0].files[0].id,
                    ctx=self.context(actor),
                    db=db,
                )
            self.assertEqual(response.filename, "结果.csv")

    async def test_submission_context_is_readable_by_codex_and_not_republished_as_an_attachment(self):
        with self.Session() as db:
            task_result = await self.publish(db)
            participant = db.merge(self.participant)
            with patch("app.main.settings", self.settings):
                submission_result = await upload_collaboration_task_submission(
                    task_result["id"],
                    note="供管理员复核",
                    files=[UploadFile(filename="marker.json", file=BytesIO(b'{"marker":"CODEX_CAN_READ_ME"}'))],
                    ctx=self.context(participant),
                    db=db,
                )

            actor = db.merge(self.admin)
            with patch("app.main.settings", self.settings):
                session_result = create_codex_task_session(
                    CodexTaskSessionCreateBody(
                        task_id=task_result["id"], submission_ids=[submission_result["id"]]
                    ),
                    ctx=self.context(actor),
                    db=db,
                )
            session = db.get(CodexTaskSession, session_result["id"])
            self.assertEqual(len(session.assets), 1)
            self.assertEqual(session.assets[0].source_kind, "submission")
            self.assertFalse(session.assets[0].include_on_publish)
            mentions = codex_asset_mentions(session.assets)
            self.assertTrue(mentions[0]["path"].startswith("/workspace/.medcontour-task-files/"))

            worker = AsyncMock(
                return_value=CodexWorkerResult(
                    thread_id="submission-thread",
                    final_response="我读取到了 CODEX_CAN_READ_ME。",
                    duration_ms=10,
                )
            )
            with patch("app.main.settings", self.settings), patch("app.main.run_codex_worker", worker):
                await send_codex_task_message(
                    session.id,
                    message="请读取所选交付文件中的 marker",
                    attachments=[],
                    ctx=self.context(actor),
                    db=db,
                )
            self.assertEqual(worker.await_args.kwargs["mentions"], mentions)

            original_attachment_count = len(db.get(CollaborationTask, task_result["id"]).attachments)
            with patch("app.main.settings", self.settings):
                revised = publish_codex_session_revision(db, session, actor, "unstarted")
            self.assertEqual(revised.current_revision, 2)
            self.assertEqual(len(revised.attachments), original_attachment_count)

    async def test_unassigned_user_cannot_open_task(self):
        with self.Session() as db:
            result = await self.publish(db)
            with self.assertRaises(HTTPException) as raised:
                get_visible_collaboration_task(db, result["id"], db.merge(self.outsider))
            self.assertEqual(raised.exception.status_code, 403)

    async def test_codex_chat_forwards_the_admin_message_without_a_task_wrapper(self):
        with self.Session() as db:
            actor = db.merge(self.admin)
            session = CodexTaskSession(
                created_by_id=actor.id,
                created_by=actor,
                mode="create",
                status="draft",
                draft=initial_task_draft(None),
                implementation_status="not_required",
            )
            session.messages.append(
                CodexTaskMessage(
                    role="assistant",
                    content="你好，想发布什么任务？",
                    message_metadata={"protocol": "natural-v2"},
                )
            )
            db.add(session)
            db.commit()
            worker = AsyncMock(
                return_value=CodexWorkerResult(
                    thread_id="natural-thread",
                    final_response="你好！今天想一起处理什么？",
                    duration_ms=12,
                )
            )
            with patch("app.main.settings", self.settings), patch("app.main.run_codex_worker", worker):
                result = await send_codex_task_message(
                    session.id,
                    message="你好",
                    attachments=[],
                    ctx=self.context(actor),
                    db=db,
                )

            request = worker.await_args.kwargs
            self.assertEqual(request["prompt"], "你好")
            self.assertEqual(request["operation"], "chat")
            self.assertIsNone(request["output_schema"])
            self.assertEqual(request["mentions"], [])
            self.assertEqual(result["messages"][-1]["content"], "你好！今天想一起处理什么？")
            self.assertEqual(result["status"], "draft")
            self.assertEqual(result["implementation_status"], "pending")

    async def test_draft_is_structured_only_after_the_admin_requests_it(self):
        with self.Session() as db:
            actor = db.merge(self.admin)
            session = CodexTaskSession(
                created_by_id=actor.id,
                created_by=actor,
                mode="create",
                status="draft",
                draft=initial_task_draft(None),
                implementation_status="pending",
            )
            session.messages.extend(
                [
                    CodexTaskMessage(role="assistant", content="你好，想发布什么任务？", message_metadata={"protocol": "natural-v2"}),
                    CodexTaskMessage(role="user", content="让协作者核对研究数据"),
                    CodexTaskMessage(role="assistant", content="可以，准备邀请谁参与？", message_metadata={"protocol": "natural-v2"}),
                ]
            )
            db.add(session)
            db.commit()
            message_count = len(session.messages)
            structured = {
                "draft": {
                    "title": "研究数据核对",
                    "description": "多人核对研究数据",
                    "priority": "normal",
                    "due_date": "",
                    "participant_ids": [self.participant.id],
                    "task_kind": "data_review",
                    "task_spec": {
                        "workflow_type": "collaboration",
                        "deliverables": ["核对记录"],
                        "steps": [{"label": "核对", "description": "逐条核对"}],
                        "fields": [],
                    },
                    "requires_code_change": False,
                    "implementation_summary": "",
                    "change_summary": "首次发布",
                }
            }
            worker = AsyncMock(
                return_value=CodexWorkerResult(
                    thread_id="one-off-draft-thread",
                    final_response=json.dumps(structured, ensure_ascii=False),
                    duration_ms=20,
                )
            )
            with patch("app.main.settings", self.settings), patch("app.main.run_codex_worker", worker):
                result = await organize_codex_task_draft(
                    session.id,
                    ctx=self.context(actor),
                    db=db,
                )

            request = worker.await_args.kwargs
            self.assertEqual(request["operation"], "draft")
            self.assertIsNone(request["thread_id"])
            self.assertIsNotNone(request["output_schema"])
            self.assertEqual(len(result["messages"]), message_count)
            self.assertEqual(result["draft"]["title"], "研究数据核对")
            self.assertEqual(result["status"], "ready")

    async def test_codex_revision_keeps_original_version_and_updates_current_task(self):
        with self.Session() as db:
            actor = db.merge(self.admin)
            original_session = CodexTaskSession(
                created_by_id=actor.id,
                created_by=actor,
                mode="create",
                draft={
                    "title": "多中心数据核对",
                    "description": "核对第一批数据",
                    "priority": "normal",
                    "due_date": "2026-09-01",
                    "participant_ids": [self.participant.id],
                    "task_kind": "data_review",
                    "task_spec": {
                        "workflow_type": "collaboration",
                        "deliverables": ["核对记录"],
                        "steps": [{"label": "核对", "description": "逐条核对"}],
                        "fields": [],
                    },
                    "requires_code_change": False,
                    "implementation_summary": "",
                    "change_summary": "首次发布",
                },
            )
            db.add(original_session)
            db.flush()

            with patch("app.main.settings", self.settings):
                task = publish_codex_session_revision(db, original_session, actor, "all_incomplete")

            revise_session = CodexTaskSession(
                task_id=task.id,
                created_by_id=actor.id,
                created_by=actor,
                mode="revise",
                draft={
                    "title": "多中心数据核对（第二批）",
                    "description": "在不影响第一版记录的前提下核对第二批数据",
                    "priority": "high",
                    "due_date": "2026-09-15",
                    "participant_ids": [self.participant.id],
                    "task_kind": "data_review",
                    "task_spec": {
                        "workflow_type": "collaboration",
                        "deliverables": ["第二批核对记录"],
                        "steps": [{"label": "复核", "description": "复核第二批"}],
                        "fields": [],
                    },
                    "requires_code_change": False,
                    "implementation_summary": "",
                    "change_summary": "新增第二批数据要求",
                },
            )
            db.add(revise_session)
            db.flush()

            with patch("app.main.settings", self.settings):
                revised = publish_codex_session_revision(db, revise_session, actor, "future_only")

            revisions = db.scalars(
                select(CollaborationTaskRevision)
                .where(CollaborationTaskRevision.task_id == revised.id)
                .order_by(CollaborationTaskRevision.revision_number)
            ).all()
            self.assertEqual(revised.current_revision, 2)
            self.assertEqual(revised.title, "多中心数据核对（第二批）")
            self.assertEqual(revised.publication_source, "codex")
            self.assertEqual([item.revision_number for item in revisions], [1, 2])
            self.assertEqual(revisions[0].title, "多中心数据核对")
            self.assertEqual(revisions[1].change_summary, "新增第二批数据要求")
            future_attachment = CollaborationTaskAttachment(
                id="future-v2-attachment",
                task_id=revised.id,
                uploaded_by_id=actor.id,
                original_name="仅第二版可见.txt",
                file_path=f"{revised.id}/future-v2-attachment",
                content_type="text/plain",
                sha256="0" * 64,
                byte_size=10,
                revision_number=2,
            )
            db.add(future_attachment)
            revisions[1].attachment_snapshot = [future_attachment.id]
            db.commit()
            db.expire(revised, ["attachments", "revisions"])
            participant_view = collaboration_task_json(revised, db.merge(self.participant))
            self.assertEqual(participant_view["view_revision"], 1)
            self.assertEqual(participant_view["title"], "多中心数据核对")
            self.assertEqual(participant_view["task_spec"]["deliverables"], ["核对记录"])
            self.assertEqual(participant_view["attachments"], [])
            with self.assertRaises(HTTPException) as raised:
                download_collaboration_task_attachment(
                    revised.id,
                    future_attachment.id,
                    ctx=self.context(db.merge(self.participant)),
                    db=db,
                )
            self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
