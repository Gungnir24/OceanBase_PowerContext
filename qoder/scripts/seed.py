#!/usr/bin/env python
"""导入 data/samples 下的示例文献；可选写入几条研究记忆。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import Config  # noqa: E402
from app.service import AppService  # noqa: E402

MEMORIES = [
    ("profile", "研究方向：多模态检索增强生成（RAG），重点在中文长文档的混合检索与可追溯引用。"),
    ("preference", "回答偏好：中文、先结论后依据；每条事实必须带文献出处。"),
    ("constraint", "实验约束：单卡 24GB 显存，向量索引优先 HNSW，batch size 不超过 64。"),
    ("status", "本周进展：完成全文检索通道消融，Recall@10 从 0.71 提升到 0.83；下周补向量维度对比。"),
    ("status", "本周进展：RRF 融合的 k 值做了 k=20/60/100 三组对比，k=60 时 Recall@10 最高，已写进实验日志。"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-memory", action="store_true", help="同时写入示例研究记忆")
    args = parser.parse_args()

    cfg = Config.load()
    service = AppService(cfg)
    service.initialize()
    health = service.health()
    if not health["seekdb"]["ok"]:
        print(f"[FAIL] seekdb 未连接：{health['warnings']}")
        return 1

    results = service.import_samples()
    print(f"[OK] 导入 {len(results)} 篇示例材料")
    for item in results:
        print(f"  - {item['title']}（{item['chunks']} 个切片，{item['year'] or '年份未知'}）")

    if args.with_memory:
        if not health["powercontext"]["ok"]:
            print("[WARN] PowerContext 未连接，跳过记忆写入")
        else:
            for kind, text in MEMORIES:
                service.remember(text, kind=kind, reason="示例数据初始化")
            print(f"[OK] 写入 {len(MEMORIES)} 条研究记忆")

    print(f"\nscope_id = {health['scope_id']}")
    print("下一步：python app/server.py，然后打开 http://127.0.0.1:8787")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
