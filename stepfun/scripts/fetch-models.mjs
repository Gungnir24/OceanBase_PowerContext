#!/usr/bin/env node
/**
 * Download the public embedding model used by the browser demo.
 *
 * Source: https://huggingface.co/Xenova/bge-small-zh-v1.5
 * Revision is pinned so model bytes, sizes and benchmark numbers are reproducible.
 *
 * Usage:
 *   node scripts/fetch-models.mjs            # fp32 + fp16 + int8 (benchmarks)
 *   node scripts/fetch-models.mjs --minimal  # int8 only (runtime demo)
 */
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const modelId = "bge-small-zh-v1.5";
const repo = "Xenova/bge-small-zh-v1.5";
const revision = "75c43b069aac4d136ba6bc1122f995fedcfd2781";
const outDir = join(root, "public", "models", modelId);

const EXPECTED = {
  "config.json": 716,
  "tokenizer.json": 439125,
  "tokenizer_config.json": 367,
  "special_tokens_map.json": 125,
  "quantize_config.json": 674,
  "onnx/model.onnx": 94851877,
  "onnx/model_fp16.onnx": 47497427,
  "onnx/model_quantized.onnx": 24010842,
  "onnx/model_int8.onnx": 23903394,
};

const minimal = process.argv.includes("--minimal");
const files = minimal
  ? [
      "config.json",
      "tokenizer.json",
      "tokenizer_config.json",
      "special_tokens_map.json",
      "onnx/model_quantized.onnx",
    ]
  : Object.keys(EXPECTED);

async function exists(path) {
  try {
    const s = await stat(path);
    return s.isFile();
  } catch {
    return false;
  }
}

async function sha256(path) {
  const hash = createHash("sha256");
  const data = await readFile(path);
  hash.update(data);
  return hash.digest("hex");
}

async function download(file) {
  const dest = join(outDir, file);
  const expected = EXPECTED[file];
  if (await exists(dest)) {
    const s = await stat(dest);
    if (!expected || s.size === expected) {
      process.stdout.write(`  ✓ ${file} (${s.size.toLocaleString()} B, cached)\n`);
      return { file, bytes: s.size, sha256: await sha256(dest), cached: true };
    }
    process.stdout.write(`  ! ${file} size mismatch, re-downloading\n`);
  }
  await mkdir(dirname(dest), { recursive: true });
  const url = `https://huggingface.co/${repo}/resolve/${revision}/${file}?download=true`;
  const tmp = `${dest}.part`;
  let lastError;
  for (let attempt = 1; attempt <= 4; attempt++) {
    try {
      const res = await fetch(url, {
        redirect: "follow",
        headers: { "user-agent": "stepfun-edge-kb/1.0" },
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const total = Number(res.headers.get("content-length") || expected || 0);
      let received = 0;
      let lastPrint = 0;
      await mkdir(dirname(tmp), { recursive: true });
      const out = createWriteStream(tmp);
      for await (const chunk of res.body) {
        received += chunk.length;
        if (!out.write(chunk)) {
          await new Promise((r) => out.once("drain", r));
        }
        const now = Date.now();
        if (total && now - lastPrint > 400) {
          lastPrint = now;
          const pct = ((received / total) * 100).toFixed(1);
          process.stdout.write(`\r  ↓ ${file} ${pct}% (${(received / 1048576).toFixed(1)} MiB)`);
        }
      }
      await new Promise((r) => out.end(r));
      if (expected && received !== expected) {
        throw new Error(`size mismatch: got ${received}, expected ${expected}`);
      }
      await rename(tmp, dest);
      process.stdout.write(`\r  ✓ ${file} (${(received / 1048576).toFixed(2)} MiB)          \n`);
      return { file, bytes: received, sha256: await sha256(dest), cached: false };
    } catch (err) {
      lastError = err;
      await rm(tmp, { force: true });
      if (attempt < 4) {
        const wait = attempt * 1200;
        process.stdout.write(`  retry ${attempt}/3 in ${wait}ms: ${err.message}\n`);
        await new Promise((r) => setTimeout(r, wait));
      }
    }
  }
  throw new Error(`Failed to download ${file}: ${lastError?.message || lastError}`);
}

console.log(`\nModel: ${repo} @ ${revision.slice(0, 12)}`);
console.log(`Target: ${outDir}\n`);
await mkdir(outDir, { recursive: true });
const manifestFiles = [];
for (const file of files) {
  manifestFiles.push(await download(file));
}

const byName = Object.fromEntries(manifestFiles.map((f) => [f.file, f]));
const totalBytes = manifestFiles.reduce((a, f) => a + f.bytes, 0);
const onnx = manifestFiles.filter((f) => f.file.startsWith("onnx/"));
const manifest = {
  modelId,
  displayName: "BAAI/bge-small-zh-v1.5 (Xenova ONNX)",
  source: `https://huggingface.co/${repo}`,
  revision,
  license: "MIT",
  dimension: 512,
  maxTokens: 512,
  files: manifestFiles,
  totals: {
    allBytes: totalBytes,
    onnxBytes: onnx.reduce((a, f) => a + f.bytes, 0),
    fp32Bytes: byName["onnx/model.onnx"]?.bytes ?? null,
    fp16Bytes: byName["onnx/model_fp16.onnx"]?.bytes ?? null,
    int8Bytes: byName["onnx/model_quantized.onnx"]?.bytes ?? null,
    int8AltBytes: byName["onnx/model_int8.onnx"]?.bytes ?? null,
  },
  variants: {
    fp32: { file: "onnx/model.onnx", dtype: "fp32", bytes: byName["onnx/model.onnx"]?.bytes ?? null },
    fp16: { file: "onnx/model_fp16.onnx", dtype: "fp16", bytes: byName["onnx/model_fp16.onnx"]?.bytes ?? null },
    int8: {
      file: "onnx/model_quantized.onnx",
      dtype: "q8",
      bytes: byName["onnx/model_quantized.onnx"]?.bytes ?? null,
      method: "ONNX Runtime dynamic INT8 weight quantization (Transformers.js q8)",
    },
  },
  generatedAt: new Date().toISOString(),
};
await writeFile(join(root, "public", "models", "manifest.json"), JSON.stringify(manifest, null, 2));
console.log(`\nWrote public/models/manifest.json (${(totalBytes / 1048576).toFixed(1)} MiB total)\n`);
