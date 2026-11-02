export type Role = "admin" | "annotator" | "reviewer";

export type User = {
  id: string;
  email: string;
  display_name: string;
  role: Role;
  roles: Role[];
  is_active: boolean;
  must_change_password: boolean;
};

export type AuthState = { user: User; csrf_token: string };
export type Label = { key: string; name: string; value: number; color: string };
export type WorkflowType = "segmentation" | "pdff_roi";
export type Project = { id: string; name: string; description: string; workflow_type: WorkflowType; source_project_id: string | null; labels: Label[] };

export type ChecklistFeedbackStatus = "unassigned" | "assigned" | "answering" | "answer_submitted" | "reviewing" | "completed";
export type ChecklistActorRole = "admin" | "answerer" | "reviewer" | "none";
export type ChecklistCriterion = {
  criterion_id: string;
  category: string;
  type: "required" | "uncertainty" | "prohibited";
  statement: string;
  weight: number;
  reference_evidence: string[];
  accepted_variants: string[];
  violation_condition: string | null;
};
export type ChecklistAnswerPayload = {
  sections: Record<string, string>;
  perceived_complexity: "low" | "medium" | "high" | null;
  estimated_minutes: number | null;
  visible_information_is_sufficient: boolean | null;
  ambiguities_or_missing_information: string;
  comments: string;
};
export type ChecklistCriterionReview = {
  criterion_id: string;
  decision: "keep" | "edit" | "delete" | null;
  revised_type?: "required" | "uncertainty" | "prohibited" | null;
  revised_statement?: string | null;
  revised_weight?: number | null;
  revised_reference_evidence?: string[] | null;
  revised_accepted_variants?: string[] | null;
  revised_violation_condition?: string | null;
  comments?: string;
};
export type ChecklistReviewPayload = {
  case_decision: "approve" | "revise" | "reject" | null;
  checklist_necessity: "necessary" | "partly_necessary" | "unnecessary" | null;
  necessity_rationale: string;
  final_complexity: "low" | "medium" | "high" | null;
  complexity_rationale: string;
  weight_total_appropriate: boolean | null;
  recommended_total_weight: number | null;
  weight_comments: string;
  task_target_is_valid: boolean | null;
  visible_information_is_sufficient: boolean | null;
  contains_answer_leakage: boolean | null;
  missing_clinical_evidence: string[];
  criterion_reviews: ChecklistCriterionReview[];
  new_criteria: ChecklistCriterion[];
  guideline_review: Record<string, unknown>;
  safety_review: Record<string, unknown>;
  overall_comments: string;
  original_total_weight?: number;
  revised_total_weight?: number;
  changed_criterion_count?: number;
};
export type ChecklistFeedbackItem = {
  id: string;
  primary_task: string;
  target_subtype: string;
  screening_complexity: "low" | "medium" | "high";
  status: ChecklistFeedbackStatus;
  answerer: User | null;
  reviewer: User | null;
  latest_answer_version: number;
  latest_review_version: number;
  question: string;
  response_sections: string[];
  criterion_count: number;
  original_total_weight: number;
  my_role: ChecklistActorRole;
  created_at: string;
  updated_at: string;
};
export type ChecklistVersion<T> = {
  id: string;
  version: number;
  state: "draft" | "submitted";
  author: User;
  sha256: string;
  created_at: string;
  payload: T;
  answer_version?: number;
  revised_checklist?: { criteria: ChecklistCriterion[]; [key: string]: unknown };
};
export type ChecklistFeedbackDetail = ChecklistFeedbackItem & {
  case_payload: {
    case_id: string;
    patient_context: Record<string, string>;
    primary_task: string;
    question: string;
    response_contract: { sections: string[] };
    [key: string]: unknown;
  };
  checklist_payload: { criteria: ChecklistCriterion[]; guideline_basis?: Array<Record<string, string>>; [key: string]: unknown } | null;
  review_template: Record<string, unknown> | null;
  answer: ChecklistVersion<ChecklistAnswerPayload> | null;
  review: ChecklistVersion<ChecklistReviewPayload> | null;
  can_answer: boolean;
  can_review: boolean;
  waiting_for_answer: boolean;
  answer_history: Array<ChecklistVersion<ChecklistAnswerPayload>>;
  review_history: Array<ChecklistVersion<ChecklistReviewPayload>>;
};

