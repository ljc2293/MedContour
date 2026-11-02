from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

import nibabel as nib
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import and_, func, inspect, or_, select, text
from sqlalchemy.orm import Session as DBSession

from .config import get_settings
from .codex_publisher import (
    CodexWorkerError,
    TASK_DRAFT_OUTPUT_SCHEMA,
    TASK_IMPLEMENTATION_OUTPUT_SCHEMA,
    codex_worker_health,
    run_codex_worker,
)
from .database import Base, SessionLocal, engine, get_db
from .importer import PDFF_PROJECT_NAME, SEGMENTATION_PROJECT_NAME, import_dataset, sync_pdff_project_cases
from .models import (
    AnnotationState,
    AnnotationVersion,
    AuditLog,
    CaseStatus,
    ChecklistAnswerVersion,
    ChecklistFeedbackCase,
    ChecklistFeedbackStatus,
    ChecklistReviewVersion,
    ChecklistVersionState,
    CollaborationTask,
    CollaborationTaskAttachment,
    CollaborationTaskComment,
    CollaborationTaskParticipant,
    CollaborationTaskRevision,
    CollaborationTaskSubmission,
    CollaborationTaskSubmissionFile,
    CollaborationTaskStatus,
    CodexTaskAsset,
    CodexTaskMessage,
    CodexTaskSession,
    ImagingCase,
    Label,
    PdffAnnotationVersion,
    Project,
    ProjectCase,
    ReviewAction,
    ReviewEvent,
    Session,
    TaskPriority,
    TaskProgress,
    User,
    UserRole,
)
from .pancreas_reference import pancreas_slice_segments, pancreas_volume_segments, resolve_pancreas_reference
from .previews import ensure_display_volume, ensure_preview
from .security import (
    AuthContext,
    csrf_context,
    current_context,
    hash_password,
    new_session,
    ready_context,
    require_roles,
    require_roles_csrf,
    verify_password,
)


settings = get_settings()
STARTUP_LOCK_NAME = "medcontour-startup"
STARTUP_READY_PATH = Path("/tmp/medcontour-startup.ready")


@contextmanager
def startup_lock():
    """Serialize schema seeding and dataset sync across API worker processes."""
    if engine.dialect.name != "postgresql":
        yield
        return
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(hashtext(:name))"), {"name": STARTUP_LOCK_NAME})
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext(:name))"), {"name": STARTUP_LOCK_NAME})


def audit(db: DBSession, actor: User | None, action: str, target_type: str, target_id: str | None, details: dict | None = None):
    db.add(AuditLog(actor_id=actor.id if actor else None, action=action, target_type=target_type, target_id=target_id, details=details or {}))


def seed_defaults(db: DBSession):
    admin_email = settings.initial_admin_email.strip().lower()
    admin = db.scalar(select(User).where(User.email == admin_email))
    if admin is None:
        if len(settings.initial_admin_password) < 10:
            raise RuntimeError("INITIAL_ADMIN_PASSWORD must contain at least 10 characters")
        admin = User(
            email=admin_email,
            display_name="系统管理员",
            password_hash=hash_password(settings.initial_admin_password),
            role=UserRole.ADMIN,
            must_change_password=True,
        )
        db.add(admin)
        db.flush()
        audit(db, admin, "user.seed_admin", "user", admin.id, {"email": admin_email})

    project = db.scalar(select(Project).where(Project.name == SEGMENTATION_PROJECT_NAME))
    if project is None:
        project = Project(
            name=SEGMENTATION_PROJECT_NAME,
            description="非增强 Dixon 四序列人工器官勾线项目",
            workflow_type="segmentation",
        )
        db.add(project)
        db.flush()
        db.add(Label(project_id=project.id, key="pancreas", name="胰腺", value=1, color="#2DD4BF"))
    else:
        project.workflow_type = "segmentation"

    pdff_project = db.scalar(select(Project).where(Project.name == PDFF_PROJECT_NAME))
    if pdff_project is None:
        pdff_project = Project(
            name=PDFF_PROJECT_NAME,
            description="复用 Dixon 四相位影像，在三个不同轴位层面分别勾画胰头、胰体、胰尾圆形 ROI，保存各平面结果并汇总病例级 PDFF 与层间一致性",
            workflow_type="pdff_roi",
            source_project_id=project.id,
        )
        db.add(pdff_project)
        db.flush()
        db.add_all([
            Label(project_id=pdff_project.id, key="head", name="胰头 ROI", value=1, color="#2DD4BF"),
            Label(project_id=pdff_project.id, key="body", name="胰体 ROI", value=2, color="#60A5FA"),
            Label(project_id=pdff_project.id, key="tail", name="胰尾 ROI", value=3, color="#FB7185"),
        ])
    else:
        pdff_project.workflow_type = "pdff_roi"
        pdff_project.source_project_id = project.id
        pdff_project.description = "复用 Dixon 四相位影像，在三个不同轴位层面分别勾画胰头、胰体、胰尾圆形 ROI，保存各平面结果并汇总病例级 PDFF 与层间一致性"
    db.commit()


def ensure_user_role_assignments(db: DBSession):
    """Backfill the multi-role table from the legacy single-role column."""
    changed = False
    for user in db.scalars(select(User)).all():
        if not user.role_assignments:
            user.set_roles({user.role})
            changed = True
    if changed:
        db.commit()


def ensure_annotation_revision_columns():
    """Apply the additive revision metadata migration for existing deployments."""
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("annotation_versions")}
        if "kind" not in columns:
            connection.execute(text("ALTER TABLE annotation_versions ADD COLUMN kind VARCHAR(32) NOT NULL DEFAULT 'annotation'"))
        if "source_version" not in columns:
            connection.execute(text("ALTER TABLE annotation_versions ADD COLUMN source_version INTEGER"))


def ensure_project_workflow_columns():
    """Add project workflow metadata without altering existing project tasks."""
    with engine.begin() as connection:
        columns = {column["name"] for column in inspect(connection).get_columns("projects")}
        if "workflow_type" not in columns:
            connection.execute(text("ALTER TABLE projects ADD COLUMN workflow_type VARCHAR(40) NOT NULL DEFAULT 'segmentation'"))
        if "source_project_id" not in columns:
            connection.execute(text("ALTER TABLE projects ADD COLUMN source_project_id VARCHAR(36)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_projects_workflow_type ON projects (workflow_type)"))


def ensure_collaboration_revision_columns():
    """Add version metadata without replacing existing published collaboration tasks."""
    with engine.begin() as connection:
        task_columns = {column["name"] for column in inspect(connection).get_columns("collaboration_tasks")}
        task_additions = {
            "current_revision": "INTEGER NOT NULL DEFAULT 1",
            "task_kind": "VARCHAR(80) NOT NULL DEFAULT 'generic'",
            "task_spec": "JSON",
            "publication_source": "VARCHAR(40) NOT NULL DEFAULT 'manual'",
            "latest_change_summary": "TEXT NOT NULL DEFAULT ''",
        }
        for column_name, definition in task_additions.items():
            if column_name not in task_columns:
                connection.execute(text(f"ALTER TABLE collaboration_tasks ADD COLUMN {column_name} {definition}"))

        participant_columns = {
            column["name"] for column in inspect(connection).get_columns("collaboration_task_participants")
        }
        if "revision_number" not in participant_columns:
            connection.execute(
                text("ALTER TABLE collaboration_task_participants ADD COLUMN revision_number INTEGER NOT NULL DEFAULT 1")
            )
        if "is_active" not in participant_columns:
            connection.execute(
                text("ALTER TABLE collaboration_task_participants ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE")
            )

        attachment_columns = {
            column["name"] for column in inspect(connection).get_columns("collaboration_task_attachments")
        }
        if "revision_number" not in attachment_columns:
            connection.execute(
                text("ALTER TABLE collaboration_task_attachments ADD COLUMN revision_number INTEGER NOT NULL DEFAULT 1")
            )

        codex_asset_columns = {column["name"] for column in inspect(connection).get_columns("codex_task_assets")}
        if "source_kind" not in codex_asset_columns:
            connection.execute(
                text("ALTER TABLE codex_task_assets ADD COLUMN source_kind VARCHAR(32) NOT NULL DEFAULT 'upload'")
            )
        if "include_on_publish" not in codex_asset_columns:
            connection.execute(
                text("ALTER TABLE codex_task_assets ADD COLUMN include_on_publish BOOLEAN NOT NULL DEFAULT TRUE")
            )


def backfill_collaboration_task_revisions(db: DBSession):
    """Capture an immutable v1 snapshot for tasks created before revision support."""
    changed = False
    tasks = db.scalars(select(CollaborationTask)).unique().all()
    for task in tasks:
        if task.revisions:
            continue
        active_participants = [participant for participant in task.participants if participant.is_active]
        db.add(
            CollaborationTaskRevision(
                task_id=task.id,
                revision_number=task.current_revision or 1,
                title=task.title,
                description=task.description,
                priority=task.priority,
                due_date=task.due_date,
                task_kind=task.task_kind or "generic",
                task_spec=normalize_task_spec(task.task_spec),
                participant_snapshot=[participant.user_id for participant in active_participants],
                attachment_snapshot=[attachment.id for attachment in task.attachments],
                change_summary="历史任务基线",
                publication_source=task.publication_source or "manual",
                created_by_id=task.created_by_id,
            )
        )
        changed = True
    if changed:
        db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.annotation_root.mkdir(parents=True, exist_ok=True)
    settings.preview_root.mkdir(parents=True, exist_ok=True)
    settings.task_file_root.mkdir(parents=True, exist_ok=True)
    with startup_lock():
        Base.metadata.create_all(bind=engine)
        ensure_project_workflow_columns()
        ensure_annotation_revision_columns()
        ensure_collaboration_revision_columns()
        if not STARTUP_READY_PATH.exists():
            with SessionLocal() as db:
                seed_defaults(db)
                ensure_user_role_assignments(db)
                backfill_collaboration_task_revisions(db)
                if settings.auto_import_dataset:
                    try:
                        import_dataset(db, settings, only_available=True)
                    except FileNotFoundError:
                        pass
                sync_pdff_project_cases(db, settings)
                db.commit()
            STARTUP_READY_PATH.touch()
    yield


app = FastAPI(title=settings.app_name, version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.trusted_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)


class LoginBody(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class UserCreateBody(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=100)
    role: UserRole | None = None
    roles: list[UserRole] | None = Field(default=None, min_length=1, max_length=2)
    temporary_password: str = Field(min_length=10, max_length=256)


class UserPatchBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: UserRole | None = None
    roles: list[UserRole] | None = Field(default=None, min_length=1, max_length=2)
    is_active: bool | None = None
    reset_password: str | None = Field(default=None, min_length=10, max_length=256)


class AssignmentBody(BaseModel):
    annotator_id: str | None = None
    reviewer_id: str | None = None


class BulkAssignmentBody(BaseModel):
    case_ids: list[str] = Field(min_length=1, max_length=500)
    update_annotator: bool = False
    annotator_id: str | None = None
    update_reviewer: bool = False
    reviewer_id: str | None = None


class ReviewBody(BaseModel):
    action: ReviewAction
    comment: str = Field(default="", max_length=4000)


class PdffRoiBody(BaseModel):
    region: Literal["head", "body", "tail"]
    center_voxel: tuple[float, float, float]
    radius_mm: float = Field(ge=math.sqrt(50.0 / math.pi))


class PdffPlaneBody(BaseModel):
    plane_index: int = Field(ge=1, le=3)
    rois: list[PdffRoiBody] = Field(min_length=3, max_length=3)


class PdffAnnotationBody(BaseModel):
    planes: list[PdffPlaneBody] = Field(min_length=1, max_length=3)
    comment: str = Field(default="", max_length=4000)


class PdffPreviewBody(BaseModel):
    rois: list[PdffRoiBody] = Field(min_length=1, max_length=3)


class ChecklistAssignmentBody(BaseModel):
    answerer_id: str | None = None
    reviewer_id: str | None = None


class ChecklistAnswerBody(BaseModel):
    submit: bool = False
    sections: dict[str, str] = Field(default_factory=dict)
    perceived_complexity: Literal["low", "medium", "high"] | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=480)
    visible_information_is_sufficient: bool | None = None
    ambiguities_or_missing_information: str = Field(default="", max_length=8000)
    comments: str = Field(default="", max_length=8000)


class ChecklistCriterionReviewBody(BaseModel):
    criterion_id: str = Field(min_length=1, max_length=80)
    decision: Literal["keep", "edit", "delete"] | None = None
    revised_type: Literal["required", "uncertainty", "prohibited"] | None = None
    revised_statement: str | None = Field(default=None, max_length=10000)
    revised_weight: int | None = Field(default=None, ge=1, le=100)
    revised_reference_evidence: list[str] | None = Field(default=None, max_length=100)
    revised_accepted_variants: list[str] | None = Field(default=None, max_length=100)
    revised_violation_condition: str | None = Field(default=None, max_length=10000)
    comments: str = Field(default="", max_length=8000)


class ChecklistNewCriterionBody(BaseModel):
    criterion_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=160)
    type: Literal["required", "uncertainty", "prohibited"]
    statement: str = Field(min_length=1, max_length=10000)
    weight: int = Field(ge=1, le=100)
    reference_evidence: list[str] = Field(default_factory=list, max_length=100)
    accepted_variants: list[str] = Field(default_factory=list, max_length=100)
    violation_condition: str | None = Field(default=None, max_length=10000)


class ChecklistReviewBody(BaseModel):
    submit: bool = False
    case_decision: Literal["approve", "revise", "reject"] | None = None
    checklist_necessity: Literal["necessary", "partly_necessary", "unnecessary"] | None = None
    necessity_rationale: str = Field(default="", max_length=10000)
    final_complexity: Literal["low", "medium", "high"] | None = None
    complexity_rationale: str = Field(default="", max_length=10000)
    weight_total_appropriate: bool | None = None
    recommended_total_weight: int | None = Field(default=None, ge=1, le=1000)
    weight_comments: str = Field(default="", max_length=10000)
    task_target_is_valid: bool | None = None
    visible_information_is_sufficient: bool | None = None
    contains_answer_leakage: bool | None = None
    missing_clinical_evidence: list[str] = Field(default_factory=list, max_length=100)
    criterion_reviews: list[ChecklistCriterionReviewBody] = Field(default_factory=list, max_length=200)
    new_criteria: list[ChecklistNewCriterionBody] = Field(default_factory=list, max_length=100)
    guideline_review: dict = Field(default_factory=dict)
    safety_review: dict = Field(default_factory=dict)
    overall_comments: str = Field(default="", max_length=20000)


class TaskProgressBody(BaseModel):
    progress: TaskProgress


class TaskCommentBody(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class CodexTaskSessionCreateBody(BaseModel):
    task_id: str | None = None
    submission_ids: list[str] = Field(default_factory=list, max_length=50)


class CodexTaskPublishBody(BaseModel):
    upgrade_scope: Literal["future_only", "unstarted", "all_incomplete", "all"] = "unstarted"


def user_json(user: User) -> dict:
    roles = user.assigned_roles()
    ordered_roles = [role.value for role in (UserRole.ADMIN, UserRole.ANNOTATOR, UserRole.REVIEWER) if role in roles]
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role.value,
        "roles": ordered_roles,
        "is_active": user.is_active,
        "must_change_password": user.must_change_password,
    }


def requested_roles(role: UserRole | None, roles: list[UserRole] | None, *, allow_admin: bool) -> set[UserRole] | None:
    if role is not None and roles is not None:
        raise HTTPException(status_code=422, detail="请勿同时提交 role 和 roles")
    if role is None and roles is None:
        return None
    result = set(roles or [role])
    if not result:
        raise HTTPException(status_code=422, detail="至少需要一个角色")
    if UserRole.ADMIN in result:
        if not allow_admin:
            raise HTTPException(status_code=422, detail="管理员权限不能通过角色编辑授予")
        if len(result) != 1:
            raise HTTPException(status_code=422, detail="管理员不能与临床工作角色组合")
    elif not result.issubset({UserRole.ANNOTATOR, UserRole.REVIEWER}):
        raise HTTPException(status_code=422, detail="工作角色无效")
    return result


def case_access_condition(user: User):
    if user.has_role(UserRole.ADMIN):
        return None
    conditions = []
    if user.has_role(UserRole.ANNOTATOR):
        conditions.append(ProjectCase.annotator_id == user.id)
    if user.has_role(UserRole.REVIEWER):
        conditions.append(
            or_(
                ProjectCase.reviewer_id == user.id,
                and_(
                    ProjectCase.reviewer_id.is_(None),
                    ProjectCase.status.in_({CaseStatus.SUBMITTED, CaseStatus.IN_REVIEW}),
                    ProjectCase.annotator_id != user.id,
                ),
            )
        )
    return or_(*conditions) if conditions else ProjectCase.id.is_(None)


def annotation_model_for_item(item: ProjectCase):
    return PdffAnnotationVersion if item.project.workflow_type == "pdff_roi" else AnnotationVersion


