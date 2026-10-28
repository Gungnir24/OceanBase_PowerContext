import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
import type {
  DemoCorpus,
  Doc,
  EmbedderState,
  Health,
  ImportProgress,
  Kb,
  SearchHit,
  SearchLog,
  SearchMeta,
  SearchMode,
  Dtype,
} from "./types";
import { chunkId, chunkText, sha256 } from "./lib/chunk";
import { cleanTitle, ms, relTime } from "./lib/format";
import {
  detectWebGpu,
  disposeEmbedder,
  embedQuery,
  embedTexts,
  getLoadedEmbedder,
  loadEmbedder,
  MODEL_DIMENSION,
  MODEL_ID,
} from "./lib/embedder";
import { Sidebar, type View } from "./components/Sidebar";
import { ImportPanel } from "./components/ImportPanel";
import { SearchPanel } from "./components/SearchPanel";
import { Inspector } from "./components/Inspector";
import { BenchPage } from "./components/BenchPage";
import { NotePage } from "./components/NotePage";
import { Modal } from "./components/Modal";
import { Toast, type ToastKind, type ToastState } from "./components/Toast";
import { Alert, ArrowUpRight, ChevronRight, Plus, Sparkles, Trash, Zap } from "./components/Icons";

const IDLE_PROGRESS: ImportProgress = {
  stage: "idle",
  label: "等待导入",
  done: 0,
  total: 0,
  percent: 0,
};

const initialEmbedder: EmbedderState = {
  status: "detecting",
  backend: "wasm",
  dtype: "q8",
  adapter: "检测中…",
  webgpuAvailable: false,
  fallbackReason: null,
  modelId: MODEL_ID,
  dimension: MODEL_DIMENSION,
  loadMs: 0,
  warmupMs: 0,
  modelBytes: 0,
  batchSize: 8,
  progress: null,
  error: null,
};

function initialPreference(): "auto" | "wasm" {
  const backend = new URLSearchParams(window.location.search).get("backend");
  if (backend === "wasm") return "wasm";
  return localStorage.getItem("stepkb.backend") === "wasm" ? "wasm" : "auto";
}

