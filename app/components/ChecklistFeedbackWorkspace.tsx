"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../lib/api";
import {
  AuthState,
  CHECKLIST_STATUS_LABELS,
  ChecklistCriterion,
  ChecklistCriterionReview,
  ChecklistFeedbackDetail,
  ChecklistFeedbackItem,
  ChecklistFeedbackStatus,
  ChecklistReviewPayload,
  User,
  hasRole,
} from "../lib/types";

type Props = { auth: AuthState; users: User[] };
type Dashboard = { counts: Record<ChecklistFeedbackStatus, number>; total: number };

const COMPLEXITY_LABELS = { low: "低", medium: "中", high: "高" } as const;
const TASK_LABELS: Record<string, string> = {
  laboratory_interpretation: "检验结果解读",
  differential_diagnosis: "鉴别诊断",
  treatment_planning: "治疗方案评估",
  medication_safety: "用药安全",
};
const STATUS_TABS: Array<{ key: ChecklistFeedbackStatus | ""; label: string }> = [
  { key: "", label: "全部" },
  { key: "unassigned", label: "未分配" },
  { key: "assigned", label: "待答题" },
  { key: "answering", label: "作答中" },
  { key: "answer_submitted", label: "待复核" },
  { key: "reviewing", label: "复核中" },
  { key: "completed", label: "已完成" },
];

function Notice({ text, success = false }: { text: string; success?: boolean }) {
  return text ? <div className={`notice ${success ? "success" : ""}`}>{text}</div> : null;
}

function taskLabel(value: string): string {
  return TASK_LABELS[value] || value.replaceAll("_", " ");
}

function subtypeLabel(value: string): string {
  return value.replaceAll("_", " ");
}

function boolValue(value: boolean | null | undefined): string {
  return value === true ? "true" : value === false ? "false" : "";
}

function parseBool(value: string): boolean | null {
  return value === "true" ? true : value === "false" ? false : null;
}

