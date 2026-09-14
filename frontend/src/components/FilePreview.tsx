// 对照 DeerFlow ArtifactFileDetail：返回列表、预览/源码切换和独立下载。
import { useEffect, useState } from "react";
import { ArrowLeft, Code2, Download, Eye, FileText } from "lucide-react";
import type { WorkspaceFile } from "../api/types";
import { MarkdownContent } from "./MarkdownContent";

const MAX_PREVIEW_BYTES = 5 * 1024 * 1024;
const TEXT_FILE = /\.(md|markdown|txt|csv|tsv|json|py|tsx?|jsx?|html?|css|sh|yaml|yml|toml|xml|log|sql)$/i;
const IMAGE_FILE = /\.(png|jpe?g|gif|webp|svg)$/i;

// 输入 HTTP 响应，输出有大小上限的内容；超过限制取消读取。
async function readPreview(response: Response): Promise<Blob> {
  if (!response.ok) throw new Error(`文件预览失败（HTTP ${response.status}）`);
  if (!response.body) throw new Error("无法读取文件内容");
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_PREVIEW_BYTES) throw new Error("文件超过 5 MB，请下载后查看。");
      chunks.push(new Uint8Array(value));
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
  return new Blob(chunks, { type: response.headers.get("content-type") ?? "application/octet-stream" });
}

// 输入选中文件、下载地址和返回动作；只请求文件并管理临时 URL，不写服务器。
export function FilePreview({ file, url, onBack }: { file: WorkspaceFile; url: string; onBack: () => void }) {
  const markdown = /\.(md|markdown)$/i.test(file.name);
  const html = /\.html?$/i.test(file.name);
  const isImage = IMAGE_FILE.test(file.name);
  const isText = TEXT_FILE.test(file.name);
  const [mode, setMode] = useState<"preview" | "code">("preview");
  const [content, setContent] = useState("");
  const [imageUrl, setImageUrl] = useState<string>();
  const [loading, setLoading] = useState(isText || isImage);
  const [error, setError] = useState<string>();

  useEffect(() => {
    if (!isText && !isImage) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    async function load() {
      try {
        if (file.size > MAX_PREVIEW_BYTES) throw new Error("文件超过 5 MB，请下载后查看。");
        const response = await fetch(url, { signal: controller.signal });
        const blob = await readPreview(response);
        if (controller.signal.aborted) return;
        if (isImage) {
          objectUrl = URL.createObjectURL(blob);
          setImageUrl(objectUrl);
        } else {
          const text = new TextDecoder("utf-8", { fatal: true }).decode(await blob.arrayBuffer());
          if (!controller.signal.aborted) setContent(text);
        }
      } catch (failure) {
        if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : "文件预览失败，请下载后查看。");
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    }
    void load();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [file.size, isImage, isText, url]);

  return (
    <section className="artifact-detail" aria-label={`文件预览 ${file.name}`}>
      <header className="artifact-toolbar">
        <button type="button" onClick={onBack} aria-label="返回文件列表" title="返回文件列表"><ArrowLeft size={17} /></button>
        <strong title={file.relative_path}>{file.name}</strong>
        <a href={url} download={file.name} aria-label={`下载 ${file.name}`} title="下载"><Download size={17} /></a>
      </header>
      {(markdown || html) && <div className="artifact-view-tabs" role="tablist" aria-label="文件显示方式">
        <button type="button" role="tab" aria-selected={mode === "preview"} onClick={() => setMode("preview")}><Eye size={14} />预览</button>
        <button type="button" role="tab" aria-selected={mode === "code"} onClick={() => setMode("code")}><Code2 size={14} />源码</button>
      </div>}
      <div className="artifact-preview-body">
        {loading ? <p role="status">正在加载预览…</p> : error ? <p role="alert">{error}</p> : isImage ? (
          <img src={imageUrl} alt={file.name} onError={() => setError("图片预览失败，请下载后查看。")} />
        ) : isText ? (
          markdown && mode === "preview" ? <MarkdownContent content={content} /> : html && mode === "preview" ? (
            <iframe title={`${file.name} 预览`} sandbox="" referrerPolicy="no-referrer" srcDoc={`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; form-action 'none'; base-uri 'none'">${content}`} />
          ) : <pre><code>{content}</code></pre>
        ) : (
          <div className="artifact-preview-fallback"><FileText size={38} /><strong>{file.name}</strong><p>此文件类型暂不支持预览。</p><a href={url} download={file.name}><Download size={15} />下载文件</a></div>
        )}
      </div>
    </section>
  );
}