function initialDtype(): Dtype | undefined {
  const dtype = new URLSearchParams(window.location.search).get("dtype");
  return dtype === "fp32" || dtype === "fp16" || dtype === "q8" ? dtype : undefined;
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

export default function App() {
  const [view, setView] = useState<View>("kb");
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [booting, setBooting] = useState(true);
  const [kbs, setKbs] = useState<Kb[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeKb, setActiveKb] = useState<Kb | null>(null);
  const [docs, setDocs] = useState<Doc[]>([]);
  const [logs, setLogs] = useState<SearchLog[]>([]);
  const [embedder, setEmbedder] = useState<EmbedderState>(initialEmbedder);
  const [backendPreference, setBackendPreference] = useState<"auto" | "wasm">(initialPreference);
  const [progress, setProgress] = useState<ImportProgress>(IDLE_PROGRESS);
  const [demoLoading, setDemoLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [topK, setTopK] = useState(5);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [meta, setMeta] = useState<SearchMeta | null>(null);
  const [embeddingMs, setEmbeddingMs] = useState<number | null>(null);
  const [e2eMs, setE2eMs] = useState<number | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<ToastState>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [creating, setCreating] = useState(false);

  const booted = useRef(false);
  const loadSeq = useRef(0);

  const notify = useCallback((kind: ToastKind, message: string, detail?: string) => {
    setToast({ id: Date.now(), kind, message, detail });
  }, []);

  const ensureModel = useCallback(
    async (preference: "auto" | "wasm" = backendPreference, dtype?: Dtype): Promise<boolean> => {
      setEmbedder((prev) => ({
        ...prev,
        status: "loading",
        error: null,
        progress: prev.progress || { percent: 0, file: "准备模型…", status: "initiate" },
      }));
      try {
        const loaded = await loadEmbedder({
          forceBackend: preference,
          forceDtype: dtype,
          onProgress: (p) => {
            setEmbedder((prev) => ({ ...prev, status: "loading", progress: p }));
            setProgress((prev) =>
              prev.stage === "loading"
                ? {
                    ...prev,
                    detail: `${p.file} · ${Math.round(p.percent)}%`,
                    percent: Math.min(48, 22 + p.percent * 0.26),
                  }
                : prev,
            );
          },
        });
        setEmbedder({
          status: "ready",
          backend: loaded.backend,
          dtype: loaded.dtype,
          adapter: loaded.adapter,
          webgpuAvailable: loaded.webgpuAvailable,
          fallbackReason: loaded.fallbackReason,
          modelId: MODEL_ID,
          dimension: MODEL_DIMENSION,
          loadMs: loaded.loadMs,
          warmupMs: loaded.warmupMs,
          modelBytes: loaded.modelBytes,
          batchSize: loaded.batchSize,
          progress: { percent: 100, file: "模型就绪", status: "ready" },
          error: null,
        });
        return true;
      } catch (error) {
        const message = errorText(error);
        setEmbedder((prev) => ({ ...prev, status: "error", error: message }));
        notify("error", "本地模型加载失败", message);
        return false;
      }
    },
    [backendPreference, notify],
  );

  useEffect(() => {
    (window as any).__stepkbBench = {
      loadModel: (backend: "auto" | "wasm", dtype?: Dtype) => ensureModel(backend, dtype),
      embedTexts: async (texts: string[], batchSize?: number) => {
        const result = await embedTexts(texts, undefined, batchSize);
        return { vectors: result.vectors, totalMs: result.totalMs, batches: result.batches };
      },
      embedQuery: async (text: string) => embedQuery(text),
      chunkText: (text: string) => chunkText(text),
      getState: () => {
        const current = getLoadedEmbedder();
        return current
          ? {
              backend: current.backend,
              dtype: current.dtype,
              adapter: current.adapter,
              webgpuAvailable: current.webgpuAvailable,
              fallbackReason: current.fallbackReason,
              loadMs: current.loadMs,
              warmupMs: current.warmupMs,
              modelBytes: current.modelBytes,
              batchSize: current.batchSize,
            }
          : null;
      },
    };
  }, [ensureModel]);

  const loadKb = useCallback(async (id: string) => {
    const seq = ++loadSeq.current;
    const detail = await api.getKb(id);
    if (seq !== loadSeq.current) return;
    setActiveId(id);
    setActiveKb(detail.kb);
    setDocs(detail.docs);
    setLogs(detail.logs);
    setResults([]);
    setMeta(null);
    setEmbeddingMs(null);
    setE2eMs(null);
    setSearchError(null);
    localStorage.setItem("stepkb.active", id);
  }, []);

  const reloadKbList = useCallback(async () => {
    const list = await api.listKbs();
    setKbs(list.kbs);
  }, []);

  useEffect(() => {
    document.title = "STEPKB · 端侧 AI 知识库";
  }, []);

  useEffect(() => {
    if (booted.current) return;
    booted.current = true;
    let cancelled = false;
    (async () => {
      try {
        const [h, list] = await Promise.all([api.health(), api.listKbs()]);
        if (cancelled) return;
        setHealth(h);
        setKbs(list.kbs);
        const fromUrl = new URLSearchParams(window.location.search).get("kb");
        const stored = fromUrl || localStorage.getItem("stepkb.active");
        const first = list.kbs.find((k) => k.id === stored) || list.kbs[0] || null;
        if (first) await loadKb(first.id);
      } catch (error) {
        if (!cancelled) setHealthError(errorText(error));
      } finally {
        if (!cancelled) setBooting(false);
      }
      if (cancelled) return;
      void detectWebGpu().then((probe) => {
        setEmbedder((prev) => ({
          ...prev,
          webgpuAvailable: probe.available,
          adapter: probe.label,
          backend: probe.available ? "webgpu" : "wasm",
          dtype: probe.available ? "fp16" : "q8",
          fallbackReason: probe.available ? null : probe.reason,
        }));
      });
      const benchMode = new URLSearchParams(window.location.search).get("bench") === "1";
      if (!benchMode) {
        window.setTimeout(() => {
          if (!cancelled) void ensureModel(initialPreference(), initialDtype());
        }, 550);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ensureModel, loadKb]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        if (view !== "kb") setView("kb");
        window.setTimeout(() => document.getElementById("search-input")?.focus(), 40);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view]);

  const createKb = useCallback(
    async (name: string, description = ""): Promise<Kb | null> => {
      try {
        const created = await api.createKb(name.trim() || "未命名知识库", description.trim());
        setKbs((prev) => [created.kb, ...prev]);
        await loadKb(created.kb.id);
        notify("success", "知识库已创建", `集合 ${created.kb.id} 已就绪`);
        return created.kb;
      } catch (error) {
        notify("error", "创建知识库失败", errorText(error));
        return null;
      }
    },
    [loadKb, notify],
  );

  async function handleCreateSubmit() {
    if (!newName.trim() || creating) return;
    setCreating(true);
    const kb = await createKb(newName, newDesc);
    setCreating(false);
    if (kb) {
      setNewOpen(false);
      setNewName("");
      setNewDesc("");
      setView("kb");
    }
  }

  async function ensureKbForImport(): Promise<Kb | null> {
    if (activeKb) return activeKb;
    return createKb("我的知识库", "在浏览器端向量化，写入 seekdb 的私有知识库");
  }

  async function importDocuments(
    items: { id?: string; title: string; source: string; text: string }[],
    targetKbId?: string,
  ) {
    if (!items.length) return;
    const kbId = targetKbId || activeKb?.id;
    if (!kbId) throw new Error("请先创建知识库");
    const started = performance.now();
    try {
      setResults([]);
      setMeta(null);
      setSearchError(null);
      setProgress({
        stage: "chunking",
        label: "按段落与长度切分文档…",
        done: 0,
        total: items.length,
        percent: 6,
        startedAt: started,
      });

      const prepared: {
        doc: { id: string; title: string; source: string; charCount: number; contentSha: string };
        chunks: { text: string; chunkIndex: number }[];
      }[] = [];
      for (let i = 0; i < items.length; i++) {
        const item = items[i];
        const text = item.text.trim();
        if (!text) continue;
        const hash = await sha256(text);
        const docId = item.id || `d_${hash.slice(0, 16)}`;
        const chunks = chunkText(text);
        if (!chunks.length) continue;
        prepared.push({
          doc: {
            id: docId,
            title: cleanTitle(item.title) || "未命名文档",
            source: item.source,
            charCount: text.length,
            contentSha: hash,
          },
          chunks: chunks.map((value, index) => ({ text: value, chunkIndex: index })),
        });
        setProgress((prev) => ({
          ...prev,
          done: i + 1,
          percent: 6 + ((i + 1) / items.length) * 12,
          label: `切分 ${i + 1}/${items.length}：${items[i].title}`,
        }));
      }
      const totalChunks = prepared.reduce((sum, item) => sum + item.chunks.length, 0);
      if (!totalChunks) throw new Error("没有可导入的文本块");
      const chunkMs = performance.now() - started;

      if (!getLoadedEmbedder()) {
        setProgress({
          stage: "loading",
          label: "加载本地 embedding 模型…",
          detail: "首次加载需要下载 / 读取权重",
          done: 0,
          total: 1,
          percent: 20,
        });
        const ok = await ensureModel();
        if (!ok) throw new Error("模型未就绪，无法向量化");
      }

      const activeEmbedder = getLoadedEmbedder();
      setProgress({
        stage: "embedding",
        label: `浏览器内向量化 ${totalChunks} 个文本块…`,
        detail: `batch ${activeEmbedder?.batchSize ?? embedder.batchSize}`,
        done: 0,
        total: totalChunks,
        percent: 50,
      });
      const embedStart = performance.now();
      const texts = prepared.flatMap((item) => item.chunks.map((chunk) => chunk.text));
      const { vectors, batches } = await embedTexts(texts, (done, total, batchMs) => {
        setProgress((prev) => ({
          ...prev,
          done,
          percent: 50 + (done / total) * 26,
          detail: `batch ${batchMs.toFixed(1)} ms · ${(done / Math.max(0.001, (performance.now() - embedStart) / 1000)).toFixed(1)} 块/秒`,
        }));
      });
      const embedMs = performance.now() - embedStart;

      setProgress({
        stage: "writing",
        label: "写入 seekdb 并登记文档…",
        detail: `${prepared.length} 篇文档`,
        done: 0,
        total: prepared.length,
        percent: 78,
      });
      const writeStart = performance.now();
      let offset = 0;
      for (let i = 0; i < prepared.length; i++) {
        const item = prepared[i];
        const docChunks = item.chunks.map((chunk, index) => ({
          id: chunkId(item.doc.id, index),
          text: chunk.text,
          embedding: vectors[offset + index],
          chunkIndex: index,
        }));
        offset += item.chunks.length;
        await api.addChunks(kbId, {
          doc: item.doc,
          chunks: docChunks,
          model: {
            id: MODEL_ID,
            dtype: activeEmbedder?.dtype ?? embedder.dtype,
            backend: activeEmbedder?.backend ?? embedder.backend,
          },
        });
        setProgress((prev) => ({
          ...prev,
          done: i + 1,
          percent: 78 + ((i + 1) / prepared.length) * 10,
          label: `写入 ${i + 1}/${prepared.length}：${item.doc.title}`,
        }));
      }
      const writeMs = performance.now() - writeStart;

      setProgress({
        stage: "refreshing",
        label: "刷新向量与全文索引…",
        detail: "CALL dbms_index_manager.refresh()",
        done: 0,
        total: 1,
        percent: 92,
      });
      const refreshStart = performance.now();
      const refreshed = await api.refresh(kbId);
      const refreshMs = performance.now() - refreshStart;

      const total = performance.now() - started;
      setProgress({
        stage: "done",
        label: "完成，新增内容已可检索",
        detail: `${prepared.length} 篇文档 · ${totalChunks} 个块`,
        done: totalChunks,
        total: totalChunks,
        percent: 100,
        startedAt: started,
        finishedAt: Date.now(),
        metrics: {
          chunking: chunkMs,
          embedding: embedMs,
          writing: writeMs,
          refreshing: Math.max(refreshMs, refreshed.refreshMs),
          total,
          throughput: totalChunks / Math.max(0.001, embedMs / 1000),
          batches: batches.length,
        },
      });
      await loadKb(kbId);
      await reloadKbList();
      notify(
        "success",
        "导入完成，索引已刷新",
        `${prepared.length} 篇文档 · ${totalChunks} 块 · ${ms(total)}`,
      );
    } catch (error) {
      const message = errorText(error);
      setProgress((prev) => ({
        ...prev,
        stage: "error",
        label: "导入中断",
        detail: message,
      }));
      notify("error", "导入失败", message);
    }
  }

  async function handleFiles(files: File[]) {
    if (!files.length) return;
    const kb = await ensureKbForImport();
    if (!kb) return;
    const items: { title: string; source: string; text: string }[] = [];
    for (const file of files) {
      if (file.size > 2_000_000) {
        notify("error", "文件过大", `${file.name} 超过 2 MB，请拆分后再导入`);
        continue;
      }
      const text = await file.text();
      if (!text.trim()) continue;
      items.push({ title: file.name, source: file.name, text });
    }
    if (items.length) await importDocuments(items, kb.id);
  }

  async function handleDemo() {
    if (demoLoading) return;
    setDemoLoading(true);
    try {
      const res = await fetch("/demo-corpus.json", { cache: "no-cache" });
      if (!res.ok) throw new Error(`示例语料加载失败 (${res.status})`);
      const corpus = (await res.json()) as DemoCorpus;
      const existing = kbs.find((kb) => kb.name === corpus.name && kb.docCount > 0);
      if (existing) {
        await loadKb(existing.id);
        setView("kb");
        notify("info", "示例语料已在库中", `${existing.docCount} 篇文档可直接检索`);
        return;
      }
      let kb = activeKb;
      if (!kb || kb.docCount > 0) {
        kb = await createKb(corpus.name, corpus.description);
      }
      if (!kb) return;
      await importDocuments(
        corpus.documents.map((doc) => ({
          id: doc.id,
          title: doc.title,
          source: doc.source,
          text: doc.text,
        })),
        kb.id,
      );
    } catch (error) {
      notify("error", "载入示例失败", errorText(error));
    } finally {
      setDemoLoading(false);
    }
  }

  async function handlePaste(title: string, text: string) {
    const kb = await ensureKbForImport();
    if (!kb) return;
    await importDocuments([{ title, source: "粘贴文本", text }], kb.id);
  }

  function changeMode(value: SearchMode) {
    setMode(value);
    setResults([]);
    setMeta(null);
    setSearchError(null);
  }

  async function handleSearch() {
    if (!activeKb || !query.trim() || busy) return;
    setBusy(true);
    setSearchError(null);
    try {
      if (mode !== "keyword") {
        const ok = await ensureModel();
        if (!ok) throw new Error("本地模型未就绪，无法生成查询向量");
      }
      const started = performance.now();
      let vector: number[] | undefined;
      let queryMs = 0;
      if (mode !== "keyword") {
        const embedded = await embedQuery(query);
        vector = embedded.vector;
        queryMs = embedded.ms;
      }
      const activeEmbedder = getLoadedEmbedder();
      const response = await api.search(activeKb.id, {
        query,
        mode,
        topK,
        embedding: vector,
        embeddingMs: queryMs,
        backend: activeEmbedder?.backend ?? embedder.backend,
        model: MODEL_ID,
        dtype: activeEmbedder?.dtype ?? embedder.dtype,
      });
      const elapsed = performance.now() - started;
      setResults(response.hits);
      setMeta(response.meta);
      setEmbeddingMs(mode === "keyword" ? 0 : queryMs);
      setE2eMs(elapsed);
      const logRes = await api.logs(activeKb.id, 8);
      setLogs(logRes.logs);
      if (!response.hits.length) notify("info", "没有命中结果", "试试更短的关键词，或先导入更多语料");
    } catch (error) {
      const message = errorText(error);
      setSearchError(message);
      setResults([]);
      setMeta(null);
      notify("error", "检索失败", message);
    } finally {
      setBusy(false);
    }
  }

  async function handleRefresh() {
    if (!activeKb || refreshing) return;
    setRefreshing(true);
    try {
      const result = await api.refresh(activeKb.id);
      await loadKb(activeKb.id);
      await reloadKbList();
      notify("success", "索引已刷新", `refresh_index 完成 · ${ms(result.refreshMs)}`);
    } catch (error) {
      notify("error", "刷新失败", errorText(error));
    } finally {
      setRefreshing(false);
    }
  }

  async function handleDeleteDoc(docId: string) {
    if (!activeKb) return;
    if (!window.confirm("从知识库删除这篇文档及其全部向量块？")) return;
    try {
      await api.deleteDoc(activeKb.id, docId);
      await loadKb(activeKb.id);
      await reloadKbList();
      notify("success", "文档已删除");
    } catch (error) {
      notify("error", "删除文档失败", errorText(error));
    }
  }

  async function handleDeleteKb() {
    if (!activeKb) return;
    if (!window.confirm(`删除知识库「${activeKb.name}」？集合与检索日志都会被移除。`)) return;
    try {
      await api.deleteKb(activeKb.id);
      const rest = kbs.filter((kb) => kb.id !== activeKb.id);
      setKbs(rest);
      setActiveKb(null);
      setActiveId(null);
      setDocs([]);
      setLogs([]);
      setResults([]);
      setMeta(null);
      localStorage.removeItem("stepkb.active");
      if (rest[0]) await loadKb(rest[0].id);
      notify("success", "知识库已删除");
    } catch (error) {
      notify("error", "删除知识库失败", errorText(error));
    }
  }

  async function handleForceWasm() {
    localStorage.setItem("stepkb.backend", "wasm");
    setBackendPreference("wasm");
    await disposeEmbedder();
    await ensureModel("wasm");
    notify("info", "已切换到 WASM", "降级演练：当前查询使用 INT8 / CPU");
  }

  async function handleReloadModel() {
    await disposeEmbedder();
    await ensureModel(backendPreference);
  }

  const hasDocs = docs.length > 0 || (activeKb?.docCount || 0) > 0;
  const docCount = activeKb?.docCount ?? docs.length;
  const chunkCount = activeKb?.chunkCount ?? docs.reduce((sum, doc) => sum + doc.chunkCount, 0);

  return (
    <div className="app-shell">
      <Sidebar
        view={view}
        onView={setView}
        kbs={kbs}
        activeId={activeId}
        onSelect={(id) => void loadKb(id)}
        onNew={() => setNewOpen(true)}
        embedder={embedder}
        health={health}
      />

      <div className="workspace">
        <header className="topbar">
          <div className="topbar-left">
            <div className="mobile-brand">
              <Sparkles size={17} />
              <b>STEPKB</b>
            </div>
            <div className="crumbs">
              <button onClick={() => setView("kb")}>知识库</button>
              <ChevronRight size={13} />
              <strong>{view === "kb" ? activeKb?.name || "未选择集合" : view === "bench" ? "基准实验室" : "技术说明"}</strong>
            </div>
          </div>
          <div className="topbar-actions">
            <span className={`backend-chip ${embedder.status === "ready" ? "ready" : ""}`}>
              <span className="status-dot" />
              {embedder.status === "ready" ? `${embedder.backend === "webgpu" ? "WebGPU" : "WASM"} · ${embedder.dtype === "q8" ? "INT8" : embedder.dtype.toUpperCase()}` : embedder.status === "loading" ? "模型加载中" : embedder.status === "error" ? "模型未就绪" : "检测设备"}
            </span>
            <button className="btn ghost" onClick={() => void handleDemo()} disabled={demoLoading}>
              <Sparkles size={14} />
              {demoLoading ? "载入中…" : "载入示例"}
            </button>
            <button className="btn accent" onClick={() => setNewOpen(true)}>
              <Plus size={15} /> 新建集合
            </button>
            {activeKb ? (
              <button className="icon-btn danger" title="删除当前知识库" onClick={() => void handleDeleteKb()}>
                <Trash size={15} />
              </button>
            ) : null}
          </div>
        </header>

        <nav className="mobile-nav" aria-label="移动端导航">
          <button className={view === "kb" ? "active" : ""} onClick={() => setView("kb")}>
            知识库
          </button>
          <button className={view === "bench" ? "active" : ""} onClick={() => setView("bench")}>
            基准
          </button>
          <button className={view === "note" ? "active" : ""} onClick={() => setView("note")}>
            说明
          </button>
          <select
            aria-label="切换知识库"
            value={activeId || ""}
            onChange={(event) => {
              if (event.target.value) void loadKb(event.target.value);
            }}
          >
            <option value="">{kbs.length ? "切换集合" : "尚无集合"}</option>
            {kbs.map((kb) => (
              <option key={kb.id} value={kb.id}>
                {kb.name}
              </option>
            ))}
          </select>
        </nav>

        <div className="main-scroll">
          {!booting && healthError ? (
            <div className="system-banner">
              <Alert size={16} />
              <div>
                <strong>无法连接本地 API</strong>
                <span>{healthError}。请确认 seekdb 已启动，并运行 npm run dev。</span>
              </div>
            </div>
          ) : null}

          {view === "kb" ? (
            <div className="page kb-page">
              <section className="hero">
                <div className="hero-copy">
                  <span className="eyebrow">EDGE RAG / SEEKDB 1.4</span>
                  <h1>
                    把知识留在设备上，
                    <br />
                    <em>把检索交给 seekdb。</em>
                  </h1>
                  <p>
                    文本在浏览器内切分、向量化；向量、原文与元数据写入 seekdb；查询时由数据库完成向量 + 全文的 RRF
                    混合检索。没有云端推理，也没有前端拼结果。
                  </p>
                  <div className="hero-actions">
                    {!activeKb ? (
                      <button className="btn accent" onClick={() => setNewOpen(true)}>
                        <Plus size={15} /> 创建第一个集合
                      </button>
                    ) : (
                      <button className="btn accent" onClick={() => void handleDemo()} disabled={demoLoading}>
                        <Sparkles size={15} /> {demoLoading ? "载入示例…" : "一键载入示例语料"}
                      </button>
                    )}
                    <button
                      className="btn ghost"
                      onClick={() => {
                        setView("bench");
                      }}
                    >
                      查看性能基线 <ArrowUpRight size={14} />
                    </button>
                  </div>
                </div>
                <div className="hero-stats">
                  <div className="stat-card">
                    <span>DOCUMENTS</span>
                    <strong>{docCount.toString().padStart(2, "0")}</strong>
                    <small>{activeKb?.name || "尚未选择集合"}</small>
                  </div>
                  <div className="stat-card">
                    <span>VECTOR CHUNKS</span>
                    <strong>{chunkCount.toLocaleString("zh-CN")}</strong>
                    <small>512 维 · cosine</small>
                  </div>
                  <div className="stat-card">
                    <span>LAST REFRESH</span>
                    <strong className="stat-time">{activeKb?.lastRefreshAt ? relTime(activeKb.lastRefreshAt) : "—"}</strong>
                    <small>refresh_index()</small>
                  </div>
                  <div className={`stat-card status-stat ${embedder.status}`}>
                    <span>INFERENCE</span>
                    <strong>{embedder.status === "ready" ? (embedder.backend === "webgpu" ? "WebGPU" : "WASM") : embedder.status === "loading" ? "LOADING" : "OFFLINE"}</strong>
                    <small>
                      {embedder.status === "ready" ? `${embedder.dtype === "q8" ? "INT8" : embedder.dtype.toUpperCase()} · ${embedder.adapter}` : embedder.error || "本地模型"}
                    </small>
                  </div>
                </div>
              </section>

              <ImportPanel
                docs={docs}
                progress={progress}
                onFiles={(files) => void handleFiles(files)}
                onDemo={() => void handleDemo()}
                onPasteText={(title, text) => void handlePaste(title, text)}
                onRefresh={() => void handleRefresh()}
                onDeleteDoc={(id) => void handleDeleteDoc(id)}
                modelReady={embedder.status === "ready"}
                onLoadModel={() => void ensureModel()}
                demoLoading={demoLoading}
                refreshing={refreshing}
              />

              <SearchPanel
                query={query}
                onQuery={setQuery}
                mode={mode}
                onMode={changeMode}
                topK={topK}
                onTopK={(value) => {
                  setTopK(value);
                  setResults([]);
                  setMeta(null);
                }}
                onSubmit={() => void handleSearch()}
                busy={busy}
                results={results}
                meta={meta}
                error={searchError}
                hasDocs={hasDocs}
                modelReady={embedder.status === "ready"}
                embeddingMs={embeddingMs}
                e2eMs={e2eMs}
                onExample={setQuery}
              />

              <footer className="page-foot">
                <span>
                  <Zap size={12} /> 本地推理 · seekdb 混合检索 · 可复现评测
                </span>
                <span>模型 {MODEL_ID} · revision 75c43b0 · MIT</span>
              </footer>
            </div>
          ) : view === "bench" ? (
            <BenchPage
              health={health}
              embedder={embedder}
              activeKbId={activeKb?.id || null}
              onGoKb={() => setView("kb")}
            />
          ) : (
            <NotePage onGoKb={() => setView("kb")} />
          )}
        </div>
      </div>

      <Inspector
        embedder={embedder}
        health={health}
        meta={meta}
        embeddingMs={embeddingMs}
        e2eMs={e2eMs}
        logs={logs}
        onReload={() => void handleReloadModel()}
        onForceWasm={() => void handleForceWasm()}
        onOpenNote={() => setView("note")}
      />

      <Modal
        open={newOpen}
        onClose={() => setNewOpen(false)}
        title="新建知识库"
        subtitle="每个知识库对应 seekdb 中一个独立的 collection。"
        footer={
          <>
            <button className="btn ghost" onClick={() => setNewOpen(false)}>
              取消
            </button>
            <button className="btn accent" onClick={() => void handleCreateSubmit()} disabled={!newName.trim() || creating}>
              {creating ? "创建中…" : "创建集合"}
            </button>
          </>
        }
      >
        <label className="field">
          <span>名称</span>
          <input
            autoFocus
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            placeholder="例如：端侧 AI 技术知识库"
            onKeyDown={(e) => {
              if (e.key === "Enter") void handleCreateSubmit();
            }}
          />
        </label>
        <label className="field">
          <span>描述（可选）</span>
          <textarea
            rows={3}
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
            placeholder="这个集合准备放什么？"
          />
        </label>
      </Modal>

      <Toast toast={toast} onClose={() => setToast(null)} />
    </div>
  );
}
