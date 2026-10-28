import { useEffect, useState } from "react";
import { marked } from "marked";
import { ArrowUpRight, Download, FileText } from "./Icons";

export function NotePage({ onGoKb }: { onGoKb: () => void }) {
  const [html, setHtml] = useState<string>("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/technical-note.md", { cache: "no-cache" })
      .then(async (res) => {
        if (!res.ok) throw new Error(`技术说明尚未生成 (${res.status})`);
        const text = await res.text();
        const rendered = await marked.parse(text, { gfm: true, breaks: false });
        setHtml(rendered);
      })
      .catch((err) => setError(err.message || String(err)));
  }, []);

  return (
    <div className="page note-page">
      <header className="page-head compact-head">
        <div>
          <span className="eyebrow">TECHNICAL NOTE / ONE PAGE</span>
          <h1>端侧推理 + seekdb 混合检索</h1>
          <p>实现、量化、评测与可复现实验的浓缩说明。可直接打印或导出 PDF。</p>
        </div>
        <div className="head-actions">
          <button className="btn ghost" onClick={onGoKb}>
            返回知识库 <ArrowUpRight size={14} />
          </button>
          <button className="btn accent" onClick={() => window.print()}>
            <Download size={14} /> 打印 / PDF
          </button>
        </div>
      </header>

      <article className="panel note-paper">
        <div className="note-paper-head">
          <div className="note-mark">
            <FileText size={18} />
          </div>
          <div>
            <strong>STEPKB · Technical Note</strong>
            <span>edge embedding · seekdb hybrid retrieval · reproducible benchmarks</span>
          </div>
        </div>
        {error ? (
          <div className="bench-empty">
            <strong>{error}</strong>
            <span>运行构建或补齐文档后刷新。</span>
          </div>
        ) : html ? (
          <div className="note-body" dangerouslySetInnerHTML={{ __html: html }} />
        ) : (
          <div className="side-empty">读取技术说明…</div>
        )}
      </article>
    </div>
  );
}
