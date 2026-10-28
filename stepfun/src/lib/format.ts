export function bytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(2)} MB`;
}

export function ms(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  if (value < 1) return `${(value * 1000).toFixed(0)} μs`;
  if (value < 1000) return `${value.toFixed(digits)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function relTime(timestamp: number | null | undefined): string {
  if (!timestamp) return "—";
  const diff = Date.now() - timestamp;
  if (diff < 15_000) return "刚刚";
  if (diff < 60_000) return `${Math.round(diff / 1000)} 秒前`;
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)} 小时前`;
  return new Date(timestamp).toLocaleDateString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
  });
}

export function plural(n: number, unit: string): string {
  return `${n.toLocaleString("zh-CN")} ${unit}`;
}

export function scoreDisplay(hit: { score: number; higherIsBetter: boolean }): string {
  if (!Number.isFinite(hit.score)) return "—";
  return hit.score.toFixed(hit.higherIsBetter ? 4 : 5);
}

export function queryTokens(query: string): string[] {
  const matches = query.match(/[\u4e00-\u9fa5]{2,}|[A-Za-z0-9_.+#-]{2,}/g) || [];
  return [...new Set(matches)]
    .filter((t) => t.length >= 2)
    .sort((a, b) => b.length - a.length)
    .slice(0, 10);
}

export function highlightParts(text: string, query: string): { text: string; hit: boolean }[] {
  const tokens = queryTokens(query);
  if (!tokens.length) return [{ text, hit: false }];
  const pattern = tokens
    .map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"))
    .join("|");
  const regex = new RegExp(`(${pattern})`, "gi");
  const parts: { text: string; hit: boolean }[] = [];
  let last = 0;
  for (const match of text.matchAll(regex)) {
    const index = match.index ?? 0;
    if (index > last) parts.push({ text: text.slice(last, index), hit: false });
    parts.push({ text: match[0], hit: true });
    last = index + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), hit: false });
  return parts.length ? parts : [{ text, hit: false }];
}

export function cleanTitle(value: string): string {
  return value
    .replace(/^#+\s*/, "")
    .replace(/\.(md|txt)$/i, "")
    .trim();
}
