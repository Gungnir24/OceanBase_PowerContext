import type { FormEvent } from "react";
import type { SearchHit, SearchMeta, SearchMode } from "../types";
import { ms } from "../lib/format";
import { ArrowUpRight, Command, Database, Search, Sparkles } from "./Icons";
import { ResultCard } from "./ResultCard";

const MODES: { key: SearchMode; label: string; hint: string }[] = [
  { key: "hybrid", label: "混合检索", hint: "向量 + 全文 · 库内 RRF" },
  { key: "vector", label: "向量检索", hint: "HNSW · cosine" },
  { key: "keyword", label: "关键词", hint: "FULLTEXT · IK / BM25" },
];

const EXAMPLES = [
  "WebGPU 不可用时怎么降级？",
  "INT8 量化压缩比是多少？",
  "新文档什么时候能被搜到？",
];

export function SearchPanel({
  query,
  onQuery,
  mode,
  onMode,
  topK,
  onTopK,
  onSubmit,
  busy,
  results,
  meta,
  error,
  hasDocs,
  modelReady,
  embeddingMs,
  e2eMs,
  onExample,
}: {
  query: string;
  onQuery: (value: string) => void;
  mode: SearchMode;
  onMode: (mode: SearchMode) => void;
  topK: number;
  onTopK: (value: number) => void;
  onSubmit: () => void;
  busy: boolean;
  results: SearchHit[];
  meta: SearchMeta | null;
  error: string | null;
  hasDocs: boolean;
  modelReady: boolean;
  embeddingMs: number | null;
  e2eMs: number | null;
  onExample: (value: string) => void;
}) {
  const maxScore = Math.max(...results.map((r) => r.score), 0.0001);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    onSubmit();
  };
  return (
    <section className="panel search-panel">
      <div className="panel-head">
        <div>
          <span className="eyebrow">02 / RETRIEVE</span>
          <h2>在 seekdb 里做一次混合检索</h2>
        </div>
        <div className="search-badges">
          <span className="pill">
            <Database size={12} /> DB 内融合
          </span>
          <span className="pill">
            <Sparkles size={12} /> 本地 query embedding
          </span>
        </div>
      </div>

      <form className="search-box" onSubmit={submit}>
        <div className="search-input-row">
          <Search size={20} className="search-leading" />
          <input
            id="search-input"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
            placeholder={hasDocs ? "问点什么，例如：量化后的模型还准吗？" : "先导入语料，再开始检索"}
            disabled={!hasDocs}
            autoComplete="off"
          />
          {query ? (
            <button
              type="button"
              className="clear-btn"
              onClick={() => onQuery("")}
              aria-label="清空"
            >
              ×
            </button>
          ) : (
            <span className="kbd">
              <Command size={11} /> K
            </span>
          )}
          <button className="btn accent search-submit" type="submit" disabled={!hasDocs || busy || !query.trim()}>
            {busy ? <span className="spinner" /> : <Search size={16} />}
            {busy ? "检索中" : mode === "keyword" ? "关键词检索" : modelReady ? "检索" : "准备并检索"}
          </button>
        </div>
        <div className="search-controls">
          <div className="mode-tabs">
            {MODES.map((item) => (
              <button
                key={item.key}
                type="button"
                className={mode === item.key ? "active" : ""}
                onClick={() => onMode(item.key)}
                title={item.hint}
              >
                {item.label}
                <small>{item.hint}</small>
              </button>
            ))}
          </div>
          <label className="topk">
            <span>Top K</span>
            <select value={topK} onChange={(e) => onTopK(Number(e.target.value))}>
              {[3, 5, 8, 10].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </label>
        </div>
      </form>

      {!results.length && !busy && !error ? (
        <div className="search-empty">
          <div className="empty-orbit">
            <Search size={22} />
          </div>
          <div>
            <strong>{hasDocs ? "输入问题，看看知识库里有什么" : "知识库还是空的"}</strong>
            <span>
              {hasDocs
                ? "查询向量在浏览器内计算，排序由 seekdb 的 DBMS_HYBRID_SEARCH 完成。"
                : "先载入示例语料，或拖入你自己的 txt / md 文件。"}
            </span>
          </div>
          {hasDocs ? (
            <div className="example-chips">
              {EXAMPLES.map((text) => (
                <button key={text} onClick={() => onExample(text)}>
                  {text}
                  <ArrowUpRight size={12} />
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div className="search-error">
          <strong>检索没有完成</strong>
          <span>{error}</span>
        </div>
      ) : null}

      {busy ? (
        <div className="result-skeletons">
          {[0, 1, 2].map((i) => (
            <div className="skeleton" key={i}>
              <span />
              <span />
              <span />
            </div>
          ))}
        </div>
      ) : null}

      {results.length && meta ? (
        <div className="results-wrap">
          <div className="results-head">
            <div>
              <h3>检索结果</h3>
              <span>
                {results.length} 条 · {meta.mode === "hybrid" ? "RRF 融合" : meta.mode === "vector" ? "HNSW 向量" : "IK 全文"} ·{" "}
                {meta.searchApi}
              </span>
            </div>
            <div className="result-timing">
              <span>
                query <b>{ms(embeddingMs)}</b>
              </span>
              <span>
                seekdb <b>{ms(meta.dbMs)}</b>
              </span>
              <span>
                端到端 <b>{ms(e2eMs)}</b>
              </span>
            </div>
          </div>
          <div className="result-list">
            {results.map((hit, index) => (
              <ResultCard
                key={hit.id}
                hit={hit}
                maxScore={maxScore}
                query={query}
                mode={mode}
                index={index}
              />
            ))}
          </div>
          <div className="result-footnote">
            <span>
              分数来自 seekdb 返回的 {meta.mode === "vector" ? "cosine 距离" : "融合 / BM25 分数"}，前端未重排。
            </span>
            <span className="mono">collection {meta.collection}</span>
          </div>
        </div>
      ) : null}
    </section>
  );
}
