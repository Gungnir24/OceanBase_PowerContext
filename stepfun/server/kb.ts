import { randomUUID } from "node:crypto";
import {
  FulltextIndexConfig,
  Schema,
  VectorIndexConfig,
  type Collection,
} from "seekdb";
import { db } from "./db.js";

export const DIMENSION = 512;

export type KbRecord = {
  id: string;
  name: string;
  description: string;
  createdAt: number;
  updatedAt: number;
  lastRefreshAt: number | null;
  model: string | null;
  dtype: string | null;
  backend: string | null;
  docCount: number;
  chunkCount: number;
};

export type DocRecord = {
  kbId: string;
  docId: string;
  title: string;
  source: string;
  charCount: number;
  chunkCount: number;
  importedAt: number;
  contentSha: string | null;
  model: string | null;
  dtype: string | null;
};

export type ChunkInput = {
  id: string;
  text: string;
  embedding: number[];
  chunkIndex: number;
};

export type SearchMode = "hybrid" | "vector" | "keyword";

export type SearchHit = {
  id: string;
  docId: string;
  title: string;
  source: string;
  text: string;
  chunkIndex: number;
  score: number;
  rank: number;
  scoreLabel: string;
  higherIsBetter: boolean;
};

const KB_ID = /^k[a-z0-9]{8,31}$/;
const DOC_ID = /^[a-zA-Z0-9_-]{4,64}$/;

export function assertKbId(id: string): string {
  if (!KB_ID.test(id)) throw new Error("Invalid knowledge base id");
  return id;
}

export function collectionName(kbId: string): string {
  return `kb_${assertKbId(kbId)}`;
}

function toKb(row: Record<string, any>): KbRecord {
  return {
    id: String(row.id),
    name: String(row.name),
    description: String(row.description || ""),
    createdAt: Number(row.created_at),
    updatedAt: Number(row.updated_at),
    lastRefreshAt: row.last_refresh_at == null ? null : Number(row.last_refresh_at),
    model: row.model ? String(row.model) : null,
    dtype: row.dtype ? String(row.dtype) : null,
    backend: row.backend ? String(row.backend) : null,
    docCount: Number(row.doc_count || 0),
    chunkCount: Number(row.chunk_count || 0),
  };
}

function toDoc(row: Record<string, any>): DocRecord {
  return {
    kbId: String(row.kb_id),
    docId: String(row.doc_id),
    title: String(row.title),
    source: String(row.source || ""),
    charCount: Number(row.char_count || 0),
    chunkCount: Number(row.chunk_count || 0),
    importedAt: Number(row.imported_at),
    contentSha: row.content_sha ? String(row.content_sha) : null,
    model: row.model ? String(row.model) : null,
    dtype: row.dtype ? String(row.dtype) : null,
  };
}

const KB_SELECT = `
  SELECT r.id, r.name, r.description, r.created_at, r.updated_at, r.last_refresh_at,
         r.model, r.dtype, r.backend,
         COALESCE(d.doc_count, 0) AS doc_count,
         COALESCE(d.chunk_count, 0) AS chunk_count
  FROM kb_registry r
  LEFT JOIN (
    SELECT kb_id, COUNT(*) AS doc_count, COALESCE(SUM(chunk_count), 0) AS chunk_count
    FROM kb_docs GROUP BY kb_id
  ) d ON d.kb_id = r.id
`;

export async function listKbs(): Promise<KbRecord[]> {
  const rows = await db().execute(`${KB_SELECT} ORDER BY r.updated_at DESC`);
  return (rows || []).map(toKb);
}

export async function getKb(kbId: string): Promise<KbRecord | null> {
  const rows = await db().execute(`${KB_SELECT} WHERE r.id = ?`, [assertKbId(kbId)]);
  return rows && rows.length ? toKb(rows[0]) : null;
}

export async function createKb(name: string, description = ""): Promise<KbRecord> {
  const now = Date.now();
  const id = `k${now.toString(36)}${randomUUID().replace(/-/g, "").slice(0, 8)}`;
  const schema = new Schema({
    fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
    vectorIndex: new VectorIndexConfig({
      hnsw: { dimension: DIMENSION, distance: "cosine" },
      embeddingFunction: null,
    }),
  });
  const collection = await db().createCollection({ name: collectionName(id), schema });
  try {
    await db().execute(
      `INSERT INTO kb_registry
        (id, name, description, created_at, updated_at, last_refresh_at, model, dtype, backend)
       VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL)`,
      [id, name.slice(0, 120), description.slice(0, 1000), now, now],
    );
  } catch (error) {
    await db().deleteCollection(collection.name).catch(() => {});
    throw error;
  }
  return {
    id,
    name,
    description,
    createdAt: now,
    updatedAt: now,
    lastRefreshAt: null,
    model: null,
    dtype: null,
    backend: null,
    docCount: 0,
    chunkCount: 0,
  };
}

export async function getCollection(kbId: string): Promise<Collection> {
  const name = collectionName(kbId);
  if (!(await db().hasCollection(name))) {
    throw Object.assign(new Error("Knowledge base not found"), { status: 404 });
  }
  return db().getCollection({ name, embeddingFunction: null });
}

