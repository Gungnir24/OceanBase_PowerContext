export type SearchMode = "hybrid" | "vector" | "keyword";
export type Backend = "webgpu" | "wasm";
export type Dtype = "fp16" | "q8" | "fp32";

export type Kb = {
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

export type Doc = {
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

export type SearchMeta = {
  mode: SearchMode;
  engine: string;
  searchApi: string;
  rrf: { rankWindowSize: number; rankConstant: number } | null;
  dbMs: number;
  overfetch: number;
  topK: number;
  fulltextQuery: string;
  collection: string;
  dimension: number;
  serverMs: number;
  requestId: string;
};

export type SearchResponse = {
  hits: SearchHit[];
  meta: SearchMeta;
};

export type SearchLog = {
  query: string;
  mode: string;
  topK: number;
  embeddingMs: number;
  dbMs: number;
  totalMs: number;
  backend: string | null;
  dtype: string | null;
  topIds: string[];
  createdAt: number;
};

export type Health = {
  ok: boolean;
  time: number;
  seekdb: {
    version: string;
    host: string;
    port: number;
    database: string;
    collections: number;
  };
  model: ModelManifest | null;
  runtime: { node: string; pid: number };
};

export type ModelManifest = {
  modelId: string;
  displayName: string;
  source: string;
  revision: string;
  license: string;
  dimension: number;
  maxTokens: number;
  files: { file: string; bytes: number; sha256: string }[];
  totals: {
    allBytes: number;
    onnxBytes: number;
    fp32Bytes: number | null;
    fp16Bytes: number | null;
    int8Bytes: number | null;
    int8AltBytes?: number | null;
  };
  variants: {
    fp32: { file: string; dtype: string; bytes: number | null };
    fp16: { file: string; dtype: string; bytes: number | null };
    int8: { file: string; dtype: string; bytes: number | null; method: string };
  };
};

export type DemoDoc = {
  id: string;
  title: string;
  source: string;
  category: string;
  text: string;
};

export type DemoQuery = {
  q: string;
  type: "semantic" | "keyword";
  relevant: string[];
};

export type DemoCorpus = {
  version: number;
  name: string;
  description: string;
  documents: DemoDoc[];
  queries: DemoQuery[];
};

export type ImportStage =
  | "idle"
  | "chunking"
  | "loading"
  | "embedding"
  | "writing"
  | "refreshing"
  | "done"
  | "error";

export type ImportProgress = {
  stage: ImportStage;
  label: string;
  detail?: string;
  done: number;
  total: number;
  percent: number;
  startedAt?: number;
  finishedAt?: number;
  metrics?: Record<string, number>;
};

export type EmbedderState = {
  status: "idle" | "detecting" | "loading" | "ready" | "error";
  backend: Backend;
  dtype: Dtype;
  adapter: string;
  webgpuAvailable: boolean;
  fallbackReason: string | null;
  modelId: string;
  dimension: number;
  loadMs: number;
  warmupMs: number;
  modelBytes: number;
  batchSize: number;
  progress: { percent: number; file: string; status: string } | null;
  error: string | null;
};
