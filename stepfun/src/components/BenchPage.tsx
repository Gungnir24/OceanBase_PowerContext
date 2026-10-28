import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { embedQuery, getLoadedEmbedder, loadEmbedder } from "../lib/embedder";
import { ms, pct } from "../lib/format";
import type { DemoCorpus, EmbedderState, Health } from "../types";
import { Activity, ArrowUpRight, Check, Download, Gauge, Play, Sparkles } from "./Icons";

type BenchRow = {
  label: string;
  backend: string;
  dtype: string;
  batchSize: number;
  modelBytes: number;
  loadMs: number;
  warmupMs: number;
  embedMsPerChunk: number;
  queryMs: number;
  dbMs: number;
  e2eMs: number;
  recall5: number;
  ndcg5: number;
  mrr: number;
  top5Overlap: number | null;
  note?: string;
};

type BenchSummary = {
  generatedAt: string;
  environment: {
    browser: string;
    device: string;
    os: string;
    seekdb: string;
    model: string;
    revision: string;
  };
  corpus: { documents: number; chunks: number; queries: number; chunkSize: number; overlap: number };
  results: BenchRow[];
};

type EvalResult = {
  queries: number;
  recall5: number;
  ndcg5: number;
  mrr: number;
  avgE2e: number;
  avgQuery: number;
  avgDb: number;
  rows: { q: string; hit: boolean; rank: number; e2e: number }[];
};

function dcg(relevances: number[]): number {
  return relevances.reduce((sum, rel, i) => sum + (Math.pow(2, rel) - 1) / Math.log2(i + 2), 0);
}

function evaluate(
  corpus: DemoCorpus,
  results: { query: string; docs: string[]; e2e: number; queryMs: number; dbMs: number }[],
): EvalResult {
  let recall = 0;
  let ndcg = 0;
  let mrr = 0;
  const rows: EvalResult["rows"] = [];
  results.forEach((result, index) => {
    const relevant = new Set(corpus.queries[index].relevant);
    const top = result.docs.slice(0, 5);
    const hits = top.filter((id) => relevant.has(id));
    recall += hits.length / relevant.size;
    const gains = top.map((id) => (relevant.has(id) ? 1 : 0));
    const ideal = Array.from({ length: Math.min(5, relevant.size) }, () => 1);
    ndcg += dcg(gains) / Math.max(dcg(ideal), 1e-9);
    const first = top.findIndex((id) => relevant.has(id));
    mrr += first >= 0 ? 1 / (first + 1) : 0;
    rows.push({
      q: result.query,
      hit: first >= 0,
      rank: first + 1,
      e2e: result.e2e,
    });
  });
  const n = Math.max(1, results.length);
  return {
    queries: results.length,
    recall5: recall / n,
    ndcg5: ndcg / n,
    mrr: mrr / n,
    avgE2e: results.reduce((s, r) => s + r.e2e, 0) / n,
    avgQuery: results.reduce((s, r) => s + r.queryMs, 0) / n,
    avgDb: results.reduce((s, r) => s + r.dbMs, 0) / n,
    rows,
  };
}

