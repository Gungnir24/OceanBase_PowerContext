import express from "express";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { z } from "zod";
import { closeDb, db, initDb, modelManifest, seekdbVersion } from "./db.js";
import {
  addDocument,
  createKb,
  deleteDocument,
  deleteKb,
  getKb,
  listDocs,
  listKbs,
  recentLogs,
  refreshKb,
  searchKb,
} from "./kb.js";
import { IS_PROD, PORT, ROOT, SEEKDB } from "./config.js";

const app = express();
app.disable("x-powered-by");
app.use(express.json({ limit: "16mb" }));

const crossOriginHeaders = (res: express.Response) => {
  res.setHeader("Cross-Origin-Opener-Policy", "same-origin");
  res.setHeader("Cross-Origin-Embedder-Policy", "require-corp");
  res.setHeader("Cross-Origin-Resource-Policy", "same-origin");
  res.setHeader("Origin-Agent-Cluster", "?1");
};
app.use((_req, res, next) => {
  crossOriginHeaders(res);
  next();
});

const ChunkSchema = z.object({
  id: z.string().min(3).max(100),
  text: z.string().min(1).max(12000),
  embedding: z.array(z.number().finite()).length(512),
  chunkIndex: z.number().int().min(0).max(10000),
});

const AddDocSchema = z.object({
  doc: z.object({
    id: z.string().min(3).max(64),
    title: z.string().min(1).max(200),
    source: z.string().max(500).default(""),
    charCount: z.number().int().min(1).max(2_000_000),
    contentSha: z.string().max(64).optional(),
  }),
  chunks: z.array(ChunkSchema).min(1).max(200),
  model: z
    .object({
      id: z.string().max(128).default("bge-small-zh-v1.5"),
      dtype: z.string().max(16).default("q8"),
      backend: z.string().max(16).default("wasm"),
    })
    .optional(),
});

const SearchSchema = z.object({
  query: z.string().min(1).max(500),
  mode: z.enum(["hybrid", "vector", "keyword"]).default("hybrid"),
  topK: z.number().int().min(1).max(20).default(6),
  embedding: z.array(z.number().finite()).length(512).optional(),
  embeddingMs: z.number().min(0).max(600000).optional(),
  backend: z.string().max(16).optional(),
  model: z.string().max(128).optional(),
  dtype: z.string().max(16).optional(),
});

app.get("/api/health", async (_req, res) => {
  const manifest = await modelManifest();
  const collections = await db().listCollections();
  res.json({
    ok: true,
    time: Date.now(),
    seekdb: {
      version: seekdbVersion(),
      host: SEEKDB.host,
      port: SEEKDB.port,
      database: SEEKDB.database,
      collections: collections.length,
    },
    model: manifest
      ? {
          modelId: manifest.modelId,
          revision: manifest.revision,
          dimension: manifest.dimension,
          source: manifest.source,
          license: manifest.license,
          totals: manifest.totals,
          variants: manifest.variants,
        }
      : null,
    runtime: {
      node: process.version,
      pid: process.pid,
    },
  });
});

app.get("/api/kbs", async (_req, res) => {
  res.json({ kbs: await listKbs() });
});

app.post("/api/kbs", async (req, res) => {
  const body = z
    .object({
      name: z.string().trim().min(1).max(80),
      description: z.string().trim().max(500).default(""),
    })
    .parse(req.body);
  const kb = await createKb(body.name, body.description);
  res.status(201).json({ kb });
});

app.get("/api/kbs/:id", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  const [docs, logs] = await Promise.all([listDocs(kb.id), recentLogs(kb.id, 6)]);
  res.json({ kb, docs, logs });
});

app.delete("/api/kbs/:id", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  await deleteKb(kb.id);
  res.json({ ok: true, id: kb.id });
});

app.get("/api/kbs/:id/docs", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  res.json({ docs: await listDocs(kb.id) });
});

app.post("/api/kbs/:id/chunks", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  const body = AddDocSchema.parse(req.body);
  await addDocument(
    kb.id,
    {
      ...body.doc,
      model: body.model?.id,
      dtype: body.model?.dtype,
    },
    body.chunks,
  );
  res.status(201).json({
    ok: true,
    docId: body.doc.id,
    chunks: body.chunks.length,
    collection: `kb_${kb.id}`,
  });
});

app.delete("/api/kbs/:id/docs/:docId", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  await deleteDocument(kb.id, req.params.docId);
  res.json({ ok: true });
});

app.post("/api/kbs/:id/refresh", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  const ms = await refreshKb(kb.id);
  const updated = await getKb(kb.id);
  res.json({
    ok: true,
    refreshMs: Number(ms.toFixed(2)),
    lastRefreshAt: updated?.lastRefreshAt ?? null,
    searchable: true,
  });
});

app.post("/api/kbs/:id/search", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  const body = SearchSchema.parse(req.body);
  const started = performance.now();
  const { hits, meta } = await searchKb({
    kbId: kb.id,
    mode: body.mode,
    query: body.query,
    embedding: body.embedding,
    topK: body.topK,
    embeddingMs: body.embeddingMs,
    backend: body.backend,
    model: body.model,
    dtype: body.dtype,
  });
  res.json({
    hits,
    meta: {
      ...meta,
      serverMs: Number((performance.now() - started).toFixed(2)),
      requestId: randomUUID().slice(0, 8),
    },
  });
});

app.get("/api/kbs/:id/logs", async (req, res) => {
  const kb = await getKb(req.params.id);
  if (!kb) return res.status(404).json({ error: "知识库不存在" });
  const limit = Number(req.query.limit || 8);
  res.json({ logs: await recentLogs(kb.id, limit) });
});

app.use("/api", (_req, res) => {
  res.status(404).json({ error: "API not found" });
});

const dist = path.join(ROOT, "dist");
if (IS_PROD) {
  app.use(
    express.static(dist, {
      setHeaders: (res) => crossOriginHeaders(res),
      maxAge: "1h",
      etag: true,
    }),
  );
  app.use((req, res, next) => {
    if (req.method !== "GET" || !req.accepts("html")) return next();
    res.sendFile(path.join(dist, "index.html"));
  });
}

app.use((err: any, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  if (err instanceof z.ZodError) {
    return res.status(400).json({
      error: "请求参数不合法",
      details: err.issues.map((i) => `${i.path.join(".")}: ${i.message}`),
    });
  }
  const status = Number(err?.status || 500);
  console.error("[api]", err?.stack || err);
  res.status(status).json({ error: String(err?.message || "Internal error") });
});

async function main() {
  console.log(`[seekdb] connecting ${SEEKDB.host}:${SEEKDB.port}/${SEEKDB.database}`);
  await initDb();
  console.log(`[seekdb] ready: ${seekdbVersion()}`);
  app.listen(PORT, "127.0.0.1", () => {
    console.log(`[api] listening on http://127.0.0.1:${PORT}${IS_PROD ? " (production)" : ""}`);
  });
}

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, async () => {
    console.log(`\n[api] ${signal}, closing`);
    await closeDb().catch(() => {});
    process.exit(0);
  });
}

main().catch((error) => {
  console.error("[fatal] failed to start. Is seekdb running? `npm run db:up`");
  console.error(error);
  process.exit(1);
});
