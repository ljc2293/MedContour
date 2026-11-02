"use client";

import { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "../lib/api";
import { AuthState, CodexPublisherStatus, CodexTaskSession, CollaborationTask, TaskPriority, User } from "../lib/types";

const PRIORITY_LABELS: Record<TaskPriority, string> = { low: "低", normal: "普通", high: "高", urgent: "紧急" };

function fileSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function messageTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

type Props = {
  auth: AuthState;
  users: User[];
  selectedTask: CollaborationTask | null;
  onPublished: (task: CollaborationTask) => void;
  startRequest?: { key: number; taskId: string; submissionIds: string[] } | null;
};

export function CodexTaskPublisher({ auth, users, selectedTask, onPublished, startRequest = null }: Props) {
  const [publisherStatus, setPublisherStatus] = useState<CodexPublisherStatus | null>(null);
  const [session, setSession] = useState<CodexTaskSession | null>(null);
  const [message, setMessage] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [upgradeScope, setUpgradeScope] = useState("unstarted");
  const [busy, setBusy] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [failedMessage, setFailedMessage] = useState("");
  const messageListRef = useRef<HTMLDivElement>(null);
  const messageInputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const handledStartRequestRef = useRef<number | null>(null);

  useEffect(() => {
    void api<CodexPublisherStatus>("/api/admin/codex-task-publisher/status")
      .then(setPublisherStatus)
      .catch((reason) => setPublisherStatus({ available: false, configured: false, config_ready: false, credential_source: "unavailable", runtime: "", workspace_ready: false, workspace_writable: false, task_files_ready: false, model: "", message: reason instanceof Error ? reason.message : "Codex 状态检查失败" }));
  }, []);

  useEffect(() => {
    const textarea = messageInputRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 160)}px`;
  }, [message, session?.id]);

  useEffect(() => {
    const list = messageListRef.current;
    if (!list) return;
    list.scrollTo({ top: list.scrollHeight, behavior: "smooth" });
  }, [sending, session?.messages.length]);

  const loadSession = useCallback(async (sessionId: string) => {
    const next = await api<CodexTaskSession>(`/api/admin/codex-task-sessions/${sessionId}`);
    setSession(next);
    return next;
  }, []);

  const startSession = useCallback(async (taskId?: string, submissionIds: string[] = []) => {
    setBusy(true); setError(""); setFailedMessage(""); setMessage(""); setFiles([]);
    try {
      setSession(await api<CodexTaskSession>("/api/admin/codex-task-sessions", {
        method: "POST",
        body: JSON.stringify({ task_id: taskId || null, submission_ids: submissionIds }),
      }, auth.csrf_token));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "发布会话创建失败"); }
    finally {
      setBusy(false);
      requestAnimationFrame(() => messageInputRef.current?.focus());
    }
  }, [auth.csrf_token]);

  useEffect(() => {
    if (!startRequest || handledStartRequestRef.current === startRequest.key) return;
    handledStartRequestRef.current = startRequest.key;
    void startSession(startRequest.taskId, startRequest.submissionIds);
  }, [startRequest, startSession]);

  const sendMessage = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || !message.trim() || busy) return;
    const content = message.trim();
    const selectedFiles = files;
    const optimisticId = `optimistic-${Date.now()}`;
    const body = new FormData();
    body.append("message", content);
    selectedFiles.forEach((file) => body.append("attachments", file));
    const sessionId = session.id;
    setMessage("");
    setFiles([]);
    if (fileInputRef.current) fileInputRef.current.value = "";
    setBusy(true);
    setSending(true);
    setError("");
    setFailedMessage("");
    setSession((current) => current?.id === sessionId ? {
      ...current,
      status: "running",
      last_error: "",
      messages: [...current.messages, {
        id: optimisticId,
        role: "user",
        content,
        metadata: { optimistic: true, attachment_count: selectedFiles.length },
        created_at: new Date().toISOString(),
      }],
    } : current);
    requestAnimationFrame(() => messageInputRef.current?.focus());
    try {
      setSession(await api<CodexTaskSession>(`/api/admin/codex-task-sessions/${sessionId}/messages`, { method: "POST", body }, auth.csrf_token));
    } catch (reason) {
      const detail = reason instanceof Error ? reason.message : "Codex 本轮处理失败";
      setError(detail);
      setFailedMessage(content);
      const refreshed = await loadSession(sessionId).catch(() => undefined);
      if (!refreshed) {
        setSession((current) => current?.id === sessionId ? {
          ...current,
          status: "failed",
          last_error: detail,
          messages: current.messages.map((item) => item.id === optimisticId ? {
            ...item,
            metadata: { ...item.metadata, optimistic: false, failed: true },
          } : item),
        } : current);
      }
    } finally {
      setSending(false);
      setBusy(false);
      requestAnimationFrame(() => messageInputRef.current?.focus());
    }
  };

  const handleMessageKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== "Enter" || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    if (!busy && message.trim() && session?.status !== "published") {
      event.currentTarget.form?.requestSubmit();
    }
  };

  const restoreFailedMessage = () => {
    setMessage((current) => current.trim() ? `${current}\n${failedMessage}` : failedMessage);
    setFailedMessage("");
    setError("");
    requestAnimationFrame(() => messageInputRef.current?.focus());
  };

  const organizeDraft = async () => {
    if (!session) return;
    const sessionId = session.id;
    setBusy(true); setError("");
    try {
      setSession(await api<CodexTaskSession>(`/api/admin/codex-task-sessions/${sessionId}/draft`, { method: "POST" }, auth.csrf_token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "发布草稿整理失败");
      await loadSession(sessionId).catch(() => undefined);
    } finally { setBusy(false); }
  };

  const implement = async () => {
    if (!session) return;
    const sessionId = session.id;
    setBusy(true); setError("");
    try {
      setSession(await api<CodexTaskSession>(`/api/admin/codex-task-sessions/${sessionId}/implement`, { method: "POST" }, auth.csrf_token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "专用能力实现失败");
      await loadSession(sessionId).catch(() => undefined);
    } finally { setBusy(false); }
  };

  const publish = async () => {
    if (!session) return;
    setBusy(true); setError("");
    try {
      const task = await api<CollaborationTask>(`/api/admin/codex-task-sessions/${session.id}/publish`, {
        method: "POST",
        body: JSON.stringify({ upgrade_scope: upgradeScope }),
      }, auth.csrf_token);
      setSession((current) => current ? { ...current, status: "published", task_id: task.id } : current);
      onPublished(task);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "任务发布失败"); }
    finally { setBusy(false); }
  };

  const participantNames = useMemo(() => {
    const byId = new Map(users.map((user) => [user.id, user.display_name]));
    return (session?.draft.participant_ids || []).map((id) => byId.get(id) || "未知用户");
  }, [session, users]);

  const draftReady = Boolean(session?.draft.title && session.draft.participant_ids.length);
  const implementationReady = !session?.draft.requires_code_change || session.implementation_status === "ready";
  const hasUserMessage = Boolean(session?.messages.some((item) => item.role === "user"));
  const draftTouched = Boolean(session?.draft.title || session?.draft.description || session?.draft.participant_ids.length);
  const canOrganize = Boolean(session && hasUserMessage && session.status !== "published" && !busy);
  const canPublish = Boolean(session && session.status === "ready" && draftReady && implementationReady && !busy);
  const spec = session?.draft.task_spec;
  const deliverables = spec?.deliverables ?? [];
  const steps = spec?.steps ?? [];
  const fields = spec?.fields ?? [];

  return <section className="codex-publisher" aria-label="Codex 任务发布助理">
    <header className="codex-publisher-header">
      <div><span className="codex-orb">✦</span><div><p>CODEX PUBLISHER</p><h2>任务发布助理</h2></div></div>
      <span className={`codex-connection ${publisherStatus?.available ? "online" : "offline"}`}><i />{publisherStatus?.available ? `已连接 · Docker · ${publisherStatus.model}` : "未连接"}</span>
    </header>

    {!session ? <div className="codex-publisher-welcome">
      <span>✦</span><h3>从一段描述开始发布任务</h3><p>Codex 会读取你上传的本地资料，生成任务步骤、交付物、参与者和动态字段；确有需要时再增量开发专用界面。</p>
      <button className="primary" disabled={busy || publisherStatus?.available === false} onClick={() => void startSession()} type="button">{busy ? "正在创建对话…" : "＋ 创建新任务"}</button>
      {selectedTask && <button className="secondary" disabled={busy || publisherStatus?.available === false} onClick={() => void startSession(selectedTask.id)} type="button">修订所选任务 v{selectedTask.current_revision}</button>}
      {publisherStatus && !publisherStatus.available && <div className="codex-setup-note"><strong>Codex 尚未就绪</strong><span>{publisherStatus.message}</span></div>}
    </div> : <>
      <div className="codex-session-bar"><div><span>{session.mode === "revise" ? "修订任务" : "新建任务"}</span><strong>{session.draft.title || "等待任务描述"}</strong></div><button className="text-button" disabled={busy} onClick={() => setSession(null)} type="button">结束会话</button></div>
      <div className="codex-publisher-body">
        <div className="codex-conversation">
          <div aria-live="polite" className="codex-message-list" ref={messageListRef}>
            {session.messages.map((item) => {
              const optimistic = item.metadata.optimistic === true;
              const failed = item.metadata.failed === true;
              return <article className={`${item.role}${optimistic ? " optimistic" : ""}${failed ? " failed" : ""}`} key={item.id}>
                <span>{item.role === "assistant" ? "✦" : auth.user.display_name.slice(0, 1)}</span>
                <div>
                  <header><strong>{item.role === "assistant" ? "Codex" : auth.user.display_name}</strong><time dateTime={item.created_at}>{messageTime(item.created_at)}</time></header>
                  <p>{item.content}</p>
                  {optimistic && <small>已发送</small>}
                  {failed && <small>发送状态未确认</small>}
                </div>
              </article>;
            })}
            {sending && <article className="assistant pending">
              <span>✦</span>
              <div><header><strong>Codex</strong></header><div aria-label="Codex 正在处理" className="codex-thinking" role="status"><span><i /><i /><i /></span><em>正在处理本轮消息</em></div></div>
            </article>}
          </div>
          {session.assets.length > 0 && <div className="codex-assets">{session.assets.map((asset) => <span key={asset.id}>⌁ {asset.source_kind === "submission" ? "交付 · " : ""}{asset.name}<small>{fileSize(asset.byte_size)}</small></span>)}</div>}
          {error && failedMessage && <div className="codex-conversation-error" role="alert"><span><strong>本轮没有完成</strong>{error}</span><button onClick={restoreFailedMessage} type="button">重新编辑</button></div>}
          <form aria-busy={sending} className="codex-composer" onSubmit={(event) => void sendMessage(event)}>
            {files.length > 0 && <div className="codex-composer-files">{files.map((file, index) => <span key={`${file.name}-${file.lastModified}-${index}`}>⌁ {file.name}<button aria-label={`移除 ${file.name}`} onClick={() => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))} type="button">×</button></span>)}</div>}
            <textarea aria-label="给 Codex 的消息" disabled={session.status === "published"} onChange={(event) => setMessage(event.target.value)} onKeyDown={handleMessageKeyDown} placeholder={session.mode === "revise" ? "说明要修改的内容、升级范围或新处理方式…" : "给 Codex 发送消息…"} ref={messageInputRef} rows={1} value={message} />
            <div className="codex-composer-toolbar"><label className={`codex-file-button${busy ? " disabled" : ""}`}>＋<span>添加资料</span><input disabled={busy || session.status === "published"} multiple onChange={(event) => setFiles(Array.from(event.currentTarget.files || []))} ref={fileInputRef} type="file" /></label><span className="codex-composer-hint">{sending ? "Codex 正在处理，你可以先写下一条" : files.length ? `已选择 ${files.length} 个文件` : "Enter 发送 · Shift+Enter 换行"}</span><button aria-label="发送给 Codex" className="primary codex-send-button" disabled={busy || !message.trim() || session.status === "published"} type="submit">{sending ? "回复中…" : busy ? "处理中…" : "发送 ↑"}</button></div>
          </form>
        </div>

        <aside className="codex-draft-panel">
          <header><div><p>发布草稿</p><h3>{session.draft.title || "尚未生成"}</h3></div><span>v{session.mode === "revise" ? (selectedTask?.current_revision || 0) + 1 : 1}</span></header>
          <dl><div><dt>优先级</dt><dd>{PRIORITY_LABELS[session.draft.priority] || "普通"}</dd></div><div><dt>截止日期</dt><dd>{session.draft.due_date || "未设置"}</dd></div><div><dt>任务类型</dt><dd>{session.draft.task_kind || "generic"}</dd></div><div><dt>参与者</dt><dd>{participantNames.length ? participantNames.join("、") : "尚未指定"}</dd></div></dl>
          <section><h4>任务说明</h4><p>{session.draft.description || "继续对话，让 Codex 补全任务目标和交付要求。"}</p></section>
          {deliverables.length > 0 && <section><h4>交付物</h4><ul>{deliverables.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul></section>}
          {steps.length > 0 && <section><h4>执行步骤</h4><ol>{steps.map((step, index) => <li key={`${step.label}-${index}`}><strong>{step.label}</strong><span>{step.description}</span></li>)}</ol></section>}
          {fields.length > 0 && <section><h4>动态处理字段</h4><div className="codex-field-chips">{fields.map((field) => <span key={field.key}>{field.label}{field.required ? " *" : ""}</span>)}</div></section>}
          {session.status === "ready" && session.draft.requires_code_change && <section className="codex-code-change"><h4>需要增量开发</h4><p>{session.draft.implementation_summary || "Codex 判断该任务需要专用界面或处理接口。"}</p><button className="secondary" disabled={busy || session.implementation_status === "ready"} onClick={() => void implement()} type="button">{session.implementation_status === "ready" ? "✓ 代码与测试已准备" : session.implementation_status === "running" ? "正在修改源码…" : "让 Codex 实现并测试"}</button></section>}
          {session.mode === "revise" && <label className="upgrade-scope">升级范围<select onChange={(event) => setUpgradeScope(event.target.value)} value={upgradeScope}><option value="future_only">仅后续新增参与者</option><option value="unstarted">未开始的参与者（推荐）</option><option value="all_incomplete">全部未完成人员</option><option value="all">所有参与者</option></select></label>}
          {error && <p className="codex-error">{error}</p>}
          <footer><span>{session.status === "published" ? "已发布并锁定当前修订" : session.status === "ready" ? "发布草稿已整理完成" : draftReady ? "对话有更新，请重新整理发布草稿" : draftTouched ? "草稿还不完整，可以继续聊聊再整理" : hasUserMessage ? "准备好后，将对话整理成发布草稿" : "先像平常一样和 Codex 聊聊"}</span><div className="codex-draft-actions"><button className="secondary" disabled={!canOrganize} onClick={() => void organizeDraft()} type="button">{busy ? "处理中…" : draftTouched ? "再次整理草稿" : "整理发布草稿"}</button><button className="primary" disabled={!canPublish} onClick={() => void publish()} type="button">{busy ? "处理中…" : session.mode === "revise" ? "发布新修订" : "确认发布任务"}</button></div></footer>
        </aside>
      </div>
    </>}
  </section>;
}
