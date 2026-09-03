import type { ChangeEvent } from "react";

import type { WorkspaceFile } from "../api/types";


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
    <section className="side-section files-section">
      <div className="section-heading">
        <div>
          <span className="eyebrow">Workspace</span>
          <h2>文件</h2>
        </div>
        <label className={`upload-button ${disabled ? "is-disabled" : ""}`}>
          上传
          <input
            type="file"
            aria-label="上传文件"
            disabled={disabled}
            onChange={handleFile}
          />
        </label>
      </div>
      {files.length === 0 ? (
        <p className="muted">这个 Workspace 还没有文件。</p>
      ) : (
        <ul className="file-list">
          {files.map((file) => (
            <li key={file.relative_path}>
              <a href={downloadUrl(file.relative_path)}>
                <span className="file-name">{file.name}</span>
                <span className="file-path">{file.relative_path}</span>
              </a>
              <span className="file-size">{formatBytes(file.size)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
