"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "../lib/api";
import { prefetchImageBuffer, prefetchJson } from "../lib/imagePrefetch";
import { AuthState, CaseDetail, CaseItem, CaseStatus, CollaborationTask, CollaborationTaskDetail, CollaborationTaskStatus, hasRole, Project, ROLE_LABELS, Role, roleSummary, STATUS_LABELS, TaskPriority, TaskProgress, User } from "../lib/types";
import { CodexTaskPublisher } from "./CodexTaskPublisher";
import { ChecklistFeedbackWorkspace } from "./ChecklistFeedbackWorkspace";
import { NiiVueViewer } from "./NiiVueViewer";
import { PdffRoiViewer } from "./PdffRoiViewer";

type View = "cases" | "tasks" | "checklist" | "users";

function BrandMark() {
  return <span className="brand-mark" aria-hidden="true"><i /><i /><i /></span>;
}

function Notice({ text, tone = "error" }: { text: string; tone?: "error" | "success" }) {
  return text ? <div className={`notice ${tone}`}>{text}</div> : null;
}

function Login({ onLogin }: { onLogin: (auth: AuthState) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      onLogin(await api<AuthState>("/api/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "登录失败");
    } finally { setBusy(false); }
  };

  return <main className="login-page">
    <div className="login-atmosphere atmosphere-one" /><div className="login-atmosphere atmosphere-two" />
    <section className="login-copy">
      <div className="brand large"><BrandMark /><span>MedContour</span></div>
      <p className="eyebrow">MEDICAL IMAGING WORKSPACE</p>
      <h1>让每一次勾画<br />都有迹可循</h1>
      <p className="lead">面向临床团队的医学影像标注与审核工作台。四序列对照、版本留痕、角色授权，数据只保存在你的设备中。</p>
      <div className="security-strip"><span>本机部署</span><span>原图只读</span><span>操作审计</span></div>
    </section>
    <section className="login-card">
      <div className="login-card-head"><p className="eyebrow teal">AUTHORIZED ACCESS</p><h2>欢迎回来</h2><p>请使用管理员授权的邮箱登录</p></div>
      <form onSubmit={submit}>
        <label>邮箱地址<input autoComplete="username" onChange={(event) => setEmail(event.target.value)} type="email" value={email} /></label>
        <label>密码<input autoComplete="current-password" autoFocus onChange={(event) => setPassword(event.target.value)} placeholder="请输入密码" type="password" value={password} /></label>
        <Notice text={error} /><button className="primary wide" disabled={busy} type="submit">{busy ? "正在验证…" : "安全登录"}</button>
      </form>
      <p className="login-foot">仅限获授权人员使用 · 患者信息受访问控制保护</p>
    </section>
  </main>;
}

function ChangePassword({ auth, onChanged }: { auth: AuthState; onChanged: (user: User) => void }) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (newPassword !== confirm) return setError("两次输入的新密码不一致");
    setBusy(true); setError("");
    try {
      const result = await api<{ user: User }>("/api/auth/change-password", { method: "POST", body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }) }, auth.csrf_token);
      onChanged(result.user);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "密码修改失败"); }
    finally { setBusy(false); }
  };
  return <main className="center-page"><section className="password-card">
    <div className="brand"><BrandMark /><span>MedContour</span></div><p className="eyebrow teal">FIRST SIGN-IN</p><h1>先设置一个新密码</h1><p>当前是一次性初始密码。修改后才会开放病例工作区。</p>
    <form onSubmit={submit}>
      <label>当前密码<input autoComplete="current-password" onChange={(event) => setCurrentPassword(event.target.value)} type="password" /></label>
      <label>新密码<input autoComplete="new-password" minLength={10} onChange={(event) => setNewPassword(event.target.value)} placeholder="至少 10 位" type="password" /></label>
      <label>确认新密码<input autoComplete="new-password" minLength={10} onChange={(event) => setConfirm(event.target.value)} type="password" /></label>
      <Notice text={error} /><button className="primary wide" disabled={busy} type="submit">{busy ? "正在保存…" : "修改并进入平台"}</button>
    </form>
  </section></main>;
}

function StatusBadge({ status }: { status: CaseStatus }) {
  return <span className={`status-badge ${status}`}><i />{STATUS_LABELS[status]}</span>;
}

function visiblePageNumbers(page: number, pageCount: number): number[] {
  const visibleCount = Math.min(5, pageCount);
  const start = Math.max(1, Math.min(page - 2, pageCount - visibleCount + 1));
  return Array.from({ length: visibleCount }, (_, index) => start + index);
}

