import { useRef, useState } from "react";
import type { Doc, ImportProgress } from "../types";
import { ms, relTime } from "../lib/format";
import { Check, FileText, Layers, Refresh, Sparkles, Trash, Upload, X } from "./Icons";
import { Modal } from "./Modal";

const STEPS: { key: ImportProgress["stage"]; label: string }[] = [
  { key: "chunking", label: "切分文档" },
  { key: "loading", label: "加载本地模型" },
  { key: "embedding", label: "浏览器向量化" },
  { key: "writing", label: "写入 seekdb" },
  { key: "refreshing", label: "刷新索引" },
  { key: "done", label: "完成" },
];

export function ImportPanel({
  docs,
  progress,
  onFiles,
  onDemo,
  onPasteText,
  onRefresh,
  onDeleteDoc,
  modelReady,
  onLoadModel,
  demoLoading,
  refreshing,
}: {
  docs: Doc[];
  progress: ImportProgress;
  onFiles: (files: File[]) => void;
  onDemo: () => void;
  onPasteText: (title: string, text: string) => void;
  onRefresh: () => void;
  onDeleteDoc: (docId: string) => void;
  modelReady: boolean;
  onLoadModel: () => void;
  demoLoading: boolean;
  refreshing: boolean;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [pasteOpen, setPasteOpen] = useState(false);
  const [pasteTitle, setPasteTitle] = useState("");
  const [pasteText, setPasteText] = useState("");

  const busy = ["chunking", "loading", "embedding", "writing", "refreshing"].includes(progress.stage);
  const active = progress.stage !== "idle";
  const stepIndex = STEPS.findIndex((s) => s.key === progress.stage);

  function pick(files: FileList | null) {
    if (!files?.length) return;
    onFiles(Array.from(files));
  }

  return (
    <>
      <section className={`panel import-panel ${docs.length ? "compact" : ""}`}>
        <div className="panel-head">
          <div>
            <span className="eyebrow">01 / INGEST</span>
            <h2>{docs.length ? "继续扩充你的知识库" : "把语料留在设备上"}</h2>
          </div>
          <div className="head-actions">
            {!modelReady ? (
              <button className="btn ghost" onClick={onLoadModel}>
                加载模型
              </button>
            ) : null}
            {docs.length ? (
              <button className="btn ghost" onClick={onRefresh} disabled={refreshing || busy}>
                <Refresh size={15} className={refreshing ? "spin" : ""} />
                {refreshing ? "刷新中" : "刷新索引"}
              </button>
            ) : null}
          </div>
        </div>

        <div className="import-grid">
          <div
            className={`dropzone ${dragging ? "dragging" : ""} ${busy ? "busy" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pick(e.dataTransfer.files);
            }}
            onClick={() => !busy && inputRef.current?.click()}
            role="button"
            tabIndex={0}
            onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
          >
            <input
              ref={inputRef}
              type="file"
              accept=".txt,.md,.markdown,text/plain,text/markdown"
              multiple
              hidden
              onChange={(e) => {
                pick(e.target.files);
                e.target.value = "";
              }}
            />
            <div className="drop-icon">
              <Upload size={22} />
            </div>
            <div className="drop-copy">
              <strong>{docs.length ? "添加更多文档" : "拖入文本或 Markdown"}</strong>
              <span>支持 .txt / .md，可多选；原文与向量都会进入 seekdb</span>
            </div>
            <div className="drop-actions">
              <button
                className="btn mini"
                onClick={(e) => {
                  e.stopPropagation();
                  setPasteOpen(true);
                }}
              >
                粘贴文本
              </button>
              <button
                className="btn mini accent"
                onClick={(e) => {
                  e.stopPropagation();
                  onDemo();
                }}
                disabled={demoLoading || busy}
              >
                <Sparkles size={14} />
                {demoLoading ? "载入中…" : "载入示例语料"}
              </button>
            </div>
          </div>

          <div className={`pipeline ${active ? "active" : ""}`}>
            <div className="pipeline-head">
              <span>INGEST PIPELINE</span>
              <span className={`pipeline-state state-${progress.stage}`}>
                {progress.stage === "error" ? "失败" : progress.stage === "done" ? "已完成" : active ? "运行中" : "待命"}
              </span>
            </div>
            <div className="pipeline-steps">
              {STEPS.map((step, i) => {
                const done = progress.stage === "done" || (stepIndex >= 0 && i < stepIndex);
                const current = step.key === progress.stage;
                return (
                  <div key={step.key} className={`pstep ${done ? "done" : ""} ${current ? "current" : ""}`}>
                    <span className="pstep-dot">{done ? <Check size={11} /> : i + 1}</span>
                    <span>{step.label}</span>
                    {current && step.key === "embedding" ? (
                      <em>
                        {progress.done}/{progress.total}
                      </em>
                    ) : current && step.key === "writing" ? (
                      <em>
                        {progress.done}/{progress.total}
                      </em>
                    ) : current && step.key === "loading" && progress.detail ? (
                      <em>{progress.detail}</em>
                    ) : done && progress.metrics?.[step.key] != null ? (
                      <em>{ms(progress.metrics[step.key])}</em>
                    ) : null}
                  </div>
                );
              })}
            </div>
            <div className="progress-track">
              <span style={{ width: `${Math.max(2, Math.min(100, progress.percent))}%` }} />
            </div>
            <div className="pipeline-foot">
              <span>{progress.label}</span>
              <strong>{Math.round(progress.percent)}%</strong>
            </div>
            {progress.stage === "done" && progress.metrics ? (
              <div className="pipeline-summary">
                <span>
                  向量化吞吐 <b>{(progress.metrics.throughput || 0).toFixed(1)}</b> 块/秒
                </span>
                <span>
                  总耗时 <b>{ms(progress.metrics.total)}</b>
                </span>
              </div>
            ) : null}
            {progress.stage === "error" ? (
              <div className="pipeline-error">
                {progress.detail || "导入失败"}
                <button className="btn mini" onClick={() => window.location.reload()}>
                  重试
                </button>
              </div>
            ) : null}
          </div>
        </div>

        {docs.length ? (
          <div className="doc-strip">
            <div className="doc-strip-head">
              <span>
                <Layers size={14} /> 已入库文档
              </span>
              <span>{docs.length} 篇</span>
            </div>
            <div className="doc-rows">
              {docs.slice(0, 6).map((doc) => (
                <div className="doc-row" key={doc.docId}>
                  <span className="doc-icon">
                    <FileText size={15} />
                  </span>
                  <div className="doc-copy">
                    <strong>{doc.title}</strong>
                    <small>
                      {doc.source || "粘贴文本"} · {doc.chunkCount} 块 · {doc.charCount.toLocaleString("zh-CN")} 字 · {relTime(doc.importedAt)}
                    </small>
                  </div>
                  <button
                    className="icon-btn danger"
                    title="删除文档"
                    onClick={() => onDeleteDoc(doc.docId)}
                    disabled={busy}
                  >
                    <Trash size={14} />
                  </button>
                </div>
              ))}
            </div>
            {docs.length > 6 ? <div className="doc-more">还有 {docs.length - 6} 篇，切换集合可查看全部</div> : null}
          </div>
        ) : null}
      </section>

      <Modal
        open={pasteOpen}
        onClose={() => setPasteOpen(false)}
        title="粘贴一段文本"
        subtitle="文本只会在浏览器内切分与向量化，然后写入 seekdb。"
        footer={
          <>
            <button className="btn ghost" onClick={() => setPasteOpen(false)}>
              取消
            </button>
            <button
              className="btn accent"
              disabled={!pasteText.trim()}
              onClick={() => {
                onPasteText(pasteTitle.trim() || "未命名文本", pasteText.trim());
                setPasteOpen(false);
                setPasteText("");
                setPasteTitle("");
              }}
            >
              导入文本
            </button>
          </>
        }
      >
        <label className="field">
          <span>标题</span>
          <input
            value={pasteTitle}
            onChange={(e) => setPasteTitle(e.target.value)}
            placeholder="例如：WebGPU 调研笔记"
          />
        </label>
        <label className="field">
          <span>正文</span>
          <textarea
            value={pasteText}
            onChange={(e) => setPasteText(e.target.value)}
            placeholder="粘贴需要进入知识库的文本…"
            rows={10}
          />
        </label>
        <div className="field-hint">
          <X size={12} /> 不会调用任何云端模型接口
        </div>
      </Modal>
    </>
  );
}
