import { useState } from "react";
import type { SearchHit, SearchMode } from "../types";
import { highlightParts, scoreDisplay } from "../lib/format";
import { ChevronRight, FileText, Layers } from "./Icons";

export function ResultCard({
  hit,
  maxScore,
  query,
  mode,
  index,
}: {
  hit: SearchHit;
  maxScore: number;
  query: string;
  mode: SearchMode;
  index: number;
}) {
  const [open, setOpen] = useState(false);
  const parts = highlightParts(hit.text, query);
  const ratio = hit.higherIsBetter
    ? Math.max(0.06, hit.score / Math.max(maxScore, 0.0001))
    : Math.max(0.06, 1 - hit.score / Math.max(maxScore, 0.0001));
  return (
    <article className={`result-card ${open ? "open" : ""}`}>
      <div className="result-rank">
        <span>{String(index + 1).padStart(2, "0")}</span>
        <em>{mode === "hybrid" ? "RRF" : mode === "vector" ? "VEC" : "FTS"}</em>
      </div>
      <div className="result-main">
        <div className="result-top">
          <div className="result-title">
            <h3>{hit.title}</h3>
            <div className="result-sub">
              <span>
                <FileText size={12} /> {hit.source || "粘贴文本"}
              </span>
              <span>
                <Layers size={12} /> chunk #{hit.chunkIndex}
              </span>
            </div>
          </div>
          <div className="result-score">
            <strong>{scoreDisplay(hit)}</strong>
            <span>{hit.scoreLabel}</span>
          </div>
        </div>
        <div className="score-track" title={`${hit.scoreLabel}: ${scoreDisplay(hit)}`}>
          <span style={{ width: `${Math.min(100, ratio * 100)}%` }} />
        </div>
        <p className={`result-text ${open ? "full" : ""}`}>
          {parts.map((part, i) =>
            part.hit ? <mark key={i}>{part.text}</mark> : <span key={i}>{part.text}</span>,
          )}
        </p>
        <div className="result-foot">
          <button className="text-btn" onClick={() => setOpen((v) => !v)}>
            {open ? "收起原文" : "查看完整原文"}
            <ChevronRight size={13} className={open ? "rot90" : ""} />
          </button>
          <span className="mono">doc: {hit.docId}</span>
        </div>
      </div>
    </article>
  );
}
