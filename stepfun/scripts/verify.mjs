import "dotenv/config";

const PORT = Number(process.env.PORT || 8787);
const BASE = `http://127.0.0.1:${PORT}/api`;

let failures = 0;
function check(label, condition, detail = "") {
  if (condition) {
    console.log(`  ✓ ${label}${detail ? ` — ${detail}` : ""}`);
  } else {
    failures += 1;
    console.error(`  ✗ ${label}${detail ? ` — ${detail}` : ""}`);
  }
}

async function req(path, init = {}) {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...(init.headers || {}) },
  });
  const text = await res.text();
  let body = null;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    body = text;
  }
  if (!res.ok) throw new Error(`${res.status} ${path}: ${text.slice(0, 400)}`);
  return body;
}

const vector = Array.from({ length: 512 }, (_, i) => Math.sin(i * 0.017));

async function main() {
  console.log("STEPKB verification\n");
  const health = await req("/health");
  check("API 可用", health.ok === true);
  check("seekdb 已连接", /seekdb/.test(health.seekdb?.version || ""), health.seekdb?.version);
  check("模型维度为 512", health.model?.dimension === 512);

  const totals = health.model?.totals;
  check("FP32 权重已记录", totals?.fp32Bytes === 94_851_877, `${totals?.fp32Bytes} B`);
  check("FP16 权重已记录", totals?.fp16Bytes === 47_497_427, `${totals?.fp16Bytes} B`);
  check("INT8 权重已记录", totals?.int8Bytes === 24_010_842, `${totals?.int8Bytes} B`);
  if (totals?.fp32Bytes && totals?.int8Bytes) {
    check(
      "INT8 压缩比 ≥ 3.9×",
      totals.fp32Bytes / totals.int8Bytes >= 3.9,
      `${(totals.fp32Bytes / totals.int8Bytes).toFixed(2)}×`,
    );
  }

  let kb = null;
  try {
    const created = await req("/kbs", {
      method: "POST",
      body: JSON.stringify({ name: `verify-${Date.now()}`, description: "npm run verify" }),
    });
    kb = created.kb;
    check("创建知识库 / collection", Boolean(kb?.id), kb?.id);

    const docs = [
      ["verify_gpu", "WebGPU 自动降级", "WebGPU 不可用时自动降级到 WASM，并在界面显示当前后端。"],
      ["verify_quant", "INT8 量化", "INT8 量化把 90.46MB 权重压缩到 22.90MB，压缩比约 3.95 倍。"],
      ["verify_hybrid", "seekdb 混合检索", "seekdb 在 DBMS_HYBRID_SEARCH 内完成向量与全文的 RRF 融合。"],
    ];
    for (const [id, title, text] of docs) {
      await req(`/kbs/${kb.id}/chunks`, {
        method: "POST",
        body: JSON.stringify({
          doc: { id, title, source: "verify.txt", charCount: text.length, contentSha: "verify" },
          chunks: [{ id: `${id}_0`, text, embedding: vector, chunkIndex: 0 }],
          model: { id: "bge-small-zh-v1.5", dtype: "q8", backend: "wasm" },
        }),
      });
    }
    check("写入 3 个文档 / 3 个块", true);

    const refreshed = await req(`/kbs/${kb.id}/refresh`, { method: "POST" });
    check("refresh_index 完成", refreshed.ok === true, `${refreshed.refreshMs.toFixed(1)} ms`);

    for (const [mode, query] of [
      ["hybrid", "浏览器端模型量化"],
      ["vector", "浏览器端模型量化"],
      ["keyword", "量化 模型体积"],
    ]) {
      const result = await req(`/kbs/${kb.id}/search`, {
        method: "POST",
        body: JSON.stringify({
          query,
          mode,
          topK: 3,
          embedding: mode === "keyword" ? undefined : vector,
          embeddingMs: 1,
          backend: "wasm",
          dtype: "q8",
        }),
      });
      check(
        `${mode} 检索返回结果`,
        result.hits.length > 0,
        `${result.hits.length} 条 · ${result.meta.dbMs} ms`,
      );
      if (mode !== "vector") {
        check(
          `${mode} 由数据库执行`,
          result.meta.searchApi.includes("DBMS_HYBRID_SEARCH"),
          result.meta.searchApi,
        );
      }
    }
  } finally {
    if (kb) {
      await req(`/kbs/${kb.id}`, { method: "DELETE" }).catch(() => {});
      console.log("\n  · 临时知识库已清理");
    }
  }

  console.log(failures ? `\nFAILED: ${failures} check(s)` : "\nAll checks passed.");
  if (failures) process.exitCode = 1;
}

main().catch((error) => {
  console.error("\nVerification failed:", error.message);
  process.exitCode = 1;
});
