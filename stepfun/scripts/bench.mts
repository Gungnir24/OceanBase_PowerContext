import "dotenv/config";
import fs from "node:fs/promises";
import path from "node:path";
import { chromium, type Page } from "playwright-core";
import { chunkText, sha256 } from "../src/lib/chunk";

type Corpus = {
  documents: { id: string; title: string; source: string; text: string }[];
  queries: { q: string; type: string; relevant: string[] }[];
};

type BenchConfig = {
  label: string;
  backend: "auto" | "wasm";
  dtype: "fp32" | "fp16" | "q8";
  batchSize: number;
  note?: string;
};

type Row = {
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
  importMs: number;
  writeMs: number;
  refreshMs: number;
  recall5: number;
  ndcg5: number;
  mrr: number;
  top5Overlap: number | null;
  note?: string;
};

const PORT = Number(process.env.PORT || 8787);
const API = process.env.BENCH_API || `http://127.0.0.1:${PORT}`;
const WEB = process.env.BENCH_WEB || "http://127.0.0.1:5173";
const EDGE =
  process.env.EDGE_PATH ||
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const CONFIGS: BenchConfig[] = [
  { label: "FP32 / WASM · b8", backend: "wasm", dtype: "fp32", batchSize: 8, note: "质量基线" },
  { label: "INT8 / WASM · b8", backend: "wasm", dtype: "q8", batchSize: 8, note: "推荐默认" },
  { label: "INT8 / WASM · b1", backend: "wasm", dtype: "q8", batchSize: 1, note: "逐条写入" },
  { label: "FP16 / WebGPU · b16", backend: "auto", dtype: "fp16", batchSize: 16, note: "优先路径" },
  { label: "FP16 / WebGPU · b1", backend: "auto", dtype: "fp16", batchSize: 1, note: "单条查询" },
];

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function mean(values: number[]): number {
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0;
}