def project_case_json(item: ProjectCase) -> dict:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "project_name": item.project.name,
        "workflow_type": item.project.workflow_type,
        "status": item.status.value,
        "latest_version": item.latest_version,
        "submitted_version": item.submitted_version,
        "approved_version": item.approved_version,
        "annotator": user_json(item.annotator) if item.annotator else None,
        "reviewer": user_json(item.reviewer) if item.reviewer else None,
        "case": {
            "id": item.case.id,
            "case_code": item.case.case_code,
            "patient_id": item.case.patient_id,
            "patient_name_zh": item.case.patient_name_zh,
            "study_date": item.case.study_date,
            "protocol_family": item.case.protocol_family,
            "needs_identity_review": item.case.needs_identity_review,
            "identity_review_note": item.case.mapping_status if item.case.needs_identity_review else "",
            "is_available": item.case.is_available,
            "sequences": sorted(item.case.sequence_paths.keys()),
        },
    }


def annotation_version_json(version: AnnotationVersion | PdffAnnotationVersion | None) -> dict | None:
    if version is None:
        return None
    result = {
        "version": version.version,
        "state": version.state.value,
        "author_id": version.author_id,
        "author": version.author.display_name,
        "kind": version.kind,
        "source_version": version.source_version,
        "created_at": version.created_at,
        "comment": version.comment,
    }
    if isinstance(version, PdffAnnotationVersion):
        payload = version.roi_payload or []
        if payload and isinstance(payload[0], dict) and "plane_index" in payload[0]:
            result["planes"] = payload
            stored_metrics = version.metrics if isinstance(version.metrics, dict) else {}
            result["case_summary"] = stored_metrics.get("case_summary")
        else:
            # Backward-compatible reading of annotations created before the
            # multi-plane protocol was introduced.
            result["rois"] = payload
            result["metrics"] = version.metrics if isinstance(version.metrics, list) else []
        result["sha256"] = version.sha256
    return result


def canonical_json_hash(payload: dict) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def checklist_feedback_access_condition(user: User):
    if user.has_role(UserRole.ADMIN):
        return None
    conditions = []
    if user.has_role(UserRole.ANNOTATOR):
        conditions.append(ChecklistFeedbackCase.answerer_id == user.id)
    if user.has_role(UserRole.REVIEWER):
        conditions.append(ChecklistFeedbackCase.reviewer_id == user.id)
    return or_(*conditions) if conditions else ChecklistFeedbackCase.id.is_(None)


