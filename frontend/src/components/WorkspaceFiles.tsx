// 参照 DeerFlow ArtifactFileList：文件卡片、独立下载按钮、点击进入详情。
import { useState, type ChangeEvent } from "react";
import { Download, FileCode, FileImage, FileText, FolderOpen, Upload } from "lucide-react";
import type { WorkspaceFile } from "../api/types";
import { FilePreview } from "./FilePreview";
import "./workspace-files.css";

interface WorkspaceFilesProps {
  files: WorkspaceFile[];
  disabled: boolean;
  downloadUrl: (relativePath: string) => string;
  onUpload: (file: File) => void | Promise<void>;
}

function formatFileBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

const groups = [
  { area: "outputs", title: "生成结果", empty: "Agent 生成的交付文件会显示在这里。" },
  { area: "uploads", title: "上传资料", empty: "上传资料后，Agent 可以读取并处理。" },
  { area: "workspace", title: "工作文件", empty: "脚本与中间文件会保留在工作区。" },
];

function fileArea(path: string): string {
  const prefix = path.split("/")[0];
  return prefix === "uploads" || prefix === "outputs" ? prefix : "workspace";
}

function FileTypeIcon({ name }: { name: string }) {
  if (/\.(png|jpe?g|gif|webp|svg)$/i.test(name)) return <FileImage size={23} />;
  if (/\.(py|tsx?|jsx?|json|html|css|sh)$/i.test(name)) return <FileCode size={23} />;
  return <FileText size={23} />;
}

// 输入当前对话的文件列表和上传/下载接口；只改变选中的预览，不修改磁盘文件。
export function WorkspaceFiles({ files, disabled, downloadUrl, onUpload }: WorkspaceFilesProps) {
  const [selected, setSelected] = useState<string | null>(null);
  const selectedFile = files.find((file) => file.relative_path === selected);
  const handleFile = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    if (file) void onUpload(file);
    event.currentTarget.value = "";
  };

  if (selectedFile) {
    const url = downloadUrl(selectedFile.relative_path);
    return <FilePreview key={`${url}:${selectedFile.modified_at}:${selectedFile.size}`} file={selectedFile} url={url} onBack={() => setSelected(null)} />;
  }

  return (
    <section className="thread-files" aria-label="对话文件">
      <header className="thread-files-heading">
        <div><FolderOpen size={18} /><h2>文件</h2><span>{files.length}</span></div>
        <label className={`thread-upload ${disabled ? "is-disabled" : ""}`}>
          <Upload size={14} />上传资料
          <input type="file" aria-label="上传文件" disabled={disabled} onChange={handleFile} />
        </label>
      </header>
      {groups.map((group) => {
        const items = files.filter((file) => fileArea(file.relative_path) === group.area);
        return (
          <section className="thread-file-group" key={group.area} aria-label={group.title}>
            <h3>{group.title}<span>{items.length}</span></h3>
            {items.length === 0 ? <p className="thread-file-empty">{group.empty}</p> : (
              <ul className="artifact-cards">
                {items.map((file) => (
                  <li className="artifact-card" key={file.relative_path}>
                    <button className="artifact-select" type="button" onClick={() => setSelected(file.relative_path)} aria-label={`预览 ${file.name}`}>
                      <span className="artifact-file-icon"><FileTypeIcon name={file.name} /></span>
                      <span className="artifact-file-copy">
                        <strong>{file.name}</strong>
                        <small>{file.name.includes(".") ? file.name.split(".").pop()?.toUpperCase() : "FILE"} · {formatFileBytes(file.size)}</small>
                        <span title={file.relative_path}>{file.relative_path}</span>
                      </span>
                    </button>
                    <a className="artifact-download" href={downloadUrl(file.relative_path)} download={file.name} aria-label={`下载 ${file.name}`} title="下载文件"><Download size={16} /></a>
                  </li>
                ))}
              </ul>
            )}
          </section>
        );
      })}
    </section>
  );
}
