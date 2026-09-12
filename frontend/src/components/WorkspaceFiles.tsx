import type { ChangeEvent } from "react";

import type { WorkspaceFile } from "../api/types";
import { Icon } from "./Icon";

interface WorkspaceFilesProps {
  files: WorkspaceFile[];
  disabled: boolean;
  downloadUrl: (relativePath: string) => string;
  onUpload: (file: File) => void | Promise<void>;
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

function fileKind(name: string): "document" | "code" | "image" {
  const extension = name.split(".").pop()?.toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(extension ?? "")) {
    return "image";
  }
  if (["ts", "tsx", "js", "jsx", "py", "json", "css", "html", "sh"].includes(extension ?? "")) {
    return "code";
  }
  return "document";
}

export function WorkspaceFiles({
  files,
  disabled,
  downloadUrl,
  onUpload,
}: WorkspaceFilesProps) {
  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    if (file) void onUpload(file);
    event.currentTarget.value = "";
  };

  return (
    <section className="side-section files-section" aria-label="Workspace 文件">
      <div className="section-heading">
        <div className="panel-title-wrap">
          <span className="panel-icon folder-icon"><Icon name="folder" size={16} /></span>
          <div>
            <span className="eyebrow">WORKSPACE</span>
            <h2>文件</h2>
          </div>
        </div>
        <label className={`upload-button ${disabled ? "is-disabled" : ""}`}>
          <Icon name="upload" size={14} />
          <span>上传</span>
          <input
            type="file"
            aria-label="上传文件"
            disabled={disabled}
            onChange={handleFile}
          />
        </label>
      </div>
      {files.length === 0 ? (
        <div className="files-empty">
          <Icon name="folder" size={21} />
          <p>这个 Workspace 还没有文件。</p>
          <span>上传资料后，Agent 可以直接读取。</span>
        </div>
      ) : (
        <ul className="file-list">
          {files.map((file) => (
            <li key={file.relative_path}>
              <span className={`file-type ${fileKind(file.name)}`}><Icon name="file" size={15} /></span>
              <a href={downloadUrl(file.relative_path)}>
                <span className="file-copy">
                  <span className="file-name">{file.name}</span>
                  <span className="file-path">{file.relative_path}</span>
                </span>
                <Icon name="download" size={14} className="file-download" />
              </a>
              <span className="file-size">{formatBytes(file.size)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