export async function deleteKb(kbId: string): Promise<void> {
  const id = assertKbId(kbId);
  const name = collectionName(id);
  if (await db().hasCollection(name)) {
    await db().deleteCollection(name);
  }
  await db().execute("DELETE FROM kb_docs WHERE kb_id = ?", [id]);
  await db().execute("DELETE FROM search_logs WHERE kb_id = ?", [id]);
  await db().execute("DELETE FROM kb_registry WHERE id = ?", [id]);
}

export async function listDocs(kbId: string): Promise<DocRecord[]> {
  const rows = await db().execute(
    "SELECT * FROM kb_docs WHERE kb_id = ? ORDER BY imported_at DESC",
    [assertKbId(kbId)],
  );
  return (rows || []).map(toDoc);
}

export async function addDocument(
  kbId: string,
  doc: {
    id: string;
    title: string;
    source: string;
    charCount: number;
    contentSha?: string;
    model?: string;
    dtype?: string;
  },
  chunks: ChunkInput[],
): Promise<void> {
  if (!DOC_ID.test(doc.id)) throw new Error("Invalid document id");
  if (!chunks.length) throw new Error("At least one chunk is required");
  if (chunks.length > 200) throw new Error("Too many chunks in one document");
  for (const chunk of chunks) {
    if (!chunk.embedding || chunk.embedding.length !== DIMENSION) {
      throw new Error(`Chunk ${chunk.id} has invalid embedding dimension`);
    }
    if (!chunk.text || chunk.text.length > 12000) {
      throw new Error(`Chunk ${chunk.id} has invalid text`);
    }
  }
  const collection = await getCollection(kbId);
  await collection.delete({ where: { docId: doc.id } });
  const now = Date.now();
  await collection.add({
    ids: chunks.map((c) => c.id),
    documents: chunks.map((c) => c.text),
    embeddings: chunks.map((c) => c.embedding),
    metadatas: chunks.map((c) => ({
      docId: doc.id,
      title: doc.title.slice(0, 200),
      source: doc.source.slice(0, 500),
      chunkIndex: c.chunkIndex,
      charCount: c.text.length,
      importedAt: now,
    })),
  });
  await db().execute(
    `INSERT INTO kb_docs
      (kb_id, doc_id, title, source, char_count, chunk_count, imported_at, content_sha, model, dtype)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
     ON DUPLICATE KEY UPDATE
       title = VALUES(title), source = VALUES(source), char_count = VALUES(char_count),
       chunk_count = VALUES(chunk_count), imported_at = VALUES(imported_at),
       content_sha = VALUES(content_sha), model = VALUES(model), dtype = VALUES(dtype)`,
    [
      assertKbId(kbId),
      doc.id,
      doc.title.slice(0, 200),
      doc.source.slice(0, 500),
      doc.charCount,
      chunks.length,
      now,
      doc.contentSha || null,
      doc.model || null,
      doc.dtype || null,
    ],
  );
  await db().execute(
    "UPDATE kb_registry SET updated_at = ?, model = COALESCE(?, model), dtype = COALESCE(?, dtype) WHERE id = ?",
    [now, doc.model || null, doc.dtype || null, kbId],
  );
}

export async function deleteDocument(kbId: string, docId: string): Promise<void> {
  if (!DOC_ID.test(docId)) throw new Error("Invalid document id");
  const collection = await getCollection(kbId);
  await collection.delete({ where: { docId } });
  await db().execute("DELETE FROM kb_docs WHERE kb_id = ? AND doc_id = ?", [kbId, docId]);
  await db().execute("UPDATE kb_registry SET updated_at = ? WHERE id = ?", [Date.now(), kbId]);
}

export async function refreshKb(kbId: string): Promise<number> {
  const collection = await getCollection(kbId);
  const start = performance.now();
  await collection.refresh_index();
  const ms = performance.now() - start;
  await db().execute("UPDATE kb_registry SET last_refresh_at = ?, updated_at = ? WHERE id = ?", [
    Date.now(),
    Date.now(),
    kbId,
  ]);
  return ms;
}