async function api<T>(
  route: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(`${API}${route}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init.headers || {}) },
  });
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { error: text };
  }
  if (!res.ok) throw new Error(`${res.status} ${route}: ${text.slice(0, 500)}`);
  return body as T;
}

function dcg(gains: number[]): number {
  return gains.reduce((sum, rel, i) => sum + (Math.pow(2, rel) - 1) / Math.log2(i + 2), 0);
}

async function main() {
  const corpus = JSON.parse(
    await fs.readFile(path.resolve("public/demo-corpus.json"), "utf8"),
  ) as Corpus;
  const prepared: {
    doc: { id: string; title: string; source: string; charCount: number; contentSha: string };
    chunks: { text: string; chunkIndex: number }[];
  }[] = [];
  for (const doc of corpus.documents) {
    const chunks = chunkText(doc.text, { target: 280, overlap: 48 });
    prepared.push({
      doc: {
        id: doc.id,
        title: doc.title,
        source: doc.source,
        charCount: doc.text.length,
        contentSha: await sha256(doc.text),
      },
      chunks: chunks.map((text, chunkIndex) => ({ text, chunkIndex })),
    });
  }
  const totalChunks = prepared.reduce((sum, p) => sum + p.chunks.length, 0);
  console.log(
    `corpus: ${prepared.length} docs, ${totalChunks} chunks, ${corpus.queries.length} queries`,
  );

  const health = await api<{ seekdb: { version: string } }>("/api/health");
  const browser = await chromium.launch({
    executablePath: EDGE,
    headless: process.env.BENCH_HEADLESS !== "0",
    args: [
      "--enable-unsafe-webgpu",
      "--enable-features=Vulkan",
      "--use-angle=d3d11",
      "--disable-gpu-sandbox",
    ],
  });

  const rows: Row[] = [];
  const raw: any = { configs: [] };
  let baselineTop5: string[][] | null = null;
  let device = "";
  let userAgent = "";

  const loadOnce = async (config: BenchConfig) => {
    const loadContext = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 1,
    });
    const loadPage: Page = await loadContext.newPage();
    loadPage.on("pageerror", (err) => console.error("[load pageerror]", err.message));
    try {
      await loadPage.goto(
        `${WEB}/?bench=1&backend=${config.backend === "wasm" ? "wasm" : "webgpu"}&dtype=${config.dtype}`,
        { waitUntil: "domcontentloaded", timeout: 120000 },
      );
      await loadPage.waitForFunction(() => Boolean((window as any).__stepkbBench), null, {
        timeout: 30000,
      });
      return await loadPage.evaluate(
        async ({ backend, dtype }) => {
          const bench = (window as any).__stepkbBench;
          const ok = await bench.loadModel(backend, dtype);
          if (!ok) throw new Error("model load failed");
          return bench.getState();
        },
        { backend: config.backend, dtype: config.dtype },
      );
    } finally {
      await loadContext.close();
    }
  };

  try {
    for (const config of CONFIGS) {
      console.log(`\n=== ${config.label} ===`);
      const loadSamples: number[] = [];
      const warmSamples: number[] = [];
      for (let i = 0; i < 3; i++) {
        const sample = await loadOnce(config);
        if (!sample) throw new Error("no embedder state");
        loadSamples.push(sample.loadMs);
        warmSamples.push(sample.warmupMs);
        device = sample.adapter || device;
        console.log(
          `  cold load ${i + 1}/3: ${sample.loadMs.toFixed(0)} ms (warmup ${sample.warmupMs.toFixed(0)} ms)`,
        );
      }
      const loadMs = median(loadSamples);
      const warmupMs = median(warmSamples);
      console.log(`  median cold load: ${loadMs.toFixed(0)} ms · warmup ${warmupMs.toFixed(0)} ms`);

      const created = await api<{ kb: { id: string; name: string } }>("/api/kbs", {
        method: "POST",
        body: JSON.stringify({ name: `bench-${Date.now()}-${config.label}`, description: "temporary benchmark KB" }),
      });
      const kbId = created.kb.id;
      const context = await browser.newContext({
        viewport: { width: 1440, height: 900 },
        deviceScaleFactor: 1,
      });
      const page: Page = await context.newPage();
      page.on("pageerror", (err) => console.error("[pageerror]", err.message));
      try {
        await page.goto(
          `${WEB}/?bench=1&kb=${kbId}&backend=${config.backend === "wasm" ? "wasm" : "webgpu"}&dtype=${config.dtype}`,
          { waitUntil: "domcontentloaded", timeout: 120000 },
        );
        await page.waitForFunction(() => Boolean((window as any).__stepkbBench), null, { timeout: 30000 });
        const state = await page.evaluate(
          async ({ backend, dtype }) => {
            const bench = (window as any).__stepkbBench;
            const ok = await bench.loadModel(backend, dtype);
            if (!ok) throw new Error("model load failed");
            return bench.getState();
          },
          { backend: config.backend, dtype: config.dtype },
        );
        if (!state) throw new Error("no embedder state");
        device = state.adapter || device;
        userAgent = await page.evaluate(() => navigator.userAgent);
        console.log(
          `model ${state.backend}/${state.dtype} ${(state.modelBytes / 1024 / 1024).toFixed(2)} MB · median load ${loadMs.toFixed(0)} ms · warmup ${warmupMs.toFixed(0)} ms`,
        );

        const texts = prepared.flatMap((p) => p.chunks.map((c) => c.text));
        const embed = await page.evaluate(
          async ({ values, batchSize }) => (window as any).__stepkbBench.embedTexts(values, batchSize),
          { values: texts, batchSize: config.batchSize },
        );
        console.log(
          `embed ${embed.totalMs.toFixed(0)} ms · ${(embed.totalMs / totalChunks).toFixed(1)} ms/chunk`,
        );

        const importStart = performance.now();
        const writeStart = performance.now();
        let offset = 0;
        for (const item of prepared) {
          const chunks = item.chunks.map((chunk, index) => ({
            id: `${item.doc.id}_${String(index).padStart(2, "0")}`,
            text: chunk.text,
            embedding: embed.vectors[offset + index],
            chunkIndex: index,
          }));
          offset += item.chunks.length;
          await api(`/api/kbs/${kbId}/chunks`, {
            method: "POST",
            body: JSON.stringify({
              doc: item.doc,
              chunks,
              model: { id: "bge-small-zh-v1.5", dtype: config.dtype, backend: state.backend },
            }),
          });
        }
        const writeMs = performance.now() - writeStart;
        const refreshStart = performance.now();
        const refreshed = await api<{ refreshMs: number }>(`/api/kbs/${kbId}/refresh`, {
          method: "POST",
        });
        const refreshMs = performance.now() - refreshStart;
        const importMs = performance.now() - importStart;
        console.log(
          `write+refresh ${importMs.toFixed(0)} ms (write ${writeMs.toFixed(0)} / refresh ${refreshMs.toFixed(0)})`,
        );

        const latency: { queryMs: number[]; dbMs: number[]; e2eMs: number[] } = {
          queryMs: [],
          dbMs: [],
          e2eMs: [],
        };
        const perQuery: { q: string; docs: string[]; top5: string[]; queryMs: number; dbMs: number; e2e: number }[] = [];
        const RUNS = 3;
        for (let run = 0; run < RUNS; run++) {
          for (const query of corpus.queries) {
            const t0 = performance.now();
            const embedded = await page.evaluate(
              async (text) => (window as any).__stepkbBench.embedQuery(text),
              query.q,
            );
            const result = await api<{
              hits: { docId: string }[];
              meta: { dbMs: number };
            }>(`/api/kbs/${kbId}/search`, {
              method: "POST",
              body: JSON.stringify({
                query: query.q,
                mode: "hybrid",
                topK: 5,
                embedding: embedded.vector,
                embeddingMs: embedded.ms,
                backend: state.backend,
                model: "bge-small-zh-v1.5",
                dtype: config.dtype,
              }),
            });
            const e2e = performance.now() - t0;
            const docs = [...new Set(result.hits.map((h) => h.docId))];
            latency.queryMs.push(embedded.ms);
            latency.dbMs.push(result.meta.dbMs);
            latency.e2eMs.push(e2e);
            if (run === 0) {
              perQuery.push({
                q: query.q,
                docs,
                top5: docs.slice(0, 5),
                queryMs: embedded.ms,
                dbMs: result.meta.dbMs,
                e2e,
              });
            }
          }
        }

        let recall = 0;
        let ndcg = 0;
        let mrr = 0;
        perQuery.forEach((item, index) => {
          const relevant = new Set(corpus.queries[index].relevant);
          const top = item.top5;
          const hits = top.filter((id) => relevant.has(id));
          recall += hits.length / relevant.size;
          const gains = top.map((id) => (relevant.has(id) ? 1 : 0));
          const ideal = Array.from({ length: Math.min(5, relevant.size) }, () => 1);
          ndcg += dcg(gains) / Math.max(dcg(ideal), 1e-9);
          const first = top.findIndex((id) => relevant.has(id));
          mrr += first >= 0 ? 1 / (first + 1) : 0;
        });
        const n = corpus.queries.length;
        let overlap = 1;
        if (!baselineTop5) baselineTop5 = perQuery.map((p) => p.top5);
        else {
          overlap =
            perQuery.reduce((sum, item, index) => {
              const base = new Set(baselineTop5![index]);
              const shared = item.top5.filter((id) => base.has(id)).length;
              return sum + shared / Math.max(1, base.size);
            }, 0) / n;
        }

        const row: Row = {
          label: config.label,
          backend: state.backend,
          dtype: config.dtype,
          batchSize: config.batchSize,
          modelBytes: state.modelBytes,
          loadMs: Number(loadMs.toFixed(2)),
          warmupMs: Number(warmupMs.toFixed(2)),
          embedMsPerChunk: Number((embed.totalMs / totalChunks).toFixed(2)),
          queryMs: Number(median(latency.queryMs).toFixed(2)),
          dbMs: Number(median(latency.dbMs).toFixed(2)),
          e2eMs: Number(median(latency.e2eMs).toFixed(2)),
          importMs: Number(importMs.toFixed(2)),
          writeMs: Number(writeMs.toFixed(2)),
          refreshMs: Number(Math.max(refreshMs, refreshed.refreshMs).toFixed(2)),
          recall5: Number((recall / n).toFixed(4)),
          ndcg5: Number((ndcg / n).toFixed(4)),
          mrr: Number((mrr / n).toFixed(4)),
          top5Overlap: Number(overlap.toFixed(4)),
          note: config.note,
        };
        rows.push(row);
        raw.configs.push({ row, perQuery });
        console.log(
          `quality R@5 ${(row.recall5 * 100).toFixed(1)}% · nDCG@5 ${(row.ndcg5 * 100).toFixed(1)}% · query ${row.queryMs} ms · db ${row.dbMs} ms · e2e ${row.e2eMs} ms`,
        );
      } finally {
        await context.close();
        await api(`/api/kbs/${kbId}`, { method: "DELETE" }).catch(() => {});
      }
    }
  } finally {
    await browser.close();
  }

  const generatedAt = new Date().toISOString();
  const summary = {
    generatedAt,
    environment: {
      browser: (userAgent.match(/(Edg|Chrome)\/[\d.]+/) || ["Chromium"])[0],
      userAgent,
      device,
      os: process.platform,
      seekdb: health.seekdb.version,
      model: "bge-small-zh-v1.5",
      revision: "75c43b069aac4d136ba6bc1122f995fedcfd2781",
    },
    corpus: {
      documents: prepared.length,
      chunks: totalChunks,
      queries: corpus.queries.length,
      chunkSize: 280,
      overlap: 48,
    },
    results: rows,
  };

  await fs.mkdir(path.resolve("public/benchmarks"), { recursive: true });
  await fs.mkdir(path.resolve("benchmarks/results"), { recursive: true });
  const stamp = generatedAt.replace(/[:.]/g, "-");
  await fs.writeFile(path.resolve("public/benchmarks/summary.json"), JSON.stringify(summary, null, 2));
  await fs.writeFile(path.resolve(`benchmarks/results/bench-${stamp}.json`), JSON.stringify(raw, null, 2));
  console.log("\n" + JSON.stringify(rows, null, 2));
  console.log(`\nWrote public/benchmarks/summary.json (${rows.length} configs)`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