def get_visible_checklist_feedback_case(
    db: DBSession, case_id: str, user: User
) -> ChecklistFeedbackCase:
    item = db.get(ChecklistFeedbackCase, case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist 反馈任务不存在")
    if not user.has_role(UserRole.ADMIN) and user.id not in {item.answerer_id, item.reviewer_id}:
        raise HTTPException(status_code=403, detail="该题目没有分配给你")
    return item


def latest_checklist_answer(item: ChecklistFeedbackCase) -> ChecklistAnswerVersion | None:
    return item.answer_versions[-1] if item.answer_versions else None


def submitted_checklist_answer(item: ChecklistFeedbackCase) -> ChecklistAnswerVersion | None:
    return next(
        (version for version in reversed(item.answer_versions) if version.state == ChecklistVersionState.SUBMITTED),
        None,
    )


def latest_checklist_review(item: ChecklistFeedbackCase) -> ChecklistReviewVersion | None:
    return item.review_versions[-1] if item.review_versions else None


def checklist_actor_role(item: ChecklistFeedbackCase, user: User) -> str:
    if user.has_role(UserRole.ADMIN):
        return "admin"
    if item.answerer_id == user.id:
        return "answerer"
    if item.reviewer_id == user.id:
        return "reviewer"
    return "none"


def checklist_version_json(
    version: ChecklistAnswerVersion | ChecklistReviewVersion | None,
    *,
    include_payload: bool = True,
) -> dict | None:
    if version is None:
        return None
    result = {
        "id": version.id,
        "version": version.version,
        "state": version.state.value,
        "author": user_json(version.author),
        "sha256": version.sha256,
        "created_at": version.created_at,
    }
    if include_payload:
        result["payload"] = version.payload
    if isinstance(version, ChecklistReviewVersion):
        result["answer_version"] = version.answer_version
        if include_payload:
            result["revised_checklist"] = version.revised_checklist
    return result


def checklist_feedback_item_json(item: ChecklistFeedbackCase, user: User) -> dict:
    criteria = item.checklist_payload.get("criteria") or []
    original_total_weight = sum(
        int(criterion.get("weight") or 0) for criterion in criteria if isinstance(criterion, dict)
    )
    return {
        "id": item.id,
        "primary_task": item.primary_task,
        "target_subtype": item.target_subtype,
        "screening_complexity": item.screening_complexity,
        "status": item.status.value,
        "answerer": user_json(item.answerer) if item.answerer else None,
        "reviewer": user_json(item.reviewer) if item.reviewer else None,
        "latest_answer_version": item.latest_answer_version,
        "latest_review_version": item.latest_review_version,
        "question": str(item.case_payload.get("question") or ""),
        "response_sections": item.case_payload.get("response_contract", {}).get("sections") or [],
        "criterion_count": len(criteria),
        "original_total_weight": original_total_weight,
        "my_role": checklist_actor_role(item, user),
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def checklist_feedback_detail_json(item: ChecklistFeedbackCase, user: User) -> dict:
    result = checklist_feedback_item_json(item, user)
    role = result["my_role"]
    submitted_answer = submitted_checklist_answer(item)
    latest_answer = latest_checklist_answer(item)
    latest_review = latest_checklist_review(item)
    is_admin = role == "admin"
    reviewer_ready = role == "reviewer" and submitted_answer is not None

    visible_answer = latest_answer if is_admin or role == "answerer" else submitted_answer if reviewer_ready else None
    visible_review = latest_review if is_admin or role == "reviewer" else None
    result.update(
        {
            "case_payload": item.case_payload,
            # Independent answerers never receive the source checklist. Reviewers receive it only after answer submission.
            "checklist_payload": item.checklist_payload if is_admin or reviewer_ready else None,
            "review_template": item.review_template if is_admin or reviewer_ready else None,
            "answer": checklist_version_json(visible_answer),
            "review": checklist_version_json(visible_review),
            "can_answer": role == "answerer" and (latest_answer is None or latest_answer.state != ChecklistVersionState.SUBMITTED),
            "can_review": role == "reviewer" and submitted_answer is not None and (
                latest_review is None or latest_review.state != ChecklistVersionState.SUBMITTED
            ),
            "waiting_for_answer": role == "reviewer" and submitted_answer is None,
            "answer_history": [checklist_version_json(version) for version in item.answer_versions] if is_admin else [],
            "review_history": [checklist_version_json(version) for version in item.review_versions] if is_admin else [],
        }
    )
    return result


def refresh_checklist_feedback_status(item: ChecklistFeedbackCase) -> None:
    review = latest_checklist_review(item)
    answer = latest_checklist_answer(item)
    if review is not None and review.state == ChecklistVersionState.SUBMITTED:
        item.status = ChecklistFeedbackStatus.COMPLETED
    elif review is not None:
        item.status = ChecklistFeedbackStatus.REVIEWING
    elif answer is not None and answer.state == ChecklistVersionState.SUBMITTED:
        item.status = ChecklistFeedbackStatus.ANSWER_SUBMITTED
    elif answer is not None:
        item.status = ChecklistFeedbackStatus.ANSWERING
    elif item.answerer_id and item.reviewer_id:
        item.status = ChecklistFeedbackStatus.ASSIGNED
    else:
        item.status = ChecklistFeedbackStatus.UNASSIGNED


def build_revised_checklist(
    item: ChecklistFeedbackCase,
    body: ChecklistReviewBody,
    *,
    next_version: int,
    answer_version: int,
    reviewer: User,
) -> tuple[dict, dict]:
    original = item.checklist_payload
    original_criteria = [criterion for criterion in original.get("criteria", []) if isinstance(criterion, dict)]
    original_ids = [str(criterion.get("criterion_id") or "") for criterion in original_criteria]
    review_by_id: dict[str, ChecklistCriterionReviewBody] = {}
    for review in body.criterion_reviews:
        criterion_id = review.criterion_id.strip()
        if criterion_id in review_by_id:
            raise HTTPException(status_code=422, detail=f"评分项 {criterion_id} 被重复评审")
        if criterion_id not in original_ids:
            raise HTTPException(status_code=422, detail=f"评分项 {criterion_id} 不属于当前 Checklist")
        review_by_id[criterion_id] = review

    if body.submit:
        missing = [criterion_id for criterion_id in original_ids if criterion_id not in review_by_id]
        undecided = [criterion_id for criterion_id, review in review_by_id.items() if review.decision is None]
        if missing or undecided:
            raise HTTPException(status_code=422, detail="提交前请逐条选择保留、修改或删除")
        required_summary = (
            body.case_decision,
            body.checklist_necessity,
            body.final_complexity,
            body.weight_total_appropriate,
            body.task_target_is_valid,
            body.visible_information_is_sufficient,
            body.contains_answer_leakage,
        )
        if any(value is None for value in required_summary):
            raise HTTPException(status_code=422, detail="提交前请完成 Checklist 必要性、复杂度、权重和病例总体评估")
        if not body.necessity_rationale.strip():
            raise HTTPException(status_code=422, detail="请说明 Checklist 是否必要的理由")
        if body.weight_total_appropriate is False and body.recommended_total_weight is None:
            raise HTTPException(status_code=422, detail="认为总权重不合理时，请填写建议总权重")

    revised_criteria: list[dict] = []
    changed_count = 0
    for original_criterion in original_criteria:
        criterion = json.loads(json.dumps(original_criterion, ensure_ascii=False))
        criterion_id = str(criterion["criterion_id"])
        review = review_by_id.get(criterion_id)
        decision = review.decision if review else None
        if decision == "delete":
            changed_count += 1
            continue
        if decision == "edit" and review is not None:
            revisions = {
                "type": review.revised_type,
                "statement": review.revised_statement.strip() if review.revised_statement is not None else None,
                "weight": review.revised_weight,
                "reference_evidence": review.revised_reference_evidence,
                "accepted_variants": review.revised_accepted_variants,
                "violation_condition": review.revised_violation_condition,
            }
            supplied = {key: value for key, value in revisions.items() if value is not None}
            if body.submit and not supplied:
                raise HTTPException(status_code=422, detail=f"评分项 {criterion_id} 选择了修改，但没有填写修改内容")
            if "statement" in supplied and not supplied["statement"]:
                raise HTTPException(status_code=422, detail=f"评分项 {criterion_id} 的描述不能为空")
            criterion.update(supplied)
            changed_count += 1
        revised_criteria.append(criterion)

    used_ids = set(original_ids)
    for new_criterion in body.new_criteria:
        criterion_id = new_criterion.criterion_id.strip()
        if criterion_id in used_ids:
            raise HTTPException(status_code=422, detail=f"新增评分项 ID 重复：{criterion_id}")
        used_ids.add(criterion_id)
        value = new_criterion.model_dump(mode="json")
        value["criterion_id"] = criterion_id
        value["category"] = value["category"].strip()
        value["statement"] = value["statement"].strip()
        revised_criteria.append(value)
        changed_count += 1

    revised = json.loads(json.dumps(original, ensure_ascii=False))
    revised["criteria"] = revised_criteria
    revised["review"] = {
        "status": "submitted" if body.submit else "draft",
        "reviewer_count": 1,
        "adjudicated": False,
    }
    revised["revision_metadata"] = {
        "case_id": item.id,
        "review_version": next_version,
        "answer_version": answer_version,
        "reviewer_id": reviewer.id,
    }
    original_total = sum(int(criterion.get("weight") or 0) for criterion in original_criteria)
    revised_total = sum(int(criterion.get("weight") or 0) for criterion in revised_criteria)
    payload = body.model_dump(mode="json", exclude={"submit"})
    payload.update(
        {
            "original_total_weight": original_total,
            "revised_total_weight": revised_total,
            "changed_criterion_count": changed_count,
        }
    )
    return payload, revised


def collaboration_revision_json(revision: CollaborationTaskRevision) -> dict:
    return {
        "id": revision.id,
        "revision_number": revision.revision_number,
        "title": revision.title,
        "description": revision.description,
        "priority": revision.priority.value,
        "due_date": revision.due_date,
        "task_kind": revision.task_kind,
        "task_spec": normalize_task_spec(revision.task_spec),
        "participant_snapshot": revision.participant_snapshot or [],
        "attachment_snapshot": revision.attachment_snapshot or [],
        "change_summary": revision.change_summary,
        "publication_source": revision.publication_source,
        "created_by": user_json(revision.created_by),
        "created_at": revision.created_at,
    }


def collaboration_submission_json(submission: CollaborationTaskSubmission) -> dict:
    return {
        "id": submission.id,
        "version": submission.version,
        "task_revision_number": submission.task_revision_number,
        "note": submission.note,
        "submitted_by": user_json(submission.submitted_by),
        "created_at": submission.created_at,
        "files": [
            {
                "id": item.id,
                "name": item.original_name,
                "content_type": item.content_type,
                "byte_size": item.byte_size,
                "sha256": item.sha256,
                "created_at": item.created_at,
                "download_url": (
                    f"/api/collaboration-tasks/{submission.task_id}/submissions/{submission.id}/files/{item.id}"
                ),
            }
            for item in submission.files
        ],
    }


def collaboration_task_view(
    task: CollaborationTask, viewer: User
) -> tuple[CollaborationTaskParticipant | None, CollaborationTaskRevision | None, int]:
    """Resolve the immutable revision a viewer is assigned to see.

    Administrators always operate on the current revision. Regular participants
    stay on their assigned revision until a later publication explicitly moves
    them, which prevents a v2 publication from silently changing their v1 work.
    """
    participation = next((participant for participant in task.participants if participant.user_id == viewer.id), None)
    revision_number = task.current_revision or 1
    if not viewer.has_role(UserRole.ADMIN) and participation is not None:
        revision_number = participation.revision_number or 1
    revision = next(
        (item for item in task.revisions if item.revision_number == revision_number),
        None,
    )
    return participation, revision, revision_number


def collaboration_task_json(task: CollaborationTask, viewer: User) -> dict:
    active_participants = [participant for participant in task.participants if participant.is_active]
    participants = [
        {
            "user": user_json(participant.user),
            "progress": participant.progress.value,
            "revision_number": participant.revision_number,
            "updated_at": participant.updated_at,
        }
        for participant in active_participants
    ]
    my_participation, view_revision, view_revision_number = collaboration_task_view(task, viewer)
    visible_attachment_ids = (
        set(view_revision.attachment_snapshot or []) if view_revision is not None else None
    )
    title = view_revision.title if view_revision is not None else task.title
    description = view_revision.description if view_revision is not None else task.description
    priority = view_revision.priority if view_revision is not None else task.priority
    due_date = view_revision.due_date if view_revision is not None else task.due_date
    task_kind = view_revision.task_kind if view_revision is not None else task.task_kind
    task_spec = view_revision.task_spec if view_revision is not None else task.task_spec
    publication_source = view_revision.publication_source if view_revision is not None else task.publication_source
    change_summary = view_revision.change_summary if view_revision is not None else task.latest_change_summary
    return {
        "id": task.id,
        "title": title,
        "description": description,
        "priority": priority.value,
        "status": task.status.value,
        "due_date": due_date,
        "created_by": user_json(task.creator),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "participants": participants,
        "completed_participants": sum(participant.progress == TaskProgress.DONE for participant in active_participants),
        "my_progress": my_participation.progress.value if my_participation else None,
        "my_revision": my_participation.revision_number if my_participation else None,
        "my_active": my_participation.is_active if my_participation else None,
        "current_revision": task.current_revision or 1,
        "view_revision": view_revision_number,
        "task_kind": task_kind or "generic",
        "task_spec": normalize_task_spec(task_spec),
        "publication_source": publication_source or "manual",
        "latest_change_summary": change_summary or "",
        "attachments": [
            {
                "id": attachment.id,
                "name": attachment.original_name,
                "content_type": attachment.content_type,
                "byte_size": attachment.byte_size,
                "revision_number": attachment.revision_number,
                "created_at": attachment.created_at,
                "download_url": f"/api/collaboration-tasks/{task.id}/attachments/{attachment.id}",
            }
            for attachment in task.attachments
            if visible_attachment_ids is None or attachment.id in visible_attachment_ids
        ],
    }


def visible_collaboration_submissions(
    task: CollaborationTask,
    viewer: User,
    view_revision_number: int,
) -> list[CollaborationTaskSubmission]:
    eligible = [item for item in task.submissions if item.task_revision_number <= view_revision_number]
    if viewer.has_role(UserRole.ADMIN):
        return task.submissions
    return eligible


def codex_task_session_json(session: CodexTaskSession) -> dict:
    return {
        "id": session.id,
        "task_id": session.task_id,
        "mode": session.mode,
        "status": session.status,
        "draft": session.draft or {},
        "implementation_status": session.implementation_status,
        "last_error": session.last_error,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "messages": [
            {
                "id": message.id,
                "role": message.role,
                "content": message.content,
                "metadata": message.message_metadata or {},
                "created_at": message.created_at,
            }
            for message in session.messages
        ],
        "assets": [
            {
                "id": asset.id,
                "name": asset.original_name,
                "content_type": asset.content_type,
                "byte_size": asset.byte_size,
                "source_kind": asset.source_kind,
                "include_on_publish": asset.include_on_publish,
                "created_at": asset.created_at,
            }
            for asset in session.assets
        ],
    }


def get_visible_collaboration_task(db: DBSession, task_id: str, user: User) -> CollaborationTask:
    task = db.get(CollaborationTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="协作任务不存在")
    if not user.has_role(UserRole.ADMIN) and not any(participant.user_id == user.id for participant in task.participants):
        raise HTTPException(status_code=403, detail="你不是该任务的参与者")
    return task


def normalized_due_date(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="截止日期格式无效") from exc


def parsed_participant_ids(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="参与者数据格式无效") from exc
    if not isinstance(value, list) or not all(isinstance(user_id, str) for user_id in value):
        raise HTTPException(status_code=422, detail="参与者数据格式无效")
    user_ids = list(dict.fromkeys(user_id.strip() for user_id in value if user_id.strip()))
    if not user_ids:
        raise HTTPException(status_code=422, detail="请至少选择一名参与者")
    if len(user_ids) > 100:
        raise HTTPException(status_code=422, detail="单个任务最多可选择 100 名参与者")
    return user_ids


def safe_attachment_name(raw: str | None) -> str:
    name = PurePosixPath((raw or "附件").replace("\\", "/")).name.strip()
    name = "".join(character for character in name if character >= " " and character != "\x7f")
    return (name or "附件")[:255]


async def persist_task_attachment(upload: UploadFile, destination: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    byte_size = 0
    with destination.open("xb") as output:
        while chunk := await upload.read(1024 * 1024):
            byte_size += len(chunk)
            if byte_size > settings.max_task_attachment_bytes:
                limit_mb = settings.max_task_attachment_bytes // (1024 * 1024)
                raise HTTPException(status_code=413, detail=f"单个任务附件不能超过 {limit_mb} MB")
            digest.update(chunk)
            output.write(chunk)
    if byte_size == 0:
        raise HTTPException(status_code=422, detail=f"附件“{safe_attachment_name(upload.filename)}”不能为空")
    return byte_size, digest.hexdigest()


def recalculate_collaboration_task_status(task: CollaborationTask):
    progresses = [participant.progress for participant in task.participants if participant.is_active]
    if progresses and all(progress == TaskProgress.DONE for progress in progresses):
        task.status = CollaborationTaskStatus.COMPLETED
    elif any(progress != TaskProgress.TODO for progress in progresses):
        task.status = CollaborationTaskStatus.IN_PROGRESS
    else:
        task.status = CollaborationTaskStatus.PUBLISHED
    task.updated_at = datetime.now(timezone.utc)


def resolve_stored_task_file(file_path: str, *, missing_detail: str) -> Path:
    relative = PurePosixPath(file_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=500, detail="任务文件路径异常")
    root = settings.task_file_root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail=missing_detail)
    return path


def resolve_task_attachment_path(attachment: CollaborationTaskAttachment) -> Path:
    return resolve_stored_task_file(attachment.file_path, missing_detail="任务附件不存在")


def get_owned_codex_task_session(db: DBSession, session_id: str, user: User) -> CodexTaskSession:
    session = db.get(CodexTaskSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Codex 任务发布会话不存在")
    if session.created_by_id != user.id:
        raise HTTPException(status_code=403, detail="不能访问其他管理员的任务发布会话")
    return session


def initial_task_draft(task: CollaborationTask | None) -> dict:
    if task is None:
        return {
            "title": "",
            "description": "",
            "priority": TaskPriority.NORMAL.value,
            "due_date": "",
            "participant_ids": [],
            "task_kind": "generic",
            "task_spec": {"workflow_type": "collaboration", "deliverables": [], "steps": [], "fields": []},
            "requires_code_change": False,
            "implementation_summary": "",
            "change_summary": "首次发布",
        }
    return {
        "title": task.title,
        "description": task.description,
        "priority": task.priority.value,
        "due_date": task.due_date,
        "participant_ids": [participant.user_id for participant in task.participants if participant.is_active],
        "task_kind": task.task_kind or "generic",
        "task_spec": task.task_spec or {"workflow_type": "collaboration", "deliverables": [], "steps": [], "fields": []},
        "requires_code_change": False,
        "implementation_summary": "",
        "change_summary": f"从 v{task.current_revision or 1} 创建修订",
    }


def normalize_task_spec(value: object) -> dict:
    if not isinstance(value, dict):
        return {"workflow_type": "collaboration", "deliverables": [], "steps": [], "fields": []}
    workflow_type = str(value.get("workflow_type") or "collaboration")[:80]
    deliverables = [str(item)[:300] for item in value.get("deliverables", []) if isinstance(item, str)][:30]
    steps = []
    for item in value.get("steps", []):
        if isinstance(item, dict):
            steps.append({"label": str(item.get("label") or "")[:120], "description": str(item.get("description") or "")[:500]})
    fields = []
    allowed_field_types = {"text", "textarea", "number", "date", "select", "checkbox", "file"}
    for item in value.get("fields", []):
        if not isinstance(item, dict):
            continue
        field_type = str(item.get("type") or "text")
        fields.append(
            {
                "key": str(item.get("key") or f"field_{len(fields) + 1}")[:80],
                "label": str(item.get("label") or "填写项")[:120],
                "type": field_type if field_type in allowed_field_types else "text",
                "required": bool(item.get("required")),
                "options": [str(option)[:120] for option in item.get("options", []) if isinstance(option, str)][:30],
            }
        )
    return {"workflow_type": workflow_type, "deliverables": deliverables, "steps": steps[:40], "fields": fields[:60]}


def normalize_codex_draft(raw: object, db: DBSession) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail="Codex 返回的任务草稿格式无效")
    title = str(raw.get("title") or "").strip()[:200]
    description = str(raw.get("description") or "").strip()[:10000]
    priority_value = str(raw.get("priority") or TaskPriority.NORMAL.value)
    if priority_value not in {priority.value for priority in TaskPriority}:
        priority_value = TaskPriority.NORMAL.value
    due_date = normalized_due_date(str(raw.get("due_date") or "")[:10])
    requested_ids = raw.get("participant_ids") if isinstance(raw.get("participant_ids"), list) else []
    participant_ids = list(dict.fromkeys(str(user_id) for user_id in requested_ids if isinstance(user_id, str)))[:100]
    valid_ids = set(db.scalars(select(User.id).where(User.id.in_(participant_ids), User.is_active.is_(True))).all()) if participant_ids else set()
    participant_ids = [user_id for user_id in participant_ids if user_id in valid_ids]
    task_kind = "".join(character for character in str(raw.get("task_kind") or "generic") if character.isalnum() or character in "-_" )[:80] or "generic"
    return {
        "title": title,
        "description": description,
        "priority": priority_value,
        "due_date": due_date,
        "participant_ids": participant_ids,
        "task_kind": task_kind,
        "task_spec": normalize_task_spec(raw.get("task_spec")),
        "requires_code_change": bool(raw.get("requires_code_change")),
        "implementation_summary": str(raw.get("implementation_summary") or "").strip()[:4000],
        "change_summary": str(raw.get("change_summary") or "").strip()[:1000],
    }


def staged_codex_asset_path(asset: CodexTaskAsset) -> Path:
    relative = PurePosixPath(asset.file_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise HTTPException(status_code=500, detail="Codex 暂存附件路径异常")
    root = settings.task_file_root.resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail=f"暂存附件“{asset.original_name}”不存在")
    return path


def codex_asset_mentions(assets: list[CodexTaskAsset]) -> list[dict]:
    return [
        {"name": asset.original_name, "path": codex_worker_asset_path(asset)}
        for asset in assets
    ]


def codex_worker_asset_path(asset: CodexTaskAsset) -> str:
    root = PurePosixPath(settings.codex_task_file_root.as_posix())
    return (root / PurePosixPath(asset.file_path)).as_posix()


def session_uses_natural_codex_chat(session: CodexTaskSession) -> bool:
    return any(
        message.role == "assistant"
        and isinstance(message.message_metadata, dict)
        and message.message_metadata.get("protocol") == "natural-v2"
        for message in session.messages
    )


def build_codex_draft_prompt(session: CodexTaskSession, users: list[User]) -> str:
    user_catalog = [
        {"id": user.id, "name": user.display_name, "email": user.email, "roles": [role.value for role in user.assigned_roles()]}
        for user in users
        if user.is_active
    ]
    asset_catalog = [
        {
            "name": asset.original_name,
            "path": codex_worker_asset_path(asset),
            "content_type": asset.content_type,
            "byte_size": asset.byte_size,
            "source_kind": asset.source_kind,
        }
        for asset in session.assets
    ]
    conversation = [
        {"role": message.role, "content": message.content}
        for message in session.messages
    ]
    return f"""
管理员刚刚主动点击了“整理发布草稿”。请根据下面的自然对话整理一个完整的多人协作任务草稿。
模式：{session.mode}；目标任务 ID：{session.task_id or '新任务'}。
当前草稿：{json.dumps(session.draft or {}, ensure_ascii=False)}
可选参与者（participant_ids 只能使用这里的 id）：{json.dumps(user_catalog, ensure_ascii=False)}
本机会话附件：{json.dumps(asset_catalog, ensure_ascii=False)}
自然对话：{json.dumps(conversation, ensure_ascii=False)}

通用引擎可直接展示步骤、交付物和字段要求，并支持任务附件、参与者进度和留言；字段目前是要求说明，不会自动保存参与者填写值。如果任务需要可填写并持久化的控件、专用标注界面、文件解析器或处理接口，必须将 requires_code_change 设为 true，并在 implementation_summary 中具体说明增量实现内容。本轮不要修改源码。
draft 必须返回完整草稿，而不是仅返回本轮变化；不要虚构对话中没有出现的关键业务要求。
""".strip()


def build_codex_implementation_prompt(session: CodexTaskSession) -> str:
    assets = [codex_worker_asset_path(asset) for asset in session.assets]
    return f"""
管理员已确认需要为下面的任务开发专用能力。请在 /workspace 中完成最小、增量、向后兼容的实现，运行相关测试，但不要删除或覆盖任何已发布任务版本，也不要执行生产数据库的破坏性迁移。

任务草稿：{json.dumps(session.draft or {}, ensure_ascii=False)}
可检查的本机暂存附件：{json.dumps(assets, ensure_ascii=False)}

优先扩展版本化任务类型、动态字段或处理器接口。完成后汇总实际修改文件、测试结果和是否可以发布。不要声称运行了未实际运行的测试。
""".strip()


def parse_codex_response(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=502, detail="Codex 返回了无法解析的结构化结果") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=502, detail="Codex 返回格式无效")
    return parsed


def participant_moves_to_revision(participant: CollaborationTaskParticipant, scope: str) -> bool:
    if scope == "all":
        return True
    if scope == "all_incomplete":
        return participant.progress != TaskProgress.DONE
    if scope == "unstarted":
        return participant.progress == TaskProgress.TODO
    return False


def copy_session_assets_to_task(
    session: CodexTaskSession,
    task: CollaborationTask,
    revision_number: int,
    actor: User,
) -> tuple[list[CollaborationTaskAttachment], list[Path]]:
    publishable_assets = [asset for asset in session.assets if asset.include_on_publish]
    if not publishable_assets:
        return [], []
    storage_root = settings.task_file_root.resolve()
    task_dir = storage_root / task.id
    task_dir.mkdir(parents=True, exist_ok=True)
    attachments: list[CollaborationTaskAttachment] = []
    created_paths: list[Path] = []
    try:
        for asset in publishable_assets:
            attachment_id = str(uuid4())
            destination = task_dir / attachment_id
            shutil.copyfile(staged_codex_asset_path(asset), destination)
            created_paths.append(destination)
            attachments.append(
                CollaborationTaskAttachment(
                    id=attachment_id,
                    task_id=task.id,
                    uploaded_by_id=actor.id,
                    original_name=asset.original_name,
                    file_path=(PurePosixPath(task.id) / attachment_id).as_posix(),
                    content_type=asset.content_type,
                    sha256=asset.sha256,
                    byte_size=asset.byte_size,
                    revision_number=revision_number,
                )
            )
    except Exception:
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise
    return attachments, created_paths


def publish_codex_session_revision(
    db: DBSession,
    session: CodexTaskSession,
    actor: User,
    upgrade_scope: str,
) -> CollaborationTask:
    draft = normalize_codex_draft(session.draft or {}, db)
    if not draft["title"]:
        raise HTTPException(status_code=422, detail="任务草稿还没有标题")
    if not draft["participant_ids"]:
        raise HTTPException(status_code=422, detail="任务草稿还没有有效参与者")
    if draft["requires_code_change"] and session.implementation_status != "ready":
        raise HTTPException(status_code=409, detail="该任务需要专用代码，请先让 Codex 完成实现并通过检查")

    users = db.scalars(
        select(User).where(User.id.in_(draft["participant_ids"]), User.is_active.is_(True))
    ).all()
    users_by_id = {user.id: user for user in users}
    if len(users_by_id) != len(draft["participant_ids"]):
        raise HTTPException(status_code=422, detail="部分参与者已不存在或被停用")

    created_files: list[Path] = []
    is_new = session.task_id is None
    if is_new:
        task = CollaborationTask(
            id=str(uuid4()),
            title=draft["title"],
            description=draft["description"],
            priority=TaskPriority(draft["priority"]),
            status=CollaborationTaskStatus.PUBLISHED,
            due_date=draft["due_date"],
            current_revision=1,
            task_kind=draft["task_kind"],
            task_spec=draft["task_spec"],
            publication_source="codex",
            latest_change_summary=draft["change_summary"] or "首次发布",
            created_by_id=actor.id,
            creator=actor,
        )
        task.participants = [
            CollaborationTaskParticipant(
                user_id=user_id,
                user=users_by_id[user_id],
                revision_number=1,
                is_active=True,
            )
            for user_id in draft["participant_ids"]
        ]
        db.add(task)
        db.flush()
        new_attachments, created_files = copy_session_assets_to_task(session, task, 1, actor)
        task.attachments.extend(new_attachments)
        revision_number = 1
    else:
        task = db.get(CollaborationTask, session.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="要修订的任务不存在")
        revision_number = (task.current_revision or 1) + 1
        task.title = draft["title"]
        task.description = draft["description"]
        task.priority = TaskPriority(draft["priority"])
        task.due_date = draft["due_date"]
        task.current_revision = revision_number
        task.task_kind = draft["task_kind"]
        task.task_spec = draft["task_spec"]
        task.publication_source = "codex"
        task.latest_change_summary = draft["change_summary"] or f"发布 v{revision_number}"
        task.updated_at = datetime.now(timezone.utc)

        existing_by_user = {participant.user_id: participant for participant in task.participants}
        selected_ids = set(draft["participant_ids"])
        for participant in task.participants:
            participant.is_active = participant.user_id in selected_ids
            if participant.is_active and participant_moves_to_revision(participant, upgrade_scope):
                participant.revision_number = revision_number
        for user_id in draft["participant_ids"]:
            if user_id not in existing_by_user:
                task.participants.append(
                    CollaborationTaskParticipant(
                        user_id=user_id,
                        user=users_by_id[user_id],
                        revision_number=revision_number,
                        is_active=True,
                    )
                )
        new_attachments, created_files = copy_session_assets_to_task(session, task, revision_number, actor)
        task.attachments.extend(new_attachments)
        recalculate_collaboration_task_status(task)

    try:
        db.flush()
        attachment_snapshot = [attachment.id for attachment in task.attachments]
        db.add(
            CollaborationTaskRevision(
                task_id=task.id,
                revision_number=revision_number,
                title=task.title,
                description=task.description,
                priority=task.priority,
                due_date=task.due_date,
                task_kind=task.task_kind,
                task_spec=normalize_task_spec(task.task_spec),
                participant_snapshot=draft["participant_ids"],
                attachment_snapshot=attachment_snapshot,
                change_summary=task.latest_change_summary,
                publication_source="codex",
                created_by_id=actor.id,
            )
        )
        session.task_id = task.id
        session.status = "published"
        session.draft = draft
        session.last_error = ""
        session.updated_at = datetime.now(timezone.utc)
        audit(
            db,
            actor,
            "collaboration_task.publish_codex" if is_new else "collaboration_task.revise_codex",
            "collaboration_task",
            task.id,
            {
                "revision": revision_number,
                "upgrade_scope": upgrade_scope,
                "task_kind": task.task_kind,
                "participant_ids": draft["participant_ids"],
                "attachment_count": sum(asset.include_on_publish for asset in session.assets),
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        for path in created_files:
            path.unlink(missing_ok=True)
        raise
    return task


def get_visible_project_case(db: DBSession, project_case_id: str, user: User) -> ProjectCase:
    item = db.get(ProjectCase, project_case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="病例任务不存在")
    allowed = user.has_role(UserRole.ADMIN) or item.annotator_id == user.id or item.reviewer_id == user.id
    if (
        user.has_role(UserRole.REVIEWER)
        and item.status in {CaseStatus.SUBMITTED, CaseStatus.IN_REVIEW}
        and item.reviewer_id is None
        and item.annotator_id != user.id
    ):
        allowed = True
    if not allowed:
        raise HTTPException(status_code=403, detail="该病例未分配给你")
    return item


def set_no_store(response: Response):
    response.headers["Cache-Control"] = "no-store, private"


@app.get("/api/health")
def health(db: DBSession = Depends(get_db)):
    db.scalar(select(func.count(User.id)))
    return {"status": "ok", "service": settings.app_name}


@app.post("/api/auth/login")
def login(body: LoginBody, request: Request, response: Response, db: DBSession = Depends(get_db)):
    email = body.email.lower().strip()
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active or not verify_password(user.password_hash, body.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="邮箱或密码错误")
    session, raw_token = new_session(db, user, request)
    audit(db, user, "auth.login", "session", session.id)
    db.commit()
    response.set_cookie(
        "annotation_session",
        raw_token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )
    set_no_store(response)
    return {"user": user_json(user), "csrf_token": session.csrf_token}


@app.get("/api/auth/me")
def me(response: Response, ctx: AuthContext = Depends(current_context)):
    set_no_store(response)
    return {"user": user_json(ctx.user), "csrf_token": ctx.session.csrf_token}


@app.post("/api/auth/logout", status_code=204)
def logout(response: Response, ctx: AuthContext = Depends(csrf_context), db: DBSession = Depends(get_db)):
    audit(db, ctx.user, "auth.logout", "session", ctx.session.id)
    db.delete(ctx.session)
    db.commit()
    response.delete_cookie("annotation_session", path="/")
    set_no_store(response)


@app.post("/api/auth/change-password")
def change_password(body: ChangePasswordBody, ctx: AuthContext = Depends(csrf_context), db: DBSession = Depends(get_db)):
    if not verify_password(ctx.user.password_hash, body.current_password):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    ctx.user.password_hash = hash_password(body.new_password)
    ctx.user.must_change_password = False
    audit(db, ctx.user, "auth.change_password", "user", ctx.user.id)
    db.commit()
    return {"user": user_json(ctx.user)}


@app.get("/api/projects")
def projects(ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    rows = db.scalars(select(Project).where(Project.is_active.is_(True)).order_by(Project.created_at)).all()
    return [
        {
            "id": row.id,
            "name": row.name,
            "description": row.description,
            "workflow_type": row.workflow_type,
            "source_project_id": row.source_project_id,
            "labels": [{"id": label.id, "key": label.key, "name": label.name, "value": label.value, "color": label.color} for label in row.labels if label.is_active],
        }
        for row in rows
    ]


@app.get("/api/checklist-feedback/dashboard")
def checklist_feedback_dashboard(
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    query = select(ChecklistFeedbackCase.status, func.count(ChecklistFeedbackCase.id)).group_by(
        ChecklistFeedbackCase.status
    )
    access_condition = checklist_feedback_access_condition(ctx.user)
    if access_condition is not None:
        query = query.where(access_condition)
    counts = {status.value: 0 for status in ChecklistFeedbackStatus}
    counts.update({task_status.value: count for task_status, count in db.execute(query).all()})
    return {"counts": counts, "total": sum(counts.values())}


@app.get("/api/checklist-feedback")
def checklist_feedback_cases(
    task_status: ChecklistFeedbackStatus | None = Query(default=None, alias="status"),
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    filters = []
    access_condition = checklist_feedback_access_condition(ctx.user)
    if access_condition is not None:
        filters.append(access_condition)
    if task_status is not None:
        filters.append(ChecklistFeedbackCase.status == task_status)
    if search.strip():
        term = f"%{search.strip()}%"
        filters.append(
            or_(
                ChecklistFeedbackCase.id.ilike(term),
                ChecklistFeedbackCase.primary_task.ilike(term),
                ChecklistFeedbackCase.target_subtype.ilike(term),
            )
        )
    base = select(ChecklistFeedbackCase).where(*filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(ChecklistFeedbackCase.id.asc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [checklist_feedback_item_json(item, ctx.user) for item in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get("/api/checklist-feedback/{case_id}")
def checklist_feedback_detail(
    case_id: str,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    item = get_visible_checklist_feedback_case(db, case_id, ctx.user)
    return checklist_feedback_detail_json(item, ctx.user)


@app.patch("/api/admin/checklist-feedback/{case_id}/assignment")
def assign_checklist_feedback_case(
    case_id: str,
    body: ChecklistAssignmentBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    item = db.scalar(
        select(ChecklistFeedbackCase).where(ChecklistFeedbackCase.id == case_id).with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist 反馈任务不存在")
    if bool(body.answerer_id) != bool(body.reviewer_id):
        raise HTTPException(status_code=422, detail="请同时选择答题医生和 Checklist 复核医生")
    if body.answerer_id is None and body.reviewer_id is None:
        if item.answer_versions or item.review_versions:
            raise HTTPException(status_code=409, detail="任务已经开始，不能清空参与者")
        item.answerer_id = None
        item.reviewer_id = None
        item.answerer = None
        item.reviewer = None
        refresh_checklist_feedback_status(item)
    else:
        if body.answerer_id == body.reviewer_id:
            raise HTTPException(status_code=422, detail="答题医生与 Checklist 复核医生必须是两个人")
        answerer = db.get(User, body.answerer_id)
        reviewer = db.get(User, body.reviewer_id)
        if answerer is None or not answerer.is_active or not answerer.has_role(UserRole.ANNOTATOR):
            raise HTTPException(status_code=422, detail="答题医生不存在、已停用或没有初标医生角色")
        if reviewer is None or not reviewer.is_active or not reviewer.has_role(UserRole.REVIEWER):
            raise HTTPException(status_code=422, detail="复核医生不存在、已停用或没有审核医生角色")
        if item.answer_versions and item.answerer_id != answerer.id:
            raise HTTPException(status_code=409, detail="答题已经开始，不能更换答题医生")
        if item.review_versions and item.reviewer_id != reviewer.id:
            raise HTTPException(status_code=409, detail="Checklist 复核已经开始，不能更换复核医生")
        item.answerer_id = answerer.id
        item.reviewer_id = reviewer.id
        item.answerer = answerer
        item.reviewer = reviewer
        refresh_checklist_feedback_status(item)
    item.updated_at = datetime.now(timezone.utc)
    audit(
        db,
        ctx.user,
        "checklist_feedback.assign",
        "checklist_feedback_case",
        item.id,
        {"answerer_id": item.answerer_id, "reviewer_id": item.reviewer_id},
    )
    db.commit()
    return checklist_feedback_detail_json(item, ctx.user)


@app.post("/api/checklist-feedback/{case_id}/answer")
def save_checklist_feedback_answer(
    case_id: str,
    body: ChecklistAnswerBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ANNOTATOR)),
    db: DBSession = Depends(get_db),
):
    item = db.scalar(
        select(ChecklistFeedbackCase).where(ChecklistFeedbackCase.id == case_id).with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist 反馈任务不存在")
    if item.answerer_id != ctx.user.id:
        raise HTTPException(status_code=403, detail="只有本题指定的答题医生可以保存答案")
    if not item.reviewer_id:
        raise HTTPException(status_code=409, detail="请由管理员先完成双人分配")
    latest = latest_checklist_answer(item)
    if latest is not None and latest.state == ChecklistVersionState.SUBMITTED:
        raise HTTPException(status_code=409, detail="独立答案已经提交，不能再修改")

    expected_sections = [
        str(section) for section in item.case_payload.get("response_contract", {}).get("sections", []) if str(section)
    ]
    normalized_sections = {
        section: str(body.sections.get(section) or "").strip() for section in expected_sections
    }
    if body.submit:
        missing = [section for section, answer in normalized_sections.items() if not answer]
        if missing:
            raise HTTPException(status_code=422, detail=f"请完成全部答题部分：{'、'.join(missing)}")
        if body.perceived_complexity is None or body.visible_information_is_sufficient is None:
            raise HTTPException(status_code=422, detail="提交前请评估题目复杂度和信息充分性")

    payload = body.model_dump(mode="json", exclude={"submit"})
    payload["sections"] = normalized_sections
    next_version = item.latest_answer_version + 1
    state = ChecklistVersionState.SUBMITTED if body.submit else ChecklistVersionState.DRAFT
    version = ChecklistAnswerVersion(
        case_id=item.id,
        version=next_version,
        author_id=ctx.user.id,
        author=ctx.user,
        state=state,
        payload=payload,
        sha256=canonical_json_hash(payload),
    )
    item.answer_versions.append(version)
    item.latest_answer_version = next_version
    item.updated_at = datetime.now(timezone.utc)
    refresh_checklist_feedback_status(item)
    audit(
        db,
        ctx.user,
        "checklist_feedback.answer_submit" if body.submit else "checklist_feedback.answer_save",
        "checklist_feedback_case",
        item.id,
        {"version": next_version},
    )
    db.commit()
    return checklist_feedback_detail_json(item, ctx.user)


@app.post("/api/checklist-feedback/{case_id}/review")
def save_checklist_feedback_review(
    case_id: str,
    body: ChecklistReviewBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    item = db.scalar(
        select(ChecklistFeedbackCase).where(ChecklistFeedbackCase.id == case_id).with_for_update()
    )
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist 反馈任务不存在")
    if item.reviewer_id != ctx.user.id:
        raise HTTPException(status_code=403, detail="只有本题指定的复核医生可以修订 Checklist")
    submitted_answer = submitted_checklist_answer(item)
    if submitted_answer is None:
        raise HTTPException(status_code=409, detail="请等待答题医生提交独立答案")
    latest = latest_checklist_review(item)
    if latest is not None and latest.state == ChecklistVersionState.SUBMITTED:
        raise HTTPException(status_code=409, detail="Checklist 复核已经提交，不能再修改")

    next_version = item.latest_review_version + 1
    payload, revised_checklist = build_revised_checklist(
        item,
        body,
        next_version=next_version,
        answer_version=submitted_answer.version,
        reviewer=ctx.user,
    )
    state = ChecklistVersionState.SUBMITTED if body.submit else ChecklistVersionState.DRAFT
    digest_payload = {"review": payload, "revised_checklist": revised_checklist}
    version = ChecklistReviewVersion(
        case_id=item.id,
        version=next_version,
        author_id=ctx.user.id,
        author=ctx.user,
        answer_version=submitted_answer.version,
        state=state,
        payload=payload,
        revised_checklist=revised_checklist,
        sha256=canonical_json_hash(digest_payload),
    )
    item.review_versions.append(version)
    item.latest_review_version = next_version
    item.updated_at = datetime.now(timezone.utc)
    refresh_checklist_feedback_status(item)
    audit(
        db,
        ctx.user,
        "checklist_feedback.review_submit" if body.submit else "checklist_feedback.review_save",
        "checklist_feedback_case",
        item.id,
        {"version": next_version, "answer_version": submitted_answer.version},
    )
    db.commit()
    return checklist_feedback_detail_json(item, ctx.user)


@app.get("/api/dashboard")
def dashboard(project_id: str | None = None, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    query = select(ProjectCase.status, func.count(ProjectCase.id)).join(ImagingCase).where(ImagingCase.needs_identity_review.is_(False)).group_by(ProjectCase.status)
    if project_id:
        query = query.where(ProjectCase.project_id == project_id)
    access_condition = case_access_condition(ctx.user)
    if access_condition is not None:
        query = query.where(access_condition)
    counts = {status.value: count for status, count in db.execute(query).all()}
    return {"counts": counts, "total": sum(counts.values())}


@app.get("/api/cases")
def cases(
    project_id: str | None = None,
    case_status: CaseStatus | None = Query(default=None, alias="status"),
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    filters = [ImagingCase.needs_identity_review.is_(False)]
    if project_id:
        filters.append(ProjectCase.project_id == project_id)
    if case_status:
        filters.append(ProjectCase.status == case_status)
    access_condition = case_access_condition(ctx.user)
    if access_condition is not None:
        filters.append(access_condition)
    if search.strip():
        term = f"%{search.strip()}%"
        filters.append(or_(ImagingCase.patient_id.ilike(term), ImagingCase.patient_name_zh.ilike(term), ImagingCase.case_code.ilike(term)))

    base = select(ProjectCase).join(ImagingCase).where(*filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(ProjectCase.updated_at.desc(), ProjectCase.id.asc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [project_case_json(row) for row in rows], "total": total, "page": page, "page_size": page_size}


@app.get("/api/cases/{project_case_id}")
def case_detail(project_case_id: str, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    result = project_case_json(item)
    result["labels"] = [
        {"key": label.key, "name": label.name, "value": label.value, "color": label.color}
        for label in db.scalars(select(Label).where(Label.project_id == item.project_id, Label.is_active.is_(True))).all()
    ]
    version_model = annotation_model_for_item(item)
    latest = db.scalar(
        select(version_model).where(version_model.project_case_id == item.id).order_by(version_model.version.desc()).limit(1)
    )
    result["latest_annotation"] = annotation_version_json(latest)
    submitted = (
        db.scalar(
            select(version_model).where(
                version_model.project_case_id == item.id,
                version_model.version == item.submitted_version,
            )
        )
        if item.submitted_version is not None
        else None
    )
    result["submitted_annotation"] = annotation_version_json(submitted)
    reviews = db.scalars(select(ReviewEvent).where(ReviewEvent.project_case_id == item.id).order_by(ReviewEvent.created_at.desc())).all()
    result["review_history"] = [
        {"action": event.action.value, "version": event.annotation_version, "comment": event.comment, "created_at": event.created_at}
        for event in reviews
    ]
    return result


def resolve_data_file(item: ProjectCase, sequence: str) -> Path:
    relative = item.case.sequence_paths.get(sequence.lower())
    if not relative:
        raise HTTPException(status_code=404, detail="序列不存在")
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise HTTPException(status_code=500, detail="数据路径异常")
    path = (settings.data_root / relative_path).resolve()
    root = settings.data_root.resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="影像文件尚未同步完成")
    return path


def volume_file_response(path: Path, filename: str, if_none_match: str | None = None):
    stat_result = path.stat()
    cache_headers = {
        "Cache-Control": "private, max-age=86400, stale-while-revalidate=3600, no-transform",
        "Vary": "Cookie",
    }
    response = FileResponse(
        path,
        media_type="application/gzip",
        filename=filename,
        headers=cache_headers,
        stat_result=stat_result,
    )
    etag = response.headers.get("etag")
    request_etags = {tag.strip() for tag in if_none_match.split(",")} if if_none_match else set()
    if etag and (etag in request_etags or "*" in request_etags):
        return Response(
            status_code=304,
            headers={
                **cache_headers,
                "ETag": etag,
                "Last-Modified": response.headers["last-modified"],
            },
        )
    return response


@app.get("/api/cases/{project_case_id}/volume/{sequence}.nii.gz")
def volume(project_case_id: str, sequence: Literal["opp", "in", "f", "w"], request: Request, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    path = resolve_data_file(item, sequence)
    return volume_file_response(path, f"{item.case.case_code}_{sequence}.nii.gz", request.headers.get("if-none-match"))


@app.get("/api/cases/{project_case_id}/display/{sequence}.nii.gz")
def display_volume(project_case_id: str, sequence: Literal["opp", "in", "f", "w"], request: Request, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    source = resolve_data_file(item, sequence)
    try:
        path = ensure_display_volume(source, settings.data_root, settings.preview_root)
    except Exception as reason:
        raise HTTPException(status_code=500, detail="优化显示影像生成失败") from reason
    return volume_file_response(path, f"{item.case.case_code}_{sequence}_display.nii.gz", request.headers.get("if-none-match"))


def _pancreas_reference_for_item(item: ProjectCase):
    if item.project.workflow_type != "pdff_roi":
        raise HTTPException(status_code=409, detail="胰腺参考轮廓仅用于 PDFF ROI 任务")
    return resolve_pancreas_reference(
        settings.pancreas_prediction_root,
        item.case.patient_id,
        resolve_data_file(item, "w"),
    )


@app.get("/api/cases/{project_case_id}/pancreas-reference")
def pancreas_reference_info(
    project_case_id: str,
    response: Response,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    resolution = _pancreas_reference_for_item(item)
    set_no_store(response)
    return {
        "available": resolution.available,
        "status": resolution.status,
        "model": resolution.prediction.model_method_id if resolution.prediction else None,
        "reference_sequence": "w",
        "slice_count": resolution.slice_count,
        "reason": resolution.reason,
        "warning": "AI 胰腺参考轮廓仅用于辅助定位，不限制 PDFF ROI 的放置范围",
    }


@app.get("/api/cases/{project_case_id}/pancreas-reference/contours")
def pancreas_reference_contours(
    project_case_id: str,
    response: Response,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    resolution = _pancreas_reference_for_item(item)
    contours = ()
    if resolution.available and resolution.prediction is not None:
        try:
            contours = pancreas_volume_segments(resolution.prediction.prediction_path)
        except (OSError, ValueError, IndexError) as reason:
            raise HTTPException(status_code=500, detail="胰腺参考轮廓读取失败") from reason
    set_no_store(response)
    return {
        "available": resolution.available,
        "status": resolution.status,
        "model": resolution.prediction.model_method_id if resolution.prediction else None,
        "reference_sequence": "w",
        "slice_count": resolution.slice_count,
        "reason": resolution.reason,
        "warning": "AI 胰腺参考轮廓仅用于辅助定位，不限制 PDFF ROI 的放置范围",
        "contours": [
            {"slice_index": slice_index, "segments": segments}
            for slice_index, segments in contours
        ],
        "nonempty_slice_count": len(contours),
        "segment_count": sum(len(segments) for _, segments in contours),
    }


@app.get("/api/cases/{project_case_id}/pancreas-reference/slices/{slice_index}")
def pancreas_reference_slice(
    project_case_id: str,
    slice_index: int,
    response: Response,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    resolution = _pancreas_reference_for_item(item)
    if not resolution.available or resolution.prediction is None:
        raise HTTPException(status_code=404, detail=resolution.reason or "本病例没有可用的胰腺参考分割")
    if slice_index < 0 or resolution.slice_count is None or slice_index >= resolution.slice_count:
        raise HTTPException(status_code=422, detail="切片索引超出胰腺参考分割范围")
    try:
        segments = pancreas_slice_segments(resolution.prediction.prediction_path, slice_index)
    except (OSError, ValueError, IndexError) as reason:
        raise HTTPException(status_code=500, detail="胰腺参考轮廓读取失败") from reason
    set_no_store(response)
    return {
        "available": True,
        "slice_index": slice_index,
        "segments": segments,
        "segment_count": len(segments),
        "has_contour": bool(segments),
    }


@app.get("/api/cases/{project_case_id}/preview/{sequence}.nii.gz")
def volume_preview(project_case_id: str, sequence: Literal["opp", "in", "f", "w"], request: Request, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    source = resolve_data_file(item, sequence)
    try:
        path = ensure_preview(source, settings.data_root, settings.preview_root)
    except Exception as reason:
        raise HTTPException(status_code=500, detail="快速预览生成失败") from reason
    return volume_file_response(path, f"{item.case.case_code}_{sequence}_preview.nii.gz", request.headers.get("if-none-match"))


@app.get("/api/cases/{project_case_id}/annotation.nii.gz")
def annotation_file(
    project_case_id: str,
    version: int | None = Query(default=None, ge=1),
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "segmentation":
        raise HTTPException(status_code=409, detail="该任务使用 ROI 数据版本，不存在分割掩膜文件")
    query = select(AnnotationVersion).where(AnnotationVersion.project_case_id == item.id)
    if version is None:
        annotation = db.scalar(query.order_by(AnnotationVersion.version.desc()).limit(1))
    else:
        annotation = db.scalar(query.where(AnnotationVersion.version == version))
    if annotation is None:
        raise HTTPException(status_code=404, detail="尚无标注")
    path = settings.annotation_root / annotation.file_path
    if not path.is_file():
        raise HTTPException(status_code=500, detail="标注文件缺失")
    return FileResponse(path, media_type="application/gzip", filename=f"{item.case.case_code}_v{annotation.version}.nii.gz", headers={"Cache-Control": "no-store, private"})


def validate_mask(mask_path: Path, reference_path: Path, allowed_values: set[int]):
    try:
        mask = nib.load(mask_path)
        reference = nib.load(reference_path)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="标注文件不是有效的 NIfTI") from exc
    if len(mask.shape) != 3 or tuple(mask.shape) != tuple(reference.shape[:3]):
        raise HTTPException(status_code=422, detail="标注尺寸与原图不一致")
    if not np.allclose(mask.affine, reference.affine, atol=1e-3):
        raise HTTPException(status_code=422, detail="标注空间坐标与原图不一致")
    values = set(int(value) for value in np.unique(np.asanyarray(mask.dataobj)))
    if not values.issubset({0, *allowed_values}):
        raise HTTPException(status_code=422, detail=f"标注包含项目未定义的标签值: {sorted(values - {0, *allowed_values})}")


EDITABLE_CASE_STATUSES = {CaseStatus.ASSIGNED, CaseStatus.ANNOTATING, CaseStatus.RETURNED}
PENDING_REVIEW_CASE_STATUSES = {CaseStatus.SUBMITTED, CaseStatus.IN_REVIEW}


def validate_submission_candidate(item: ProjectCase, latest: AnnotationVersion | PdffAnnotationVersion | None):
    if item.status not in {CaseStatus.ANNOTATING, CaseStatus.RETURNED}:
        raise HTTPException(status_code=409, detail="当前阶段不可提交标注")
    if latest is None:
        raise HTTPException(status_code=409, detail="请先保存标注")
    if latest.state != AnnotationState.DRAFT:
        if item.status == CaseStatus.RETURNED:
            raise HTTPException(status_code=409, detail="退回病例必须修改上一次标注并保存为新版本后才能重新提交")
        raise HTTPException(status_code=409, detail="当前没有可提交的草稿版本")
    if item.status == CaseStatus.RETURNED and (item.submitted_version is None or latest.version <= item.submitted_version):
        raise HTTPException(status_code=409, detail="退回病例必须修改上一次标注并保存为新版本后才能重新提交")
    if isinstance(latest, PdffAnnotationVersion):
        planes = latest.roi_payload or []
        valid_planes = (
            len(planes) == 3
            and all(isinstance(plane, dict) and len(plane.get("rois") or []) == 3 for plane in planes)
            and {plane.get("plane_index") for plane in planes} == {1, 2, 3}
            and len({plane.get("slice_index") for plane in planes}) == 3
        )
        if not valid_planes:
            raise HTTPException(status_code=409, detail="请先完成并保存三个不同轴位层面的 PDFF 测量")


def resolve_review_transition(item: ProjectCase, action: ReviewAction, latest: AnnotationVersion | None = None) -> tuple[int, bool]:
    if item.status in PENDING_REVIEW_CASE_STATUSES:
        if item.submitted_version is None:
            raise HTTPException(status_code=409, detail="待审核版本不存在")
        if latest is not None and latest.version > item.submitted_version:
            if latest.state != AnnotationState.DRAFT or latest.kind != "review_revision" or latest.source_version != item.submitted_version:
                raise HTTPException(status_code=409, detail="审核微调版本状态异常")
            return latest.version, False
        return item.submitted_version, False
    if item.status == CaseStatus.APPROVED and action == ReviewAction.RETURN:
        if item.approved_version is None:
            raise HTTPException(status_code=409, detail="已通过版本不存在")
        return item.approved_version, True
    if item.status == CaseStatus.APPROVED:
        raise HTTPException(status_code=409, detail="已通过病例只能撤回后重新审核")
    raise HTTPException(status_code=409, detail="该病例当前不在待审核状态")


def validate_independent_reviewer(
    item: ProjectCase,
    reviewer_id: str,
    version: AnnotationVersion | PdffAnnotationVersion | None = None,
    *,
    has_authored_case: bool = False,
):
    if has_authored_case or item.annotator_id == reviewer_id or (version is not None and version.author_id == reviewer_id):
        raise HTTPException(status_code=403, detail="本人参与初标的病例必须由其他审核医生审核")


def validate_review_revision_candidate(item: ProjectCase):
    if item.status not in PENDING_REVIEW_CASE_STATUSES:
        raise HTTPException(status_code=409, detail="只有待审核病例可以由审核医生直接修改")
    if item.submitted_version is None:
        raise HTTPException(status_code=409, detail="待审核版本不存在")


def submitted_annotation_for_review(db: DBSession, item: ProjectCase) -> AnnotationVersion | PdffAnnotationVersion:
    if item.submitted_version is None:
        raise HTTPException(status_code=409, detail="待审核版本不存在")
    version_model = annotation_model_for_item(item)
    version = db.scalar(
        select(version_model).where(
            version_model.project_case_id == item.id,
            version_model.version == item.submitted_version,
        )
    )
    if version is None:
        raise HTTPException(status_code=409, detail="待审核版本不存在")
    return version


def validate_and_claim_reviewer(db: DBSession, item: ProjectCase, reviewer: User) -> AnnotationVersion | PdffAnnotationVersion:
    submitted = submitted_annotation_for_review(db, item)
    version_model = annotation_model_for_item(item)
    has_authored_submission = bool(
        db.scalar(
            select(func.count(version_model.id)).where(
                version_model.project_case_id == item.id,
                version_model.author_id == reviewer.id,
                version_model.version <= item.submitted_version,
                version_model.kind == "annotation",
            )
        )
    )
    validate_independent_reviewer(item, reviewer.id, submitted, has_authored_case=has_authored_submission)
    if item.reviewer_id is None:
        item.reviewer_id = reviewer.id
    elif item.reviewer_id != reviewer.id and not reviewer.has_role(UserRole.ADMIN):
        raise HTTPException(status_code=403, detail="该病例已由其他审核医生领取")
    return submitted


async def persist_annotation_version(
    db: DBSession,
    item: ProjectCase,
    mask: UploadFile,
    author: User,
    comment: str,
    *,
    kind: str = "annotation",
    source_version: int | None = None,
) -> AnnotationVersion:
    case_dir = settings.annotation_root / item.id
    case_dir.mkdir(parents=True, exist_ok=True)
    temp_path = case_dir / f".upload-{os.urandom(8).hex()}.nii.gz"
    digest = hashlib.sha256()
    byte_size = 0
    try:
        with temp_path.open("wb") as output:
            while chunk := await mask.read(1024 * 1024):
                byte_size += len(chunk)
                if byte_size > settings.max_annotation_bytes:
                    raise HTTPException(status_code=413, detail="标注文件过大")
                digest.update(chunk)
                output.write(chunk)
        allowed_values = set(db.scalars(select(Label.value).where(Label.project_id == item.project_id, Label.is_active.is_(True))).all())
        validate_mask(temp_path, resolve_data_file(item, "w"), allowed_values)
        next_version = item.latest_version + 1
        relative_path = Path(item.id) / f"v{next_version:04d}.nii.gz"
        final_path = settings.annotation_root / relative_path
        temp_path.replace(final_path)

        previous = db.scalars(
            select(AnnotationVersion).where(
                AnnotationVersion.project_case_id == item.id,
                AnnotationVersion.state == AnnotationState.DRAFT,
            )
        ).all()
        for old in previous:
            old.state = AnnotationState.SUPERSEDED
        version = AnnotationVersion(
            project_case_id=item.id,
            version=next_version,
            author_id=author.id,
            kind=kind,
            source_version=source_version,
            state=AnnotationState.DRAFT,
            file_path=relative_path.as_posix(),
            sha256=digest.hexdigest(),
            byte_size=byte_size,
            comment=comment[:4000],
        )
        db.add(version)
        item.latest_version = next_version
        return version
    finally:
        if temp_path.exists():
            temp_path.unlink()


PDFF_REGION_LABELS = {"head": "胰头", "body": "胰体", "tail": "胰尾"}


def _numeric_stats(values: np.ndarray) -> dict[str, float]:
    return {
        "mean": round(float(np.mean(values)), 4),
        "sd": round(float(np.std(values)), 4),
        "min": round(float(np.min(values)), 4),
        "max": round(float(np.max(values)), 4),
    }


def _sample_stats(values: list[float]) -> dict[str, float | int | None]:
    finite = np.asarray([value for value in values if np.isfinite(value)], dtype=np.float64)
    if finite.size == 0:
        return {"count": 0, "mean": None, "sd": None, "cv_percent": None, "min": None, "max": None, "range": None}
    mean = float(np.mean(finite))
    sd = float(np.std(finite, ddof=1)) if finite.size > 1 else 0.0
    return {
        "count": int(finite.size),
        "mean": round(mean, 4),
        "sd": round(sd, 4),
        "cv_percent": round(abs(sd / mean) * 100.0, 4) if abs(mean) > 1e-6 else None,
        "min": round(float(np.min(finite)), 4),
        "max": round(float(np.max(finite)), 4),
        "range": round(float(np.max(finite) - np.min(finite)), 4),
    }


def calculate_pdff_roi_metrics(
    fat_path: Path,
    water_path: Path,
    in_phase_path: Path,
    opposed_phase_path: Path,
    rois: list[PdffRoiBody],
    *,
    require_complete: bool = True,
) -> tuple[list[dict], list[dict]]:
    """Validate three co-planar ROIs and measure all four co-registered Dixon phases."""
    try:
        fat_image = nib.as_closest_canonical(nib.load(fat_path))
        water_image = nib.as_closest_canonical(nib.load(water_path))
        in_phase_image = nib.as_closest_canonical(nib.load(in_phase_path))
        opposed_phase_image = nib.as_closest_canonical(nib.load(opposed_phase_path))
        fat_data = np.asarray(fat_image.get_fdata(dtype=np.float32))
        water_data = np.asarray(water_image.get_fdata(dtype=np.float32))
        in_phase_data = np.asarray(in_phase_image.get_fdata(dtype=np.float32))
        opposed_phase_data = np.asarray(opposed_phase_image.get_fdata(dtype=np.float32))
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Dixon 四相位影像无法完整读取") from exc

    paired_images = (
        ("水相", water_image, water_data),
        ("同相位", in_phase_image, in_phase_data),
        ("反相位", opposed_phase_image, opposed_phase_data),
    )
    if fat_data.ndim != 3:
        raise HTTPException(status_code=422, detail="脂相影像不是有效的三维影像")
    for label, image, data in paired_images:
        if data.ndim != 3 or data.shape != fat_data.shape:
            raise HTTPException(status_code=422, detail=f"{label}与脂相影像尺寸不一致，无法进行四相位配对 ROI 测量")
        if not np.allclose(fat_image.affine, image.affine, atol=1e-3):
            raise HTTPException(status_code=422, detail=f"{label}与脂相空间坐标不一致，无法进行四相位配对 ROI 测量")

    regions = [roi.region for roi in rois]
    if not regions or len(regions) != len(set(regions)) or any(region not in PDFF_REGION_LABELS for region in regions):
        raise HTTPException(status_code=422, detail="每个部位只能放置一个 ROI")
    if require_complete and set(regions) != set(PDFF_REGION_LABELS):
        raise HTTPException(status_code=422, detail="必须分别放置胰头、胰体、胰尾三个 ROI")

    spacing = tuple(float(value) for value in fat_image.header.get_zooms()[:3])
    if any(not np.isfinite(value) or value <= 0 for value in spacing):
        raise HTTPException(status_code=422, detail="影像体素间距无效")
    shape = fat_data.shape
    normalized: list[dict] = []
    slice_indices: set[int] = set()
    for roi in rois:
        center = tuple(float(value) for value in roi.center_voxel)
        radius = float(roi.radius_mm)
        if any(not np.isfinite(value) for value in (*center, radius)):
            raise HTTPException(status_code=422, detail="ROI 坐标或半径无效")
        cx, cy, cz = center
        z_index = int(round(cz))
        if not (0 <= cx < shape[0] and 0 <= cy < shape[1] and 0 <= z_index < shape[2]):
            raise HTTPException(status_code=422, detail=f"{PDFF_REGION_LABELS[roi.region]} ROI 超出影像范围")
        if cx * spacing[0] < radius or (shape[0] - 1 - cx) * spacing[0] < radius or cy * spacing[1] < radius or (shape[1] - 1 - cy) * spacing[1] < radius:
            raise HTTPException(status_code=422, detail=f"{PDFF_REGION_LABELS[roi.region]} ROI 太靠近影像边缘")
        slice_indices.add(z_index)
        normalized.append({
            "region": roi.region,
            "center_voxel": [round(cx, 4), round(cy, 4), float(z_index)],
            "radius_mm": round(radius, 2),
        })
    if len(slice_indices) != 1:
        raise HTTPException(status_code=422, detail="胰头、胰体、胰尾三个 ROI 必须位于同一轴位层面")

    for index, left in enumerate(normalized):
        for right in normalized[index + 1:]:
            dx_mm = (left["center_voxel"][0] - right["center_voxel"][0]) * spacing[0]
            dy_mm = (left["center_voxel"][1] - right["center_voxel"][1]) * spacing[1]
            if np.hypot(dx_mm, dy_mm) < left["radius_mm"] + right["radius_mm"]:
                raise HTTPException(status_code=422, detail="三个 ROI 不能相互重叠")

    metrics: list[dict] = []
    for roi in normalized:
        cx, cy, z_value = roi["center_voxel"]
        z_index = int(z_value)
        radius = roi["radius_mm"]
        x_radius = radius / spacing[0]
        y_radius = radius / spacing[1]
        x_indices = np.arange(max(0, int(np.floor(cx - x_radius))), min(shape[0] - 1, int(np.ceil(cx + x_radius))) + 1)
        y_indices = np.arange(max(0, int(np.floor(cy - y_radius))), min(shape[1] - 1, int(np.ceil(cy + y_radius))) + 1)
        x_grid, y_grid = np.meshgrid(x_indices, y_indices, indexing="ij")
        disk = ((x_grid - cx) * spacing[0]) ** 2 + ((y_grid - cy) * spacing[1]) ** 2 <= radius ** 2
        fat_values = fat_data[x_grid[disk], y_grid[disk], z_index].astype(np.float64, copy=False)
        water_values = water_data[x_grid[disk], y_grid[disk], z_index].astype(np.float64, copy=False)
        in_phase_values = in_phase_data[x_grid[disk], y_grid[disk], z_index].astype(np.float64, copy=False)
        opposed_phase_values = opposed_phase_data[x_grid[disk], y_grid[disk], z_index].astype(np.float64, copy=False)
        finite = (
            np.isfinite(fat_values)
            & np.isfinite(water_values)
            & np.isfinite(in_phase_values)
            & np.isfinite(opposed_phase_values)
        )
        fat_values = fat_values[finite]
        water_values = water_values[finite]
        in_phase_values = in_phase_values[finite]
        opposed_phase_values = opposed_phase_values[finite]
        if fat_values.size < 3:
            raise HTTPException(status_code=422, detail=f"{PDFF_REGION_LABELS[roi['region']]} ROI 内有效体素不足")
        denominator = fat_values + water_values
        valid_ratio = np.isfinite(denominator) & (np.abs(denominator) > 1e-6)
        pdff_values = fat_values[valid_ratio] / denominator[valid_ratio] * 100.0
        if pdff_values.size < 3:
            raise HTTPException(status_code=422, detail=f"{PDFF_REGION_LABELS[roi['region']]} ROI 无法计算 PDFF")
        mean_denominator = float(np.mean(fat_values) + np.mean(water_values))
        ratio_of_means = float(np.mean(fat_values) / mean_denominator * 100.0) if abs(mean_denominator) > 1e-6 else float(np.mean(pdff_values))

        # Under the magnitude two-point Dixon water-dominant assumption:
        # In = |Water + Fat| and Opposed = |Water - Fat|, therefore the
        # signal fat fraction estimate is |In - Opposed| / (2 * |In|).
        valid_in_opposed = np.abs(in_phase_values) > 1e-6
        pdff_in_opposed_values = (
            np.abs(in_phase_values[valid_in_opposed] - opposed_phase_values[valid_in_opposed])
            / (2.0 * np.abs(in_phase_values[valid_in_opposed]))
            * 100.0
        )
        pdff_in_opposed_values = pdff_in_opposed_values[np.isfinite(pdff_in_opposed_values)]
        if pdff_in_opposed_values.size < 3:
            raise HTTPException(status_code=422, detail=f"{PDFF_REGION_LABELS[roi['region']]} ROI 同相位信号不足，无法计算 In/Opposed PDFF")
        mean_in_phase = float(np.mean(in_phase_values))
        in_opposed_ratio_of_means = (
            float(abs(mean_in_phase - np.mean(opposed_phase_values)) / (2.0 * abs(mean_in_phase)) * 100.0)
            if abs(mean_in_phase) > 1e-6
            else float(np.mean(pdff_in_opposed_values))
        )
        metrics.append({
            "region": roi["region"],
            "label": PDFF_REGION_LABELS[roi["region"]],
            "slice_index": z_index,
            "radius_mm": radius,
            "area_mm2": round(float(fat_values.size * spacing[0] * spacing[1]), 3),
            "voxel_count": int(fat_values.size),
            "fat": _numeric_stats(fat_values),
            "water": _numeric_stats(water_values),
            "in_phase": _numeric_stats(in_phase_values),
            "opposed_phase": _numeric_stats(opposed_phase_values),
            "pdff": _numeric_stats(pdff_values),
            "pdff_ratio_of_means": round(ratio_of_means, 4),
            "pdff_voxel_count": int(pdff_values.size),
            "pdff_in_opposed": _numeric_stats(pdff_in_opposed_values),
            "pdff_in_opposed_ratio_of_means": round(in_opposed_ratio_of_means, 4),
            "pdff_in_opposed_voxel_count": int(pdff_in_opposed_values.size),
        })
    metrics.sort(key=lambda value: ["head", "body", "tail"].index(value["region"]))
    normalized.sort(key=lambda value: ["head", "body", "tail"].index(value["region"]))
    return normalized, metrics


def calculate_pdff_plane_measurements(
    fat_path: Path,
    water_path: Path,
    in_phase_path: Path,
    opposed_phase_path: Path,
    planes: list[PdffPlaneBody],
) -> tuple[list[dict], dict]:
    """Measure up to three distinct axial planes and derive case-level repeatability summaries."""
    plane_indices = [plane.plane_index for plane in planes]
    if not planes or len(plane_indices) != len(set(plane_indices)) or any(index not in {1, 2, 3} for index in plane_indices):
        raise HTTPException(status_code=422, detail="测量平面编号必须为 1、2、3，且不能重复")

    normalized_planes: list[dict] = []
    slice_indices: list[int] = []
    for plane in sorted(planes, key=lambda value: value.plane_index):
        normalized_rois, metrics = calculate_pdff_roi_metrics(
            fat_path,
            water_path,
            in_phase_path,
            opposed_phase_path,
            plane.rois,
        )
        slice_index = int(metrics[0]["slice_index"])
        slice_indices.append(slice_index)
        fat_water_values = [float(metric["pdff_ratio_of_means"]) for metric in metrics]
        in_opposed_values = [float(metric["pdff_in_opposed_ratio_of_means"]) for metric in metrics]
        normalized_planes.append({
            "plane_index": plane.plane_index,
            "slice_index": slice_index,
            "rois": normalized_rois,
            "metrics": metrics,
            "summary": {
                "fat_water_mean": round(float(np.mean(fat_water_values)), 4),
                "fat_water_sd": round(float(np.std(fat_water_values, ddof=1)), 4),
                "in_opposed_mean": round(float(np.mean(in_opposed_values)), 4),
                "in_opposed_sd": round(float(np.std(in_opposed_values, ddof=1)), 4),
                "total_area_mm2": round(float(sum(metric["area_mm2"] for metric in metrics)), 3),
                "total_voxel_count": int(sum(metric["voxel_count"] for metric in metrics)),
            },
        })

    if len(set(slice_indices)) != len(slice_indices):
        raise HTTPException(status_code=422, detail="不同测量平面必须位于不同的轴位层面")

    fat_water_plane_means = [float(plane["summary"]["fat_water_mean"]) for plane in normalized_planes]
    in_opposed_plane_means = [float(plane["summary"]["in_opposed_mean"]) for plane in normalized_planes]
    region_summaries: dict[str, dict] = {}
    for region in ("head", "body", "tail"):
        region_metrics = [
            next(metric for metric in plane["metrics"] if metric["region"] == region)
            for plane in normalized_planes
        ]
        region_summaries[region] = {
            "fat_water": _sample_stats([float(metric["pdff_ratio_of_means"]) for metric in region_metrics]),
            "in_opposed": _sample_stats([float(metric["pdff_in_opposed_ratio_of_means"]) for metric in region_metrics]),
        }

    case_summary = {
        "plane_count": len(normalized_planes),
        "roi_count": len(normalized_planes) * 3,
        "slice_indices": slice_indices,
        "fat_water": _sample_stats(fat_water_plane_means),
        "in_opposed": _sample_stats(in_opposed_plane_means),
        "regions": region_summaries,
        "total_area_mm2": round(float(sum(plane["summary"]["total_area_mm2"] for plane in normalized_planes)), 3),
        "total_voxel_count": int(sum(plane["summary"]["total_voxel_count"] for plane in normalized_planes)),
    }
    return normalized_planes, case_summary


def persist_pdff_annotation_version(
    db: DBSession,
    item: ProjectCase,
    body: PdffAnnotationBody,
    author: User,
    *,
    kind: str = "annotation",
    source_version: int | None = None,
) -> PdffAnnotationVersion:
    normalized_planes, case_summary = calculate_pdff_plane_measurements(
        resolve_data_file(item, "f"),
        resolve_data_file(item, "w"),
        resolve_data_file(item, "in"),
        resolve_data_file(item, "opp"),
        body.planes,
    )
    digest_payload = json.dumps(
        {"planes": normalized_planes, "case_summary": case_summary},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    previous = db.scalars(
        select(PdffAnnotationVersion).where(
            PdffAnnotationVersion.project_case_id == item.id,
            PdffAnnotationVersion.state == AnnotationState.DRAFT,
        )
    ).all()
    for old in previous:
        old.state = AnnotationState.SUPERSEDED
    version = PdffAnnotationVersion(
        project_case_id=item.id,
        version=item.latest_version + 1,
        author_id=author.id,
        kind=kind,
        source_version=source_version,
        state=AnnotationState.DRAFT,
        roi_payload=normalized_planes,
        metrics={"case_summary": case_summary},
        sha256=hashlib.sha256(digest_payload).hexdigest(),
        comment=body.comment.strip()[:4000],
    )
    db.add(version)
    item.latest_version = version.version
    return version


@app.post("/api/cases/{project_case_id}/annotation")
async def save_annotation(
    project_case_id: str,
    mask: UploadFile = File(...),
    comment: str = Form(default=""),
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR)),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "segmentation":
        raise HTTPException(status_code=409, detail="该任务请使用 PDFF ROI 标注接口")
    if not ctx.user.has_role(UserRole.ADMIN) and item.annotator_id != ctx.user.id:
        raise HTTPException(status_code=403, detail="该病例未分配给你")
    if item.status not in EDITABLE_CASE_STATUSES:
        raise HTTPException(status_code=409, detail="当前阶段不可修改标注；待审核版本需先由审核医生退回")

    version = await persist_annotation_version(db, item, mask, ctx.user, comment)
    if item.status == CaseStatus.ASSIGNED:
        item.status = CaseStatus.ANNOTATING
    audit(db, ctx.user, "annotation.save", "project_case", item.id, {"version": version.version, "sha256": version.sha256})
    db.commit()
    return {"version": version.version, "state": version.state.value, "sha256": version.sha256}


@app.post("/api/cases/{project_case_id}/review-revision")
async def save_review_revision(
    project_case_id: str,
    mask: UploadFile = File(...),
    comment: str = Form(default=""),
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "segmentation":
        raise HTTPException(status_code=409, detail="该任务请使用 PDFF ROI 审核修订接口")
    validate_review_revision_candidate(item)
    submitted = validate_and_claim_reviewer(db, item, ctx.user)
    revision_comment = comment.strip() or f"审核微调，基于提交版本 v{submitted.version}"
    version = await persist_annotation_version(
        db,
        item,
        mask,
        ctx.user,
        revision_comment,
        kind="review_revision",
        source_version=submitted.version,
    )
    item.status = CaseStatus.IN_REVIEW
    audit(
        db,
        ctx.user,
        "review.revision.save",
        "project_case",
        item.id,
        {"version": version.version, "base_version": submitted.version, "sha256": version.sha256},
    )
    db.commit()
    return {"version": version.version, "base_version": submitted.version, "state": version.state.value, "sha256": version.sha256}


@app.post("/api/cases/{project_case_id}/pdff-annotation")
def save_pdff_annotation(
    project_case_id: str,
    body: PdffAnnotationBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR)),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "pdff_roi":
        raise HTTPException(status_code=409, detail="该任务不是 PDFF ROI 工作流")
    if not ctx.user.has_role(UserRole.ADMIN) and item.annotator_id != ctx.user.id:
        raise HTTPException(status_code=403, detail="该病例未分配给你")
    if item.status not in EDITABLE_CASE_STATUSES:
        raise HTTPException(status_code=409, detail="当前阶段不可修改 ROI；待审核版本需先由审核医生退回")
    version = persist_pdff_annotation_version(db, item, body, ctx.user)
    if item.status == CaseStatus.ASSIGNED:
        item.status = CaseStatus.ANNOTATING
    audit(db, ctx.user, "pdff_roi.save", "project_case", item.id, {"version": version.version, "sha256": version.sha256})
    db.commit()
    return annotation_version_json(version)


@app.post("/api/cases/{project_case_id}/pdff-preview")
def preview_pdff_annotation(
    project_case_id: str,
    body: PdffPreviewBody,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    """Calculate live ROI statistics without creating or changing an annotation version."""
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "pdff_roi":
        raise HTTPException(status_code=409, detail="该任务不是 PDFF ROI 工作流")
    normalized_rois, metrics = calculate_pdff_roi_metrics(
        resolve_data_file(item, "f"),
        resolve_data_file(item, "w"),
        resolve_data_file(item, "in"),
        resolve_data_file(item, "opp"),
        body.rois,
        require_complete=False,
    )
    return {"rois": normalized_rois, "metrics": metrics}


@app.post("/api/cases/{project_case_id}/pdff-review-revision")
def save_pdff_review_revision(
    project_case_id: str,
    body: PdffAnnotationBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if item.project.workflow_type != "pdff_roi":
        raise HTTPException(status_code=409, detail="该任务不是 PDFF ROI 工作流")
    validate_review_revision_candidate(item)
    submitted = validate_and_claim_reviewer(db, item, ctx.user)
    revision_body = body.model_copy(update={
        "comment": body.comment.strip() or f"审核微调，基于提交版本 v{submitted.version}",
    })
    version = persist_pdff_annotation_version(
        db,
        item,
        revision_body,
        ctx.user,
        kind="review_revision",
        source_version=submitted.version,
    )
    item.status = CaseStatus.IN_REVIEW
    audit(db, ctx.user, "pdff_roi.review_revision.save", "project_case", item.id, {
        "version": version.version,
        "base_version": submitted.version,
        "sha256": version.sha256,
    })
    db.commit()
    result = annotation_version_json(version) or {}
    result["base_version"] = submitted.version
    return result


@app.post("/api/cases/{project_case_id}/submit")
def submit_annotation(project_case_id: str, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR)), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    if not ctx.user.has_role(UserRole.ADMIN) and item.annotator_id != ctx.user.id:
        raise HTTPException(status_code=403, detail="该病例未分配给你")
    version_model = annotation_model_for_item(item)
    latest = db.scalar(select(version_model).where(version_model.project_case_id == item.id, version_model.version == item.latest_version))
    validate_submission_candidate(item, latest)
    latest.state = AnnotationState.SUBMITTED
    item.submitted_version = latest.version
    item.status = CaseStatus.SUBMITTED
    audit(db, ctx.user, "pdff_roi.submit" if item.project.workflow_type == "pdff_roi" else "annotation.submit", "project_case", item.id, {"version": latest.version})
    db.commit()
    return project_case_json(item)


@app.post("/api/cases/{project_case_id}/review")
def review_annotation(project_case_id: str, body: ReviewBody, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.REVIEWER)), db: DBSession = Depends(get_db)):
    item = get_visible_project_case(db, project_case_id, ctx.user)
    version_model = annotation_model_for_item(item)
    latest = db.scalar(
        select(version_model).where(
            version_model.project_case_id == item.id,
            version_model.version == item.latest_version,
        )
    )
    review_version, revoking_approval = resolve_review_transition(item, body.action, latest)
    version = db.scalar(select(version_model).where(version_model.project_case_id == item.id, version_model.version == review_version))
    if version is None:
        raise HTTPException(status_code=409, detail="审核版本不存在")
    submitted = validate_and_claim_reviewer(db, item, ctx.user)
    if body.action == ReviewAction.APPROVE:
        if version.version != submitted.version:
            submitted.state = AnnotationState.SUPERSEDED
        version.state = AnnotationState.APPROVED
        item.approved_version = version.version
        item.status = CaseStatus.APPROVED
    else:
        if not body.comment.strip():
            detail = "撤回通过时必须填写原因" if revoking_approval else "退回时必须填写修改意见"
            raise HTTPException(status_code=422, detail=detail)
        version.state = AnnotationState.SUPERSEDED
        if version.version != submitted.version:
            submitted.state = AnnotationState.SUPERSEDED
        if revoking_approval:
            item.approved_version = None
        item.status = CaseStatus.RETURNED
    db.add(ReviewEvent(project_case_id=item.id, annotation_version=version.version, reviewer_id=ctx.user.id, action=body.action, comment=body.comment.strip()))
    audit_action = "review.approval_revoke" if revoking_approval else f"review.{body.action.value}"
    audit(db, ctx.user, audit_action, "project_case", item.id, {"version": version.version})
    db.commit()
    return project_case_json(item)


@app.get("/api/admin/codex-task-publisher/status")
async def codex_task_publisher_status(ctx: AuthContext = Depends(require_roles(UserRole.ADMIN))):
    try:
        health = await codex_worker_health(settings.codex_worker_url)
        return {
            "available": bool(
                health.get("configured") and health.get("workspace_ready") and health.get("task_files_ready")
            ),
            "configured": bool(health.get("configured")),
            "config_ready": bool(health.get("config_ready")),
            "credential_source": health.get("credential_source", "unknown"),
            "runtime": health.get("runtime", "codex-cli"),
            "workspace_ready": bool(health.get("workspace_ready")),
            "workspace_writable": bool(health.get("workspace_writable")),
            "task_files_ready": bool(health.get("task_files_ready")),
            "model": health.get("model", "default"),
            "message": (
                "Docker 内的 Codex CLI 已连接"
                if health.get("configured") and health.get("task_files_ready")
                else "Codex 任务文件只读挂载尚未就绪"
                if health.get("configured")
                else "请填写 codex-worker/auth.json"
            ),
        }
    except CodexWorkerError as exc:
        return {
            "available": False,
            "configured": False,
            "config_ready": False,
            "credential_source": "unavailable",
            "runtime": "",
            "workspace_ready": False,
            "workspace_writable": False,
            "task_files_ready": False,
            "model": "",
            "message": str(exc),
        }


@app.post("/api/admin/codex-task-sessions", status_code=201)
def create_codex_task_session(
    body: CodexTaskSessionCreateBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    task = None
    if body.task_id:
        task = db.get(CollaborationTask, body.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="要修改的任务不存在")
    submission_ids = list(dict.fromkeys(body.submission_ids))
    if submission_ids and task is None:
        raise HTTPException(status_code=422, detail="交付文件只能带入对应任务的修订会话")
    submissions: list[CollaborationTaskSubmission] = []
    if submission_ids:
        rows = db.scalars(
            select(CollaborationTaskSubmission).where(CollaborationTaskSubmission.id.in_(submission_ids))
        ).unique().all()
        by_id = {item.id: item for item in rows if item.task_id == task.id}
        if len(by_id) != len(submission_ids):
            raise HTTPException(status_code=422, detail="部分所选交付版本不存在或不属于当前任务")
        submissions = [by_id[item_id] for item_id in submission_ids]
        if not any(item.files for item in submissions):
            raise HTTPException(status_code=422, detail="所选交付版本没有可供 Codex 读取的文件")
        if sum(len(item.files) for item in submissions) > 20:
            raise HTTPException(status_code=422, detail="单个 Codex 修订会话最多可带入 20 个交付文件")
    session = CodexTaskSession(
        task_id=task.id if task else None,
        created_by_id=ctx.user.id,
        created_by=ctx.user,
        mode="revise" if task else "create",
        status="draft",
        draft=initial_task_draft(task),
        implementation_status="not_required",
    )
    context_file_count = 0
    for submission in submissions:
        for item in submission.files:
            session.assets.append(
                CodexTaskAsset(
                    original_name=item.original_name,
                    file_path=item.file_path,
                    content_type=item.content_type,
                    sha256=item.sha256,
                    byte_size=item.byte_size,
                    source_kind="submission",
                    include_on_publish=False,
                )
            )
            context_file_count += 1
    session.messages.append(
        CodexTaskMessage(
            role="assistant",
            content=(
                (
                    f"这是“{task.title}”的 v{task.current_revision}。已带入 {len(submissions)} 个交付版本、"
                    f"共 {context_file_count} 个文件，我可以直接读取并据此协助修订。"
                    if submissions
                    else f"这是“{task.title}”的 v{task.current_revision}。你想怎么改？"
                )
                if task
                else "你好，想发布什么任务？可以直接告诉我，也可以把相关文件发过来。"
            ),
            message_metadata={"protocol": "natural-v2", "submission_ids": submission_ids},
        )
    )
    db.add(session)
    audit(
        db,
        ctx.user,
        "codex_task_session.create",
        "codex_task_session",
        session.id,
        {"task_id": body.task_id, "submission_ids": submission_ids, "context_file_count": context_file_count},
    )
    db.commit()
    return codex_task_session_json(session)


@app.get("/api/admin/codex-task-sessions/{session_id}")
def codex_task_session_detail(
    session_id: str,
    ctx: AuthContext = Depends(require_roles(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    return codex_task_session_json(get_owned_codex_task_session(db, session_id, ctx.user))


@app.post("/api/admin/codex-task-sessions/{session_id}/messages")
async def send_codex_task_message(
    session_id: str,
    message: str = Form(..., min_length=1, max_length=20000),
    attachments: list[UploadFile] = File(default=[]),
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    session = get_owned_codex_task_session(db, session_id, ctx.user)
    if session.status == "published":
        raise HTTPException(status_code=409, detail="该发布会话已经完成，请新建修订会话")
    content = message.strip()
    files = [attachment for attachment in attachments if attachment.filename]
    if len(session.assets) + len(files) > 20:
        raise HTTPException(status_code=422, detail="单个发布会话最多可上传 20 个附件")

    asset_dir = settings.task_file_root.resolve() / "_codex_staging" / session.id
    if files:
        asset_dir.mkdir(parents=True, exist_ok=True)
    created_paths: list[Path] = []
    new_asset_ids: list[str] = []
    try:
        for upload in files:
            asset_id = str(uuid4())
            destination = asset_dir / asset_id
            byte_size, digest = await persist_task_attachment(upload, destination)
            created_paths.append(destination)
            asset = CodexTaskAsset(
                id=asset_id,
                original_name=safe_attachment_name(upload.filename),
                file_path=(PurePosixPath("_codex_staging") / session.id / asset_id).as_posix(),
                content_type=(upload.content_type or "application/octet-stream")[:160],
                sha256=digest,
                byte_size=byte_size,
                source_kind="upload",
                include_on_publish=True,
            )
            session.assets.append(asset)
            new_asset_ids.append(asset_id)
        session.messages.append(
            CodexTaskMessage(role="user", content=content, message_metadata={"asset_ids": new_asset_ids})
        )
        session.status = "running"
        session.last_error = ""
        session.updated_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        for path in created_paths:
            path.unlink(missing_ok=True)
        raise

    chat_thread_id = session.codex_thread_id if session_uses_natural_codex_chat(session) else None
    try:
        worker_result = await run_codex_worker(
            settings.codex_worker_url,
            prompt=content,
            thread_id=chat_thread_id,
            output_schema=None,
            operation="chat",
            timeout_seconds=settings.codex_worker_timeout_seconds,
            mentions=codex_asset_mentions(session.assets),
        )
        session.codex_thread_id = worker_result.thread_id
        session.status = "draft"
        session.implementation_status = "pending"
        session.last_error = ""
        session.messages.append(
            CodexTaskMessage(
                role="assistant",
                content=worker_result.final_response[:10000],
                message_metadata={"duration_ms": worker_result.duration_ms, "protocol": "natural-v2"},
            )
        )
        audit(db, ctx.user, "codex_task_session.message", "codex_task_session", session.id)
        db.commit()
        return codex_task_session_json(session)
    except (CodexWorkerError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        session.status = "failed"
        session.last_error = str(detail)[:1000]
        session.messages.append(CodexTaskMessage(role="assistant", content=f"本轮处理没有完成：{session.last_error}"))
        db.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/admin/codex-task-sessions/{session_id}/draft")
async def organize_codex_task_draft(
    session_id: str,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    session = get_owned_codex_task_session(db, session_id, ctx.user)
    if session.status == "published":
        raise HTTPException(status_code=409, detail="该发布会话已经完成，请新建修订会话")
    if not any(message.role == "user" for message in session.messages):
        raise HTTPException(status_code=409, detail="请先和 Codex 说说要发布的任务")
    session.status = "running"
    session.last_error = ""
    db.commit()

    users = db.scalars(select(User).where(User.is_active.is_(True)).order_by(User.display_name)).all()
    try:
        worker_result = await run_codex_worker(
            settings.codex_worker_url,
            prompt=build_codex_draft_prompt(session, users),
            thread_id=None,
            output_schema=TASK_DRAFT_OUTPUT_SCHEMA,
            operation="draft",
            timeout_seconds=settings.codex_worker_timeout_seconds,
            mentions=codex_asset_mentions(session.assets),
        )
        result = parse_codex_response(worker_result.final_response)
        draft = normalize_codex_draft(result.get("draft"), db)
        session.draft = draft
        session.status = "ready" if draft["title"] and draft["participant_ids"] else "draft"
        session.implementation_status = "required" if draft["requires_code_change"] else "not_required"
        session.last_error = ""
        session.updated_at = datetime.now(timezone.utc)
        audit(db, ctx.user, "codex_task_session.organize_draft", "codex_task_session", session.id)
        db.commit()
        return codex_task_session_json(session)
    except (CodexWorkerError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        session.status = "draft"
        session.last_error = str(detail)[:1000]
        db.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/admin/codex-task-sessions/{session_id}/implement")
async def implement_codex_task_session(
    session_id: str,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    session = get_owned_codex_task_session(db, session_id, ctx.user)
    draft = normalize_codex_draft(session.draft or {}, db)
    if not draft["requires_code_change"]:
        raise HTTPException(status_code=409, detail="当前任务可以直接由通用任务引擎发布，无需修改源码")
    session.status = "running"
    session.implementation_status = "running"
    session.last_error = ""
    db.commit()
    try:
        worker_result = await run_codex_worker(
            settings.codex_worker_url,
            prompt=build_codex_implementation_prompt(session),
            thread_id=session.codex_thread_id,
            output_schema=TASK_IMPLEMENTATION_OUTPUT_SCHEMA,
            operation="implement",
            timeout_seconds=settings.codex_worker_timeout_seconds,
        )
        result = parse_codex_response(worker_result.final_response)
        ready = bool(result.get("ready_to_publish"))
        session.codex_thread_id = worker_result.thread_id
        session.status = "ready" if ready else "draft"
        session.implementation_status = "ready" if ready else "review"
        session.last_error = ""
        session.messages.append(
            CodexTaskMessage(
                role="assistant",
                content=str(result.get("assistant_message") or result.get("summary") or "专用能力实现完成。")[:10000],
                message_metadata={
                    "summary": str(result.get("summary") or "")[:4000],
                    "changed_files": result.get("changed_files") if isinstance(result.get("changed_files"), list) else [],
                    "test_summary": str(result.get("test_summary") or "")[:4000],
                    "ready_to_publish": ready,
                },
            )
        )
        audit(db, ctx.user, "codex_task_session.implement", "codex_task_session", session.id, {"ready": ready})
        db.commit()
        return codex_task_session_json(session)
    except CodexWorkerError as exc:
        session.status = "failed"
        session.implementation_status = "failed"
        session.last_error = str(exc)[:1000]
        session.messages.append(CodexTaskMessage(role="assistant", content=f"代码实现没有完成：{session.last_error}"))
        db.commit()
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.post("/api/admin/codex-task-sessions/{session_id}/publish", status_code=201)
def publish_codex_task_session(
    session_id: str,
    body: CodexTaskPublishBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    session = get_owned_codex_task_session(db, session_id, ctx.user)
    task = publish_codex_session_revision(db, session, ctx.user, body.upgrade_scope)
    return collaboration_task_json(task, ctx.user)


@app.get("/api/collaboration-tasks")
def list_collaboration_tasks(
    task_status: CollaborationTaskStatus | None = Query(default=None, alias="status"),
    search: str = "",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    access_filters = [] if ctx.user.has_role(UserRole.ADMIN) else [CollaborationTask.participants.any(user_id=ctx.user.id)]
    filters = list(access_filters)
    if task_status is not None:
        filters.append(CollaborationTask.status == task_status)
    if search.strip():
        term = f"%{search.strip()}%"
        filters.append(or_(CollaborationTask.title.ilike(term), CollaborationTask.description.ilike(term)))

    base = select(CollaborationTask).where(*filters)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(CollaborationTask.updated_at.desc(), CollaborationTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    count_rows = db.execute(
        select(CollaborationTask.status, func.count(CollaborationTask.id)).where(*access_filters).group_by(CollaborationTask.status)
    ).all()
    counts = {task_state.value: count for task_state, count in count_rows}
    return {
        "items": [collaboration_task_json(task, ctx.user) for task in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "counts": counts,
    }


@app.get("/api/collaboration-tasks/{task_id}")
def collaboration_task_detail(task_id: str, ctx: AuthContext = Depends(ready_context), db: DBSession = Depends(get_db)):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    result = collaboration_task_json(task, ctx.user)
    comments = db.scalars(
        select(CollaborationTaskComment)
        .where(CollaborationTaskComment.task_id == task.id)
        .order_by(CollaborationTaskComment.created_at.asc())
    ).all()
    result["comments"] = [
        {
            "id": comment.id,
            "body": comment.body,
            "author": user_json(comment.author),
            "created_at": comment.created_at,
        }
        for comment in comments
    ]
    _, _, view_revision_number = collaboration_task_view(task, ctx.user)
    visible_revisions = (
        task.revisions
        if ctx.user.has_role(UserRole.ADMIN)
        else [revision for revision in task.revisions if revision.revision_number <= view_revision_number]
    )
    result["revisions"] = [collaboration_revision_json(revision) for revision in visible_revisions]
    visible_submissions = visible_collaboration_submissions(task, ctx.user, view_revision_number)
    result["submissions"] = [collaboration_submission_json(item) for item in visible_submissions]
    return result


@app.get("/api/collaboration-tasks/{task_id}/attachments/{attachment_id}")
def download_collaboration_task_attachment(
    task_id: str,
    attachment_id: str,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    attachment = db.get(CollaborationTaskAttachment, attachment_id)
    if attachment is None or attachment.task_id != task.id:
        raise HTTPException(status_code=404, detail="任务附件不存在")
    if not ctx.user.has_role(UserRole.ADMIN):
        _, view_revision, _ = collaboration_task_view(task, ctx.user)
        allowed_ids = set(view_revision.attachment_snapshot or []) if view_revision is not None else set()
        if attachment.id not in allowed_ids:
            raise HTTPException(status_code=404, detail="该任务版本没有此附件")
    return FileResponse(
        resolve_task_attachment_path(attachment),
        media_type="application/octet-stream",
        filename=attachment.original_name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/collaboration-tasks/{task_id}/submissions", status_code=201)
async def upload_collaboration_task_submission(
    task_id: str,
    note: str = Form(default="", max_length=4000),
    files: list[UploadFile] = File(default=[]),
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR, UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    participant = next(
        (item for item in task.participants if item.user_id == ctx.user.id and item.is_active),
        None,
    )
    if participant is None:
        raise HTTPException(status_code=403, detail="只有当前任务的参与者可以提交交付文件")
    uploads = [item for item in files if item.filename]
    if not uploads:
        raise HTTPException(status_code=422, detail="请至少选择一个交付文件")
    if len(uploads) > 20:
        raise HTTPException(status_code=422, detail="单次最多可以提交 20 个交付文件")

    next_version = (
        db.scalar(
            select(func.max(CollaborationTaskSubmission.version)).where(
                CollaborationTaskSubmission.task_id == task.id,
                CollaborationTaskSubmission.submitted_by_id == ctx.user.id,
            )
        )
        or 0
    ) + 1
    submission = CollaborationTaskSubmission(
        id=str(uuid4()),
        task_id=task.id,
        submitted_by_id=ctx.user.id,
        submitted_by=ctx.user,
        version=next_version,
        task_revision_number=participant.revision_number or 1,
        note=note.strip(),
    )

    storage_root = settings.task_file_root.resolve()
    parent_dir = storage_root / task.id / "submissions" / ctx.user.id
    parent_dir.mkdir(parents=True, exist_ok=True)
    staging_dir = parent_dir / f".{submission.id}.uploading"
    final_dir = parent_dir / submission.id
    staging_dir.mkdir(parents=False, exist_ok=False)
    try:
        for upload in uploads:
            file_id = str(uuid4())
            byte_size, digest = await persist_task_attachment(upload, staging_dir / file_id)
            submission.files.append(
                CollaborationTaskSubmissionFile(
                    id=file_id,
                    original_name=safe_attachment_name(upload.filename),
                    file_path=(
                        PurePosixPath(task.id) / "submissions" / ctx.user.id / submission.id / file_id
                    ).as_posix(),
                    content_type=(upload.content_type or "application/octet-stream")[:160],
                    sha256=digest,
                    byte_size=byte_size,
                )
            )
        staging_dir.replace(final_dir)
        db.add(submission)
        if participant.progress == TaskProgress.TODO:
            participant.progress = TaskProgress.IN_PROGRESS
            participant.updated_at = datetime.now(timezone.utc)
        recalculate_collaboration_task_status(task)
        audit(
            db,
            ctx.user,
            "collaboration_task.submission_upload",
            "collaboration_task_submission",
            submission.id,
            {
                "task_id": task.id,
                "version": next_version,
                "task_revision_number": submission.task_revision_number,
                "file_count": len(submission.files),
            },
        )
        db.commit()
        return collaboration_submission_json(submission)
    except Exception:
        db.rollback()
        if final_dir.exists():
            shutil.rmtree(final_dir)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


@app.get("/api/collaboration-tasks/{task_id}/submissions/{submission_id}/files/{file_id}")
def download_collaboration_task_submission_file(
    task_id: str,
    submission_id: str,
    file_id: str,
    ctx: AuthContext = Depends(ready_context),
    db: DBSession = Depends(get_db),
):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    submission = db.get(CollaborationTaskSubmission, submission_id)
    item = db.get(CollaborationTaskSubmissionFile, file_id)
    if submission is None or submission.task_id != task.id or item is None or item.submission_id != submission.id:
        raise HTTPException(status_code=404, detail="交付文件不存在")
    if not ctx.user.has_role(UserRole.ADMIN):
        _, _, view_revision_number = collaboration_task_view(task, ctx.user)
        if submission.task_revision_number > view_revision_number:
            raise HTTPException(status_code=404, detail="当前任务版本没有此交付文件")
    return FileResponse(
        resolve_stored_task_file(item.file_path, missing_detail="交付文件不存在"),
        media_type="application/octet-stream",
        filename=item.original_name,
        content_disposition_type="attachment",
        headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"},
    )


@app.post("/api/admin/collaboration-tasks", status_code=201)
async def publish_collaboration_task(
    title: str = Form(..., min_length=1, max_length=200),
    description: str = Form(default="", max_length=10000),
    priority: TaskPriority = Form(default=TaskPriority.NORMAL),
    due_date: str = Form(default="", max_length=10),
    participant_ids: str = Form(...),
    attachments: list[UploadFile] = File(default=[]),
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)),
    db: DBSession = Depends(get_db),
):
    normalized_title = title.strip()
    if not normalized_title:
        raise HTTPException(status_code=422, detail="任务标题不能为空")
    user_ids = parsed_participant_ids(participant_ids)
    users = db.scalars(select(User).where(User.id.in_(user_ids), User.is_active.is_(True))).all()
    users_by_id = {user.id: user for user in users}
    missing = [user_id for user_id in user_ids if user_id not in users_by_id]
    if missing:
        raise HTTPException(status_code=422, detail=f"有 {len(missing)} 名参与者不存在或已停用")

    files = [attachment for attachment in attachments if attachment.filename]
    if len(files) > 20:
        raise HTTPException(status_code=422, detail="单个任务最多可上传 20 个附件")
    task_id = str(uuid4())
    storage_root = settings.task_file_root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    staging_dir = storage_root / f".{task_id}.uploading"
    final_dir = storage_root / task_id
    if files:
        staging_dir.mkdir(parents=False, exist_ok=False)

    task = CollaborationTask(
        id=task_id,
        title=normalized_title,
        description=description.strip(),
        priority=priority,
        status=CollaborationTaskStatus.PUBLISHED,
        due_date=normalized_due_date(due_date),
        current_revision=1,
        task_kind="generic",
        task_spec=normalize_task_spec({}),
        publication_source="manual",
        latest_change_summary="首次发布",
        created_by_id=ctx.user.id,
        creator=ctx.user,
    )
    task.participants = [CollaborationTaskParticipant(user_id=user_id, user=users_by_id[user_id]) for user_id in user_ids]

    try:
        for upload in files:
            attachment_id = str(uuid4())
            byte_size, digest = await persist_task_attachment(upload, staging_dir / attachment_id)
            task.attachments.append(
                CollaborationTaskAttachment(
                    id=attachment_id,
                    uploaded_by_id=ctx.user.id,
                    original_name=safe_attachment_name(upload.filename),
                    file_path=(PurePosixPath(task_id) / attachment_id).as_posix(),
                    content_type=(upload.content_type or "application/octet-stream")[:160],
                    sha256=digest,
                    byte_size=byte_size,
                )
            )
        if files:
            staging_dir.replace(final_dir)

        db.add(task)
        db.flush()
        db.add(
            CollaborationTaskRevision(
                task_id=task.id,
                revision_number=1,
                title=task.title,
                description=task.description,
                priority=task.priority,
                due_date=task.due_date,
                task_kind=task.task_kind,
                task_spec=normalize_task_spec(task.task_spec),
                participant_snapshot=user_ids,
                attachment_snapshot=[attachment.id for attachment in task.attachments],
                change_summary="首次发布",
                publication_source="manual",
                created_by_id=ctx.user.id,
            )
        )
        audit(
            db,
            ctx.user,
            "collaboration_task.publish",
            "collaboration_task",
            task.id,
            {
                "priority": task.priority.value,
                "participant_ids": user_ids,
                "attachment_count": len(task.attachments),
                "due_date": task.due_date,
            },
        )
        db.commit()
        return collaboration_task_json(task, ctx.user)
    except Exception:
        db.rollback()
        if final_dir.exists():
            shutil.rmtree(final_dir)
        raise
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir)


@app.post("/api/collaboration-tasks/{task_id}/progress")
def update_collaboration_task_progress(
    task_id: str,
    body: TaskProgressBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR, UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    participant = next((item for item in task.participants if item.user_id == ctx.user.id and item.is_active), None)
    if participant is None:
        raise HTTPException(status_code=403, detail="只有任务参与者可以更新自己的进度")
    participant.progress = body.progress
    participant.updated_at = datetime.now(timezone.utc)
    recalculate_collaboration_task_status(task)
    audit(
        db,
        ctx.user,
        "collaboration_task.progress",
        "collaboration_task",
        task.id,
        {"progress": body.progress.value},
    )
    db.commit()
    return collaboration_task_json(task, ctx.user)


@app.post("/api/collaboration-tasks/{task_id}/comments", status_code=201)
def add_collaboration_task_comment(
    task_id: str,
    body: TaskCommentBody,
    ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN, UserRole.ANNOTATOR, UserRole.REVIEWER)),
    db: DBSession = Depends(get_db),
):
    task = get_visible_collaboration_task(db, task_id, ctx.user)
    content = body.body.strip()
    if not content:
        raise HTTPException(status_code=422, detail="留言内容不能为空")
    comment = CollaborationTaskComment(task_id=task.id, author_id=ctx.user.id, author=ctx.user, body=content)
    db.add(comment)
    task.updated_at = datetime.now(timezone.utc)
    audit(db, ctx.user, "collaboration_task.comment", "collaboration_task", task.id)
    db.commit()
    return {
        "id": comment.id,
        "body": comment.body,
        "author": user_json(comment.author),
        "created_at": comment.created_at,
    }


@app.get("/api/admin/users")
def list_users(ctx: AuthContext = Depends(require_roles(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    return [user_json(user) for user in db.scalars(select(User).order_by(User.created_at)).all()]


@app.post("/api/admin/users", status_code=201)
def create_user(body: UserCreateBody, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    email = body.email.lower().strip()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=409, detail="该邮箱已注册")
    roles = requested_roles(body.role, body.roles, allow_admin=True)
    if roles is None:
        raise HTTPException(status_code=422, detail="至少需要一个角色")
    legacy_role = UserRole.ADMIN if UserRole.ADMIN in roles else UserRole.ANNOTATOR if UserRole.ANNOTATOR in roles else UserRole.REVIEWER
    user = User(email=email, display_name=body.display_name.strip(), password_hash=hash_password(body.temporary_password), role=legacy_role, must_change_password=True)
    user.set_roles(roles)
    db.add(user)
    db.flush()
    audit(db, ctx.user, "user.create", "user", user.id, {"email": email, "roles": sorted(role.value for role in roles)})
    db.commit()
    return user_json(user)


@app.patch("/api/admin/users/{user_id}")
def patch_user(user_id: str, body: UserPatchBody, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
    roles = requested_roles(body.role, body.roles, allow_admin=False)
    if roles is not None:
        if user.has_role(UserRole.ADMIN):
            raise HTTPException(status_code=400, detail="管理员权限固定，不能修改")
        user.set_roles(roles)
    if body.is_active is not None:
        if user.id == ctx.user.id and not body.is_active:
            raise HTTPException(status_code=400, detail="不能停用当前管理员账户")
        user.is_active = body.is_active
    if body.reset_password is not None:
        user.password_hash = hash_password(body.reset_password)
        user.must_change_password = True
        db.query(Session).filter(Session.user_id == user.id).delete()
    audit(db, ctx.user, "user.update", "user", user.id, {"roles": sorted(role.value for role in user.assigned_roles())})
    db.commit()
    return user_json(user)


def validate_user_deletion(
    user: User,
    annotation_count: int,
    review_count: int,
    collaboration_count: int = 0,
    checklist_history_count: int = 0,
):
    if user.has_role(UserRole.ADMIN):
        raise HTTPException(status_code=400, detail="管理员账户不能删除")
    if collaboration_count:
        raise HTTPException(status_code=409, detail="该人员已有协作任务历史，不能删除，请改为停用账户")
    if checklist_history_count:
        raise HTTPException(status_code=409, detail="该人员已有 Checklist 答题或复核历史，不能删除，请改为停用账户")
    if annotation_count or review_count:
        raise HTTPException(status_code=409, detail="该人员已有标注或审核历史，不能删除，请改为停用账户")


def release_user_assignments(item: ProjectCase, user_id: str) -> bool:
    changed = False
    if item.annotator_id == user_id:
        item.annotator_id = None
        if item.latest_version == 0 and item.status in {CaseStatus.ASSIGNED, CaseStatus.ANNOTATING}:
            item.status = CaseStatus.UNASSIGNED
        changed = True
    if item.reviewer_id == user_id:
        item.reviewer_id = None
        changed = True
    return changed


def release_checklist_feedback_assignment(item: ChecklistFeedbackCase, user_id: str) -> bool:
    if user_id not in {item.answerer_id, item.reviewer_id}:
        return False
    if item.answer_versions or item.review_versions:
        return False
    item.answerer_id = None
    item.reviewer_id = None
    item.answerer = None
    item.reviewer = None
    refresh_checklist_feedback_status(item)
    return True


@app.delete("/api/admin/users/{user_id}")
def delete_user(user_id: str, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    annotation_count = db.scalar(
        select(func.count()).select_from(AnnotationVersion).where(AnnotationVersion.author_id == user.id)
    ) or 0
    review_count = db.scalar(
        select(func.count()).select_from(ReviewEvent).where(ReviewEvent.reviewer_id == user.id)
    ) or 0
    collaboration_count = (
        (db.scalar(select(func.count()).select_from(CollaborationTask).where(CollaborationTask.created_by_id == user.id)) or 0)
        + (db.scalar(select(func.count()).select_from(CollaborationTaskParticipant).where(CollaborationTaskParticipant.user_id == user.id)) or 0)
        + (db.scalar(select(func.count()).select_from(CollaborationTaskComment).where(CollaborationTaskComment.author_id == user.id)) or 0)
    )
    checklist_history_count = (
        (db.scalar(select(func.count()).select_from(ChecklistAnswerVersion).where(ChecklistAnswerVersion.author_id == user.id)) or 0)
        + (db.scalar(select(func.count()).select_from(ChecklistReviewVersion).where(ChecklistReviewVersion.author_id == user.id)) or 0)
    )
    validate_user_deletion(user, annotation_count, review_count, collaboration_count, checklist_history_count)

    assigned_cases = db.scalars(
        select(ProjectCase).where(or_(ProjectCase.annotator_id == user.id, ProjectCase.reviewer_id == user.id))
    ).all()
    for item in assigned_cases:
        release_user_assignments(item, user.id)

    assigned_checklist_cases = db.scalars(
        select(ChecklistFeedbackCase).where(
            or_(ChecklistFeedbackCase.answerer_id == user.id, ChecklistFeedbackCase.reviewer_id == user.id)
        )
    ).all()
    released_checklist_assignments = sum(
        release_checklist_feedback_assignment(item, user.id) for item in assigned_checklist_cases
    )

    deleted_user = {
        "email": user.email,
        "display_name": user.display_name,
        "roles": sorted(role.value for role in user.assigned_roles()),
        "released_assignments": len(assigned_cases) + released_checklist_assignments,
    }
    audit(db, ctx.user, "user.delete", "user", user.id, deleted_user)
    db.delete(user)
    db.commit()
    return {"deleted": True, "released_assignments": len(assigned_cases) + released_checklist_assignments}


def validate_assignment_user(db: DBSession, user_id: str | None, role: UserRole, label: str):
    if user_id is None:
        return
    user = db.get(User, user_id)
    if not user or not user.has_role(role) or not user.is_active:
        raise HTTPException(status_code=422, detail=f"{label}无效")


def apply_case_assignment(
    item: ProjectCase,
    *,
    update_annotator: bool,
    annotator_id: str | None,
    update_reviewer: bool,
    reviewer_id: str | None,
):
    next_annotator_id = annotator_id if update_annotator else item.annotator_id
    next_reviewer_id = reviewer_id if update_reviewer else item.reviewer_id
    if next_annotator_id is not None and next_annotator_id == next_reviewer_id:
        raise HTTPException(status_code=422, detail="初标医生与审核医生不能为同一人")
    if update_annotator:
        if item.latest_version > 0 and annotator_id != item.annotator_id:
            raise HTTPException(status_code=409, detail="病例已有标注版本，不能更换初标医生")
        item.annotator_id = annotator_id
        if item.status == CaseStatus.UNASSIGNED and item.annotator_id:
            item.status = CaseStatus.ASSIGNED
        elif item.status == CaseStatus.ASSIGNED and not item.annotator_id:
            item.status = CaseStatus.UNASSIGNED
    if update_reviewer:
        item.reviewer_id = reviewer_id


@app.post("/api/admin/cases/bulk-assign")
def bulk_assign_cases(body: BulkAssignmentBody, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    if not body.update_annotator and not body.update_reviewer:
        raise HTTPException(status_code=422, detail="请至少选择一种需要批量更新的角色")
    validate_assignment_user(db, body.annotator_id if body.update_annotator else None, UserRole.ANNOTATOR, "初标医生")
    validate_assignment_user(db, body.reviewer_id if body.update_reviewer else None, UserRole.REVIEWER, "审核医生")
    case_ids = list(dict.fromkeys(body.case_ids))
    items = db.scalars(select(ProjectCase).where(ProjectCase.id.in_(case_ids))).all()
    found_ids = {item.id for item in items}
    missing = [case_id for case_id in case_ids if case_id not in found_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"有 {len(missing)} 个病例任务不存在，请刷新列表后重试")
    quarantined = [item.case.case_code for item in items if item.case.needs_identity_review]
    if quarantined:
        raise HTTPException(status_code=409, detail=f"{len(quarantined)} 个病例仍待身份核对，不能进入任务分配")

    if body.update_annotator:
        conflicts = [item.case.case_code for item in items if item.latest_version > 0 and body.annotator_id != item.annotator_id]
        if conflicts:
            preview = "、".join(conflicts[:5])
            suffix = " 等" if len(conflicts) > 5 else ""
            raise HTTPException(status_code=409, detail=f"{len(conflicts)} 个病例已有标注，不能更换初标医生：{preview}{suffix}")

    for item in items:
        apply_case_assignment(
            item,
            update_annotator=body.update_annotator,
            annotator_id=body.annotator_id,
            update_reviewer=body.update_reviewer,
            reviewer_id=body.reviewer_id,
        )
        audit(db, ctx.user, "case.assign_bulk", "project_case", item.id, {
            "annotator_id": body.annotator_id if body.update_annotator else item.annotator_id,
            "reviewer_id": body.reviewer_id if body.update_reviewer else item.reviewer_id,
            "batch_size": len(items),
        })
    db.commit()
    return {"updated": len(items)}


@app.post("/api/admin/cases/{project_case_id}/assign")
def assign_case(project_case_id: str, body: AssignmentBody, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    item = db.get(ProjectCase, project_case_id)
    if item is None:
        raise HTTPException(status_code=404, detail="病例任务不存在")
    if item.case.needs_identity_review:
        raise HTTPException(status_code=409, detail="该病例仍待身份核对，不能进入任务分配")
    validate_assignment_user(db, body.annotator_id, UserRole.ANNOTATOR, "初标医生")
    validate_assignment_user(db, body.reviewer_id, UserRole.REVIEWER, "审核医生")
    apply_case_assignment(item, update_annotator=True, annotator_id=body.annotator_id, update_reviewer=True, reviewer_id=body.reviewer_id)
    audit(db, ctx.user, "case.assign", "project_case", item.id, {"annotator_id": body.annotator_id, "reviewer_id": body.reviewer_id})
    db.commit()
    return project_case_json(item)


@app.post("/api/admin/import")
def run_import(only_available: bool = True, ctx: AuthContext = Depends(require_roles_csrf(UserRole.ADMIN)), db: DBSession = Depends(get_db)):
    result = import_dataset(db, settings, only_available=only_available)
    audit(db, ctx.user, "dataset.import", "dataset", "dixon", result)
    db.commit()
    return result