function AssignmentDialog({ item, users, csrf, onClose, onSaved }: { item: CaseItem; users: User[]; csrf: string; onClose: () => void; onSaved: () => void }) {
  const [annotator, setAnnotator] = useState(item.annotator?.id ?? "");
  const [reviewer, setReviewer] = useState(item.reviewer?.id ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true); setError("");
    try {
      await api(`/api/admin/cases/${item.id}/assign`, { method: "POST", body: JSON.stringify({ annotator_id: annotator || null, reviewer_id: reviewer || null }) }, csrf);
      onSaved();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "分配失败"); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section className="modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}>
    <p className="eyebrow teal">CASE ASSIGNMENT</p><h2>分配病例</h2><p className="modal-patient">{item.case.patient_name_zh} · {item.case.patient_id}</p>
    <label>初标医生<select value={annotator} onChange={(event) => { const value = event.target.value; setAnnotator(value); if (reviewer === value) setReviewer(""); }}><option value="">暂不分配</option>{users.filter((user) => hasRole(user, "annotator") && user.is_active).map((user) => <option key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
    <label>审核医生<select value={reviewer} onChange={(event) => setReviewer(event.target.value)}><option value="">提交后由审核医生领取</option>{users.filter((user) => hasRole(user, "reviewer") && user.is_active && user.id !== annotator).map((user) => <option key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
    <p className="modal-hint">同一位医生可以同时拥有两种工作角色，但不能审核自己初标的病例。</p>
    <Notice text={error} /><div className="modal-actions"><button className="secondary" onClick={onClose} type="button">取消</button><button className="primary" disabled={busy} onClick={() => void save()} type="button">{busy ? "保存中…" : "确认分配"}</button></div>
  </section></div>;
}

function BulkAssignmentDialog({ caseIds, users, csrf, onClose, onSaved }: { caseIds: string[]; users: User[]; csrf: string; onClose: () => void; onSaved: (updated: number) => void }) {
  const keepValue = "__keep__";
  const [annotator, setAnnotator] = useState(keepValue);
  const [reviewer, setReviewer] = useState(keepValue);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const hasChange = annotator !== keepValue || reviewer !== keepValue;
  const save = async () => {
    if (!hasChange) return;
    setBusy(true); setError("");
    try {
      const result = await api<{ updated: number }>("/api/admin/cases/bulk-assign", {
        method: "POST",
        body: JSON.stringify({
          case_ids: caseIds,
          update_annotator: annotator !== keepValue,
          annotator_id: annotator === keepValue ? null : annotator,
          update_reviewer: reviewer !== keepValue,
          reviewer_id: reviewer === keepValue ? null : reviewer,
        }),
      }, csrf);
      onSaved(result.updated);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "批量分配失败"); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section className="modal bulk-assignment-modal" role="dialog" aria-modal="true" aria-label="批量分配病例" onMouseDown={(event) => event.stopPropagation()}>
    <p className="eyebrow teal">BULK ASSIGNMENT</p><h2>批量分配任务</h2><p className="modal-patient">已选择 {caseIds.length} 个影像任务，可来自不同页</p>
    <label>批量设置初标医生<select aria-label="批量设置初标医生" value={annotator} onChange={(event) => { const value = event.target.value; setAnnotator(value); if (reviewer === value) setReviewer(keepValue); }}><option value={keepValue}>保持每例当前初标医生</option>{users.filter((user) => hasRole(user, "annotator") && user.is_active && user.id !== reviewer).map((user) => <option key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
    <label>批量设置审核医生<select aria-label="批量设置审核医生" value={reviewer} onChange={(event) => { const value = event.target.value; setReviewer(value); if (annotator === value) setAnnotator(keepValue); }}><option value={keepValue}>保持每例当前审核医生</option>{users.filter((user) => hasRole(user, "reviewer") && user.is_active && user.id !== annotator).map((user) => <option key={user.id} value={user.id}>{user.display_name} · {user.email}</option>)}</select></label>
    <p className="modal-hint">已有标注版本的病例仍可批量设置审核医生，但不能更换初标医生，以免破坏版本归属。</p>
    <Notice text={error} /><div className="modal-actions"><button className="secondary" onClick={onClose} type="button">取消</button><button className="primary" disabled={busy || !hasChange} onClick={() => void save()} type="button">{busy ? "分配中…" : `确认分配 ${caseIds.length} 例`}</button></div>
  </section></div>;
}

const COLLABORATION_STATUS_LABELS: Record<CollaborationTaskStatus, string> = { published: "待开始", in_progress: "协作中", completed: "已完成" };
const TASK_PRIORITY_LABELS: Record<TaskPriority, string> = { low: "低", normal: "普通", high: "高", urgent: "紧急" };
const TASK_PROGRESS_LABELS: Record<TaskProgress, string> = { todo: "待处理", in_progress: "进行中", done: "已完成" };

function readableFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function readableDateTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function CollaborationTaskDetailDialog({ taskId, auth, onClose, onChanged, onOpenInCodex }: { taskId: string; auth: AuthState; onClose: () => void; onChanged: () => void; onOpenInCodex?: (taskId: string, submissionIds: string[]) => void }) {
  const [task, setTask] = useState<CollaborationTaskDetail | null>(null);
  const [comment, setComment] = useState("");
  const [submissionNote, setSubmissionNote] = useState("");
  const [submissionFiles, setSubmissionFiles] = useState<File[]>([]);
  const [selectedSubmissionIds, setSelectedSubmissionIds] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => setTask(await api<CollaborationTaskDetail>(`/api/collaboration-tasks/${taskId}`)), [taskId]);
  useEffect(() => {
    let cancelled = false;
    void api<CollaborationTaskDetail>(`/api/collaboration-tasks/${taskId}`).then((result) => { if (!cancelled) setTask(result); }).catch((reason) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "任务加载失败"); });
    return () => { cancelled = true; };
  }, [taskId]);

  const updateProgress = async (progress: TaskProgress) => {
    setBusy(true); setError("");
    try {
      await api(`/api/collaboration-tasks/${taskId}/progress`, { method: "POST", body: JSON.stringify({ progress }) }, auth.csrf_token);
      await load(); onChanged();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "进度更新失败"); }
    finally { setBusy(false); }
  };

  const addComment = async (event: FormEvent) => {
    event.preventDefault();
    if (!comment.trim()) return;
    setBusy(true); setError("");
    try {
      await api(`/api/collaboration-tasks/${taskId}/comments`, { method: "POST", body: JSON.stringify({ body: comment.trim() }) }, auth.csrf_token);
      setComment(""); await load(); onChanged();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "留言发送失败"); }
    finally { setBusy(false); }
  };

  const uploadSubmission = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!submissionFiles.length) return;
    const form = event.currentTarget;
    const body = new FormData();
    body.append("note", submissionNote.trim());
    submissionFiles.forEach((file) => body.append("files", file));
    setBusy(true); setError("");
    try {
      await api(`/api/collaboration-tasks/${taskId}/submissions`, { method: "POST", body }, auth.csrf_token);
      setSubmissionNote(""); setSubmissionFiles([]); form.reset(); await load(); onChanged();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "交付文件提交失败"); }
    finally { setBusy(false); }
  };

  const deliverables = task?.task_spec?.deliverables ?? [];
  const steps = task?.task_spec?.steps ?? [];
  const isAdmin = hasRole(auth.user, "admin");

  return <div className="modal-backdrop" role="presentation" onMouseDown={busy ? undefined : onClose}><section aria-label="协作任务详情" aria-modal="true" className="modal collaboration-detail-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
    {!task ? <div className="task-detail-loading"><span className="spinner" />{error || "正在加载任务…"}</div> : <>
      <header className="task-detail-header"><div><div className="task-detail-badges"><span className={`task-priority ${task.priority}`}>{TASK_PRIORITY_LABELS[task.priority]}优先级</span><span className={`collaboration-status ${task.status}`}>{COLLABORATION_STATUS_LABELS[task.status]}</span><span className="task-revision-badge">v{task.view_revision}</span>{task.publication_source === "codex" && <span className="codex-source-badge">✦ Codex 发布</span>}</div><h2>{task.title}</h2><p>由 {task.created_by.display_name} 发布 · {readableDateTime(task.created_at)}{task.due_date ? ` · 截止 ${task.due_date}` : ""}</p></div><button aria-label="关闭任务详情" className="modal-close" disabled={busy} onClick={onClose} type="button">×</button></header>
      <div className="task-detail-layout"><div className="task-detail-main"><section className="task-description"><h3>任务说明</h3><p>{task.description || "发布者未填写补充说明。"}</p></section>
        {(steps.length > 0 || deliverables.length > 0) && <section className="task-runtime-spec"><h3>任务流程与交付</h3>{deliverables.length > 0 && <ul>{deliverables.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul>}{steps.length > 0 && <ol>{steps.map((step, index) => <li key={`${step.label}-${index}`}><strong>{step.label}</strong><span>{step.description}</span></li>)}</ol>}</section>}
        {task.attachments.length > 0 && <section className="task-attachments"><h3>本机附件 <span>{task.attachments.length}</span></h3><div>{task.attachments.map((attachment) => <a href={attachment.download_url} key={attachment.id}><span>↓</span><div><strong>{attachment.name}</strong><small>{readableFileSize(attachment.byte_size)} · v{attachment.revision_number}</small></div></a>)}</div></section>}
        <section className="task-submissions"><h3>交付版本 <span>{task.submissions.length}</span></h3>
          {task.my_active && <form className="task-submission-form" onSubmit={(event) => void uploadSubmission(event)}><textarea disabled={busy} maxLength={4000} onChange={(event) => setSubmissionNote(event.target.value)} placeholder="补充本次交付说明（可选）" rows={2} value={submissionNote} /><div><label className="secondary">＋ 选择交付文件<input disabled={busy} multiple onChange={(event) => setSubmissionFiles(Array.from(event.currentTarget.files || []))} type="file" /></label><span>{submissionFiles.length ? `已选择 ${submissionFiles.length} 个文件` : "每次提交会生成一个新版本，不会覆盖历史文件"}</span><button className="primary compact" disabled={busy || !submissionFiles.length} type="submit">{busy ? "上传中…" : "提交新版本"}</button></div>{submissionFiles.length > 0 && <ul>{submissionFiles.map((file, index) => <li key={`${file.name}-${file.lastModified}-${index}`}>{file.name}<button aria-label={`移除 ${file.name}`} onClick={() => setSubmissionFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))} type="button">×</button></li>)}</ul>}</form>}
          <div className="task-submission-list">{task.submissions.length === 0 ? <p className="task-empty-copy">还没有交付版本。参与者上传后，文件会保存在本机并在这里留存版本。</p> : [...task.submissions].reverse().map((submission) => <article className={selectedSubmissionIds.includes(submission.id) ? "selected" : ""} key={submission.id}><header>{isAdmin && <input aria-label={`选择 ${submission.submitted_by.display_name} 的第 ${submission.version} 版交付`} checked={selectedSubmissionIds.includes(submission.id)} onChange={() => setSelectedSubmissionIds((current) => current.includes(submission.id) ? current.filter((id) => id !== submission.id) : [...current, submission.id])} type="checkbox" />}<span className="avatar">{submission.submitted_by.display_name.slice(0, 1)}</span><div><strong>{submission.submitted_by.display_name} · 交付 v{submission.version}</strong><small>任务 v{submission.task_revision_number} · {readableDateTime(submission.created_at)}</small></div></header>{submission.note && <p>{submission.note}</p>}<div className="task-submission-files">{submission.files.map((file) => <a href={file.download_url} key={file.id}><span>↓</span><div><strong>{file.name}</strong><small>{readableFileSize(file.byte_size)}</small></div></a>)}</div></article>)}</div>
          {isAdmin && task.submissions.length > 0 && <div className="task-submission-codex"><span>{selectedSubmissionIds.length ? `已选择 ${selectedSubmissionIds.length} 个交付版本` : "勾选需要分析或用于修订的版本"}</span><button className="secondary" disabled={busy || !selectedSubmissionIds.length || !onOpenInCodex} onClick={() => onOpenInCodex?.(task.id, selectedSubmissionIds)} type="button">✦ 带入 Codex 修订</button></div>}
        </section>
        <section className="task-comments"><h3>协作留言 <span>{task.comments.length}</span></h3><div className="comment-list">{task.comments.length === 0 ? <p className="task-empty-copy">还没有留言，可以先同步进展或提出问题。</p> : task.comments.map((item) => <article key={item.id}><span className="avatar">{item.author.display_name.slice(0, 1)}</span><div><header><strong>{item.author.display_name}</strong><time>{readableDateTime(item.created_at)}</time></header><p>{item.body}</p></div></article>)}</div><form onSubmit={(event) => void addComment(event)}><textarea disabled={busy} maxLength={4000} onChange={(event) => setComment(event.target.value)} placeholder="同步进展、留下问题或补充说明…" rows={3} value={comment} /><button className="primary compact" disabled={busy || !comment.trim()} type="submit">发送留言</button></form></section>
      </div><aside className="task-detail-side"><section><h3>整体进度</h3><div className="task-progress-summary"><strong>{task.completed_participants}/{task.participants.length}</strong><span>人已完成</span><i><b style={{ width: `${task.participants.length ? task.completed_participants / task.participants.length * 100 : 0}%` }} /></i></div></section>
        {task.my_progress && <section className="my-task-progress"><h3>我的进度</h3><div>{(["todo", "in_progress", "done"] as TaskProgress[]).map((progress) => <button className={task.my_progress === progress ? "active" : ""} disabled={busy} key={progress} onClick={() => void updateProgress(progress)} type="button">{TASK_PROGRESS_LABELS[progress]}</button>)}</div></section>}
        <section className="task-participant-list"><h3>参与者</h3><div>{task.participants.map((participant) => <article key={participant.user.id}><span className="avatar">{participant.user.display_name.slice(0, 1)}</span><div><strong>{participant.user.display_name}</strong><small>{roleSummary(participant.user)}</small></div><em className={participant.progress}>{TASK_PROGRESS_LABELS[participant.progress]}</em></article>)}</div></section>
        <section className="task-revision-list"><h3>修订历史</h3><div>{[...task.revisions].reverse().map((revision) => <article key={revision.id}><span>v{revision.revision_number}</span><div><strong>{revision.change_summary || "任务更新"}</strong><small>{readableDateTime(revision.created_at)} · {revision.publication_source === "codex" ? "Codex" : revision.created_by.display_name}</small></div></article>)}</div></section>
      </aside></div><Notice text={error} />
    </>}
  </section></div>;
}

