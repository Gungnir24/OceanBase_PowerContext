import type {
  Doc,
  Health,
  Kb,
  SearchLog,
  SearchMode,
  SearchResponse,
} from "./types";

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  details?: string[];
  constructor(message: string, status: number, details?: string[]) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      ...(init.headers || {}),
    },
  });
  const text = await res.text();
  let body: any = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = { error: text };
  }
  if (!res.ok) {
    const message =
      typeof body?.error === "string"
        ? body.error
        : body?.error?.message
          ? String(body.error.message)
          : `请求失败 (${res.status})`;
    throw new ApiError(message, res.status, body?.details);
  }
  return body as T;
}

export const api = {
  health: () => request<Health>("/health"),
  listKbs: () => request<{ kbs: Kb[] }>("/kbs"),
  createKb: (name: string, description = "") =>
    request<{ kb: Kb }>("/kbs", {
      method: "POST",
      body: JSON.stringify({ name, description }),
    }),
  getKb: (id: string) =>
    request<{ kb: Kb; docs: Doc[]; logs: SearchLog[] }>(`/kbs/${id}`),
  deleteKb: (id: string) =>
    request<{ ok: boolean }>(`/kbs/${id}`, { method: "DELETE" }),
  addChunks: (
    id: string,
    payload: {
      doc: {
        id: string;
        title: string;
        source: string;
        charCount: number;
        contentSha?: string;
      };
      chunks: { id: string; text: string; embedding: number[]; chunkIndex: number }[];
      model: { id: string; dtype: string; backend: string };
    },
  ) =>
    request<{ ok: boolean; docId: string; chunks: number; collection: string }>(
      `/kbs/${id}/chunks`,
      { method: "POST", body: JSON.stringify(payload) },
    ),
  deleteDoc: (id: string, docId: string) =>
    request<{ ok: boolean }>(`/kbs/${id}/docs/${docId}`, { method: "DELETE" }),
  refresh: (id: string) =>
    request<{ ok: boolean; refreshMs: number; lastRefreshAt: number; searchable: boolean }>(
      `/kbs/${id}/refresh`,
      { method: "POST" },
    ),
  search: (
    id: string,
    payload: {
      query: string;
      mode: SearchMode;
      topK: number;
      embedding?: number[];
      embeddingMs?: number;
      backend?: string;
      model?: string;
      dtype?: string;
    },
  ) =>
    request<SearchResponse>(`/kbs/${id}/search`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  logs: (id: string, limit = 8) =>
    request<{ logs: SearchLog[] }>(`/kbs/${id}/logs?limit=${limit}`),
};
