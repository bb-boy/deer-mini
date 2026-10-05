import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import { ApiError, getRestoreOperation, listRestorePoints, previewRestorePoint, restoreThread } from "../api/client";
import type { RestoreInput, RestoreOperation, RestorePoint, RestorePreview } from "../api/types";
import { Icon } from "./Icon";
import "./restore-points.css";

interface RestorePointsProps {
  threadId: string;
  userId: string;
  running: boolean;
  onCancelRun: () => Promise<void>;
  onRestored: () => Promise<void>;
  onClose: () => void;
  onBusyChange: (busy: boolean) => void;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "请求失败，请重试";
}

function createOperationId(): string {
  try {
    if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
    if (typeof globalThis.crypto?.getRandomValues === "function") {
      const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
      return `restore-${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
    }
  } catch { /* 随机数不可用时不提交，以免生成可碰撞的操作编号。 */ }
  throw new Error("无法生成恢复操作编号，请更换浏览器后重试。");
}

// 仅保存操作凭证，便于网络中断或关闭面板后继续查询同一操作。
export function restoreStorageKey(userId: string, threadId: string): string {
  return `deer-mini-restore:${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;
}

export function RestorePoints(props: RestorePointsProps) {
  const { threadId, userId } = props;
  const [points, setPoints] = useState<RestorePoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [preview, setPreview] = useState<RestorePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [credentialStored, setCredentialStored] = useState(false);
  const inputRef = useRef<RestoreInput | null>(null);
  const rejectionRef = useRef<string | null>(null);
  const busyRef = useRef(false);
  const generationRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollCountRef = useRef(0);
  const callbacks = useRef(props);
  callbacks.current = props;
  const dialogRef = useRef<HTMLElement>(null);
  const storageKey = restoreStorageKey(userId, threadId);
  const current = (generation: number) => generation === generationRef.current;

  function changeBusy(value: boolean) {
    busyRef.current = value;
    setBusy(value);
    callbacks.current.onBusyChange(value);
  }
  function remember(input: RestoreInput | null) {
    inputRef.current = input;
    setPending(input !== null);
    setCredentialStored(false);
    if (!input) rejectionRef.current = null;
    try {
      if (input) {
        sessionStorage.setItem(storageKey, JSON.stringify(input));
        setCredentialStored(true);
      }
      else {
        sessionStorage.removeItem(storageKey);
        sessionStorage.removeItem(`${storageKey}:rejected`);
      }
    } catch { /* 禁用浏览器存储时，本次面板仍保留操作编号。 */ }
  }
  async function loadPoints(generation: number) {
    try {
      const values = await listRestorePoints(threadId, userId);
      if (current(generation)) setPoints(values);
    } catch (reason) {
      if (current(generation)) setError(errorMessage(reason));
    } finally {
      if (current(generation)) setLoading(false);
    }
  }
  async function showPreview(pointId: string) {
    const generation = generationRef.current;
    changeBusy(true);
    setPreview(null);
    setError(null);
    setNotice(callbacks.current.running ? "正在停止当前运行，停止后重新生成预览…" : "正在检查消息与文件…");
    try {
      if (callbacks.current.running) await callbacks.current.onCancelRun();
      if (!current(generation)) return;
      const value = await previewRestorePoint(threadId, pointId, userId);
      if (!current(generation)) return;
      setPreview(value);
      setNotice(null);
      return true;
    } catch (reason) {
      if (current(generation)) { setError(errorMessage(reason)); setNotice(null); }
    } finally {
      if (current(generation)) changeBusy(false);
    }
  }
  function scheduleQuery(generation: number) {
    if (!current(generation)) return;
    if (++pollCountRef.current > 30) {
      setNotice("恢复结果尚未确认，请继续查询。确认完成前不能开始任务或上传文件。");
      return;
    }
    timerRef.current = setTimeout(() => { void queryOperation(generation); }, Math.min(1000 * pollCountRef.current, 5000));
  }
  async function receiveOperation(value: RestoreOperation, generation: number) {
    if (!current(generation)) return;
    setError(null);
    if (value.status === "committed" && value.cleaned) {
      setNotice("恢复已提交，正在刷新消息与文件…");
      try {
        await callbacks.current.onRestored();
        if (!current(generation)) return;
        remember(null);
        changeBusy(false);
        callbacks.current.onClose();
      } catch (reason) {
        if (current(generation)) setError(`恢复已提交，但页面刷新失败：${errorMessage(reason)}。请继续查询并刷新。`);
      }
      return;
    }
    if (value.status === "rolled_back" && value.cleaned) {
      remember(null);
      setPreview(null);
      setNotice(null);
      setError("恢复未完成，已回到恢复前状态。请重新预览后再试。");
      changeBusy(false);
      void loadPoints(generation);
      return;
    }
    if (value.status === "needs_recovery") {
      setNotice(null);
      setError("恢复状态无法确认，需要人工恢复。此对话的运行、上传与再次恢复已禁用。");
      return;
    }
    setNotice(value.status === "committed" || value.status === "rolled_back"
      ? "消息与文件状态已确定，正在完成清理…"
      : "正在恢复消息与文件，请稍候…");
    scheduleQuery(generation);
  }
  async function submissionFailed(reason: unknown, generation: number) {
    if (!current(generation)) return;
    if (reason instanceof ApiError && [400, 422, 507].includes(reason.status)) {
      rejectionRef.current = errorMessage(reason);
      try { sessionStorage.setItem(`${storageKey}:rejected`, rejectionRef.current); }
      catch { setCredentialStored(false); }
      setNotice("恢复请求未成功，正在确认是否已有操作…");
      await queryOperation(generation);
    } else if (reason instanceof ApiError && reason.status === 409) {
      const pointId = inputRef.current?.restore_point_id;
      remember(null);
      const refreshed = pointId && await showPreview(pointId);
      if (current(generation)) setNotice(refreshed
        ? "状态已变化，已刷新预览，请重新确认。"
        : "状态已变化，当前无法生成新预览，请稍后重新选择恢复点。");
    } else if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500) {
      remember(null);
      setPreview(null);
      setNotice(null);
      setError(errorMessage(reason));
      changeBusy(false);
    } else {
      setNotice("连接中断，恢复结果尚未确认，正在查询同一操作…");
      await queryOperation(generation);
    }
  }
  async function queryOperation(generation: number) {
    const input = inputRef.current;
    if (!input || !current(generation)) return;
    if (timerRef.current) clearTimeout(timerRef.current);
    setChecking(true);
    try {
      let value: RestoreOperation;
      try {
        value = await getRestoreOperation(threadId, input.operation_id, userId);
      } catch (reason) {
        if (!current(generation)) return;
        if (reason instanceof ApiError && reason.status === 404) {
          if (rejectionRef.current) {
            const rejection = rejectionRef.current;
            remember(null);
            setPreview(null);
            setNotice(null);
            setError(rejection);
            changeBusy(false);
            return;
          }
          // 查询不存在且原请求结果未知才重发原凭证；不生成新的 operation_id。
          try { value = await restoreThread(threadId, userId, input); }
          catch (retryError) {
            // 原请求可能仍在保存快照，尚未登记操作；此时 404 后的 409 不能证明预览过期。
            if (retryError instanceof ApiError && retryError.status !== 409
              && ((retryError.status >= 400 && retryError.status < 500) || retryError.status === 507)) {
              await submissionFailed(retryError, generation);
              return;
            }
            throw retryError;
          }
        } else throw reason;
      }
      await receiveOperation(value, generation);
    } catch (reason) {
      if (current(generation)) {
        setError(`恢复结果尚未确认：${errorMessage(reason)}`);
        scheduleQuery(generation);
      }
    } finally {
      if (current(generation)) setChecking(false);
    }
  }
  async function confirmRestore() {
    if (!preview || busyRef.current || inputRef.current) return;
    const generation = generationRef.current;
    let operationId: string;
    try { operationId = createOperationId(); }
    catch (reason) { setError(errorMessage(reason)); return; }
    const input: RestoreInput = {
      operation_id: operationId, restore_point_id: preview.restore_point_id,
      revision: preview.revision, fingerprint: preview.fingerprint,
    };
    remember(input);
    changeBusy(true);
    setChecking(true);
    setError(null);
    setNotice("正在恢复消息与文件，请稍候…");
    pollCountRef.current = 0;
    try {
      const value = await restoreThread(threadId, userId, input);
      await receiveOperation(value, generation);
    } catch (reason) {
      await submissionFailed(reason, generation);
    } finally {
      if (current(generation)) setChecking(false);
    }
  }

  useEffect(() => {
    const generation = ++generationRef.current;
    const previousFocus = document.activeElement as HTMLElement | null;
    dialogRef.current?.focus();
    void loadPoints(generation);
    try {
      const saved = sessionStorage.getItem(storageKey);
      if (saved) {
        const input = JSON.parse(saved) as RestoreInput;
        if (typeof input.operation_id === "string" && typeof input.restore_point_id === "string"
          && typeof input.revision === "number" && typeof input.fingerprint === "string") {
          rejectionRef.current = sessionStorage.getItem(`${storageKey}:rejected`);
          remember(input);
          changeBusy(true);
          void queryOperation(generation);
        }
      }
    } catch { /* 浏览器存储不可用时仍可正常预览。 */ }
    return () => {
      generationRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
      previousFocus?.focus();
    };
    // 父组件按用户和 Thread 设置 key；回调由 ref 读取最新值。
  }, [storageKey]);

  const closeDisabled = checking || (busy && !pending) || (pending && !credentialStored);
  function handleKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key === "Escape") {
      event.preventDefault(); event.stopPropagation();
      if (!closeDisabled) callbacks.current.onClose();
    }
    if (event.key !== "Tab") return;
    const controls = dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), [href], [tabindex='0']");
    if (!controls?.length) { event.preventDefault(); return; }
    const first = controls[0]; const last = controls[controls.length - 1];
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialogRef.current)) {
      event.preventDefault(); first.focus();
    }
  }

  return (
    <div className="modal-backdrop">
      <section className="action-dialog restore-dialog" role="dialog" aria-modal="true" aria-labelledby="restore-title" aria-describedby="restore-scope" ref={dialogRef} tabIndex={-1} onKeyDown={handleKeyDown}>
        <header className="restore-heading"><div><span className="dialog-kicker">RESTORE POINTS</span><h2 id="restore-title">恢复对话与文件</h2></div>
          <button type="button" className="icon-button" aria-label="关闭恢复面板" disabled={closeDisabled} onClick={() => callbacks.current.onClose()}><Icon name="close" size={16} /></button>
        </header>
        <div className="restore-scope" id="restore-scope">
          <p>消息与 uploads、workspace、outputs 中的文件将一起恢复到所选轮次开始前。</p>
          <p>后续上传的文件可能被删除。环境、外部操作和长期记忆不会恢复。</p>
          <p>每轮开始前都会保存恢复点，首轮可恢复到尚无消息的状态。</p>
        </div>
        {props.running ? <p className="muted">预览前会先停止当前运行，等待收尾后重新检查消息与文件。</p> : null}
        {loading ? <p role="status">正在加载恢复点…</p> : null}
        {!loading && points.length === 0 ? <p>还没有恢复点。新一轮开始前会自动创建。</p> : null}
        <ul className="restore-point-list" aria-label="恢复点">
          {points.map((point) => <li key={point.id}>
            <button type="button" disabled={busy || !point.available} onClick={() => void showPreview(point.id)} aria-label={`预览恢复：${point.message || "本轮开始前"}`} aria-pressed={preview?.restore_point_id === point.id}>
              <span className="restore-point-kind">{point.kind === "recovery" ? "恢复前备份" : "本轮开始前"}</span>
              <strong>{point.message || "本轮开始前"}</strong><time dateTime={point.created_at}>{new Date(point.created_at).toLocaleString("zh-CN")}</time>
              {!point.available ? <small>{point.unavailable_reason || "此轮次没有可用的文件备份"}</small> : null}
            </button>
          </li>)}
        </ul>
        {preview ? <section className="restore-preview" aria-label="恢复预览">
          <h3>恢复预览</h3>
          <p>将撤销 {preview.removed_messages} 条消息，恢复后保留 {preview.target_messages} 条消息。</p>
          <div className="restore-file-changes">
            {([['created', '新增'], ['modified', '覆盖'], ['deleted', '删除']] as const).map(([key, label]) => <section key={key} aria-label={`${label}文件`}>
              <h4>{label} · {preview[key].length}</h4>
              {preview[key].length ? <ul>{preview[key].map((path) => <li key={path}>{path}</li>)}</ul> : <p>无</p>}
            </section>)}
          </div>
          <p className="muted">恢复前会保存当前状态，可从“恢复前备份”撤销这次恢复。</p>
        </section> : null}
        {pending && !credentialStored ? <p className="restore-status" role="status">浏览器无法保存操作编号，请保持恢复面板打开，直到结果确认。</p> : null}
        {notice ? <p className="restore-status" role="status">{notice}</p> : null}
        {error ? <p className="restore-error" role="alert">{error}</p> : null}
        <div className="dialog-actions">
          {pending ? <button type="button" className="dialog-secondary" disabled={checking} onClick={() => { pollCountRef.current = 0; void queryOperation(generationRef.current); }}>继续查询恢复结果</button> : null}
          {!pending && !loading && !preview ? <button type="button" className="dialog-secondary" disabled={busy} onClick={() => { setLoading(true); setError(null); void loadPoints(generationRef.current); }}>刷新恢复点</button> : null}
          {preview && !pending ? <button type="button" className="dialog-danger" disabled={busy} onClick={() => void confirmRestore()}>确认恢复消息与文件</button> : null}
        </div>
      </section>
    </div>
  );
}