export function BenchPage({
  health,
  embedder,
  activeKbId,
  onGoKb,
}: {
  health: Health | null;
  embedder: EmbedderState;
  activeKbId: string | null;
  onGoKb: () => void;
}) {
  const [summary, setSummary] = useState<BenchSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState(0);
  const [evalResult, setEvalResult] = useState<EvalResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/benchmarks/summary.json", { cache: "no-cache" })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setSummary(data))
      .catch(() => setSummary(null))
      .finally(() => setLoading(false));
  }, []);

  const best = useMemo(() => {
    if (!summary?.results.length) return null;
    return summary.results.reduce((a, b) => (b.recall5 > a.recall5 ? b : a));
  }, [summary]);

  async function runEval() {
    if (!activeKbId) return;
    setRunning(true);
    setError(null);
    setEvalResult(null);
    setProgress(0);
    try {
      const corpusRes = await fetch("/demo-corpus.json");
      const corpus = (await corpusRes.json()) as DemoCorpus;
      if (!getLoadedEmbedder()) {
        await loadEmbedder({ forceBackend: "auto" });
      }
      const results: { query: string; docs: string[]; e2e: number; queryMs: number; dbMs: number }[] = [];
      for (let i = 0; i < corpus.queries.length; i++) {
        const item = corpus.queries[i];
        const start = performance.now();
        const { vector, ms: queryMs } = await embedQuery(item.q);
        const response = await api.search(activeKbId, {
          query: item.q,
          mode: "hybrid",
          topK: 5,
          embedding: vector,
          embeddingMs: queryMs,
          backend: embedder.backend,
          model: embedder.modelId,
          dtype: embedder.dtype,
        });
        const docs = [...new Set(response.hits.map((h) => h.docId))].slice(0, 5);
        results.push({
          query: item.q,
          docs,
          e2e: performance.now() - start,
          queryMs,
          dbMs: response.meta.dbMs,
        });
        setProgress(Math.round(((i + 1) / corpus.queries.length) * 100));
      }
      setEvalResult(evaluate(corpus, results));
    } catch (err: any) {
      setError(err?.message || String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="page bench-page">
      <header className="page-head">
        <div>
          <span className="eyebrow">BENCH LAB / REPRODUCIBLE</span>
          <h1>
            把性能数字
            <br />
            变成可复核的证据。
          </h1>
          <p>
            同一设备、同一浏览器、同一模型 revision、同一语料与查询集。冷加载与预热分开报告，全部保留原始 JSON。
          </p>
        </div>
        <div className="bench-hero-badge">
          <Gauge size={24} />
          <div>
            <strong>{summary?.corpus.queries || 24}</strong>
            <span>条标注查询</span>
          </div>
        </div>
      </header>

      <div className="bench-context">
        <div>
          <span>DEVICE</span>
          <b>{summary?.environment.device || embedder.adapter || "当前设备"}</b>
        </div>
        <div>
          <span>BROWSER</span>
          <b>{summary?.environment.browser || navigator.userAgent.split(" ").slice(-2).join(" ")}</b>
        </div>
        <div>
          <span>SEEKDB</span>
          <b>{summary?.environment.seekdb || health?.seekdb.version || "1.4.0"}</b>
        </div>
        <div>
          <span>MODEL</span>
          <b>{summary?.environment.model || "bge-small-zh-v1.5"}</b>
        </div>
      </div>

      <section className="panel bench-run">
        <div className="panel-head">
          <div>
            <span className="eyebrow">LIVE EVAL</span>
            <h2>用当前集合跑一遍质量评测</h2>
          </div>
          <button className="btn accent" onClick={runEval} disabled={running || !activeKbId}>
            {running ? <span className="spinner" /> : <Play size={15} />}
            {running ? `评测中 ${progress}%` : "运行 24 条查询"}
          </button>
        </div>
        {!activeKbId ? (
          <div className="bench-empty">
            先到知识库载入示例语料，再回来运行评测。
            <button className="text-btn" onClick={onGoKb}>
              去导入 <ArrowUpRight size={13} />
            </button>
          </div>
        ) : null}
        {error ? <div className="search-error">{error}</div> : null}
        {evalResult ? (
          <div className="eval-result">
            <div className="eval-metrics">
              <div>
                <strong>{pct(evalResult.recall5)}</strong>
                <span>Recall@5</span>
              </div>
              <div>
                <strong>{pct(evalResult.ndcg5)}</strong>
                <span>nDCG@5</span>
              </div>
              <div>
                <strong>{evalResult.mrr.toFixed(3)}</strong>
                <span>MRR</span>
              </div>
              <div>
                <strong>{ms(evalResult.avgE2e)}</strong>
                <span>平均端到端</span>
              </div>
              <div>
                <strong>{ms(evalResult.avgQuery)}</strong>
                <span>查询向量</span>
              </div>
              <div>
                <strong>{ms(evalResult.avgDb)}</strong>
                <span>seekdb</span>
              </div>
            </div>
            <div className="eval-table">
              {evalResult.rows.map((row, i) => (
                <div className="eval-row" key={i}>
                  <span className={`hit-dot ${row.hit ? "ok" : "miss"}`}>{row.hit ? <Check size={10} /> : "×"}</span>
                  <span className="eval-q">{row.q}</span>
                  <span className="mono">{row.hit ? `#${row.rank}` : "miss"}</span>
                  <span className="mono">{ms(row.e2e)}</span>
                </div>
              ))}
            </div>
            <p className="bench-note">
              这组指标反映当前集合、当前模型精度的实际表现；它不会修改索引，也不会写入新的向量。完整的量化对比由{" "}
              <code>npm run bench</code> 生成。
            </p>
          </div>
        ) : null}
      </section>

      <section className="panel bench-table-panel">
        <div className="panel-head">
          <div>
            <span className="eyebrow">BASELINE MATRIX</span>
            <h2>量化 × 后端 × 批大小</h2>
          </div>
          <div className="head-actions">
            {summary ? (
              <a className="btn ghost mini" href="/benchmarks/summary.json" target="_blank" rel="noreferrer">
                <Download size={13} /> 原始 JSON
              </a>
            ) : null}
          </div>
        </div>
        {loading ? (
          <div className="side-empty">读取基准结果…</div>
        ) : summary ? (
          <>
            <div className="bench-table-wrap">
              <table className="bench-table">
                <thead>
                  <tr>
                    <th>配置</th>
                    <th>后端</th>
                    <th>精度</th>
                    <th>batch</th>
                    <th>加载</th>
                    <th>向量化 / 块</th>
                    <th>查询向量</th>
                    <th>seekdb</th>
                    <th>端到端</th>
                    <th>Recall@5</th>
                    <th>nDCG@5</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.results.map((row) => (
                    <tr key={row.label} className={row.label === best?.label ? "best" : ""}>
                      <td>
                        <strong>{row.label}</strong>
                        {row.note ? <small>{row.note}</small> : null}
                      </td>
                      <td>
                        <span className={`table-badge ${row.backend}`}>{row.backend.toUpperCase()}</span>
                      </td>
                      <td>{row.dtype === "q8" ? "INT8" : row.dtype.toUpperCase()}</td>
                      <td>{row.batchSize}</td>
                      <td>{ms(row.loadMs)}</td>
                      <td>{ms(row.embedMsPerChunk)}</td>
                      <td>{ms(row.queryMs)}</td>
                      <td>{ms(row.dbMs)}</td>
                      <td>{ms(row.e2eMs)}</td>
                      <td>
                        <span className="metric-cell">
                          <b>{pct(row.recall5)}</b>
                          <i style={{ width: `${row.recall5 * 100}%` }} />
                        </span>
                      </td>
                      <td>{pct(row.ndcg5)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="bench-foot">
              <span>
                <Sparkles size={13} /> 生成于 {new Date(summary.generatedAt).toLocaleString("zh-CN")}
              </span>
              <span>
                {summary.corpus.documents} 文档 · {summary.corpus.chunks} 块 · {summary.corpus.queries} 查询 · chunk{" "}
                {summary.corpus.chunkSize}/{summary.corpus.overlap}
              </span>
            </div>
          </>
        ) : (
          <div className="bench-empty">
            <div>
              <strong>还没有基准结果</strong>
              <span>运行下面的命令生成 public/benchmarks/summary.json，然后刷新本页。</span>
              <code>npm run bench</code>
            </div>
          </div>
        )}
      </section>

      <section className="bench-method">
        <div>
          <span className="eyebrow">METHOD</span>
          <h3>怎么读这张表</h3>
        </div>
        <ul>
          <li>
            <Activity size={14} /> 加载：从创建 pipeline 到 warmup 完成，包含权重读取与图初始化。
          </li>
          <li>
            <Activity size={14} /> 文档 / 块：批量向量化总耗时除以块数，使用同一批语料。
          </li>
          <li>
            <Activity size={14} /> 端到端：查询向量化 + HTTP + seekdb 检索，不含首次模型加载。
          </li>
          <li>
            <Activity size={14} /> 质量：按 docId 去重后取 Top-5，对比标注相关文档；FTS 与混合模式分别评测。
          </li>
        </ul>
      </section>
    </div>
  );
}