export const CHECKLIST_STATUS_LABELS: Record<ChecklistFeedbackStatus, string> = {
  unassigned: "未分配",
  assigned: "待答题",
  answering: "作答中",
  answer_submitted: "待复核",
  reviewing: "复核中",
  completed: "已完成",
};

export type CollaborationTaskStatus = "published" | "in_progress" | "completed";
export type TaskPriority = "low" | "normal" | "high" | "urgent";
export type TaskProgress = "todo" | "in_progress" | "done";

export type TaskSpec = {
  workflow_type: string;
  deliverables: string[];
  steps: Array<{ label: string; description: string }>;
  fields: Array<{ key: string; label: string; type: "text" | "textarea" | "number" | "date" | "select" | "checkbox" | "file"; required: boolean; options: string[] }>;
};

export type TaskDraft = {
  title: string;
  description: string;
  priority: TaskPriority;
  due_date: string;
  participant_ids: string[];
  task_kind: string;
  task_spec: TaskSpec;
  requires_code_change: boolean;
  implementation_summary: string;
  change_summary: string;
};

export type CollaborationTask = {
  id: string;
  title: string;
  description: string;
  priority: TaskPriority;
  status: CollaborationTaskStatus;
  due_date: string;
  created_by: User;
  created_at: string;
  updated_at: string;
  participants: Array<{ user: User; progress: TaskProgress; revision_number: number; updated_at: string }>;
  completed_participants: number;
  my_progress: TaskProgress | null;
  my_revision: number | null;
  my_active: boolean | null;
  current_revision: number;
  view_revision: number;
  task_kind: string;
  task_spec: TaskSpec;
  publication_source: "manual" | "codex";
  latest_change_summary: string;
  attachments: Array<{ id: string; name: string; content_type: string; byte_size: number; revision_number: number; created_at: string; download_url: string }>;
};

export type CollaborationTaskSubmission = {
  id: string;
  version: number;
  task_revision_number: number;
  note: string;
  submitted_by: User;
  created_at: string;
  files: Array<{
    id: string;
    name: string;
    content_type: string;
    byte_size: number;
    sha256: string;
    created_at: string;
    download_url: string;
  }>;
};

export type CollaborationTaskDetail = CollaborationTask & {
  comments: Array<{ id: string; body: string; author: User; created_at: string }>;
  revisions: Array<{
    id: string;
    revision_number: number;
    title: string;
    description: string;
    priority: TaskPriority;
    due_date: string;
    task_kind: string;
    task_spec: TaskSpec;
    participant_snapshot: string[];
    attachment_snapshot: string[];
    change_summary: string;
    publication_source: "manual" | "codex";
    created_by: User;
    created_at: string;
  }>;
  submissions: CollaborationTaskSubmission[];
};

export type CodexPublisherStatus = {
  available: boolean;
  configured: boolean;
  config_ready: boolean;
  credential_source: string;
  runtime: string;
  workspace_ready: boolean;
  workspace_writable: boolean;
  task_files_ready: boolean;
  model: string;
  message: string;
};

export type CodexTaskSession = {
  id: string;
  task_id: string | null;
  mode: "create" | "revise";
  status: "draft" | "running" | "ready" | "failed" | "published";
  draft: TaskDraft;
  implementation_status: "pending" | "not_required" | "required" | "running" | "ready" | "review" | "failed";
  last_error: string;
  created_at: string;
  updated_at: string;
  messages: Array<{ id: string; role: "user" | "assistant"; content: string; metadata: Record<string, unknown>; created_at: string }>;
  assets: Array<{
    id: string;
    name: string;
    content_type: string;
    byte_size: number;
    source_kind: "upload" | "submission" | string;
    include_on_publish: boolean;
    created_at: string;
  }>;
};

export type CaseStatus = "unassigned" | "assigned" | "annotating" | "submitted" | "in_review" | "returned" | "approved";

export type CaseItem = {
  id: string;
  project_id: string;
  project_name: string;
  workflow_type: WorkflowType;
  status: CaseStatus;
  latest_version: number;
  submitted_version: number | null;
  approved_version: number | null;
  annotator: User | null;
  reviewer: User | null;
  case: {
    id: string;
    case_code: string;
    patient_id: string;
    patient_name_zh: string;
    study_date: string;
    protocol_family: string;
    needs_identity_review: boolean;
    identity_review_note: string;
    is_available: boolean;
    sequences: string[];
  };
};

