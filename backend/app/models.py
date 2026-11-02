from __future__ import annotations

import enum
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    ANNOTATOR = "annotator"
    REVIEWER = "reviewer"


class CaseStatus(str, enum.Enum):
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"
    ANNOTATING = "annotating"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    RETURNED = "returned"
    APPROVED = "approved"


class AnnotationState(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class ReviewAction(str, enum.Enum):
    APPROVE = "approve"
    RETURN = "return"


class CollaborationTaskStatus(str, enum.Enum):
    PUBLISHED = "published"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class TaskPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class TaskProgress(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class ChecklistFeedbackStatus(str, enum.Enum):
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"
    ANSWERING = "answering"
    ANSWER_SUBMITTED = "answer_submitted"
    REVIEWING = "reviewing"
    COMPLETED = "completed"


class ChecklistVersionState(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"


class UserRoleAssignment(Base):
    __tablename__ = "user_roles"
    __table_args__ = (UniqueConstraint("user_id", "role", name="uq_user_role"),)

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), primary_key=True)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    role_assignments: Mapped[list[UserRoleAssignment]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    def assigned_roles(self) -> set[UserRole]:
        roles = {assignment.role for assignment in self.role_assignments}
        return roles or {self.role}

    def has_role(self, role: UserRole) -> bool:
        return role in self.assigned_roles()

    def set_roles(self, roles: set[UserRole]):
        self.role_assignments[:] = [assignment for assignment in self.role_assignments if assignment.role in roles]
        existing = {assignment.role for assignment in self.role_assignments}
        self.role_assignments.extend(UserRoleAssignment(role=role) for role in roles - existing)
        if UserRole.ADMIN in roles:
            self.role = UserRole.ADMIN
        elif UserRole.ANNOTATOR in roles:
            self.role = UserRole.ANNOTATOR
        else:
            self.role = UserRole.REVIEWER


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship()


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    workflow_type: Mapped[str] = mapped_column(String(40), default="segmentation", server_default="segmentation", index=True)
    source_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    labels: Mapped[list[Label]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Label(Base):
    __tablename__ = "labels"
    __table_args__ = (UniqueConstraint("project_id", "value", name="uq_project_label_value"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(100))
    value: Mapped[int] = mapped_column(Integer)
    color: Mapped[str] = mapped_column(String(16), default="#4FD1C5")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    project: Mapped[Project] = relationship(back_populates="labels")


class ImagingCase(Base):
    __tablename__ = "imaging_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    case_code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    patient_id: Mapped[str] = mapped_column(String(160), index=True)
    patient_name_zh: Mapped[str] = mapped_column(String(160), index=True)
    mapping_status: Mapped[str] = mapped_column(String(80), default="")
    needs_identity_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    study_date: Mapped[str] = mapped_column(String(80), default="")
    protocol_family: Mapped[str] = mapped_column(String(160), default="")
    sequence_paths: Mapped[dict] = mapped_column(JSON)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectCase(Base):
    __tablename__ = "project_cases"
    __table_args__ = (
        UniqueConstraint("project_id", "case_id", name="uq_project_case"),
        Index("ix_project_case_queue", "project_id", "status", "annotator_id", "reviewer_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("imaging_cases.id", ondelete="CASCADE"), index=True)
    status: Mapped[CaseStatus] = mapped_column(Enum(CaseStatus, native_enum=False), default=CaseStatus.UNASSIGNED, index=True)
    annotator_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    latest_version: Mapped[int] = mapped_column(Integer, default=0)
    submitted_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship()
    case: Mapped[ImagingCase] = relationship()
    annotator: Mapped[User | None] = relationship(foreign_keys=[annotator_id])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_id])


class AnnotationVersion(Base):
    __tablename__ = "annotation_versions"
    __table_args__ = (UniqueConstraint("project_case_id", "version", name="uq_annotation_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_case_id: Mapped[str] = mapped_column(ForeignKey("project_cases.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="annotation", server_default="annotation")
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[AnnotationState] = mapped_column(Enum(AnnotationState, native_enum=False), default=AnnotationState.DRAFT)
    file_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author: Mapped[User] = relationship()


class PdffAnnotationVersion(Base):
    __tablename__ = "pdff_annotation_versions"
    __table_args__ = (UniqueConstraint("project_case_id", "version", name="uq_pdff_annotation_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_case_id: Mapped[str] = mapped_column(ForeignKey("project_cases.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="annotation", server_default="annotation")
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[AnnotationState] = mapped_column(Enum(AnnotationState, native_enum=False), default=AnnotationState.DRAFT)
    roi_payload: Mapped[list] = mapped_column(JSON)
    metrics: Mapped[list] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author: Mapped[User] = relationship()


class ReviewEvent(Base):
    __tablename__ = "review_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_case_id: Mapped[str] = mapped_column(ForeignKey("project_cases.id", ondelete="CASCADE"), index=True)
    annotation_version: Mapped[int] = mapped_column(Integer)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    action: Mapped[ReviewAction] = mapped_column(Enum(ReviewAction, native_enum=False))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChecklistFeedbackCase(Base):
    __tablename__ = "checklist_feedback_cases"
    __table_args__ = (
        Index("ix_checklist_feedback_queue", "status", "answerer_id", "reviewer_id", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    primary_task: Mapped[str] = mapped_column(String(120), index=True)
    target_subtype: Mapped[str] = mapped_column(String(120), default="", index=True)
    screening_complexity: Mapped[str] = mapped_column(String(24), index=True)
    case_payload: Mapped[dict] = mapped_column(JSON)
    checklist_payload: Mapped[dict] = mapped_column(JSON)
    review_template: Mapped[dict] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(64))
    source_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[ChecklistFeedbackStatus] = mapped_column(
        Enum(ChecklistFeedbackStatus, native_enum=False),
        default=ChecklistFeedbackStatus.UNASSIGNED,
        index=True,
    )
    answerer_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewer_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    latest_answer_version: Mapped[int] = mapped_column(Integer, default=0)
    latest_review_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    answerer: Mapped[User | None] = relationship(foreign_keys=[answerer_id])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_id])
    answer_versions: Mapped[list[ChecklistAnswerVersion]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ChecklistAnswerVersion.version",
    )
    review_versions: Mapped[list[ChecklistReviewVersion]] = relationship(
        back_populates="case",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="ChecklistReviewVersion.version",
    )


class ChecklistAnswerVersion(Base):
    __tablename__ = "checklist_answer_versions"
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_checklist_answer_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("checklist_feedback_cases.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    state: Mapped[ChecklistVersionState] = mapped_column(
        Enum(ChecklistVersionState, native_enum=False), default=ChecklistVersionState.DRAFT
    )
    payload: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[ChecklistFeedbackCase] = relationship(back_populates="answer_versions")
    author: Mapped[User] = relationship()


class ChecklistReviewVersion(Base):
    __tablename__ = "checklist_review_versions"
    __table_args__ = (UniqueConstraint("case_id", "version", name="uq_checklist_review_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_id: Mapped[str] = mapped_column(
        ForeignKey("checklist_feedback_cases.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    answer_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[ChecklistVersionState] = mapped_column(
        Enum(ChecklistVersionState, native_enum=False), default=ChecklistVersionState.DRAFT
    )
    payload: Mapped[dict] = mapped_column(JSON)
    revised_checklist: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[ChecklistFeedbackCase] = relationship(back_populates="review_versions")
    author: Mapped[User] = relationship()


class CollaborationTask(Base):
    __tablename__ = "collaboration_tasks"
    __table_args__ = (Index("ix_collaboration_task_queue", "status", "priority", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(200), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[TaskPriority] = mapped_column(Enum(TaskPriority, native_enum=False), default=TaskPriority.NORMAL, index=True)
    status: Mapped[CollaborationTaskStatus] = mapped_column(
        Enum(CollaborationTaskStatus, native_enum=False), default=CollaborationTaskStatus.PUBLISHED, index=True
    )
    due_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    task_kind: Mapped[str] = mapped_column(String(80), default="generic", server_default="generic")
    task_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    publication_source: Mapped[str] = mapped_column(String(40), default="manual", server_default="manual")
    latest_change_summary: Mapped[str] = mapped_column(Text, default="", server_default="")
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    creator: Mapped[User] = relationship(foreign_keys=[created_by_id])
    participants: Mapped[list[CollaborationTaskParticipant]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin", order_by="CollaborationTaskParticipant.created_at"
    )
    attachments: Mapped[list[CollaborationTaskAttachment]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin", order_by="CollaborationTaskAttachment.created_at"
    )
    revisions: Mapped[list[CollaborationTaskRevision]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin", order_by="CollaborationTaskRevision.revision_number"
    )
    submissions: Mapped[list[CollaborationTaskSubmission]] = relationship(
        back_populates="task", cascade="all, delete-orphan", lazy="selectin", order_by="CollaborationTaskSubmission.created_at"
    )


class CollaborationTaskParticipant(Base):
    __tablename__ = "collaboration_task_participants"
    __table_args__ = (Index("ix_collaboration_participant_user_progress", "user_id", "progress"),)

    task_id: Mapped[str] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    progress: Mapped[TaskProgress] = mapped_column(Enum(TaskProgress, native_enum=False), default=TaskProgress.TODO, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    task: Mapped[CollaborationTask] = relationship(back_populates="participants")
    user: Mapped[User] = relationship()


class CollaborationTaskAttachment(Base):
    __tablename__ = "collaboration_task_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="CASCADE"), index=True)
    uploaded_by_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    revision_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[CollaborationTask] = relationship(back_populates="attachments")
    uploaded_by: Mapped[User] = relationship()


class CollaborationTaskComment(Base):
    __tablename__ = "collaboration_task_comments"
    __table_args__ = (Index("ix_collaboration_comment_task_created", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="CASCADE"), index=True)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[CollaborationTask] = relationship()
    author: Mapped[User] = relationship()


class CollaborationTaskRevision(Base):
    __tablename__ = "collaboration_task_revisions"
    __table_args__ = (UniqueConstraint("task_id", "revision_number", name="uq_collaboration_task_revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="CASCADE"), index=True)
    revision_number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[TaskPriority] = mapped_column(Enum(TaskPriority, native_enum=False), default=TaskPriority.NORMAL)
    due_date: Mapped[str] = mapped_column(String(10), default="")
    task_kind: Mapped[str] = mapped_column(String(80), default="generic")
    task_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    participant_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    attachment_snapshot: Mapped[list] = mapped_column(JSON, default=list)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    publication_source: Mapped[str] = mapped_column(String(40), default="codex")
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[CollaborationTask] = relationship(back_populates="revisions")
    created_by: Mapped[User] = relationship()


class CollaborationTaskSubmission(Base):
    __tablename__ = "collaboration_task_submissions"
    __table_args__ = (
        UniqueConstraint("task_id", "submitted_by_id", "version", name="uq_collaboration_task_submission_version"),
        Index("ix_collaboration_submission_task_created", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="CASCADE"), index=True)
    submitted_by_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    task_revision_number: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[CollaborationTask] = relationship(back_populates="submissions")
    submitted_by: Mapped[User] = relationship()
    files: Mapped[list[CollaborationTaskSubmissionFile]] = relationship(
        back_populates="submission", cascade="all, delete-orphan", lazy="selectin", order_by="CollaborationTaskSubmissionFile.created_at"
    )


class CollaborationTaskSubmissionFile(Base):
    __tablename__ = "collaboration_task_submission_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("collaboration_task_submissions.id", ondelete="CASCADE"), index=True
    )
    original_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    submission: Mapped[CollaborationTaskSubmission] = relationship(back_populates="files")


class CodexTaskSession(Base):
    __tablename__ = "codex_task_sessions"
    __table_args__ = (Index("ix_codex_task_session_owner_updated", "created_by_id", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str | None] = mapped_column(ForeignKey("collaboration_tasks.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(20), default="create")
    status: Mapped[str] = mapped_column(String(24), default="draft", index=True)
    codex_thread_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    draft: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    implementation_status: Mapped[str] = mapped_column(String(24), default="not_required")
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    task: Mapped[CollaborationTask | None] = relationship()
    created_by: Mapped[User] = relationship()
    messages: Mapped[list[CodexTaskMessage]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin", order_by="CodexTaskMessage.created_at"
    )
    assets: Mapped[list[CodexTaskAsset]] = relationship(
        back_populates="session", cascade="all, delete-orphan", lazy="selectin", order_by="CodexTaskAsset.created_at"
    )


class CodexTaskMessage(Base):
    __tablename__ = "codex_task_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("codex_task_sessions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    message_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[CodexTaskSession] = relationship(back_populates="messages")


class CodexTaskAsset(Base):
    __tablename__ = "codex_task_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_id: Mapped[str] = mapped_column(ForeignKey("codex_task_sessions.id", ondelete="CASCADE"), index=True)
    original_name: Mapped[str] = mapped_column(String(255))
    file_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream")
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(32), default="upload", server_default="upload")
    include_on_publish: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    session: Mapped[CodexTaskSession] = relationship(back_populates="assets")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
