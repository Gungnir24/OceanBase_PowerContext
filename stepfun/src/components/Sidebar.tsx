import type { EmbedderState, Health, Kb } from "../types";
import { Activity, Book, Database, FileText, Gauge, Plus, Sparkles } from "./Icons";
import { relTime } from "../lib/format";

export type View = "kb" | "bench" | "note";

export function Sidebar({
  view,
  onView,
  kbs,
  activeId,
  onSelect,
  onNew,
  embedder,
  health,
}: {
  view: View;
  onView: (view: View) => void;
  kbs: Kb[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  embedder: EmbedderState;
  health: Health | null;
}) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <div className="brand-mark">
          <Sparkles size={21} />
        </div>
        <div className="brand-word">
          <strong>STEPKB</strong>
          <span>edge knowledge base</span>
        </div>
      </div>

      <nav className="side-nav">
        <button className={`nav-item ${view === "kb" ? "active" : ""}`} onClick={() => onView("kb")}>
          <Book size={17} />
          <span>知识库</span>
          <em>{kbs.length}</em>
        </button>
        <button className={`nav-item ${view === "bench" ? "active" : ""}`} onClick={() => onView("bench")}>
          <Gauge size={17} />
          <span>基准实验室</span>
        </button>
        <button className={`nav-item ${view === "note" ? "active" : ""}`} onClick={() => onView("note")}>
          <FileText size={17} />
          <span>技术说明</span>
        </button>
      </nav>

      <div className="side-sep">
        <span>我的集合</span>
        <button className="mini-add" onClick={onNew} title="新建知识库">
          <Plus size={14} />
        </button>
      </div>

      <div className="kb-list">
        {kbs.map((kb) => (
          <button
            key={kb.id}
            className={`kb-item ${activeId === kb.id && view === "kb" ? "active" : ""}`}
            onClick={() => {
              onView("kb");
              onSelect(kb.id);
            }}
          >
            <span className="kb-dot" />
            <span className="kb-copy">
              <strong>{kb.name}</strong>
              <small>
                {kb.docCount} 文档 · {kb.chunkCount} 块
              </small>
            </span>
          </button>
        ))}
        {!kbs.length ? (
          <div className="kb-empty">
            <Database size={18} />
            <span>还没有集合</span>
            <button onClick={onNew}>创建第一个</button>
          </div>
        ) : null}
      </div>

      <div className="side-foot">
        <div className="runtime-card">
          <div className="runtime-row">
            <span className={`status-dot ${embedder.status === "ready" ? "ok" : embedder.status === "error" ? "bad" : "warm"}`} />
            <strong>{embedder.status === "ready" ? "本地推理就绪" : embedder.status === "error" ? "推理未就绪" : "正在准备模型"}</strong>
          </div>
          <div className="runtime-meta">
            <span>{embedder.backend === "webgpu" ? "WebGPU" : "WASM"}</span>
            <span>·</span>
            <span>{embedder.dtype === "q8" ? "INT8" : embedder.dtype.toUpperCase()}</span>
          </div>
          <div className="runtime-sub">
            <Activity size={12} />
            seekdb {health?.seekdb.version.match(/seekdb-v([\d.]+)/)?.[1] || "—"}
          </div>
        </div>
        <div className="side-credit">
          <span>last refresh</span>
          <span>{relTime(kbs.find((k) => k.id === activeId)?.lastRefreshAt)}</span>
        </div>
      </div>
    </aside>
  );
}
