"""回答与周报生成。

两种模式：
* 配置了 LLM_API_KEY：调用任意 OpenAI 兼容的 /chat/completions，要求模型只基于
  给定证据作答，并用 [n] 标注出处；
* 没有配置：使用可复现的抽取式模板。它不会编造文献内容，而是从检索到的切片
  里挑选与问题最相关的句子，再叠加 PowerContext 记忆，保证评审在无 API Key
  的环境下也能看到完整闭环。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Sequence

from .embeddings import tokenize

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？；!?;])|\n+")


def _sentences(text: str) -> List[str]:
    return [part.strip(" -\t") for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]


def _overlap(query_tokens: set[str], text: str) -> float:
    tokens = set(tokenize(text))
    if not tokens:
        return 0.0
    return len(query_tokens & tokens) / len(tokens)


def _best_sentences(query: str, text: str, limit: int = 2) -> List[str]:
    query_tokens = set(tokenize(query))
    sentences = _sentences(text)
    if not sentences:
        return []
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (_overlap(query_tokens, item[1]), -item[0]),
        reverse=True,
    )
    picked = [sentence for _, sentence in ranked[:limit]]
    # 按原文顺序返回，读起来更自然。
    picked.sort(key=lambda sentence: text.find(sentence))
    return picked


def _cite(hit: Dict[str, Any]) -> str:
    section = hit.get("section") or ""
    location = f"《{hit['title']}》"
    if section:
        location += f" · {section}"
    location += f" · 片段 {int(hit.get('chunk_index', 0)) + 1}"
    return location


class Generator:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ 问答
    def answer(
        self,
        question: str,
        hits: Sequence[Dict[str, Any]],
        memories: Sequence[Dict[str, Any]],
        prepared: Dict[str, Any] | None,
        *,
        retrieval_meta: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        citations = [
            {
                "index": position + 1,
                "chunk_id": hit["chunk_id"],
                "paper_id": hit["paper_id"],
                "title": hit["title"],
                "section": hit.get("section", ""),
                "chunk_index": hit.get("chunk_index", 0),
                "matched_by": hit.get("matched_by", []),
                "rrf_score": hit.get("rrf_score", 0),
                "label": _cite(hit),
            }
            for position, hit in enumerate(hits)
        ]
        if self.cfg.use_llm and hits:
            try:
                text = self._llm_answer(question, hits, memories, prepared)
                return {
                    "answer": text,
                    "engine": f"llm:{self.cfg.llm_model}",
                    "citations": citations,
                    "memories": list(memories),
                    "retrieval": retrieval_meta or {},
                }
            except Exception as exc:  # noqa: BLE001 - 失败时退回抽取式，不阻塞用户
                fallback = self._extractive_answer(question, hits, memories)
                fallback["warning"] = f"LLM 调用失败，已退回抽取式回答：{exc}"
                fallback["citations"] = citations
                fallback["memories"] = list(memories)
                fallback["retrieval"] = retrieval_meta or {}
                return fallback

        result = self._extractive_answer(question, hits, memories)
        result["citations"] = citations
        result["memories"] = list(memories)
        result["retrieval"] = retrieval_meta or {}
        return result

    def _extractive_answer(
        self, question: str, hits: Sequence[Dict[str, Any]], memories: Sequence[Dict[str, Any]]
    ) -> Dict[str, Any]:
        if not hits:
            return {
                "engine": "extractive",
                "answer": (
                    f"在你的文献库里没有检索到与「{question}」足够相关的内容。\n\n"
                    "可以试试：导入更多笔记、换一个更具体的关键词，或在筛选条件里放宽年份。"
                ),
            }

        lines: List[str] = []
        papers: List[str] = []
        for hit in hits:
            if hit["title"] not in papers:
                papers.append(hit["title"])
        lines.append(
            f"**结论**：与「{question}」最相关的是 {len(papers)} 篇材料——"
            + "、".join(f"《{title}》" for title in papers[:4])
            + "。下面先给出可核对的原文依据，再给出结合你研究记忆的解读。"
        )
        lines.append("")
        lines.append("**关键依据**")
        for position, hit in enumerate(hits[:4], start=1):
            sentences = _best_sentences(question, hit["content"], limit=2)
            excerpt = "".join(sentences)[:260] if sentences else hit["content"][:200]
            lines.append(f"{position}. [{position}] {_cite(hit)}：{excerpt}")

        if memories:
            lines.append("")
            lines.append("**结合你的研究记忆**")
            for memory in memories[:4]:
                lines.append(f"- {memory.get('text', '').strip()}")

        lines.append("")
        lines.append("**建议下一步**")
        suggestions = self._suggest(question, hits, memories)
        lines.extend(f"- {item}" for item in suggestions)
        return {"engine": "extractive", "answer": "\n".join(lines)}

    def _suggest(
        self, question: str, hits: Sequence[Dict[str, Any]], memories: Sequence[Dict[str, Any]]
    ) -> List[str]:
        suggestions = []
        if hits:
            first = hits[0]
            suggestions.append(
                f"打开 {_cite(first)} 核对上下文，确认结论是否受实验设置限制。"
            )
        if memories:
            suggestions.append("对照「研究记忆」里的约束检查方案是否可落地，必要时更新记忆。")
        else:
            suggestions.append("把研究方向、表达偏好或实验约束写入研究记忆，后续回答会自动带上。")
        suggestions.append("把这次检索的关键结论沉淀到周报，避免组会前再从零整理。")
        return suggestions[:3]

    def _llm_answer(
        self,
        question: str,
        hits: Sequence[Dict[str, Any]],
        memories: Sequence[Dict[str, Any]],
        prepared: Dict[str, Any] | None,
    ) -> str:
        evidence = []
        for position, hit in enumerate(hits[:6], start=1):
            evidence.append(
                f"[{position}] 出处：{_cite(hit)}\n原文：{hit['content'][:900]}"
            )
        memory_text = "\n".join(
            f"- {item.get('text', '')}" for item in memories[:8]
        ) or "（暂无）"
        prepared_text = (prepared or {}).get("content") or "（无）"
        system = (
            "你是科研阅读助手。只依据用户提供的文献证据回答，禁止编造。"
            "每个事实性论断后用 [n] 标注对应证据编号；没有证据时明确说“文献中未找到”。"
            "用中文回答，先给结论，再给依据，最后给可执行的下一步。"
        )
        user = (
            f"问题：{question}\n\n"
            f"文献证据：\n" + "\n\n".join(evidence) + "\n\n"
            f"用户研究记忆（可能包含偏好、约束与进展）：\n{memory_text}\n\n"
            f"PowerContext 准备的历史上下文：\n{prepared_text}"
        )
        body = json.dumps(
            {
                "model": self.cfg.llm_model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.cfg.llm_base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.llm_api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.cfg.llm_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"].strip()

    # ------------------------------------------------------------------ 周报
    def weekly_report(
        self,
        *,
        days: int,
        activity: Sequence[Dict[str, Any]],
        hits: Sequence[Dict[str, Any]],
        memories: Sequence[Dict[str, Any]],
        stats: Dict[str, Any],
        prepared: Dict[str, Any] | None,
    ) -> Dict[str, Any]:
        if self.cfg.use_llm and (hits or activity):
            try:
                text = self._llm_weekly(days, activity, hits, memories, stats, prepared)
                return {"engine": f"llm:{self.cfg.llm_model}", "markdown": text}
            except Exception as exc:  # noqa: BLE001
                result = self._template_weekly(days, activity, hits, memories, stats)
                result["warning"] = f"LLM 调用失败，已退回模板周报：{exc}"
                return result
        return self._template_weekly(days, activity, hits, memories, stats)

    def _template_weekly(
        self,
        days: int,
        activity: Sequence[Dict[str, Any]],
        hits: Sequence[Dict[str, Any]],
        memories: Sequence[Dict[str, Any]],
        stats: Dict[str, Any],
    ) -> Dict[str, Any]:
        lines: List[str] = []
        lines.append(f"# 科研周报（近 {days} 天）")
        lines.append("")
        lines.append("## 一、本周概览")
        lines.append(
            f"- 文献库：{stats.get('papers', 0)} 篇材料 / {stats.get('chunks', 0)} 个切片；"
            f"累计检索 {stats.get('searches', 0)} 次。"
        )
        questions = [item for item in activity if item.get("kind") == "ask"]
        reads = [item for item in activity if item.get("kind") == "read"]
        lines.append(
            f"- 本周提问 {len(questions)} 次，打开文献 {len(reads)} 次，"
            f"记录行为 {len(activity)} 条。"
        )
        lines.append("")
        lines.append("## 二、阅读与检索")
        if questions:
            for item in questions[:5]:
                lines.append(f"- {item.get('title') or item.get('detail', '')[:80]}")
        else:
            lines.append("- 本周还没有问答记录，可以从「检索问答」开始。")
        lines.append("")
        lines.append("## 三、关键发现（带出处）")
        if hits:
            for position, hit in enumerate(hits[:4], start=1):
                sentences = _best_sentences(hit.get("query", ""), hit["content"], limit=2)
                excerpt = "".join(sentences)[:220] if sentences else hit["content"][:180]
                lines.append(f"{position}. [{position}] {_cite(hit)}：{excerpt}")
        else:
            lines.append("- 本周没有可引用的检索结果。")
        lines.append("")
        lines.append("## 四、进展与阻塞（来自研究记忆）")
        if memories:
            for memory in memories[:6]:
                lines.append(f"- {memory.get('text', '').strip()}")
        else:
            lines.append("- 尚未记录进展。可以在「研究记忆」中写入本周结论与卡点。")
        lines.append("")
        lines.append("## 五、下周计划")
        plan = self._plan_from_memories(memories)
        lines.extend(f"- {item}" for item in plan)
        lines.append("")
        if hits:
            lines.append("## 六、证据清单")
            for position, hit in enumerate(hits[:4], start=1):
                lines.append(
                    f"- [{position}] {_cite(hit)}（chunk_id: {hit['chunk_id']}，"
                    f"命中通道：{'+'.join(hit.get('matched_by', [])) or '—'}）"
                )
        return {"engine": "template", "markdown": "\n".join(lines)}

    def _plan_from_memories(self, memories: Sequence[Dict[str, Any]]) -> List[str]:
        plan = []
        for memory in memories:
            text = memory.get("text", "")
            if any(keyword in text for keyword in ("下周", "计划", "待办", "准备")):
                plan.append(text.strip())
        if not plan:
            plan = [
                "完成本周未收尾实验的消融对比，并把结论写入研究记忆。",
                "围绕高频问题补充 2–3 篇文献，更新检索库。",
                "组会前用本页生成一版周报，补充导师反馈后再归档。",
            ]
        return plan[:3]

    def _llm_weekly(
        self,
        days: int,
        activity: Sequence[Dict[str, Any]],
        hits: Sequence[Dict[str, Any]],
        memories: Sequence[Dict[str, Any]],
        stats: Dict[str, Any],
        prepared: Dict[str, Any] | None,
    ) -> str:
        evidence = "\n".join(
            f"[{position}] {_cite(hit)}：{hit['content'][:500]}"
            for position, hit in enumerate(hits[:6], start=1)
        )
        history = "\n".join(
            f"- {item.get('kind')}｜{item.get('title')}｜{item.get('detail', '')[:120]}"
            for item in activity[:15]
        )
        memory_text = "\n".join(f"- {item.get('text', '')}" for item in memories[:10])
        system = (
            "你是科研组会周报助手。基于用户提供的本周活动、文献证据与研究记忆，"
            "生成结构清晰的中文 Markdown 周报。事实必须来自给定材料，引用证据用 [n] 标注，"
            "不要编造实验结果或数据。"
        )
        user = (
            f"统计：{json.dumps(stats, ensure_ascii=False)}\n"
            f"时间范围：近 {days} 天\n\n本周活动：\n{history or '（无）'}\n\n"
            f"文献证据：\n{evidence or '（无）'}\n\n研究记忆：\n{memory_text or '（无）'}\n\n"
            f"PowerContext 历史上下文：\n{(prepared or {}).get('content') or '（无）'}"
        )
        body = json.dumps(
            {
                "model": self.cfg.llm_model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.cfg.llm_base_url}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.cfg.llm_api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.cfg.llm_timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"].strip()


def summarize_hits_for_report(hits: Iterable[Dict[str, Any]], limit: int = 4) -> List[Dict[str, Any]]:
    return list(hits)[:limit]