export function sanitizeFulltext(query: string): string {
  return query
    .replace(/[+\-=&|><!(){}[\]^"~*?:\\/]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 300);
}

function toScore(result: any, index: number): number {
  return Number(result?.distances?.[0]?.[index] ?? 0);
}

export async function searchKb(args: {
  kbId: string;
  mode: SearchMode;
  query: string;
  embedding?: number[];
  topK: number;
  embeddingMs?: number;
  backend?: string;
  model?: string;
  dtype?: string;
}): Promise<{
  hits: SearchHit[];
  meta: Record<string, unknown>;
}> {
  const { kbId, mode, query, embedding, topK } = args;
  if ((mode === "hybrid" || mode === "vector") && (!embedding || embedding.length !== DIMENSION)) {
    throw Object.assign(new Error("A 512-dimension query embedding is required"), { status: 400 });
  }
  const fts = sanitizeFulltext(query);
  if ((mode === "hybrid" || mode === "keyword") && !fts) {
    throw Object.assign(new Error("Query has no searchable keywords"), { status: 400 });
  }
  const collection = await getCollection(kbId);
  const overfetch = Math.min(128, Math.max(topK * 8, 48));
  const started = performance.now();
  let result: any;
  if (mode === "hybrid") {
    result = await collection.hybridSearch({
      query: { whereDocument: { $contains: fts }, nResults: overfetch },
      knn: { queryEmbeddings: [embedding!], nResults: overfetch },
      rank: { rrf: { rankWindowSize: 60, rankConstant: 60 } },
      nResults: overfetch,
      include: ["documents", "metadatas"],
    });
  } else if (mode === "keyword") {
    result = await collection.hybridSearch({
      query: { whereDocument: { $contains: fts }, nResults: overfetch },
      nResults: overfetch,
      include: ["documents", "metadatas"],
    });
  } else {
    result = await collection.query({
      queryEmbeddings: [embedding!],
      nResults: overfetch,
      include: ["documents", "metadatas", "distances"],
    });
  }
  const dbMs = performance.now() - started;
  const ids: string[] = result?.ids?.[0] || [];
  const docs: string[] = result?.documents?.[0] || [];
  const metas: any[] = result?.metadatas?.[0] || [];
  const rawHits: SearchHit[] = ids.map((id, i) => {
    const meta = metas[i] || {};
    return {
      id,
      docId: String(meta.docId || id.split("_")[0]),
      title: String(meta.title || "未命名文档"),
      source: String(meta.source || ""),
      text: String(docs[i] || ""),
      chunkIndex: Number(meta.chunkIndex || 0),
      score: toScore(result, i),
      rank: i + 1,
      scoreLabel: mode === "hybrid" ? "RRF 融合分" : mode === "keyword" ? "BM25 关键词分" : "余弦距离",
      higherIsBetter: mode !== "vector",
    };
  });
  // Present one row per document (best-ranked chunk). Order still comes from seekdb.
  const seenDocs = new Set<string>();
  const hits = rawHits
    .filter((hit) => {
      if (seenDocs.has(hit.docId)) return false;
      seenDocs.add(hit.docId);
      return true;
    })
    .slice(0, topK)
    .map((hit, index) => ({ ...hit, rank: index + 1 }));
  const meta = {
    mode,
    engine: "seekdb",
    searchApi: mode === "vector" ? "VECTOR INDEX / HNSW" : "DBMS_HYBRID_SEARCH.GET_SQL",
    rrf: mode === "hybrid" ? { rankWindowSize: 60, rankConstant: 60 } : null,
    dbMs: Number(dbMs.toFixed(2)),
    overfetch,
    topK,
    fulltextQuery: fts,
    collection: collection.name,
    dimension: DIMENSION,
  };
  await logSearch({
    kbId,
    query,
    mode,
    topK,
    embeddingMs: args.embeddingMs ?? 0,
    dbMs,
    totalMs: (args.embeddingMs ?? 0) + dbMs,
    backend: args.backend,
    model: args.model,
    dtype: args.dtype,
    topIds: hits.map((h) => h.id),
  });
  return { hits, meta };
}

async function logSearch(input: {
  kbId: string;
  query: string;
  mode: string;
  topK: number;
  embeddingMs: number;
  dbMs: number;
  totalMs: number;
  backend?: string;
  model?: string;
  dtype?: string;
  topIds: string[];
}): Promise<void> {
  try {
    await db().execute(
      `INSERT INTO search_logs
        (id, kb_id, query_text, mode, top_k, embedding_ms, db_ms, total_ms, backend, model, dtype, top_ids, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [
        randomUUID().replace(/-/g, ""),
        input.kbId,
        input.query.slice(0, 500),
        input.mode,
        input.topK,
        input.embeddingMs,
        input.dbMs,
        input.totalMs,
        input.backend || null,
        input.model || null,
        input.dtype || null,
        JSON.stringify(input.topIds),
        Date.now(),
      ],
    );
  } catch (error) {
    console.error("[seekdb] failed to write search log", error);
  }
}

export async function recentLogs(kbId: string, limit = 8): Promise<any[]> {
  const rows = await db().execute(
    `SELECT query_text, mode, top_k, embedding_ms, db_ms, total_ms, backend, dtype, top_ids, created_at
     FROM search_logs WHERE kb_id = ? ORDER BY created_at DESC LIMIT ?`,
    [assertKbId(kbId), Math.min(50, Math.max(1, limit))],
  );
  return (rows || []).map((r) => ({
    query: String(r.query_text),
    mode: String(r.mode),
    topK: Number(r.top_k),
    embeddingMs: Number(r.embedding_ms || 0),
    dbMs: Number(r.db_ms || 0),
    totalMs: Number(r.total_ms || 0),
    backend: r.backend ? String(r.backend) : null,
    dtype: r.dtype ? String(r.dtype) : null,
    topIds: (() => {
      try {
        return JSON.parse(String(r.top_ids || "[]"));
      } catch {
        return [];
      }
    })(),
    createdAt: Number(r.created_at),
  }));
}