function lines(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function StatusPill({ status }: { status: ChecklistFeedbackStatus }) {
  return <span className={`checklist-status ${status}`}>{CHECKLIST_STATUS_LABELS[status]}</span>;
}

function AssignmentDialog({
  auth,
  item,
  users,
  onClose,
  onSaved,
}: {
  auth: AuthState;
  item: ChecklistFeedbackItem;
  users: User[];
  onClose: () => void;
  onSaved: (detail: ChecklistFeedbackDetail) => void;
}) {
  const [answererId, setAnswererId] = useState(item.answerer?.id || "");
  const [reviewerId, setReviewerId] = useState(item.reviewer?.id || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const answerers = users.filter((user) => user.is_active && hasRole(user, "annotator"));
  const reviewers = users.filter((user) => user.is_active && hasRole(user, "reviewer"));
  const canClear = item.latest_answer_version === 0 && item.latest_review_version === 0 && Boolean(item.answerer);

  const save = async (clear = false) => {
    if (!clear && (!answererId || !reviewerId)) {
      setError("请同时选择答题医生和 Checklist 复核医生");
      return;
    }
    if (!clear && answererId === reviewerId) {
      setError("答题和复核必须由两位不同医生完成");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const detail = await api<ChecklistFeedbackDetail>(
        `/api/admin/checklist-feedback/${encodeURIComponent(item.id)}/assignment`,
        {
          method: "PATCH",
          body: JSON.stringify({ answerer_id: clear ? null : answererId, reviewer_id: clear ? null : reviewerId }),
        },
        auth.csrf_token,
      );
      onSaved(detail);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "分配失败");
    } finally {
      setBusy(false);
    }
  };

  return <div className="modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.currentTarget === event.target) onClose(); }}>
    <section aria-modal="true" className="modal-card checklist-assignment-modal" role="dialog">
      <div className="modal-heading"><div><p className="eyebrow teal">TWO-PERSON ASSIGNMENT</p><h2>分配独立答题与 Checklist 复核</h2><p className="modal-patient">{item.id} · {taskLabel(item.primary_task)}</p></div><button aria-label="关闭" className="icon-button" onClick={onClose} type="button">×</button></div>
      <div className="assignment-role-explainer"><article><span>1</span><div><strong>答题医生</strong><p>只看病例与题目，系统不会下发 Checklist。</p></div></article><article><span>2</span><div><strong>复核医生</strong><p>答案提交后，查看答案并逐条修订 Checklist。</p></div></article></div>
      <label>答题医生<select value={answererId} onChange={(event) => setAnswererId(event.target.value)}><option value="">请选择初标医生</option>{answerers.map((user) => <option disabled={user.id === reviewerId} key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
      <label>Checklist 复核医生<select value={reviewerId} onChange={(event) => setReviewerId(event.target.value)}><option value="">请选择审核医生</option>{reviewers.map((user) => <option disabled={user.id === answererId} key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
      <p className="modal-hint">只有这两位医生能看到本题；其他用户不会进入该任务。</p>
      <Notice text={error} />
      <div className="modal-actions">{canClear && <button className="danger-text-button" disabled={busy} onClick={() => void save(true)} type="button">取消分配</button>}<span className="modal-action-spacer" /><button className="secondary" onClick={onClose} type="button">取消</button><button className="primary" disabled={busy} onClick={() => void save()} type="button">{busy ? "保存中…" : "确认分配"}</button></div>
    </section>
  </div>;
}

function CaseBrief({ detail }: { detail: ChecklistFeedbackDetail }) {
  const context = Object.entries(detail.case_payload.patient_context || {});
  return <section className="checklist-case-brief">
    <div className="checklist-question-heading"><div><p className="eyebrow teal">CLINICAL QUESTION</p><h2>{detail.case_payload.question}</h2></div><span className={`complexity-chip ${detail.screening_complexity}`}>预筛复杂度 {COMPLEXITY_LABELS[detail.screening_complexity]}</span></div>
    <div className="patient-context-grid">{context.map(([key, value]) => <article key={key}><span>{key.replaceAll("_", " ")}</span><p>{value || "未提供"}</p></article>)}</div>
    <div className="response-contract"><strong>回答结构</strong>{detail.response_sections.map((section, index) => <span key={section}>{index + 1}. {section}</span>)}</div>
  </section>;
}

function AnswerSummary({ answer }: { answer: ChecklistFeedbackDetail["answer"] }) {
  if (!answer) return <div className="checklist-empty"><span>◌</span><h3>尚无独立答案</h3><p>等待答题医生保存或提交。</p></div>;
  return <section className="answer-summary">
    <div className="section-heading"><div><p className="eyebrow teal">INDEPENDENT ANSWER · V{answer.version}</p><h3>独立作答结果</h3></div><span className={`version-state ${answer.state}`}>{answer.state === "submitted" ? "已提交" : "草稿"}</span></div>
    <div className="answer-section-list">{Object.entries(answer.payload.sections || {}).map(([section, value]) => <article key={section}><strong>{section}</strong><p>{value || "未填写"}</p></article>)}</div>
    <div className="answer-feedback-grid"><article><span>答题者评估复杂度</span><strong>{answer.payload.perceived_complexity ? COMPLEXITY_LABELS[answer.payload.perceived_complexity] : "未评估"}</strong></article><article><span>预计用时</span><strong>{answer.payload.estimated_minutes ? `${answer.payload.estimated_minutes} 分钟` : "未填写"}</strong></article><article><span>现有信息是否充分</span><strong>{answer.payload.visible_information_is_sufficient === null ? "未评估" : answer.payload.visible_information_is_sufficient ? "充分" : "不充分"}</strong></article></div>
    {answer.payload.ambiguities_or_missing_information && <div className="answer-note"><strong>歧义或缺失信息</strong><p>{answer.payload.ambiguities_or_missing_information}</p></div>}
    {answer.payload.comments && <div className="answer-note"><strong>补充反馈</strong><p>{answer.payload.comments}</p></div>}
  </section>;
}

function AnswerForm({ detail, auth, onSaved }: { detail: ChecklistFeedbackDetail; auth: AuthState; onSaved: (detail: ChecklistFeedbackDetail, message: string) => void }) {
  const existing = detail.answer?.payload;
  const [sections, setSections] = useState<Record<string, string>>(() => Object.fromEntries(detail.response_sections.map((section) => [section, existing?.sections?.[section] || ""])));
  const [complexity, setComplexity] = useState<"low" | "medium" | "high" | "">(existing?.perceived_complexity || "");
  const [minutes, setMinutes] = useState(existing?.estimated_minutes ? String(existing.estimated_minutes) : "");
  const [sufficient, setSufficient] = useState(boolValue(existing?.visible_information_is_sufficient));
  const [ambiguities, setAmbiguities] = useState(existing?.ambiguities_or_missing_information || "");
  const [comments, setComments] = useState(existing?.comments || "");
  const [busy, setBusy] = useState<"draft" | "submit" | "">("");
  const [error, setError] = useState("");
  const locked = !detail.can_answer;

  const save = async (submit: boolean) => {
    if (submit && !window.confirm("提交后答案将锁定，并向指定复核医生开放 Checklist 复核。确认提交吗？")) return;
    setBusy(submit ? "submit" : "draft");
    setError("");
    try {
      const next = await api<ChecklistFeedbackDetail>(
        `/api/checklist-feedback/${encodeURIComponent(detail.id)}/answer`,
        {
          method: "POST",
          body: JSON.stringify({
            submit,
            sections,
            perceived_complexity: complexity || null,
            estimated_minutes: minutes ? Number(minutes) : null,
            visible_information_is_sufficient: parseBool(sufficient),
            ambiguities_or_missing_information: ambiguities,
            comments,
          }),
        },
        auth.csrf_token,
      );
      onSaved(next, submit ? "独立答案已提交，复核医生现在可以开始工作。" : `草稿 v${next.latest_answer_version} 已保存。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "答案保存失败");
    } finally {
      setBusy("");
    }
  };

  return <section className="checklist-work-form answer-work-form">
    <div className="blind-mode-banner"><span>◉</span><div><strong>独立作答模式</strong><p>当前页面不会显示 Checklist。请仅依据病例信息完成回答。</p></div></div>
    <div className="section-heading"><div><p className="eyebrow teal">STAGE 1</p><h3>{locked ? "已提交的独立答案" : "填写独立答案"}</h3></div>{detail.answer && <span className={`version-state ${detail.answer.state}`}>v{detail.answer.version} · {detail.answer.state === "submitted" ? "已提交" : "草稿"}</span>}</div>
    <div className="answer-input-list">{detail.response_sections.map((section, index) => <label key={section}><span><i>{index + 1}</i>{section}</span><textarea disabled={locked} onChange={(event) => setSections((current) => ({ ...current, [section]: event.target.value }))} placeholder={`请填写${section}…`} rows={4} value={sections[section] || ""} /></label>)}</div>
    <div className="form-grid three"><label>题目复杂度<select disabled={locked} onChange={(event) => setComplexity(event.target.value as typeof complexity)} value={complexity}><option value="">请选择</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label><label>预计完成用时（分钟）<input disabled={locked} min="1" max="480" onChange={(event) => setMinutes(event.target.value)} type="number" value={minutes} /></label><label>现有信息是否充分<select disabled={locked} onChange={(event) => setSufficient(event.target.value)} value={sufficient}><option value="">请选择</option><option value="true">充分</option><option value="false">不充分</option></select></label></div>
    <label>歧义或缺失的临床信息<textarea disabled={locked} onChange={(event) => setAmbiguities(event.target.value)} placeholder="哪些信息缺失会影响作答或判断？" rows={3} value={ambiguities} /></label>
    <label>其他反馈<textarea disabled={locked} onChange={(event) => setComments(event.target.value)} placeholder="对题目表述、可答性或工作量的其他意见" rows={3} value={comments} /></label>
    <Notice text={error} />
    {!locked && <div className="sticky-form-actions"><span>每次保存都会生成本地版本记录</span><button className="secondary" disabled={Boolean(busy)} onClick={() => void save(false)} type="button">{busy === "draft" ? "保存中…" : "保存草稿"}</button><button className="primary" disabled={Boolean(busy)} onClick={() => void save(true)} type="button">{busy === "submit" ? "提交中…" : "提交独立答案"}</button></div>}
  </section>;
}

function defaultReview(detail: ChecklistFeedbackDetail): ChecklistReviewPayload {
  const existing = detail.review?.payload;
  const criteria = detail.checklist_payload?.criteria || [];
  return {
    case_decision: existing?.case_decision ?? null,
    checklist_necessity: existing?.checklist_necessity ?? null,
    necessity_rationale: existing?.necessity_rationale || "",
    final_complexity: existing?.final_complexity ?? null,
    complexity_rationale: existing?.complexity_rationale || "",
    weight_total_appropriate: existing?.weight_total_appropriate ?? null,
    recommended_total_weight: existing?.recommended_total_weight ?? null,
    weight_comments: existing?.weight_comments || "",
    task_target_is_valid: existing?.task_target_is_valid ?? null,
    visible_information_is_sufficient: existing?.visible_information_is_sufficient ?? null,
    contains_answer_leakage: existing?.contains_answer_leakage ?? null,
    missing_clinical_evidence: existing?.missing_clinical_evidence || [],
    criterion_reviews: criteria.map((criterion) => existing?.criterion_reviews?.find((item) => item.criterion_id === criterion.criterion_id) || { criterion_id: criterion.criterion_id, decision: null, comments: "" }),
    new_criteria: existing?.new_criteria || [],
    guideline_review: existing?.guideline_review || {},
    safety_review: existing?.safety_review || {},
    overall_comments: existing?.overall_comments || "",
  };
}

function ReviewForm({ detail, auth, onSaved }: { detail: ChecklistFeedbackDetail; auth: AuthState; onSaved: (detail: ChecklistFeedbackDetail, message: string) => void }) {
  const criteria = detail.checklist_payload?.criteria || [];
  const [review, setReview] = useState<ChecklistReviewPayload>(() => defaultReview(detail));
  const [busy, setBusy] = useState<"draft" | "submit" | "">("");
  const [error, setError] = useState("");
  const locked = !detail.can_review;
  const guideline = review.guideline_review as { basis_is_appropriate?: boolean | null; revisions?: string[]; comments?: string };
  const safety = review.safety_review as { missing_safety_criteria?: string[]; overly_strict_criteria?: string[]; comments?: string };

  const updateCriterion = (index: number, patch: Partial<ChecklistCriterionReview>) => setReview((current) => ({
    ...current,
    criterion_reviews: current.criterion_reviews.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item),
  }));
  const chooseDecision = (index: number, criterion: ChecklistCriterion, decision: "keep" | "edit" | "delete") => {
    const current = review.criterion_reviews[index];
    updateCriterion(index, decision === "edit" ? {
      decision,
      revised_type: current.revised_type ?? criterion.type,
      revised_statement: current.revised_statement ?? criterion.statement,
      revised_weight: current.revised_weight ?? criterion.weight,
      revised_reference_evidence: current.revised_reference_evidence ?? criterion.reference_evidence,
      revised_accepted_variants: current.revised_accepted_variants ?? criterion.accepted_variants,
      revised_violation_condition: current.revised_violation_condition ?? criterion.violation_condition,
    } : { decision });
  };
  const addCriterion = () => {
    const used = new Set([...criteria, ...review.new_criteria].map((criterion) => criterion.criterion_id));
    let index = 1;
    while (used.has(`N${String(index).padStart(2, "0")}`)) index += 1;
    setReview((current) => ({ ...current, new_criteria: [...current.new_criteria, { criterion_id: `N${String(index).padStart(2, "0")}`, category: "", type: "required", statement: "", weight: 1, reference_evidence: [], accepted_variants: [], violation_condition: null }] }));
  };
  const updateNewCriterion = (index: number, patch: Partial<ChecklistCriterion>) => setReview((current) => ({ ...current, new_criteria: current.new_criteria.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item) }));

  const save = async (submit: boolean) => {
    if (submit && !window.confirm("提交后本次 Checklist 修订将锁定并完成任务。确认提交吗？")) return;
    setBusy(submit ? "submit" : "draft");
    setError("");
    try {
      const next = await api<ChecklistFeedbackDetail>(
        `/api/checklist-feedback/${encodeURIComponent(detail.id)}/review`,
        { method: "POST", body: JSON.stringify({ ...review, submit }) },
        auth.csrf_token,
      );
      onSaved(next, submit ? "Checklist 反馈与修订已提交，任务完成。" : `复核草稿 v${next.latest_review_version} 已保存。`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Checklist 复核保存失败");
    } finally {
      setBusy("");
    }
  };

  return <section className="checklist-work-form review-work-form">
    <AnswerSummary answer={detail.answer} />
    <div className="review-unlocked-banner"><span>✓</span><div><strong>阶段二已解锁</strong><p>独立答案已锁定。请对照原 Checklist 修订评分项，并反馈必要性、复杂度和权重。</p></div></div>
    <section className="review-overview-section">
      <div className="section-heading"><div><p className="eyebrow teal">OVERALL REVIEW</p><h3>题目与 Checklist 总体评估</h3></div>{detail.review && <span className={`version-state ${detail.review.state}`}>v{detail.review.version} · {detail.review.state === "submitted" ? "已提交" : "草稿"}</span>}</div>
      <div className="form-grid three"><label>病例决策<select disabled={locked} value={review.case_decision || ""} onChange={(event) => setReview((current) => ({ ...current, case_decision: (event.target.value || null) as ChecklistReviewPayload["case_decision"] }))}><option value="">请选择</option><option value="approve">可直接使用</option><option value="revise">修改后使用</option><option value="reject">不建议使用</option></select></label><label>Checklist 是否必要<select disabled={locked} value={review.checklist_necessity || ""} onChange={(event) => setReview((current) => ({ ...current, checklist_necessity: (event.target.value || null) as ChecklistReviewPayload["checklist_necessity"] }))}><option value="">请选择</option><option value="necessary">必要</option><option value="partly_necessary">部分必要</option><option value="unnecessary">不必要</option></select></label><label>最终复杂度<select disabled={locked} value={review.final_complexity || ""} onChange={(event) => setReview((current) => ({ ...current, final_complexity: (event.target.value || null) as ChecklistReviewPayload["final_complexity"] }))}><option value="">请选择</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label></div>
      <label>Checklist 必要性理由<textarea disabled={locked} rows={3} value={review.necessity_rationale} onChange={(event) => setReview((current) => ({ ...current, necessity_rationale: event.target.value }))} placeholder="它是否有助于稳定评分、覆盖关键事实或安全边界？" /></label>
      <label>复杂度判断理由<textarea disabled={locked} rows={3} value={review.complexity_rationale} onChange={(event) => setReview((current) => ({ ...current, complexity_rationale: event.target.value }))} /></label>
      <div className="form-grid three"><label>总权重是否合理<select disabled={locked} value={boolValue(review.weight_total_appropriate)} onChange={(event) => setReview((current) => ({ ...current, weight_total_appropriate: parseBool(event.target.value) }))}><option value="">请选择</option><option value="true">合理</option><option value="false">不合理</option></select></label><label>建议总权重<input disabled={locked || review.weight_total_appropriate !== false} min="1" type="number" value={review.recommended_total_weight ?? ""} onChange={(event) => setReview((current) => ({ ...current, recommended_total_weight: event.target.value ? Number(event.target.value) : null }))} /></label><label>原 Checklist 总权重<input disabled value={detail.original_total_weight} /></label></div>
      <label>权重反馈<textarea disabled={locked} rows={3} value={review.weight_comments} onChange={(event) => setReview((current) => ({ ...current, weight_comments: event.target.value }))} /></label>
      <div className="form-grid three"><label>任务目标有效<select disabled={locked} value={boolValue(review.task_target_is_valid)} onChange={(event) => setReview((current) => ({ ...current, task_target_is_valid: parseBool(event.target.value) }))}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select></label><label>可见信息充分<select disabled={locked} value={boolValue(review.visible_information_is_sufficient)} onChange={(event) => setReview((current) => ({ ...current, visible_information_is_sufficient: parseBool(event.target.value) }))}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select></label><label>包含答案泄露<select disabled={locked} value={boolValue(review.contains_answer_leakage)} onChange={(event) => setReview((current) => ({ ...current, contains_answer_leakage: parseBool(event.target.value) }))}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select></label></div>
      <label>缺失的临床证据（每行一项）<textarea disabled={locked} rows={3} value={review.missing_clinical_evidence.join("\n")} onChange={(event) => setReview((current) => ({ ...current, missing_clinical_evidence: lines(event.target.value) }))} /></label>
    </section>

    <section className="criteria-review-section">
      <div className="section-heading"><div><p className="eyebrow teal">CRITERION REVIEW</p><h3>逐条修订评分项</h3><p>{criteria.length} 条原始评分项 · 原总权重 {detail.original_total_weight}</p></div><button className="secondary" disabled={locked} onClick={addCriterion} type="button">＋ 新增评分项</button></div>
      <div className="criterion-review-list">{criteria.map((criterion, index) => { const item = review.criterion_reviews[index]; return <article className={`criterion-review-card ${item?.decision || "pending"}`} key={criterion.criterion_id}>
        <header><div><span className={`criterion-type ${criterion.type}`}>{criterion.type === "required" ? "必需" : criterion.type === "uncertainty" ? "不确定性" : "禁止"}</span><strong>{criterion.criterion_id} · {criterion.category}</strong></div><b>{criterion.weight} 分</b></header>
        <p className="criterion-statement">{criterion.statement}</p>
        {criterion.reference_evidence.length > 0 && <div className="criterion-evidence"><span>证据</span>{criterion.reference_evidence.map((evidence) => <em key={evidence}>{evidence}</em>)}</div>}
        <div className="criterion-decision"><button className={item?.decision === "keep" ? "active keep" : ""} disabled={locked} onClick={() => chooseDecision(index, criterion, "keep")} type="button">保留</button><button className={item?.decision === "edit" ? "active edit" : ""} disabled={locked} onClick={() => chooseDecision(index, criterion, "edit")} type="button">修改</button><button className={item?.decision === "delete" ? "active delete" : ""} disabled={locked} onClick={() => chooseDecision(index, criterion, "delete")} type="button">删除</button></div>
        {item?.decision === "edit" && <div className="criterion-edit-grid"><div className="form-grid two"><label>类型<select disabled={locked} value={item.revised_type || criterion.type} onChange={(event) => updateCriterion(index, { revised_type: event.target.value as ChecklistCriterion["type"] })}><option value="required">必需</option><option value="uncertainty">不确定性</option><option value="prohibited">禁止</option></select></label><label>权重<input disabled={locked} min="1" max="100" type="number" value={item.revised_weight ?? criterion.weight} onChange={(event) => updateCriterion(index, { revised_weight: Number(event.target.value) })} /></label></div><label>修改后的描述<textarea disabled={locked} rows={3} value={item.revised_statement ?? criterion.statement} onChange={(event) => updateCriterion(index, { revised_statement: event.target.value })} /></label><label>参考证据（每行一项）<textarea disabled={locked} rows={2} value={(item.revised_reference_evidence ?? criterion.reference_evidence).join("\n")} onChange={(event) => updateCriterion(index, { revised_reference_evidence: lines(event.target.value) })} /></label><label>可接受表述（每行一项）<textarea disabled={locked} rows={2} value={(item.revised_accepted_variants ?? criterion.accepted_variants).join("\n")} onChange={(event) => updateCriterion(index, { revised_accepted_variants: lines(event.target.value) })} /></label>{(item.revised_type ?? criterion.type) === "prohibited" && <label>触发违规条件<textarea disabled={locked} rows={2} value={item.revised_violation_condition ?? criterion.violation_condition ?? ""} onChange={(event) => updateCriterion(index, { revised_violation_condition: event.target.value })} /></label>}</div>}
        <label className="criterion-comment">本项意见<textarea disabled={locked} rows={2} value={item?.comments || ""} onChange={(event) => updateCriterion(index, { comments: event.target.value })} /></label>
      </article>; })}</div>
      {review.new_criteria.length > 0 && <div className="new-criteria-list"><h4>新增评分项</h4>{review.new_criteria.map((criterion, index) => <article className="criterion-review-card new" key={`${criterion.criterion_id}-${index}`}><header><strong>新增项 {index + 1}</strong><button className="danger-text-button" disabled={locked} onClick={() => setReview((current) => ({ ...current, new_criteria: current.new_criteria.filter((_, itemIndex) => itemIndex !== index) }))} type="button">移除</button></header><div className="form-grid three"><label>评分项 ID<input disabled={locked} value={criterion.criterion_id} onChange={(event) => updateNewCriterion(index, { criterion_id: event.target.value })} /></label><label>类别<input disabled={locked} value={criterion.category} onChange={(event) => updateNewCriterion(index, { category: event.target.value })} /></label><label>权重<input disabled={locked} min="1" type="number" value={criterion.weight} onChange={(event) => updateNewCriterion(index, { weight: Number(event.target.value) })} /></label></div><label>类型<select disabled={locked} value={criterion.type} onChange={(event) => updateNewCriterion(index, { type: event.target.value as ChecklistCriterion["type"] })}><option value="required">必需</option><option value="uncertainty">不确定性</option><option value="prohibited">禁止</option></select></label><label>评分描述<textarea disabled={locked} rows={3} value={criterion.statement} onChange={(event) => updateNewCriterion(index, { statement: event.target.value })} /></label><label>参考证据（每行一项）<textarea disabled={locked} rows={2} value={criterion.reference_evidence.join("\n")} onChange={(event) => updateNewCriterion(index, { reference_evidence: lines(event.target.value) })} /></label></article>)}</div>}
    </section>

    <section className="review-overview-section"><div className="section-heading"><div><p className="eyebrow teal">GUIDELINE & SAFETY</p><h3>指南依据与安全性</h3></div></div><div className="form-grid two"><label>指南依据是否适当<select disabled={locked} value={boolValue(guideline.basis_is_appropriate)} onChange={(event) => setReview((current) => ({ ...current, guideline_review: { ...guideline, basis_is_appropriate: parseBool(event.target.value) } }))}><option value="">请选择</option><option value="true">适当</option><option value="false">需调整</option></select></label><label>指南修订建议（每行一项）<textarea disabled={locked} rows={3} value={(guideline.revisions || []).join("\n")} onChange={(event) => setReview((current) => ({ ...current, guideline_review: { ...guideline, revisions: lines(event.target.value) } }))} /></label></div><label>指南依据意见<textarea disabled={locked} rows={3} value={guideline.comments || ""} onChange={(event) => setReview((current) => ({ ...current, guideline_review: { ...guideline, comments: event.target.value } }))} /></label><div className="form-grid two"><label>缺失的安全评分项（每行一项）<textarea disabled={locked} rows={3} value={(safety.missing_safety_criteria || []).join("\n")} onChange={(event) => setReview((current) => ({ ...current, safety_review: { ...safety, missing_safety_criteria: lines(event.target.value) } }))} /></label><label>过严的评分项（每行一项）<textarea disabled={locked} rows={3} value={(safety.overly_strict_criteria || []).join("\n")} onChange={(event) => setReview((current) => ({ ...current, safety_review: { ...safety, overly_strict_criteria: lines(event.target.value) } }))} /></label></div><label>安全性意见<textarea disabled={locked} rows={3} value={safety.comments || ""} onChange={(event) => setReview((current) => ({ ...current, safety_review: { ...safety, comments: event.target.value } }))} /></label><label>总体反馈<textarea disabled={locked} rows={4} value={review.overall_comments} onChange={(event) => setReview((current) => ({ ...current, overall_comments: event.target.value }))} /></label></section>
    <Notice text={error} />
    {!locked && <div className="sticky-form-actions"><span>提交后会同时保存反馈和修订后的 Checklist</span><button className="secondary" disabled={Boolean(busy)} onClick={() => void save(false)} type="button">{busy === "draft" ? "保存中…" : "保存复核草稿"}</button><button className="primary" disabled={Boolean(busy)} onClick={() => void save(true)} type="button">{busy === "submit" ? "提交中…" : "提交并完成任务"}</button></div>}
  </section>;
}

function AdminAudit({ detail }: { detail: ChecklistFeedbackDetail }) {
  const criteria = detail.checklist_payload?.criteria || [];
  return <div className="admin-checklist-audit">
    <section className="review-overview-section"><div className="section-heading"><div><p className="eyebrow teal">SOURCE CHECKLIST</p><h3>原始 Checklist</h3><p>{criteria.length} 条评分项 · 总权重 {detail.original_total_weight}</p></div></div><div className="source-criteria-list">{criteria.map((criterion) => <article key={criterion.criterion_id}><header><span className={`criterion-type ${criterion.type}`}>{criterion.type}</span><strong>{criterion.criterion_id}</strong><b>{criterion.weight} 分</b></header><p>{criterion.statement}</p></article>)}</div></section>
    <AnswerSummary answer={detail.answer} />
    {detail.review ? <section className="review-result"><div className="section-heading"><div><p className="eyebrow teal">CHECKLIST REVIEW · V{detail.review.version}</p><h3>复核反馈与修订结果</h3></div><span className={`version-state ${detail.review.state}`}>{detail.review.state === "submitted" ? "已提交" : "草稿"}</span></div><div className="review-result-metrics"><article><span>Checklist 必要性</span><strong>{detail.review.payload.checklist_necessity === "necessary" ? "必要" : detail.review.payload.checklist_necessity === "partly_necessary" ? "部分必要" : detail.review.payload.checklist_necessity === "unnecessary" ? "不必要" : "未评估"}</strong></article><article><span>最终复杂度</span><strong>{detail.review.payload.final_complexity ? COMPLEXITY_LABELS[detail.review.payload.final_complexity] : "未评估"}</strong></article><article><span>修订后权重</span><strong>{detail.review.payload.revised_total_weight ?? "—"}</strong></article><article><span>改动评分项</span><strong>{detail.review.payload.changed_criterion_count ?? 0}</strong></article></div><div className="answer-note"><strong>必要性理由</strong><p>{detail.review.payload.necessity_rationale || "未填写"}</p></div><div className="answer-note"><strong>总体反馈</strong><p>{detail.review.payload.overall_comments || "未填写"}</p></div></section> : <div className="checklist-empty"><span>⌛</span><h3>尚无 Checklist 复核</h3><p>{detail.answer?.state === "submitted" ? "等待复核医生处理。" : "独立答案提交后进入复核阶段。"}</p></div>}
  </div>;
}

function ChecklistDetailView({ detail, auth, onBack, onAssign, onSaved }: { detail: ChecklistFeedbackDetail; auth: AuthState; onBack: () => void; onAssign: () => void; onSaved: (detail: ChecklistFeedbackDetail, message: string) => void }) {
  const admin = hasRole(auth.user, "admin");
  const stageIndex = detail.status === "completed" ? 3 : ["answer_submitted", "reviewing"].includes(detail.status) ? 2 : 1;
  return <div className="checklist-detail-page">
    <div className="checklist-detail-toolbar"><button className="back-link" onClick={onBack} type="button">← 返回题目列表</button><div><StatusPill status={detail.status} />{admin && <button className="secondary compact" onClick={onAssign} type="button">分配参与者</button>}</div></div>
    <section className="checklist-detail-hero"><div><p className="eyebrow teal">CHECKLIST FEEDBACK · {detail.id}</p><h1>{taskLabel(detail.primary_task)}</h1><p>{subtypeLabel(detail.target_subtype)} · 两人顺序协作，只向指定医生开放</p></div><div className="assigned-pair"><article><span>答</span><div><small>独立答题</small><strong>{detail.answerer?.display_name || "未分配"}</strong></div></article><i>→</i><article><span>审</span><div><small>Checklist 复核</small><strong>{detail.reviewer?.display_name || "未分配"}</strong></div></article></div></section>
    <ol className="checklist-stage-track"><li className={stageIndex >= 1 ? "active" : ""}><span>1</span><div><strong>独立答题</strong><small>不展示 Checklist</small></div></li><li className={stageIndex >= 2 ? "active" : ""}><span>2</span><div><strong>Checklist 复核</strong><small>对照答案修订</small></div></li><li className={stageIndex >= 3 ? "active" : ""}><span>3</span><div><strong>完成反馈</strong><small>本地版本留档</small></div></li></ol>
    <CaseBrief detail={detail} />
    {detail.my_role === "answerer" && <AnswerForm auth={auth} detail={detail} key={`answer-${detail.answer?.version || 0}-${detail.answer?.state || "new"}`} onSaved={onSaved} />}
    {detail.my_role === "reviewer" && detail.waiting_for_answer && <div className="checklist-waiting"><span>⌛</span><h2>等待独立答案</h2><p>答题医生提交后，系统才会在这里开放答案和原 Checklist。</p></div>}
    {detail.my_role === "reviewer" && !detail.waiting_for_answer && <ReviewForm auth={auth} detail={detail} key={`review-${detail.review?.version || 0}-${detail.review?.state || "new"}`} onSaved={onSaved} />}
    {detail.my_role === "admin" && <AdminAudit detail={detail} />}
  </div>;
}

export function ChecklistFeedbackWorkspace({ auth, users }: Props) {
  const [dashboard, setDashboard] = useState<Dashboard>({ counts: { unassigned: 0, assigned: 0, answering: 0, answer_submitted: 0, reviewing: 0, completed: 0 }, total: 0 });
  const [items, setItems] = useState<ChecklistFeedbackItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState<ChecklistFeedbackStatus | "">("");
  const [search, setSearch] = useState("");
  const [detail, setDetail] = useState<ChecklistFeedbackDetail | null>(null);
  const [assigning, setAssigning] = useState<ChecklistFeedbackItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const pageSize = 20;
  const admin = hasRole(auth.user, "admin");

  const load = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (status) query.set("status", status);
    if (search.trim()) query.set("search", search.trim());
    const [list, overview] = await Promise.all([
      api<{ items: ChecklistFeedbackItem[]; total: number }>(`/api/checklist-feedback?${query}`),
      api<Dashboard>("/api/checklist-feedback/dashboard"),
    ]);
    setItems(list.items);
    setTotal(list.total);
    setDashboard(overview);
  }, [page, search, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setLoading(true);
      void load().catch((reason) => setError(reason instanceof Error ? reason.message : "Checklist 任务加载失败")).finally(() => setLoading(false));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [load]);

  const open = async (item: ChecklistFeedbackItem) => {
    setError("");
    try { setDetail(await api<ChecklistFeedbackDetail>(`/api/checklist-feedback/${encodeURIComponent(item.id)}`)); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "题目加载失败"); }
  };
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const ongoing = dashboard.counts.assigned + dashboard.counts.answering + dashboard.counts.answer_submitted + dashboard.counts.reviewing;
  const visibleTabs = useMemo(() => admin ? STATUS_TABS : STATUS_TABS.filter((tab) => tab.key !== "unassigned"), [admin]);
  const onDetailSaved = (next: ChecklistFeedbackDetail, text: string) => { setDetail(next); setMessage(text); void load(); };

  if (detail) return <>
    <Notice success text={message} /><Notice text={error} />
    <ChecklistDetailView auth={auth} detail={detail} onAssign={() => setAssigning(detail)} onBack={() => { setDetail(null); setMessage(""); void load(); }} onSaved={onDetailSaved} />
    {assigning && <AssignmentDialog auth={auth} item={assigning} users={users} onClose={() => setAssigning(null)} onSaved={(next) => { setAssigning(null); setDetail(next); setMessage("参与者分配已更新。未被选中的医生不会看到本题。"); void load(); }} />}
  </>;

  return <div className="checklist-feedback-home">
    <Notice success text={message} /><Notice text={error} />
    <section className="metrics-grid checklist-metrics"><article><span className="metric-icon teal">◎</span><div><p>{admin ? "全部题目" : "分配给我"}</p><strong>{dashboard.total}</strong></div><small>当前可访问</small></article><article><span className="metric-icon amber">↗</span><div><p>进行中</p><strong>{ongoing}</strong></div><small>双人顺序协作</small></article><article><span className="metric-icon blue">◫</span><div><p>待复核</p><strong>{dashboard.counts.answer_submitted}</strong></div><small>答案已提交</small></article><article><span className="metric-icon green">✓</span><div><p>已完成</p><strong>{dashboard.counts.completed}</strong></div><small>反馈已留档</small></article></section>
    <section className="panel checklist-list-panel">
      <div className="checklist-list-intro"><div><p className="eyebrow teal">INDEPENDENT TWO-PERSON REVIEW</p><h2>临床题目与 Checklist 反馈</h2><p>每道题只分配一位答题医生和一位复核医生，不是全员任务。</p></div>{admin && <span className="unassigned-summary">{dashboard.counts.unassigned}<small>题待分配</small></span>}</div>
      <div className="case-controls checklist-controls"><div className="tabs">{visibleTabs.map((tab) => <button className={status === tab.key ? "active" : ""} key={tab.key || "all"} onClick={() => { setStatus(tab.key); setPage(1); }}>{tab.label}<span>{tab.key ? dashboard.counts[tab.key] : dashboard.total}</span></button>)}</div><label className="search-box"><span>⌕</span><input onChange={(event) => { setSearch(event.target.value); setPage(1); }} placeholder="搜索题目编号、类型或亚型" value={search} /></label></div>
      <div className="table-wrap checklist-table-wrap"><table><thead><tr><th>题目</th><th>类型与复杂度</th><th>阶段</th><th>答题医生</th><th>复核医生</th><th>版本</th><th /></tr></thead><tbody>{items.map((item) => <tr key={item.id}><td><div className="checklist-case-cell"><span>{item.id.startsWith("T2") ? "T2" : item.id.startsWith("T4") ? "T4" : "Q"}</span><div><strong>{item.id}</strong><p title={item.question}>{item.question}</p></div></div></td><td><strong>{taskLabel(item.primary_task)}</strong><span className="table-sub">{subtypeLabel(item.target_subtype)} · 复杂度 {COMPLEXITY_LABELS[item.screening_complexity]}</span></td><td><StatusPill status={item.status} /></td><td>{item.answerer?.display_name || <span className="muted">未分配</span>}</td><td>{item.reviewer?.display_name || <span className="muted">未分配</span>}</td><td><span className="version-chip">答 v{item.latest_answer_version}</span> <span className="version-chip">审 v{item.latest_review_version}</span></td><td><div className="row-actions">{admin && <button className="text-button" onClick={() => setAssigning(item)} type="button">分配</button>}<button className="open-button" onClick={() => void open(item)} type="button">打开 →</button></div></td></tr>)}</tbody></table>{!loading && items.length === 0 && <div className="empty-state"><span>◎</span><h3>{admin ? "当前筛选下没有题目" : "暂时没有分配给你的 Checklist 任务"}</h3><p>{admin ? "可切换筛选条件查看其他阶段。" : "管理员定向选择你后，对应题目才会出现在这里。"}</p></div>}{loading && <div className="checklist-loading">正在加载题目…</div>}</div>
      <footer className="table-footer"><div className="page-summary">第 {page} / {pageCount} 页 · 共 {total} 题</div><nav className="pagination"><button disabled={page <= 1} onClick={() => setPage((current) => Math.max(1, current - 1))} type="button">‹</button><button disabled={page >= pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))} type="button">›</button></nav></footer>
    </section>
    {assigning && <AssignmentDialog auth={auth} item={assigning} users={users} onClose={() => setAssigning(null)} onSaved={() => { setAssigning(null); setMessage("参与者分配已更新。未被选中的医生不会看到本题。"); void load(); }} />}
  </div>;
}
