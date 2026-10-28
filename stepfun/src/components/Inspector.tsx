import type { EmbedderState, Health, SearchLog, SearchMeta } from "../types";
import { bytes, ms, relTime } from "../lib/format";
import { Activity, Alert, Check, Cpu, Database, Gauge, Refresh, Shield, Sliders, Zap } from "./Icons";

export function Inspector({
  embedder,
  health,
  meta,
  embeddingMs,
  e2eMs,
  logs,
  onReload,
  onForceWasm,
  onOpenNote,
}: {
  embedder: EmbedderState;
  health: Health | null;
  meta: SearchMeta | null;
  embeddingMs: number | null;
  e2eMs: number | null;
  logs: SearchLog[];
  onReload: () => void;
  onForceWasm: () => void;
  onOpenNote: () => void;
}) {
  const manifest = health?.model;
  const compression =
    manifest?.totals.fp32Bytes && manifest?.totals.int8Bytes
      ? manifest.totals.fp32Bytes / manifest.totals.int8Bytes
      : null;
  const ready = embedder.status === "ready";
  return (
    <aside className="inspector">
      <section className="side-card backend-card">
        <div className="side-card-head">
          <span>INFERENCE</span>
          <span className={`status-dot ${ready ? "ok" : embedder.status === "error" ? "bad" : "warm"}`} />
        </div>
        <div className="backend-hero">
          <div className={`backend-glyph ${embedder.backend}`}>
            {embedder.backend === "webgpu" ? <Zap size={22} /> : <Cpu size={22} />}
          </div>
          <div>
            <strong>{embedder.backend === "webgpu" ? "WebGPU" : "WASM"}</strong>
            <span>{embedder.dtype === "q8" ? "INT8 动态量化" : embedder.dtype.toUpperCase()}</span>
          </div>
        </div>
        {embedder.fallbackReason ? (
          <div className="fallback-note">
            <Alert size={13} />
            <span>{embedder.fallbackReason}</span>
          </div>
        ) : null}
        <div className="kv-list">
          <div>
            <span>模型</span>
            <b>{embedder.modelId}</b>
          </div>
          <div>
            <span>适配器</span>
            <b>{embedder.adapter || "—"}</b>
          </div>
          <div>
            <span>权重体积</span>
            <b>{bytes(embedder.modelBytes)}</b>
          </div>
          <div>
            <span>加载 / 预热</span>
            <b>
              {ms(embedder.loadMs)} / {ms(embedder.warmupMs)}
            </b>
          </div>
          <div>
            <span>批大小</span>
            <b>{embedder.batchSize}</b>
          </div>
        </div>
        {embedder.status === "loading" ? (
          <div className="model-progress">
            <div className="progress-track">
              <span style={{ width: `${Math.max(2, embedder.progress?.percent || 0)}%` }} />
            </div>
            <small>
              {embedder.progress?.file || "加载模型"} · {Math.round(embedder.progress?.percent || 0)}%
            </small>
          </div>
        ) : null}
        {embedder.status === "error" ? (
          <div className="error-note">
            <Alert size={14} />
            <span>{embedder.error}</span>
          </div>
        ) : null}
        <div className="side-actions">
          <button className="btn ghost mini" onClick={onReload} disabled={embedder.status === "loading"}>
            <Refresh size={13} /> 重载模型
          </button>
          <button className="btn ghost mini" onClick={onForceWasm} disabled={embedder.status === "loading" || embedder.backend === "wasm"}>
            <Sliders size={13} /> 降级演练
          </button>
        </div>
      </section>

      <section className="side-card">
        <div className="side-card-head">
          <span>LAST QUERY</span>
          <Gauge size={14} />
        </div>
        {meta ? (
          <div className="latency-grid">
            <div className="latency-main">
              <strong>{ms(e2eMs)}</strong>
              <span>端到端</span>
            </div>
            <div className="latency-rows">
              <div>
                <span>查询向量</span>
                <b>{ms(embeddingMs)}</b>
              </div>
              <div>
                <span>seekdb 检索</span>
                <b>{ms(meta.dbMs)}</b>
              </div>
              <div>
                <span>HTTP / 其他</span>
                <b>{ms(Math.max(0, (e2eMs || 0) - (embeddingMs || 0) - meta.dbMs))}</b>
              </div>
            </div>
          </div>
        ) : (
          <div className="side-empty">提交一次查询后显示分阶段延迟</div>
        )}
        {meta ? (
          <div className="query-meta">
            <span>{meta.searchApi}</span>
            {meta.rrf ? <span>RRF k={meta.rrf.rankConstant}</span> : null}
            <span>overfetch {meta.overfetch}</span>
          </div>
        ) : null}
      </section>

      <section className="side-card">
        <div className="side-card-head">
          <span>SEEKDB</span>
          <Database size={14} />
        </div>
        <div className="kv-list">
          <div>
            <span>版本</span>
            <b>seekdb 1.4.0</b>
          </div>
          <div>
            <span>连接</span>
            <b>
              {health?.seekdb.host}:{health?.seekdb.port}
            </b>
          </div>
          <div>
            <span>数据库</span>
            <b>{health?.seekdb.database || "stepfun_kb"}</b>
          </div>
          <div>
            <span>集合</span>
            <b>{meta?.collection || "—"}</b>
          </div>
          <div>
            <span>索引</span>
            <b>HNSW · FULLTEXT(IK)</b>
          </div>
        </div>
        <div className="check-list">
          <span>
            <Check size={12} /> 向量 + 全文在库内融合
          </span>
          <span>
            <Check size={12} /> 新导入内容刷新后可检索
          </span>
        </div>
      </section>

      <section className="side-card">
        <div className="side-card-head">
          <span>QUALITY & PRIVACY</span>
          <Shield size={14} />
        </div>
        <div className="quality-grid">
          <div>
            <strong>{compression ? `${compression.toFixed(2)}×` : "—"}</strong>
            <span>INT8 压缩比</span>
          </div>
          <div>
            <strong>{manifest?.dimension || 512}</strong>
            <span>向量维度</span>
          </div>
          <div>
            <strong>100%</strong>
            <span>本地计算</span>
          </div>
        </div>
        <button className="text-btn note-link" onClick={onOpenNote}>
          查看技术说明与评测方法
          <Activity size={12} />
        </button>
      </section>

      <section className="side-card">
        <div className="side-card-head">
          <span>RECENT QUERIES</span>
          <span className="tiny-count">{logs.length}</span>
        </div>
        <div className="log-list">
          {logs.length ? (
            logs.slice(0, 5).map((log, i) => (
              <div className="log-row" key={`${log.createdAt}-${i}`}>
                <span className={`log-mode ${log.mode}`}>{log.mode === "hybrid" ? "H" : log.mode === "vector" ? "V" : "K"}</span>
                <div>
                  <strong>{log.query}</strong>
                  <small>
                    {ms(log.totalMs)} · {relTime(log.createdAt)}
                  </small>
                </div>
              </div>
            ))
          ) : (
            <div className="side-empty">检索日志会写入 seekdb 的 search_logs 表</div>
          )}
        </div>
      </section>
    </aside>
  );
}
