import {
  env,
  pipeline,
  type FeatureExtractionPipeline,
} from "@huggingface/transformers";
import type { Backend, Dtype, ModelManifest } from "../types";

export const MODEL_ID = "bge-small-zh-v1.5";
export const MODEL_DIMENSION = 512;
export const QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章：";

export type LoadOptions = {
  forceBackend?: "auto" | Backend;
  forceDtype?: Dtype;
  onProgress?: (progress: { percent: number; file: string; status: string }) => void;
};

export type LoadedEmbedder = {
  backend: Backend;
  dtype: Dtype;
  adapter: string;
  webgpuAvailable: boolean;
  fallbackReason: string | null;
  loadMs: number;
  warmupMs: number;
  modelBytes: number;
  batchSize: number;
  model: FeatureExtractionPipeline;
};

let loaded: LoadedEmbedder | null = null;
let pending: Promise<LoadedEmbedder> | null = null;
let manifest: ModelManifest | null = null;
let envReady = false;

function prepareEnv() {
  if (envReady) return;
  envReady = true;
  env.allowRemoteModels = false;
  env.allowLocalModels = true;
  env.localModelPath = "/models/";
  env.useBrowserCache = true;
  const onnx = env.backends.onnx as any;
  if (onnx?.wasm) {
    onnx.wasm.simd = true;
    onnx.wasm.proxy = false;
    onnx.wasm.numThreads =
      typeof crossOriginIsolated !== "undefined" && crossOriginIsolated
        ? Math.min(4, Math.max(1, navigator.hardwareConcurrency || 4))
        : 1;
  }
  if (onnx?.webgpu) {
    onnx.webgpu.powerPreference = "high-performance";
  }
}

export async function getManifest(): Promise<ModelManifest | null> {
  if (manifest) return manifest;
  try {
    const res = await fetch("/models/manifest.json", { cache: "no-cache" });
    if (res.ok) manifest = (await res.json()) as ModelManifest;
  } catch {
    manifest = null;
  }
  return manifest;
}

export type GpuProbe = {
  available: boolean;
  reason: string | null;
  adapter: any | null;
  label: string;
  f16: boolean;
};

export async function detectWebGpu(): Promise<GpuProbe> {
  const gpu = (navigator as any).gpu;
  if (!gpu) {
    return { available: false, reason: "当前浏览器未暴露 navigator.gpu", adapter: null, label: "—", f16: false };
  }
  try {
    const adapter = await gpu.requestAdapter({ powerPreference: "high-performance" });
    if (!adapter) {
      return { available: false, reason: "requestAdapter() 未返回可用适配器", adapter: null, label: "—", f16: false };
    }
    const info = adapter.info || {};
    const label = [info.vendor, info.architecture].filter(Boolean).join(" / ") || "WebGPU 适配器";
    return {
      available: true,
      reason: null,
      adapter,
      label,
      f16: adapter.features?.has?.("shader-f16") ?? false,
    };
  } catch (error: any) {
    return {
      available: false,
      reason: `WebGPU 适配器申请失败：${error?.message || error}`,
      adapter: null,
      label: "—",
      f16: false,
    };
  }
}

function variantBytes(dtype: Dtype): number {
  if (!manifest) return 0;
  if (dtype === "fp32") return manifest.variants.fp32.bytes || 0;
  if (dtype === "fp16") return manifest.variants.fp16.bytes || 0;
  return manifest.variants.int8.bytes || 0;
}

