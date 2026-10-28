export type ChunkOptions = {
  target?: number;
  overlap?: number;
};

/**
 * Paragraph-first Chinese/English chunker with a tail overlap.
 * Keeps semantic boundaries where possible and hard-splits only over-long sentences.
 */
export function chunkText(text: string, options: ChunkOptions = {}): string[] {
  const target = Math.max(120, options.target ?? 280);
  const overlap = Math.max(0, Math.min(120, options.overlap ?? 48));
  const normalized = text
    .replace(/\r\n/g, "\n")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  if (!normalized) return [];

  const chunks: string[] = [];
  let current = "";

  const flush = () => {
    const value = current.trim();
    if (value) chunks.push(value);
    current = "";
  };

  const pushLong = (textValue: string) => {
    let rest = textValue.trim();
    while (rest.length > target) {
      chunks.push(rest.slice(0, target).trim());
      rest = rest.slice(target - overlap).trim();
    }
    if (rest) current = rest;
  };

  const paragraphs = normalized.split(/\n{2,}/);
  for (const paragraph of paragraphs) {
    const p = paragraph.trim();
    if (!p) continue;
    if (p.length <= target) {
      if (!current) current = p;
      else if (current.length + p.length + 2 <= target) current += `\n\n${p}`;
      else {
        flush();
        current = p;
      }
      continue;
    }

    const sentences = p.match(/[^。！？!?；;\n]+[。！？!?；;]?/g) || [p];
    for (const raw of sentences) {
      const sentence = raw.trim();
      if (!sentence) continue;
      if (!current) {
        current = sentence;
      } else if (current.length + sentence.length + 1 <= target) {
        current += sentence;
      } else {
        flush();
        current = sentence;
      }
      if (current.length > target * 1.25) {
        const value = current;
        current = "";
        pushLong(value);
      }
    }
    flush();
  }
  flush();

  if (overlap > 0 && chunks.length > 1) {
    for (let i = 1; i < chunks.length; i++) {
      const tail = chunks[i - 1].slice(-overlap).trim();
      if (tail && !chunks[i].startsWith(tail)) {
        chunks[i] = `${tail}\n${chunks[i]}`;
      }
    }
  }
  return chunks.filter(Boolean);
}

export function chunkId(docId: string, index: number): string {
  return `${docId}_${String(index).padStart(2, "0")}`;
}

export async function sha256(text: string): Promise<string> {
  const data = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
