# 端侧 AI 推理性能优化与 seekdb 混合检索

**STEPKB · 一页技术说明** ｜ 分支 `2026-competition-ecosystem` ｜ 模型 `Xenova/bge-small-zh-v1.5@75c43b0`（MIT）

## 1. 目标与约束

在浏览器内完成文本切分与 512 维 embedding，把向量、原文与元数据写入 seekdb；查询时由 seekdb 在数据库内完成向量与全文的混合检索。约束是：**不调用云端推理、不预计算回放、不把两路结果拿到前端合并**。WebGPU 为优先路径，失败时自动降级到 WASM；两种精度都必须可量化对比。

## 2. 实现

```text
txt / md ──浏览器──> 分段(280字, 48重叠) ──> Transformers.js
                                              ├─ WebGPU / FP16
                                              └─ WASM / INT8 (自动降级)
                                                   │ 512d 向量
                                                   ▼
                         Node/Express + seekdb-js ──> seekdb collection
                         TEXT + VECTOR(512) + HNSW(cosine) + FULLTEXT(IK)
                                                   │
查询 ──浏览器 query embedding──> POST /api/kbs/:id/search
                                                   ▼
                         DBMS_HYBRID_SEARCH.GET_SQL
                         query($contains) + knn + RRF(k=60) ──> Top-K
```

- 每个知识库一个 collection；`kb_registry` / `kb_docs` / `search_logs` 管理集合、文档与检索审计。
- 导入完成后调用 `collection.refresh_index()`，底层执行 `CALL dbms_index_manager.refresh()`；新块随后可见。
- 混合检索由 `collection.hybridSearch()` 生成 `DBMS_HYBRID_SEARCH.GET_SQL`，在 seekdb 内做 RRF：`Σ 1/(60+rank)`；前端只展示数据库返回的顺序与分数。
- 模型文件固定 revision，由 `npm run setup` 下载到同源 `/models`；`allowRemoteModels=false`，推理只在本地 WebGPU/WASM 执行。

## 3. 量化与质量

| 权重 | 文件 | 字节 | 相对 FP32 |
|---|---|---:|---:|
| FP32 | `onnx/model.onnx` | 94,851,877 | 1.00× |
| FP16 | `onnx/model_fp16.onnx` | 47,497,427 | 2.00× |
| INT8 | `onnx/model_quantized.onnx` | 24,010,842 | **3.95×** |

评测集：24 篇中文技术/干扰文档，46 个块，24 条标注查询；按 docId 去重后取 Top-5，比较 Recall@5、nDCG@5、MRR 与相对 FP32 的 Top-5 重合度。语料、分块、查询、topK、seekdb 配置完全一致，只改变模型精度。

| 配置 | Recall@5 | nDCG@5 | MRR | Top-5 重合 |
|---|---:|---:|---:|---:|
| FP32 / WASM | 97.9% | 92.4% | 0.920 | 100% |
| INT8 / WASM | 93.8% | 91.1% | 0.917 | 94.2% |
| FP16 / WebGPU | 97.9% | 92.4% | 0.920 | 97.5% |

INT8 把权重压到约 1/3.95，Recall@5 下降 4.2 个百分点，nDCG@5 仅下降 1.2 个百分点；下降主要来自个别相关文档跌出 Top-5。FP16 在 24 条查询上与 FP32 指标持平。对“质量优先”的默认路径使用 WebGPU/FP16；WASM/INT8 作为兼容与低带宽路径。

## 4. 性能

环境：Windows 11、Edge 156（Chromium 156）、Intel UHD Graphics 630（Gen9）、seekdb `5.7.25-OceanBase seekdb-v1.4.0.0`、Node 24。46 个块，24 条查询各重复 3 轮取中位数；冷加载为 3 次新浏览器上下文的中位数。

| 配置 | 冷加载 | 预热 | 向量化/块 | 查询向量 | seekdb | 端到端 |
|---|---:|---:|---:|---:|---:|---:|
| FP32 / WASM · b8 | 5.71 s | 71 ms | 60.0 ms | 12.4 ms | 9.1 ms | 36.4 ms |
| INT8 / WASM · b8 | 3.18 s | 78 ms | 42.8 ms | 9.2 ms | 9.6 ms | 32.9 ms |
| INT8 / WASM · b1 | 5.39 s | 79 ms | 36.1 ms | 8.6 ms | 9.3 ms | 33.4 ms |
| FP16 / WebGPU · b16 | 4.75 s | 471 ms | 50.2 ms | 24.4 ms | 10.6 ms | 52.6 ms |
| FP16 / WebGPU · b1 | 4.25 s | 478 ms | 52.0 ms | 23.5 ms | 10.1 ms | 51.1 ms |

写入 + 刷新 46 个块约 1.1–1.4 s，其中 `refresh_index` 约 0.42–0.62 s；索引刷新是全局操作，批量导入只做一次。

**结论与取舍**：在这台集成显卡上，FP16/WebGPU 的查询 embedding 比 INT8/WASM 慢约 2.6 倍，文档向量化也略慢；WebGPU 仍是优先路径，但本项目不隐藏“小模型 + 弱 GPU 上 WASM 更快”的事实。WASM/INT8 是低延迟交互的更优选择；独立显卡/更大模型上 WebGPU 的批处理优势更明显。Windows 主机的冷加载存在 3–30 s 的文件缓存/杀软抖动，表中取三次中位数，原始样本保留在 `benchmarks/results/`。

## 5. 复现

```bash
docker compose up -d          # seekdb 1.4.0, 127.0.0.1:2881
npm install
npm run setup                 # 固定 revision 下载 ONNX / tokenizer
npm run dev                   # Vite 5173 + API 8787
npm run verify                # 健康检查、写入、三种检索模式
npm run bench                 # Edge + Playwright，生成 public/benchmarks/summary.json
npm run build && npm start    # 生产构建 + 单端口服务
```

`npm run bench` 需要本机 Edge（`EDGE_PATH` 可覆盖）与已启动的 API/Vite；`BENCH_API`、`BENCH_WEB` 可改地址。原始逐查询结果、模型文件大小与运行环境均随结果输出。

## 6. 限制

- 当前评测是 24 篇、46 块的桌面级小规模数据，不能替代百万级 HNSW 调参结论。
- 质量只评估了混合检索；未加入 reranker，也未评估跨语言/长文档。
- 结果来自单机单浏览器，WebGPU 驱动、集成显卡功耗策略会造成查询延迟波动。
- INT8 有可测量的质量损失；若业务要求与 FP32 完全一致，应使用 FP16 或 FP32。