function CollaborationTasksView({ auth, users }: { auth: AuthState; users: User[] }) {
  const [tasks, setTasks] = useState<CollaborationTask[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState<CollaborationTaskStatus | "">("");
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [focusedTaskId, setFocusedTaskId] = useState<string | null>(null);
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [codexStartRequest, setCodexStartRequest] = useState<{ key: number; taskId: string; submissionIds: string[] } | null>(null);
  const pageSize = 20;
  const isAdmin = hasRole(auth.user, "admin");

  const load = useCallback(async () => {
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
    if (statusFilter) query.set("status", statusFilter);
    if (search.trim()) query.set("search", search.trim());
    const result = await api<{ items: CollaborationTask[]; total: number; counts: Record<string, number> }>(`/api/collaboration-tasks?${query}`);
    setTasks(result.items); setTotal(result.total); setCounts(result.counts);
  }, [page, search, statusFilter]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setLoading(true);
      void load().catch((reason) => setError(reason instanceof Error ? reason.message : "协作任务加载失败")).finally(() => setLoading(false));
    }, 180);
    return () => window.clearTimeout(timer);
  }, [load]);

  const totalAccessible = Object.values(counts).reduce((sum, value) => sum + value, 0);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  const tabs: Array<{ key: CollaborationTaskStatus | ""; label: string; count: number }> = [
    { key: "", label: "全部", count: totalAccessible },
    { key: "published", label: "待开始", count: counts.published || 0 },
    { key: "in_progress", label: "协作中", count: counts.in_progress || 0 },
    { key: "completed", label: "已完成", count: counts.completed || 0 },
  ];
  const focusedTask = tasks.find((task) => task.id === focusedTaskId) || null;

  return <>
    <section className="task-studio-summary"><div><span>◎</span><strong>{totalAccessible}</strong><small>全部任务</small></div><div><span>↗</span><strong>{counts.in_progress || 0}</strong><small>协作中</small></div><div><span>✓</span><strong>{counts.completed || 0}</strong><small>已完成</small></div><p><i />任务数据、修订和附件均保存在本机</p></section>
    <section className="task-studio-layout">
      <aside className="panel task-list-panel" aria-label="任务列表">
        <header><div><p>任务列表</p><strong>{total} 个可见任务</strong></div>{isAdmin && <span>CODEX 发布</span>}</header>
        <label className="task-list-search"><span>⌕</span><input onChange={(event) => { setSearch(event.target.value); setPage(1); }} placeholder="搜索任务" value={search} /></label>
        <div className="task-list-tabs">{tabs.map((tab) => <button aria-pressed={statusFilter === tab.key} className={statusFilter === tab.key ? "active" : ""} key={tab.key || "all"} onClick={() => { setStatusFilter(tab.key); setPage(1); }} type="button">{tab.label}<span>{tab.count}</span></button>)}</div>
        <Notice text={error} /><Notice text={message} tone="success" />
        <div className="task-list-scroll">{loading ? <div className="task-list-loading"><span className="spinner" />加载任务…</div> : tasks.length === 0 ? <div className="task-list-empty"><span>◎</span><strong>{search ? "没有匹配任务" : "暂无任务"}</strong><p>{isAdmin ? "在右侧告诉 Codex 你要发布什么。" : "管理员邀请后会显示在这里。"}</p></div> : tasks.map((task) => {
          const overdue = Boolean(task.due_date && task.due_date < today && task.status !== "completed");
          const progress = task.participants.length ? task.completed_participants / task.participants.length * 100 : 0;
          return <article className={`${focusedTaskId === task.id ? "active" : ""} ${overdue ? "overdue" : ""}`} key={task.id}>
            <button className="task-list-select" onClick={() => setFocusedTaskId(task.id)} type="button"><div><span className={`task-priority ${task.priority}`}>{TASK_PRIORITY_LABELS[task.priority]}</span><span className={`collaboration-status ${task.status}`}>{COLLABORATION_STATUS_LABELS[task.status]}</span><em>v{task.view_revision}</em></div><h3>{task.title}</h3><p>{task.description || "未填写补充说明"}</p><i className="task-list-progress"><b style={{ width: `${progress}%` }} /></i><footer><span>{task.completed_participants}/{task.participants.length} 人完成</span><time>{task.due_date || "无截止日期"}</time></footer></button>
            <button className="task-list-open" onClick={() => setDetailTaskId(task.id)} type="button">查看详情 →</button>
          </article>;
        })}</div>
        <footer><span>第 {page}/{pageCount} 页</span><div><button disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))} type="button">‹</button><button disabled={page === pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))} type="button">›</button></div></footer>
      </aside>
      {isAdmin ? <CodexTaskPublisher auth={auth} onPublished={(task) => { setStatusFilter(""); setSearch(""); setPage(1); setFocusedTaskId(task.id); setMessage(`“${task.title}”v${task.current_revision} 已由 Codex 发布。`); void load(); }} selectedTask={focusedTask} startRequest={codexStartRequest} users={users} /> : <section className="panel participant-task-focus">{focusedTask ? <><span className={`collaboration-status ${focusedTask.status}`}>{COLLABORATION_STATUS_LABELS[focusedTask.status]}</span><h2>{focusedTask.title}</h2><p>{focusedTask.description || "未填写补充说明"}</p><div><strong>v{focusedTask.view_revision}</strong><span>我的进度：{focusedTask.my_progress ? TASK_PROGRESS_LABELS[focusedTask.my_progress] : "未参与"}</span></div><button className="primary" onClick={() => setDetailTaskId(focusedTask.id)} type="button">打开任务</button></> : <><span className="focus-empty-icon">◎</span><h2>从左侧选择任务</h2><p>查看任务资料、参与者、留言和自己的完成进度。</p></>}</section>}
    </section>
    {detailTaskId && <CollaborationTaskDetailDialog auth={auth} onChanged={() => void load()} onClose={() => setDetailTaskId(null)} onOpenInCodex={(taskId, submissionIds) => { setFocusedTaskId(taskId); setCodexStartRequest((current) => ({ key: (current?.key || 0) + 1, taskId, submissionIds })); setDetailTaskId(null); setMessage(`已将 ${submissionIds.length} 个交付版本带入 Codex 修订会话。`); }} taskId={detailTaskId} />}
  </>;
}

function RoleOptions({ roles, onChange, includeAdmin = false }: { roles: Role[]; onChange: (roles: Role[]) => void; includeAdmin?: boolean }) {
  const toggle = (role: Role) => {
    if (role === "admin") {
      onChange(roles.includes("admin") ? [] : ["admin"]);
      return;
    }
    const clinicalRoles = roles.filter((item) => item !== "admin");
    onChange(clinicalRoles.includes(role) ? clinicalRoles.filter((item) => item !== role) : [...clinicalRoles, role]);
  };
  return <fieldset className="role-options"><legend>角色</legend><div>
    <label className={roles.includes("annotator") ? "selected" : ""}><input aria-label="初标医生角色" checked={roles.includes("annotator")} onChange={() => toggle("annotator")} type="checkbox" /><span><strong>初标医生</strong><small>勾画并提交标注</small></span></label>
    <label className={roles.includes("reviewer") ? "selected" : ""}><input aria-label="审核医生角色" checked={roles.includes("reviewer")} onChange={() => toggle("reviewer")} type="checkbox" /><span><strong>审核医生</strong><small>审核其他医生的标注</small></span></label>
    {includeAdmin && <label className={`admin-option ${roles.includes("admin") ? "selected" : ""}`}><input aria-label="管理员角色" checked={roles.includes("admin")} onChange={() => toggle("admin")} type="checkbox" /><span><strong>管理员</strong><small>创建后权限固定</small></span></label>}
  </div></fieldset>;
}