export type PdffRegion = "head" | "body" | "tail";
export type PdffRoi = { region: PdffRegion; center_voxel: [number, number, number]; radius_mm: number };
export type NumericStats = { mean: number; sd: number; min: number; max: number };
export type PdffRoiMetric = {
  region: PdffRegion;
  label: string;
  slice_index: number;
  radius_mm: number;
  area_mm2: number;
  voxel_count: number;
  fat: NumericStats;
  water: NumericStats;
  in_phase?: NumericStats;
  opposed_phase?: NumericStats;
  pdff: NumericStats;
  pdff_ratio_of_means: number;
  pdff_voxel_count?: number;
  pdff_in_opposed?: NumericStats;
  pdff_in_opposed_ratio_of_means?: number;
  pdff_in_opposed_voxel_count?: number;
};

export type PdffPreviewResult = {
  rois: PdffRoi[];
  metrics: PdffRoiMetric[];
};

export type PancreasReferenceInfo = {
  available: boolean;
  status: "available" | "missing" | "ambiguous" | "geometry_mismatch" | "invalid";
  model: string | null;
  reference_sequence: "w";
  slice_count: number | null;
  reason: string;
  warning: string;
};

export type PancreasReferenceSegment = [number, number, number, number];

export type PancreasReferenceSlice = {
  available: true;
  slice_index: number;
  segments: PancreasReferenceSegment[];
  segment_count: number;
  has_contour: boolean;
};

export type PancreasReferenceContours = PancreasReferenceInfo & {
  contours: Array<{
    slice_index: number;
    segments: PancreasReferenceSegment[];
  }>;
  nonempty_slice_count: number;
  segment_count: number;
};

export type PdffPlaneSummary = {
  fat_water_mean: number;
  fat_water_sd: number;
  in_opposed_mean: number;
  in_opposed_sd: number;
  total_area_mm2: number;
  total_voxel_count: number;
};

export type PdffMeasurementPlane = {
  plane_index: 1 | 2 | 3;
  slice_index?: number;
  rois: PdffRoi[];
  metrics: PdffRoiMetric[];
  summary?: PdffPlaneSummary;
};

export type PdffConsistencyStats = {
  count: number;
  mean: number | null;
  sd: number | null;
  cv_percent: number | null;
  min: number | null;
  max: number | null;
  range: number | null;
};

export type PdffCaseSummary = {
  plane_count: number;
  roi_count: number;
  slice_indices: number[];
  fat_water: PdffConsistencyStats;
  in_opposed: PdffConsistencyStats;
  regions: Record<PdffRegion, { fat_water: PdffConsistencyStats; in_opposed: PdffConsistencyStats }>;
  total_area_mm2: number;
  total_voxel_count: number;
};

export type AnnotationVersionDetail = {
  version: number;
  state: string;
  author_id: string;
  author: string;
  kind: "annotation" | "review_revision";
  source_version: number | null;
  created_at: string;
  comment: string;
  planes?: PdffMeasurementPlane[];
  case_summary?: PdffCaseSummary | null;
  rois?: PdffRoi[];
  metrics?: PdffRoiMetric[];
  sha256?: string;
};

export type CaseDetail = CaseItem & {
  labels: Label[];
  latest_annotation: AnnotationVersionDetail | null;
  submitted_annotation: AnnotationVersionDetail | null;
  review_history: Array<{ action: "approve" | "return"; version: number; comment: string; created_at: string }>;
};

export const STATUS_LABELS: Record<CaseStatus, string> = {
  unassigned: "未分配",
  assigned: "待初标",
  annotating: "标注中",
  submitted: "待审核",
  in_review: "审核中",
  returned: "待返工",
  approved: "已通过",
};

export const ROLE_LABELS: Record<Role, string> = {
  admin: "管理员",
  annotator: "初标医生",
  reviewer: "审核医生",
};

export function hasRole(user: User, role: Role): boolean {
  return (user.roles?.length ? user.roles : [user.role]).includes(role);
}

export function roleSummary(user: User): string {
  const roles = user.roles?.length ? user.roles : [user.role];
  return roles.map((role) => ROLE_LABELS[role]).join(" · ");
}
