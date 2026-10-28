import fs from "node:fs/promises";
import path from "node:path";

type Summary = {
  generatedAt: string;
  environment: Record<string, string>;
  corpus: Record<string, number>;
  results: {
    label: string;
    backend: string;
    dtype: string;
    batchSize: number;
    loadMs: number;
    embedMsPerChunk: number;
    queryMs: number;
    dbMs: number;
    e2eMs: number;
    recall5: number;
    ndcg5: number;
    top5Overlap: number | null;
  }[];
};

const file = path.resolve("public/benchmarks/summary.json");
const summary = JSON.parse(await fs.readFile(file, "utf8")) as Summary;
const n = (v: number, digits = 1) => v.toFixed(digits);

console.log(`# STEPKB benchmark · ${summary.generatedAt}\n`);
console.log(
  `device: ${summary.environment.device} · browser: ${summary.environment.browser} · seekdb: ${summary.environment.seekdb}\n`,
);
console.log(
  `corpus: ${summary.corpus.documents} docs / ${summary.corpus.chunks} chunks / ${summary.corpus.queries} queries\n`,
);
console.log("| config | backend | dtype | batch | load (ms) | embed/chunk (ms) | query (ms) | seekdb (ms) | e2e (ms) | R@5 | nDCG@5 | overlap |");
console.log("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|");
for (const row of summary.results) {
  console.log(
    `| ${row.label} | ${row.backend} | ${row.dtype} | ${row.batchSize} | ${n(row.loadMs, 0)} | ${n(
      row.embedMsPerChunk,
    )} | ${n(row.queryMs)} | ${n(row.dbMs)} | ${n(row.e2eMs)} | ${(row.recall5 * 100).toFixed(
      1,
    )}% | ${(row.ndcg5 * 100).toFixed(1)}% | ${row.top5Overlap == null ? "—" : `${(row.top5Overlap * 100).toFixed(1)}%`} |`,
  );
}