function RoleEditDialog({ user, csrf, onClose, onSaved }: { user: User; csrf: string; onClose: () => void; onSaved: () => Promise<void> }) {
  const initialRoles = (user.roles?.length ? user.roles : [user.role]).filter((role) => role !== "admin");
  const [roles, setRoles] = useState<Role[]>(initialRoles);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const save = async () => {
    if (roles.length === 0) return;
    setBusy(true); setError("");
    try {
      await api(`/api/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ roles }) }, csrf);
      await onSaved(); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "权限更新失败"); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}><section aria-label={`修改${user.display_name}的权限`} aria-modal="true" className="modal role-edit-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
    <p className="eyebrow teal">WORK ROLES</p><h2>修改工作权限</h2><p className="modal-patient">{user.display_name} · {user.email}</p>
    <RoleOptions onChange={setRoles} roles={roles} />
    <p className="modal-hint">可同时选择两种角色；系统会强制禁止医生审核自己初标的病例。</p>
    <Notice text={error} /><div className="modal-actions"><button className="secondary" onClick={onClose} type="button">取消</button><button className="primary" disabled={busy || roles.length === 0} onClick={() => void save()} type="button">{busy ? "保存中…" : "保存权限"}</button></div>
  </section></div>;
}

function DeleteUserDialog({ user, csrf, onClose, onDeleted }: { user: User; csrf: string; onClose: () => void; onDeleted: () => Promise<void> }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const remove = async () => {
    setBusy(true); setError("");
    try {
      await api(`/api/admin/users/${user.id}`, { method: "DELETE" }, csrf);
      await onDeleted();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "删除失败"); }
    finally { setBusy(false); }
  };
  return <div className="modal-backdrop" role="presentation" onMouseDown={busy ? undefined : onClose}><section aria-label={`删除${user.display_name}`} aria-modal="true" className="modal delete-user-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
    <p className="eyebrow danger-eyebrow">REMOVE MEMBER</p><h2>删除授权人员</h2><p className="modal-patient">{user.display_name} · {user.email}</p>
    <div className="delete-user-warning"><strong>此操作不可撤销</strong><p>删除后，该人员将立即失去登录权限，尚未开始的任务分配会自动解除。已有标注或审核历史的人员不能删除，请改为停用账户。</p></div>
    <Notice text={error} /><div className="modal-actions"><button className="secondary" disabled={busy} onClick={onClose} type="button">取消</button><button className="danger-button" disabled={busy} onClick={() => void remove()} type="button">{busy ? "删除中…" : "确认删除"}</button></div>
  </section></div>;
}

function UserManagement({ users, csrf, refresh }: { users: User[]; csrf: string; refresh: () => Promise<void> }) {
  const [email, setEmail] = useState(""); const [name, setName] = useState(""); const [roles, setRoles] = useState<Role[]>(["annotator"]); const [temporaryPassword, setTemporaryPassword] = useState("");
  const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [editingUser, setEditingUser] = useState<User | null>(null); const [deletingUser, setDeletingUser] = useState<User | null>(null);
  const create = async (event: FormEvent) => {
    event.preventDefault(); setError(""); setMessage("");
    if (roles.length === 0) return setError("请至少选择一个角色");
    try {
      await api("/api/admin/users", { method: "POST", body: JSON.stringify({ email, display_name: name, roles, temporary_password: temporaryPassword }) }, csrf);
      setEmail(""); setName(""); setRoles(["annotator"]); setTemporaryPassword(""); setMessage("账户已创建，对方首次登录时必须修改密码"); await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : "创建失败"); }
  };
  const toggle = async (user: User) => {
    setError("");
    try { await api(`/api/admin/users/${user.id}`, { method: "PATCH", body: JSON.stringify({ is_active: !user.is_active }) }, csrf); await refresh(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "更新失败"); }
  };
  return <div className="users-layout">
    <section className="panel user-form-panel"><p className="eyebrow teal">NEW MEMBER</p><h2>添加授权人员</h2><p>同一位医生可同时承担初标与审核，但不能审核自己的病例。</p><form onSubmit={create}>
      <label>姓名<input onChange={(event) => setName(event.target.value)} placeholder="医生姓名" required value={name} /></label><label>邮箱<input onChange={(event) => setEmail(event.target.value)} required type="email" value={email} /></label>
      <RoleOptions includeAdmin onChange={setRoles} roles={roles} />
      <label>一次性密码<input minLength={10} onChange={(event) => setTemporaryPassword(event.target.value)} placeholder="至少 10 位" required type="password" value={temporaryPassword} /></label>
      <Notice text={error} /><Notice text={message} tone="success" /><button className="primary wide" type="submit">创建账户</button>
    </form></section>
    <section className="panel user-list-panel"><div className="panel-heading"><div><p className="eyebrow">AUTHORIZED USERS</p><h2>授权人员</h2></div><span>{users.length} 人</span></div><div className="user-list">
      {users.map((user) => { const isAdmin = hasRole(user, "admin"); const userRoles = user.roles?.length ? user.roles : [user.role]; return <div className="user-row" key={user.id}><div className="avatar">{user.display_name.slice(0, 1)}</div><div className="user-main"><strong>{user.display_name}</strong><span>{user.email}</span></div><div className="role-pills">{userRoles.map((role) => <span className={`role-pill ${role}`} key={role}>{ROLE_LABELS[role]}</span>)}</div><span className={user.is_active ? "account-state active" : "account-state"}>{user.is_active ? "启用" : "停用"}</span><div className="user-actions">{isAdmin ? <span className="permission-fixed">权限固定</span> : <><button className="text-button" onClick={() => setEditingUser(user)} type="button">修改权限</button><button className="text-button" onClick={() => void toggle(user)} type="button">{user.is_active ? "停用" : "启用"}</button><button className="text-button delete-user-button" onClick={() => { setError(""); setMessage(""); setDeletingUser(user); }} type="button">删除</button></>}</div></div>; })}
    </div></section>
    {editingUser && <RoleEditDialog csrf={csrf} onClose={() => setEditingUser(null)} onSaved={refresh} user={editingUser} />}
    {deletingUser && <DeleteUserDialog csrf={csrf} onClose={() => setDeletingUser(null)} onDeleted={async () => { const deletedName = deletingUser.display_name; await refresh(); setDeletingUser(null); setError(""); setMessage(`已删除人员：${deletedName}`); }} user={deletingUser} />}
  </div>;
}

type WorkflowStepState = "done" | "current" | "pending" | "returned";
type WorkflowStep = { label: string; detail: string; state: WorkflowStepState };
type WorkflowPresentation = {
  title: string;
  description: string;
  nextAction: string;
  tone: "neutral" | "active" | "review" | "warning" | "success";
  steps: WorkflowStep[];
  isRework: boolean;
  returnVersion: number | null;
  returnComment: string;
};

function buildSegmentationWorkflowPresentation(detail: CaseDetail): WorkflowPresentation {
  const lastReturnIndex = detail.review_history.findIndex((event) => event.action === "return");
  const lastReturn = lastReturnIndex >= 0 ? detail.review_history[lastReturnIndex] : null;
  const approvalWasRevoked = Boolean(lastReturn && detail.review_history.slice(lastReturnIndex + 1).some((event) => event.action === "approve" && event.version === lastReturn.version));
  const returnActionLabel = approvalWasRevoked ? "撤回通过" : "审核退回";
  const returnVersion = lastReturn?.version ?? detail.submitted_version ?? detail.latest_version;
  const revisionVersion = detail.latest_version > returnVersion ? detail.latest_version : null;
  const resubmittedVersion = detail.submitted_version && detail.submitted_version > returnVersion ? detail.submitted_version : null;
  const hasReviewRevision = Boolean(
    ["submitted", "in_review"].includes(detail.status) &&
    detail.submitted_version !== null &&
    detail.latest_version > detail.submitted_version &&
    detail.latest_annotation?.state === "draft" &&
    detail.latest_annotation.kind === "review_revision" &&
    detail.latest_annotation.source_version === detail.submitted_version
  );
  const reworkFinished = detail.status === "approved" && Boolean(detail.approved_version && detail.approved_version > returnVersion);
  const isRework = detail.status === "returned" || Boolean(lastReturn && (
    (detail.status === "annotating" && revisionVersion) ||
    (["submitted", "in_review"].includes(detail.status) && resubmittedVersion) ||
    reworkFinished
  ));

  if (isRework) {
    if (hasReviewRevision) {
      return {
        title: "返工版本正在审核微调",
        description: `审核医生已基于重新提交的 v${detail.submitted_version} 保存修订草稿 v${detail.latest_version}，原提交版本完整保留。`,
        nextAction: `确认修订版 v${detail.latest_version}，或继续微调后再次保存`,
        tone: "review",
        isRework: true,
        returnVersion,
        returnComment: lastReturn?.comment || "审核医生要求修改后重新提交",
        steps: [
          { label: `${returnActionLabel} v${returnVersion}`, detail: "已完成返工要求", state: "returned" },
          { label: `返工提交 v${detail.submitted_version}`, detail: "初标修改已重新提交", state: "done" },
          { label: `审核微调 v${detail.latest_version}`, detail: "审核修订草稿已保存", state: "current" },
          { label: "确认审核结论", detail: "通过修订版或退回", state: "pending" },
        ],
      };
    }
    if (reworkFinished) {
      return {
        title: "返工审核已通过",
        description: `返工版本 v${detail.approved_version} 已由审核医生确认，病例流程完成。`,
        nextAction: "无需继续操作",
        tone: "success",
        isRework: true,
        returnVersion,
        returnComment: lastReturn?.comment || "审核医生要求修改后重新提交",
        steps: [
          { label: `${returnActionLabel} v${returnVersion}`, detail: "已完成返工要求", state: "returned" },
          { label: `返工修改 v${detail.latest_version}`, detail: "基于上次标注完成修改", state: "done" },
          { label: `重新提交 v${detail.submitted_version}`, detail: "新版本已锁定", state: "done" },
          { label: "再次审核", detail: "审核已通过", state: "done" },
        ],
      };
    }
    if (["submitted", "in_review"].includes(detail.status) && resubmittedVersion) {
      return {
        title: detail.status === "in_review" ? "返工版本审核中" : "返工版本待审核",
        description: `返工版本 v${resubmittedVersion} 已重新提交，当前标注只读并等待审核结论。`,
        nextAction: "等待审核医生确认或再次退回",
        tone: "review",
        isRework: true,
        returnVersion,
        returnComment: lastReturn?.comment || "审核医生要求修改后重新提交",
        steps: [
          { label: `${returnActionLabel} v${returnVersion}`, detail: "已收到修改意见", state: "returned" },
          { label: `返工修改 v${detail.latest_version}`, detail: "已保存新版本", state: "done" },
          { label: `重新提交 v${resubmittedVersion}`, detail: "返工版本已锁定", state: "done" },
          { label: "再次审核", detail: detail.status === "in_review" ? "审核医生正在处理" : "等待审核医生领取", state: "current" },
        ],
      };
    }
    const hasRevisionDraft = Boolean(revisionVersion && detail.latest_annotation?.state === "draft");
    return {
      title: hasRevisionDraft ? "返工版本待提交" : approvalWasRevoked ? "通过已撤回 · 待返工" : "审核退回 · 待修改",
      description: hasRevisionDraft
        ? `已在退回版本 v${returnVersion} 基础上保存返工草稿 v${revisionVersion}。`
        : `初标医生打开后会载入退回版本 v${returnVersion}，可直接修改原有轮廓，无需重新勾画。`,
      nextAction: hasRevisionDraft ? `重新提交 v${revisionVersion} 给审核医生` : `由初标医生根据意见修改并保存为 v${returnVersion + 1}`,
      tone: "warning",
      isRework: true,
      returnVersion,
      returnComment: lastReturn?.comment || "审核医生要求修改后重新提交",
      steps: [
        { label: approvalWasRevoked ? `原通过版本 v${returnVersion}` : `上次提交 v${returnVersion}`, detail: "原版本完整保留", state: "done" },
        { label: returnActionLabel, detail: approvalWasRevoked ? "查看下方撤回原因" : "查看下方本轮审核意见", state: "returned" },
        { label: "返工修改", detail: hasRevisionDraft ? `已保存草稿 v${revisionVersion}` : `在 v${returnVersion} 上直接修改`, state: hasRevisionDraft ? "done" : "current" },
        { label: "重新提交与复审", detail: hasRevisionDraft ? `下一步提交 v${revisionVersion}` : "保存新版本后开放", state: hasRevisionDraft ? "current" : "pending" },
      ],
    };
  }

  if (detail.status === "unassigned") return {
    title: "等待任务分配", description: "该病例尚未指定初标医生，当前仅可浏览。", nextAction: "由管理员分配初标医生", tone: "neutral", isRework: false, returnVersion: null, returnComment: "",
    steps: [{ label: "任务分配", detail: "指定初标与审核医生", state: "current" }, { label: "初标与保存", detail: "尚未开始", state: "pending" }, { label: "提交审核", detail: "尚未开始", state: "pending" }, { label: "审核确认", detail: "尚未开始", state: "pending" }],
  };
  if (detail.status === "assigned") return {
    title: "待开始初标", description: "病例已分配，可核对四序列并开始逐层勾画。", nextAction: "完成勾画并保存第一个草稿版本", tone: "active", isRework: false, returnVersion: null, returnComment: "",
    steps: [{ label: "四序列核对", detail: "W / F / In / Opp", state: "current" }, { label: "逐层勾画", detail: "闭合轮廓并填充", state: "pending" }, { label: "提交审核", detail: "保存草稿后开放", state: "pending" }, { label: "审核确认", detail: "尚未开始", state: "pending" }],
  };
  if (detail.status === "annotating") {
    const hasDraft = detail.latest_annotation?.state === "draft";
    return {
      title: hasDraft ? "初标草稿待提交" : "初标进行中",
      description: hasDraft ? `草稿 v${detail.latest_version} 已保存，可继续修改或提交审核。` : "正在进行逐层勾画，尚未形成可提交草稿。",
      nextAction: hasDraft ? `提交 v${detail.latest_version} 给审核医生` : "保存当前标注草稿",
      tone: "active", isRework: false, returnVersion: null, returnComment: "",
      steps: [{ label: "四序列核对", detail: "W / F / In / Opp", state: "done" }, { label: "逐层勾画", detail: hasDraft ? `已保存草稿 v${detail.latest_version}` : "正在勾画", state: hasDraft ? "done" : "current" }, { label: "提交审核", detail: hasDraft ? "当前可提交" : "保存草稿后开放", state: hasDraft ? "current" : "pending" }, { label: "审核确认", detail: "尚未开始", state: "pending" }],
    };
  }
  if (hasReviewRevision) return {
    title: "审核微调草稿待确认",
    description: `审核医生已基于原提交 v${detail.submitted_version} 保存修订版 v${detail.latest_version}；两个版本均可追溯。`,
    nextAction: `继续微调，或确认通过修订版 v${detail.latest_version}`,
    tone: "review", isRework: false, returnVersion: null, returnComment: "",
    steps: [{ label: "四序列核对", detail: "已完成", state: "done" }, { label: `初标提交 v${detail.submitted_version}`, detail: "原始提交完整保留", state: "done" }, { label: `审核微调 v${detail.latest_version}`, detail: "修订草稿已保存", state: "current" }, { label: "审核确认", detail: "等待通过修订版", state: "pending" }],
  };
  if (["submitted", "in_review"].includes(detail.status)) return {
    title: detail.status === "in_review" ? "审核进行中" : "已提交 · 等待审核",
    description: `版本 v${detail.submitted_version} 已锁定，初标医生暂时不可修改。`,
    nextAction: detail.status === "in_review" ? "由审核医生给出结论" : "等待审核医生领取任务",
    tone: "review", isRework: false, returnVersion: null, returnComment: "",
    steps: [{ label: "四序列核对", detail: "已完成", state: "done" }, { label: "逐层勾画", detail: `已保存 v${detail.latest_version}`, state: "done" }, { label: "提交审核", detail: `已提交 v${detail.submitted_version}`, state: "done" }, { label: "审核确认", detail: detail.status === "in_review" ? "审核中" : "等待领取", state: "current" }],
  };
  const wasReviewRevision = Boolean(detail.approved_version && detail.submitted_version && detail.approved_version > detail.submitted_version);
  return {
    title: wasReviewRevision ? "审核微调后已通过" : "审核已完成", description: wasReviewRevision ? `审核医生基于原提交 v${detail.submitted_version} 修订并批准 v${detail.approved_version}；原提交仍完整保留。` : `通过版本 v${detail.approved_version} 已确认并锁定；如后续发现问题，审核医生仍可撤回。`, nextAction: "无问题则流程结束；有问题可撤回通过并退回返工", tone: "success", isRework: false, returnVersion: null, returnComment: "",
    steps: [{ label: "四序列核对", detail: "已完成", state: "done" }, { label: "逐层勾画", detail: `最终版本 v${detail.approved_version}`, state: "done" }, { label: "提交审核", detail: "已完成", state: "done" }, { label: "审核确认", detail: "审核通过", state: "done" }],
  };
}

function buildWorkflowPresentation(detail: CaseDetail): WorkflowPresentation {
  const presentation = buildSegmentationWorkflowPresentation(detail);
  if (detail.workflow_type !== "pdff_roi") return presentation;
  const pdffCopy = (value: string) => value
    .replaceAll("闭合轮廓并填充", "每平面头、体、尾三个 ROI")
    .replaceAll("四序列", "Fat / Water 配对")
    .replaceAll("W / F / In / Opp", "Fat / Water")
    .replaceAll("逐层勾画", "三平面三点 ROI")
    .replaceAll("同层三点 ROI", "三平面三点 ROI")
    .replaceAll("勾画", "放置 ROI")
    .replaceAll("轮廓", "ROI")
    .replaceAll("标注", "PDFF ROI")
    .replaceAll("画布", "双序列视图");
  return {
    ...presentation,
    description: pdffCopy(presentation.description),
    nextAction: pdffCopy(presentation.nextAction),
    steps: presentation.steps.map((step) => ({ ...step, label: pdffCopy(step.label), detail: pdffCopy(step.detail) })),
  };
}

function Workspace({ detail, auth, onBack, refreshDetail }: { detail: CaseDetail; auth: AuthState; onBack: () => void; refreshDetail: () => Promise<void> }) {
  const [message, setMessage] = useState(""); const [error, setError] = useState(""); const [reviewComment, setReviewComment] = useState(""); const [busy, setBusy] = useState(false);
  const [reviewEditing, setReviewEditing] = useState(false); const [viewerDirty, setViewerDirty] = useState(false);
  const [pdffConsoleHost, setPdffConsoleHost] = useState<HTMLDivElement | null>(null);
  const workflow = buildWorkflowPresentation(detail);
  const isPdff = detail.workflow_type === "pdff_roi";
  const latestReturnIndex = detail.review_history.findIndex((event) => event.action === "return");
  const latestReturnWasApprovalRevoke = latestReturnIndex >= 0 && detail.review_history.slice(latestReturnIndex + 1).some((event) => event.action === "approve" && event.version === detail.review_history[latestReturnIndex].version);
  const isAdmin = hasRole(auth.user, "admin");
  const hasDrawingRole = hasRole(auth.user, "annotator") || isAdmin;
  const annotatorCanDraw = hasDrawingRole && (isAdmin || detail.annotator?.id === auth.user.id) && ["assigned", "annotating", "returned"].includes(detail.status);
  const latestIsDraft = detail.latest_annotation?.state === "draft";
  const pdffReadyToSubmit = !isPdff || detail.latest_annotation?.case_summary?.plane_count === 3;
  const canSubmit = annotatorCanDraw && latestIsDraft && pdffReadyToSubmit && (
    detail.status === "annotating" ||
    (detail.status === "returned" && detail.submitted_version !== null && detail.latest_version > detail.submitted_version)
  );
  const hasReviewRole = hasRole(auth.user, "reviewer") || isAdmin;
  const isOwnAnnotation = detail.annotator?.id === auth.user.id || detail.submitted_annotation?.author_id === auth.user.id;
  const canReview = hasReviewRole && !isOwnAnnotation && ["submitted", "in_review"].includes(detail.status);
  const hasReviewRevision = Boolean(
    detail.submitted_version !== null &&
    detail.latest_version > detail.submitted_version &&
    detail.latest_annotation?.state === "draft" &&
    detail.latest_annotation.kind === "review_revision" &&
    detail.latest_annotation.source_version === detail.submitted_version &&
    ["submitted", "in_review"].includes(detail.status)
  );
  const effectiveReviewEditing = canReview && (reviewEditing || hasReviewRevision);
  const reviewCanDraw = effectiveReviewEditing;
  const viewerCanDraw = annotatorCanDraw || reviewCanDraw;
  const canRevokeApproval = hasReviewRole && !isOwnAnnotation && detail.status === "approved";
  const submit = async () => { setBusy(true); setError(""); try { await api(`/api/cases/${detail.id}/submit`, { method: "POST" }, auth.csrf_token); setMessage(workflow.isRework ? "返工版本已重新提交，标注版本已锁定" : "已提交审核，标注版本已锁定"); await refreshDetail(); } catch (reason) { setError(reason instanceof Error ? reason.message : "提交失败"); } finally { setBusy(false); } };
  const review = async (action: "approve" | "return") => { const revokingApproval = detail.status === "approved" && action === "return"; setBusy(true); setError(""); try { await api(`/api/cases/${detail.id}/review`, { method: "POST", body: JSON.stringify({ action, comment: reviewComment }) }, auth.csrf_token); setMessage(action === "approve" ? hasReviewRevision ? `审核修订版 v${detail.latest_version} 已通过` : "审核已通过" : revokingApproval ? "已撤回通过并退回初标医生返工" : "已退回初标医生修改"); setReviewComment(""); setReviewEditing(false); setViewerDirty(false); await refreshDetail(); } catch (reason) { setError(reason instanceof Error ? reason.message : "审核操作失败"); } finally { setBusy(false); } };
  const exitReviewEditing = () => {
    if (viewerDirty && !window.confirm("当前审核修改尚未保存，确定放弃这些修改吗？")) return;
    setReviewEditing(false);
    setViewerDirty(false);
    setMessage("");
  };
  return <main className="workspace-page">
    <header className="workspace-header"><button className="back-button" onClick={onBack} type="button">← 返回任务列表</button><div className="workspace-brand"><BrandMark /><span>{isPdff ? "MedContour PDFF 工作台" : "MedContour 影像工作台"}</span></div><div className="workspace-status"><StatusBadge status={detail.status} /><span>v{detail.latest_version || 0}</span></div></header>
    <div className="workspace-grid">
      <aside className="case-sidebar"><p className="eyebrow teal">PATIENT</p><h1>{detail.case.patient_name_zh}</h1><p className="patient-id">{detail.case.patient_id}</p>{detail.case.needs_identity_review && <div className="identity-alert"><strong>身份映射待管理员核对</strong><span>{detail.case.identity_review_note || "来源身份信息存在冲突"}</span></div>}
        <dl><div><dt>病例编号</dt><dd>{detail.case.case_code}</dd></div><div><dt>检查日期</dt><dd>{detail.case.study_date || "未记录"}</dd></div><div><dt>协议族</dt><dd>{detail.case.protocol_family || "Dixon"}</dd></div><div><dt>初标医生</dt><dd>{detail.annotator?.display_name || "未分配"}</dd></div><div><dt>审核医生</dt><dd>{detail.reviewer?.display_name || "待领取"}</dd></div></dl>
        {isPdff ? <div className="pdff-sidebar-console-host" ref={setPdffConsoleHost} /> : <div className="label-card"><span className="label-dot" /><div><strong>{detail.labels[0]?.name || "目标结构"}</strong><small>标签值 {detail.labels[0]?.value || 1}</small></div></div>}<div className="privacy-note">患者信息仅对本病例授权人员可见</div>
      </aside>
      {isPdff ? <PdffRoiViewer annotationVersion={detail.latest_annotation?.version ?? null} canDraw={viewerCanDraw} consoleHost={pdffConsoleHost} csrfToken={auth.csrf_token} initialMetrics={detail.latest_annotation?.metrics ?? []} initialPlanes={detail.latest_annotation?.planes ?? []} initialRois={detail.latest_annotation?.rois ?? []} key={`pdff-${detail.id}`} onDirtyChange={setViewerDirty} onSaved={(version) => { if (reviewCanDraw) setMessage(`审核修订版 v${version} 已保存，原提交版本未被覆盖`); void refreshDetail(); }} projectCaseId={detail.id} reviewBaseVersion={reviewCanDraw ? detail.submitted_version : null} reviewMode={reviewCanDraw} saveComment={reviewCanDraw ? reviewComment : ""} /> : <NiiVueViewer annotationVersion={detail.latest_annotation?.version ?? null} canDraw={viewerCanDraw} csrfToken={auth.csrf_token} onDirtyChange={setViewerDirty} onSaved={(version) => { if (reviewCanDraw) setMessage(`审核修订版 v${version} 已保存，原提交版本未被覆盖`); void refreshDetail(); }} projectCaseId={detail.id} reworkBaseVersion={annotatorCanDraw && workflow.isRework ? workflow.returnVersion : null} reviewBaseVersion={reviewCanDraw ? detail.submitted_version : null} reviewMode={reviewCanDraw} saveComment={reviewCanDraw ? reviewComment : ""} />}
      <aside className="action-sidebar"><p className="eyebrow">CASE WORKFLOW</p><h2>任务流程</h2>
        <section className={`workflow-current-card ${workflow.tone}`} aria-label="当前任务阶段">
          <div><span>当前阶段</span><b>v{detail.latest_version || 0}</b></div>
          <strong>{workflow.title}</strong>
          <p>{workflow.description}</p>
          <dl><div><dt>下一步</dt><dd>{workflow.nextAction}</dd></div></dl>
        </section>
        {workflow.isRework && workflow.returnVersion && <section className="rework-feedback" aria-label={latestReturnWasApprovalRevoke ? "撤回通过原因" : "本轮审核意见"}><span>{latestReturnWasApprovalRevoke ? "撤回通过原因" : "审核意见 · 退回"} v{workflow.returnVersion}</span><p>{workflow.returnComment}</p><small>退回版本保持不变；修改后保存为新版本，可随时回溯。</small></section>}
        <ol className="workflow-steps detailed">{workflow.steps.map((step, index) => <li className={step.state} key={`${step.label}-${index}`}><i>{step.state === "done" ? "✓" : step.state === "returned" ? "!" : index + 1}</i><span>{step.label}<small>{step.detail}</small></span></li>)}</ol>
        <Notice text={error} /><Notice text={message} tone="success" />
        {hasReviewRole && isOwnAnnotation && ["submitted", "in_review", "approved"].includes(detail.status) && <div className="workflow-blocked-note"><strong>本人初标，不能参与审核</strong><span>该病例必须由另一位具有审核权限的医生确认。</span></div>}
        {annotatorCanDraw && workflow.isRework && !canSubmit && <div className="workflow-blocked-note"><strong>先修改并保存返工版本</strong><span>{isPdff ? `双序列视图已载入 v${workflow.returnVersion} 的三个 ROI；调整位置或半径并保存后才可重新提交。` : `画布已载入 v${workflow.returnVersion}，可直接覆盖、擦除或补画原轮廓；保存后才可重新提交。`}</span></div>}
        {annotatorCanDraw && isPdff && latestIsDraft && !pdffReadyToSubmit && <div className="workflow-blocked-note"><strong>完成三个独立测量平面后提交</strong><span>当前已保存 {detail.latest_annotation?.case_summary?.plane_count ?? (detail.latest_annotation?.rois?.length === 3 ? 1 : 0)}/3 个平面；每个平面需要胰头、胰体、胰尾三个 ROI。</span></div>}
        {canSubmit && <button className="primary wide" disabled={busy} onClick={() => void submit()} type="button">{workflow.isRework ? `重新提交 v${detail.latest_version}` : `提交 v${detail.latest_version} 给审核医生`}</button>}
        {canReview && !effectiveReviewEditing && <div className="review-box"><label>审核意见<textarea onChange={(event) => setReviewComment(event.target.value)} placeholder="退回时必须填写；通过或直接微调时可填写说明" rows={4} value={reviewComment} /></label><div className="review-actions review-triple"><button className="danger-button" disabled={busy || !reviewComment.trim()} onClick={() => void review("return")} type="button">退回修改</button><button className="secondary" disabled={busy} onClick={() => { setError(""); setMessage("已进入审核微调模式；原提交版本保持不变"); setReviewEditing(true); }} type="button">直接微调</button><button className="primary" disabled={busy} onClick={() => void review("approve")} type="button">直接通过</button></div></div>}
        {canReview && effectiveReviewEditing && <div className="review-box review-revision-box"><div className="review-revision-banner"><strong>审核微调模式</strong><span>基于提交版本 v{detail.submitted_version}；保存会生成新版本，不覆盖原稿。</span></div><label>微调说明<textarea onChange={(event) => setReviewComment(event.target.value)} placeholder={isPdff ? "可说明调整了哪个 ROI 的层面、位置或半径" : "可简要说明补画、擦除或轮廓修正的位置"} rows={3} value={reviewComment} /></label>{viewerDirty && <p className="review-save-hint">{isPdff ? "ROI 有未保存修改，请先点击顶部“计算并保存草稿”。" : "画布有未保存修改，请先点击顶部“保存审核修改”。"}</p>}{!viewerDirty && hasReviewRevision && <p className="review-save-hint ready">修订版 v{detail.latest_version} 已保存，可以继续修改或确认通过。</p>}<div className={hasReviewRevision ? "review-actions" : "review-actions review-triple"}>{!hasReviewRevision && <button className="secondary" disabled={busy} onClick={exitReviewEditing} type="button">{viewerDirty ? "放弃修改" : "退出微调"}</button>}<button className="danger-button" disabled={busy || viewerDirty || !reviewComment.trim()} onClick={() => void review("return")} type="button">退回修改</button><button className="primary" disabled={busy || viewerDirty || !hasReviewRevision} onClick={() => void review("approve")} type="button">{hasReviewRevision ? `通过修订版 v${detail.latest_version}` : "先保存修改"}</button></div></div>}
        {detail.status === "approved" && <div className="approved-card"><strong>✓ 审核完成</strong><span>最终版本 v{detail.approved_version}</span></div>}
        {canRevokeApproval && <div className="approval-revoke-box"><strong>发现通过结果仍需修改？</strong><p>填写原因后可撤回本次通过，将当前版本作为只读底稿退回初标医生返工。</p><label>撤回原因<textarea onChange={(event) => setReviewComment(event.target.value)} placeholder="请说明撤回通过及需要修改的具体原因" rows={3} value={reviewComment} /></label><button className="danger-button wide" disabled={busy || !reviewComment.trim()} onClick={() => void review("return")} type="button">撤回通过并退回返工</button></div>}
        {detail.review_history.length > 0 && <div className="review-history"><h3>审核记录</h3>{detail.review_history.map((event, index) => { const revokedApproval = event.action === "return" && detail.review_history.slice(index + 1).some((older) => older.action === "approve" && older.version === event.version); return <div key={`${event.created_at}-${index}`}><span>{event.action === "approve" ? "通过" : revokedApproval ? "撤回通过 · 退回返工" : "退回"} · v{event.version}</span><p>{event.comment || "无补充意见"}</p></div>; })}</div>}
      </aside>
    </div>
    <footer className="workspace-footer"><span>原始影像只读 · {isPdff ? "ROI 与测量结果" : "标注"}独立版本化保存</span><span>MedContour 医学影像工作台 · 本机安全运行</span></footer>
  </main>;
}

export function AnnotationPlatform() {
  const [auth, setAuth] = useState<AuthState | null>(null); const [booting, setBooting] = useState(true); const [view, setView] = useState<View>("cases");
  const [projects, setProjects] = useState<Project[]>([]); const [cases, setCases] = useState<CaseItem[]>([]); const [users, setUsers] = useState<User[]>([]); const [counts, setCounts] = useState<Record<string, number>>({}); const [total, setTotal] = useState(0);
  const [selectedProjectId, setSelectedProjectId] = useState("");
  const [statusFilter, setStatusFilter] = useState<CaseStatus | "">(""); const [search, setSearch] = useState(""); const [page, setPage] = useState(1); const pageSize = 20;
  const [selected, setSelected] = useState<CaseDetail | null>(null); const [assigning, setAssigning] = useState<CaseItem | null>(null); const [batchAssigning, setBatchAssigning] = useState(false); const [selectedCaseIds, setSelectedCaseIds] = useState<Set<string>>(new Set());
  const [error, setError] = useState(""); const [message, setMessage] = useState(""); const [importing, setImporting] = useState(false);

  useEffect(() => { api<AuthState>("/api/auth/me").then(setAuth).catch((reason) => { if (!(reason instanceof ApiError) || reason.status !== 401) setError(reason.message); }).finally(() => setBooting(false)); }, []);
  const loadUsers = useCallback(async () => { if (auth && hasRole(auth.user, "admin") && !auth.user.must_change_password) setUsers(await api<User[]>("/api/admin/users")); }, [auth]);
  const loadCases = useCallback(async () => {
    if (!auth || auth.user.must_change_password || !selectedProjectId) return;
    const query = new URLSearchParams({ page: String(page), page_size: String(pageSize), project_id: selectedProjectId }); if (statusFilter) query.set("status", statusFilter); if (search.trim()) query.set("search", search.trim());
    const result = await api<{ items: CaseItem[]; total: number; page: number; page_size: number }>(`/api/cases?${query}`);
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize));
    if (page > lastPage) { setPage(lastPage); return; }
    setCases(result.items); setTotal(result.total);
  }, [auth, page, pageSize, search, selectedProjectId, statusFilter]);
  const loadOverview = useCallback(async () => { if (!auth || auth.user.must_change_password || !selectedProjectId) return; const dashboard = await api<{ counts: Record<string, number> }>(`/api/dashboard?project_id=${encodeURIComponent(selectedProjectId)}`); setCounts(dashboard.counts); }, [auth, selectedProjectId]);
  useEffect(() => {
    if (!auth || auth.user.must_change_password) return;
    let cancelled = false;
    const overviewRequest = api<Project[]>("/api/projects").then((projectRows) => {
      if (!cancelled) {
        setProjects(projectRows);
        setSelectedProjectId((current) => projectRows.some((project) => project.id === current) ? current : projectRows[0]?.id || "");
      }
    });
    const usersRequest = hasRole(auth.user, "admin")
      ? api<User[]>("/api/admin/users").then((rows) => { if (!cancelled) setUsers(rows); })
      : Promise.resolve();
    void Promise.all([overviewRequest, usersRequest]).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "工作区加载失败");
    });
    return () => { cancelled = true; };
  }, [auth]);
  useEffect(() => { const timer = window.setTimeout(() => void loadCases().catch((reason) => setError(reason.message)), 250); return () => window.clearTimeout(timer); }, [loadCases]);
  useEffect(() => { const timer = window.setTimeout(() => void loadOverview().catch((reason) => setError(reason instanceof Error ? reason.message : "任务统计加载失败")), 0); return () => window.clearTimeout(timer); }, [loadOverview]);
  const openCase = async (item: CaseItem) => {
    try {
      const warmupUrls = activeProject?.workflow_type === "pdff_roi"
        ? ["f", "w"].map((sequence) => `/api/cases/${item.id}/volume/${sequence}.nii.gz`)
        : [
          `/api/cases/${item.id}/preview/w.nii.gz`,
          `/api/cases/${item.id}/volume/w.nii.gz`,
        ];
      const warmups: Promise<unknown>[] = warmupUrls.map(prefetchImageBuffer);
      if (activeProject?.workflow_type === "pdff_roi") {
        warmups.push(prefetchJson(`/api/cases/${item.id}/pancreas-reference/contours`));
      }
      const openingWarmup = Promise.allSettled(warmups);
      const detail = await api<CaseDetail>(`/api/cases/${item.id}`);
      await Promise.race([
        openingWarmup,
        new Promise<void>((resolve) => window.setTimeout(resolve, 150)),
      ]);
      setSelected(detail);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "病例加载失败");
    }
  };
  const refreshDetail = useCallback(async () => { if (selected) setSelected(await api<CaseDetail>(`/api/cases/${selected.id}`)); }, [selected]);
  const logout = async () => { if (!auth) return; await api("/api/auth/logout", { method: "POST" }, auth.csrf_token); setAuth(null); setSelected(null); setUsers([]); setProjects([]); setSelectedProjectId(""); setView("cases"); setSelectedCaseIds(new Set()); };
  const runImport = async () => { if (!auth) return; setImporting(true); setError(""); setMessage(""); try { const result = await api<{ created: number; updated: number; identity_review: number; repeat_study_resolved: number }>("/api/admin/import", { method: "POST" }, auth.csrf_token); setMessage(`同步完成：新增 ${result.created} 例，更新 ${result.updated} 例；${result.repeat_study_resolved} 例多次检查已自动确认，${result.identity_review} 例仍需身份核对。`); await loadOverview(); await loadCases(); } catch (reason) { setError(reason instanceof Error ? reason.message : "数据导入失败"); } finally { setImporting(false); } };
  const taskTabs = useMemo(() => [
    { key: "" as const, label: "全部任务", count: Object.values(counts).reduce((sum, value) => sum + value, 0) }, { key: "unassigned" as const, label: "未分配", count: counts.unassigned || 0 }, { key: "assigned" as const, label: "待初标", count: counts.assigned || 0 }, { key: "annotating" as const, label: "标注中", count: counts.annotating || 0 }, { key: "submitted" as const, label: "待审核", count: counts.submitted || 0 }, { key: "returned" as const, label: "待返工", count: counts.returned || 0 }, { key: "approved" as const, label: "已通过", count: counts.approved || 0 },
  ], [counts]);
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  const pageNumbers = useMemo(() => visiblePageNumbers(page, pageCount), [page, pageCount]);
  const pageStart = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const pageEnd = Math.min(page * pageSize, total);
  const allVisibleSelected = cases.length > 0 && cases.every((item) => selectedCaseIds.has(item.id));
  const toggleCase = (caseId: string, checked: boolean) => setSelectedCaseIds((current) => { const next = new Set(current); if (checked) next.add(caseId); else next.delete(caseId); return next; });
  const toggleVisibleCases = (checked: boolean) => setSelectedCaseIds((current) => { const next = new Set(current); for (const item of cases) { if (checked) next.add(item.id); else next.delete(item.id); } return next; });
  const changeFilter = (nextStatus: CaseStatus | "") => { setStatusFilter(nextStatus); setPage(1); setSelectedCaseIds(new Set()); };
  const activeProject = projects.find((project) => project.id === selectedProjectId) ?? projects[0] ?? null;
  const changeProject = (projectId: string) => {
    if (projectId === selectedProjectId) return;
    setSelectedProjectId(projectId); setStatusFilter(""); setSearch(""); setPage(1); setCases([]); setTotal(0); setCounts({}); setSelectedCaseIds(new Set()); setError(""); setMessage("");
  };

  if (booting) return <main className="boot-page"><BrandMark /><span>正在启动安全工作区…</span></main>;
  if (!auth) return <Login onLogin={(nextAuth) => { setUsers([]); setProjects([]); setSelectedProjectId(""); setView("cases"); setSelectedCaseIds(new Set()); setAuth(nextAuth); }} />;
  if (auth.user.must_change_password) return <ChangePassword auth={auth} onChanged={(user) => setAuth({ ...auth, user })} />;
  if (selected) return <Workspace auth={auth} detail={selected} onBack={() => { setSelected(null); void loadCases(); void loadOverview(); }} refreshDetail={refreshDetail} />;
  const currentUserIsAdmin = hasRole(auth.user, "admin");
  const activeView: View = view === "users" && !currentUserIsAdmin ? "cases" : view;
  const viewTitle = activeView === "cases" ? activeProject?.workflow_type === "pdff_roi" ? "PDFF 勾画任务" : "胰腺勾画任务" : activeView === "tasks" ? "任务发布中心" : activeView === "checklist" ? "Checklist 反馈任务" : "人员与权限";
  const viewDescription = activeView === "cases" ? activeProject?.workflow_type === "pdff_roi" ? "四相位联动查看：完成三个不同轴位平面的胰头、胰体、胰尾 ROI，并汇总病例级 PDFF 与层间一致性" : "完成胰腺影像勾画、提交与双角色审核闭环" : activeView === "tasks" ? "使用 Codex 创建、修订和发布任意多人协作任务" : activeView === "checklist" ? "独立答题后由另一位医生修订 Checklist，并反馈必要性、复杂度与得分权重" : "管理可登录平台的医生及其工作角色";

  return <div className="app-shell">
    <aside className="main-sidebar"><div className="brand"><BrandMark /><span>MedContour</span></div><nav><button className={activeView === "cases" ? "active" : ""} onClick={() => setView("cases")}><span>▦</span>病例任务</button><button className={activeView === "tasks" ? "active" : ""} onClick={() => setView("tasks")}><span>✦</span>任务中心</button><button className={activeView === "checklist" ? "active" : ""} onClick={() => setView("checklist")}><span>✓</span>Checklist 反馈</button>{currentUserIsAdmin && <button className={activeView === "users" ? "active" : ""} onClick={() => setView("users")}><span>♙</span>人员与权限</button>}</nav>
      <div className="sidebar-project-list"><p>影像任务</p>{projects.map((project) => <button aria-current={project.id === selectedProjectId ? "page" : undefined} className={project.id === selectedProjectId ? "active" : ""} key={project.id} onClick={() => { changeProject(project.id); setView("cases"); }} type="button"><i>{project.workflow_type === "pdff_roi" ? "％" : "◌"}</i><span><strong>{project.workflow_type === "pdff_roi" ? "PDFF 三平面 ROI" : "胰腺勾画"}</strong><small>{project.workflow_type === "pdff_roi" ? "独立新任务" : "原始任务"}</small></span>{project.id === selectedProjectId && <em />}</button>)}<div><i />本机安全运行</div></div><div className="sidebar-user"><div className="avatar">{auth.user.display_name.slice(0, 1)}</div><div><strong>{auth.user.display_name}</strong><span>{roleSummary(auth.user)}</span></div><button aria-label="退出登录" onClick={() => void logout()}>↗</button></div>
    </aside>
    <main className="main-content"><header className="content-header"><div><p className="eyebrow">{activeView === "tasks" ? "TEAM WORKSPACE" : activeView === "checklist" ? "CLINICAL EVALUATION" : "CLINICAL WORKSPACE"}</p><h1>{viewTitle}</h1><p>{viewDescription}</p></div><div className="header-actions">{currentUserIsAdmin && activeView === "cases" && <button className="secondary" disabled={importing} onClick={() => void runImport()}>{importing ? "正在扫描…" : "同步新病例"}</button>}</div></header>
      <Notice text={error} /><Notice text={message} tone="success" />{activeView === "users" ? <UserManagement csrf={auth.csrf_token} refresh={loadUsers} users={users} /> : activeView === "tasks" ? <CollaborationTasksView auth={auth} users={users} /> : activeView === "checklist" ? <ChecklistFeedbackWorkspace auth={auth} users={users} /> : <>
        <section className="metrics-grid"><article><span className="metric-icon teal">◌</span><div><p>我的任务</p><strong>{Object.values(counts).reduce((sum, value) => sum + value, 0)}</strong></div><small>当前可访问</small></article><article><span className="metric-icon amber">◐</span><div><p>进行中</p><strong>{(counts.annotating || 0) + (counts.returned || 0)}</strong></div><small>需要处理</small></article><article><span className="metric-icon blue">◎</span><div><p>待审核</p><strong>{counts.submitted || 0}</strong></div><small>已提交</small></article><article><span className="metric-icon green">✓</span><div><p>已通过</p><strong>{counts.approved || 0}</strong></div><small>审核完成</small></article></section>
        <section className="panel cases-panel">
          <div className="case-controls"><div className="tabs">{taskTabs.map((tab) => <button className={statusFilter === tab.key ? "active" : ""} key={tab.key || "all"} onClick={() => changeFilter(tab.key)}>{tab.label}<span>{tab.count}</span></button>)}</div><label className="search-box"><span>⌕</span><input onChange={(event) => { setSearch(event.target.value); setPage(1); setSelectedCaseIds(new Set()); }} placeholder="搜索姓名、PatientID 或病例编号" value={search} /></label></div>
          {currentUserIsAdmin && <div className="bulk-toolbar"><div><strong>{selectedCaseIds.size > 0 ? `已选择 ${selectedCaseIds.size} 例` : "批量分配"}</strong><span>{selectedCaseIds.size > 0 ? "选择会在翻页后保留" : "勾选病例，可跨页统一分配医生"}</span></div><div>{selectedCaseIds.size > 0 && <button className="text-button" onClick={() => setSelectedCaseIds(new Set())} type="button">清空选择</button>}<button className="secondary" disabled={selectedCaseIds.size === 0} onClick={() => setBatchAssigning(true)} type="button">批量分配{selectedCaseIds.size > 0 ? ` ${selectedCaseIds.size} 例` : ""}</button></div></div>}
          <div className="table-wrap"><table><thead><tr>{currentUserIsAdmin && <th className="selection-column"><input aria-label="选择当前页全部病例" checked={allVisibleSelected} onChange={(event) => toggleVisibleCases(event.currentTarget.checked)} type="checkbox" /></th>}<th>患者</th><th>检查信息</th><th>任务状态</th><th>初标医生</th><th>审核医生</th><th>版本</th><th /></tr></thead><tbody>{cases.map((item) => <tr className={selectedCaseIds.has(item.id) ? "selected-row" : ""} key={item.id}>{currentUserIsAdmin && <td className="selection-column"><input aria-label={`选择病例 ${item.case.patient_name_zh} ${item.case.case_code}`} checked={selectedCaseIds.has(item.id)} onChange={(event) => toggleCase(item.id, event.currentTarget.checked)} type="checkbox" /></td>}<td><div className="patient-cell"><div className="patient-monogram">{item.case.patient_name_zh.slice(0, 1)}</div><div><strong>{item.case.patient_name_zh}{item.case.needs_identity_review && <em title={item.case.identity_review_note || "来源身份信息存在冲突"}>待核对</em>}</strong><span>{item.case.patient_id}</span></div></div></td><td><strong className="case-code">{item.case.case_code}</strong><span className="table-sub" title={item.case.protocol_family}>{item.case.study_date || "日期未记录"} · {item.case.protocol_family || "Dixon 四序列"}</span></td><td><StatusBadge status={item.status} /></td><td>{item.annotator?.display_name || <span className="muted">未分配</span>}</td><td>{item.reviewer?.display_name || <span className="muted">待领取</span>}</td><td><span className="version-chip">v{item.latest_version}</span></td><td><div className="row-actions">{currentUserIsAdmin && <button className="text-button" onClick={() => setAssigning(item)}>分配</button>}<button className="open-button" onClick={() => void openCase(item)}>打开 →</button></div></td></tr>)}</tbody></table>{cases.length === 0 && <div className="empty-state"><span>○</span><h3>当前列表暂无病例</h3><p>{currentUserIsAdmin ? "点击“同步新病例”扫描 D 盘中已完成同步的数据。" : "请联系管理员为你分配任务。"}</p></div>}</div>
          <footer className="table-footer"><div className="page-summary">显示 {pageStart}–{pageEnd} / {total} 例 · 第 {page} / {pageCount} 页</div><nav aria-label="病例分页" className="pagination"><button aria-label="第一页" disabled={page === 1} onClick={() => setPage(1)} type="button">«</button><button aria-label="上一页" disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))} type="button">‹</button>{pageNumbers.map((pageNumber) => <button aria-current={pageNumber === page ? "page" : undefined} className={pageNumber === page ? "active" : ""} key={pageNumber} onClick={() => setPage(pageNumber)} type="button">{pageNumber}</button>)}<button aria-label="下一页" disabled={page === pageCount} onClick={() => setPage((current) => Math.min(pageCount, current + 1))} type="button">›</button><button aria-label="最后一页" disabled={page === pageCount} onClick={() => setPage(pageCount)} type="button">»</button><span className="page-size">每页 20 例</span></nav></footer>
        </section>
      </>}
    </main>
    {assigning && <AssignmentDialog csrf={auth.csrf_token} item={assigning} onClose={() => setAssigning(null)} onSaved={() => { setAssigning(null); void loadCases(); void loadOverview(); }} users={users} />}
    {batchAssigning && <BulkAssignmentDialog caseIds={[...selectedCaseIds]} csrf={auth.csrf_token} onClose={() => setBatchAssigning(false)} onSaved={(updated) => { setBatchAssigning(false); setSelectedCaseIds(new Set()); setMessage(`已批量分配 ${updated} 个病例任务。`); void loadCases(); void loadOverview(); }} users={users} />}
  </div>;
}