export async function loadEmbedder(options: LoadOptions = {}): Promise<LoadedEmbedder> {
  prepareEnv();
  const forceBackend = options.forceBackend ?? "auto";
  if (
    loaded &&
    (forceBackend === "auto" || loaded.backend === forceBackend) &&
    (!options.forceDtype || loaded.dtype === options.forceDtype)
  ) {
    return loaded;
  }
  if (pending) return pending;

  pending = (async () => {
    await getManifest();
    if (loaded) {
      try {
        await loaded.model.dispose();
      } catch {
        // ignore dispose errors
      }
      loaded = null;
    }
    const probe = await detectWebGpu();
    const relative: { percent: number; file: string; status: string } = {
      percent: 0,
      file: "初始化运行时…",
      status: "initiate",
    };
    const files = new Map<string, number>();
    const report = (p: any) => {
      if (p?.file) {
        const value = Number(p.progress || 0);
        files.set(p.file, value);
        relative.file = p.file;
        relative.status = p.status || "progress";
        relative.percent = Math.max(...files.values(), 0);
      } else if (p?.status === "ready") {
        relative.percent = 100;
        relative.status = "ready";
      }
      options.onProgress?.({ ...relative });
    };

    let backend: Backend =
      forceBackend === "wasm" ? "wasm" : probe.available ? "webgpu" : "wasm";
    let dtype: Dtype = options.forceDtype || (backend === "webgpu" ? "fp16" : "q8");
    let fallbackReason: string | null = null;
    if (forceBackend === "auto" && !probe.available) {
      fallbackReason = probe.reason || "WebGPU 不可用，已自动降级到 WASM";
    }
    if (forceBackend === "wasm" && probe.available) {
      fallbackReason = "已手动切换到 WASM（用于降级验证）";
    }
    if (forceBackend === "webgpu" && !probe.available) {
      fallbackReason = probe.reason || "WebGPU 不可用，已自动降级到 WASM";
      backend = "wasm";
      dtype = "q8";
    }
    if (backend === "webgpu" && dtype === "fp16" && !probe.f16) {
      fallbackReason = "WebGPU 适配器不支持 shader-f16，已自动降级到 WASM";
      backend = "wasm";
      dtype = "q8";
    }

    const started = performance.now();
    let lastError: any = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        report({ status: "progress", file: `加载 ${MODEL_ID} / ${dtype}`, progress: 0 });
        const model = (await (pipeline as any)("feature-extraction", MODEL_ID, {
          device: backend,
          dtype,
          progress_callback: report,
        })) as FeatureExtractionPipeline;
        const warmStart = performance.now();
        await model([QUERY_PREFIX + "预热"], { pooling: "cls", normalize: true });
        const warmupMs = performance.now() - warmStart;
        const loadMs = performance.now() - started;
        loaded = {
          backend,
          dtype,
          adapter: probe.label,
          webgpuAvailable: probe.available,
          fallbackReason,
          loadMs,
          warmupMs,
          modelBytes: variantBytes(dtype),
          batchSize: backend === "webgpu" ? 16 : 8,
          model,
        };
        report({ status: "ready", file: "模型就绪", progress: 100 });
        return loaded;
      } catch (error: any) {
        lastError = error;
        if (backend === "webgpu" && forceBackend !== "wasm") {
          fallbackReason = `WebGPU 初始化失败，已自动降级到 WASM：${error?.message || error}`;
          backend = "wasm";
          dtype = "q8";
          files.clear();
          report({ status: "progress", file: "WebGPU 失败，切换 WASM…", progress: 0 });
          continue;
        }
        break;
      }
    }
    throw new Error(
      `模型加载失败：${lastError?.message || lastError || "unknown error"}。请先运行 npm run setup。`,
    );
  })();

  try {
    return await pending;
  } finally {
    pending = null;
  }
}

export async function disposeEmbedder(): Promise<void> {
  if (loaded) {
    try {
      await loaded.model.dispose();
    } catch {
      // ignore
    }
    loaded = null;
  }
}

export function getLoadedEmbedder(): LoadedEmbedder | null {
  return loaded;
}

export async function embedTexts(
  texts: string[],
  onProgress?: (done: number, total: number, batchMs: number) => void,
  batchSizeOverride?: number,
): Promise<{ vectors: number[][]; totalMs: number; batches: number[] }> {
  if (!loaded) throw new Error("模型尚未加载");
  const batchSize = Math.max(1, batchSizeOverride || loaded.batchSize);
  const vectors: number[][] = [];
  const batches: number[] = [];
  const started = performance.now();
  for (let i = 0; i < texts.length; i += batchSize) {
    const batch = texts.slice(i, i + batchSize);
    const t0 = performance.now();
    const tensor = (await loaded.model(batch, {
      pooling: "cls",
      normalize: true,
    })) as any;
    const batchVectors = tensor.tolist() as number[][];
    vectors.push(...batchVectors);
    const batchMs = performance.now() - t0;
    batches.push(Number(batchMs.toFixed(2)));
    onProgress?.(Math.min(i + batch.length, texts.length), texts.length, batchMs);
  }
  return { vectors, totalMs: performance.now() - started, batches };
}

export async function embedQuery(query: string): Promise<{ vector: number[]; ms: number }> {
  if (!loaded) throw new Error("模型尚未加载");
  const t0 = performance.now();
  const tensor = (await loaded.model([QUERY_PREFIX + query], {
    pooling: "cls",
    normalize: true,
  })) as any;
  const vector = (tensor.tolist() as number[][])[0];
  return { vector, ms: performance.now() - t0 };
}
