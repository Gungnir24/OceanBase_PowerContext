import "dotenv/config";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(__dirname, "..");
export const IS_PROD = process.env.NODE_ENV === "production" || process.argv.includes("--prod");
export const PORT = Number(process.env.PORT || 8787);

export const SEEKDB = {
  host: process.env.SEEKDB_HOST || "127.0.0.1",
  port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || "root",
  password: process.env.SEEKDB_PASSWORD || "",
  database: process.env.SEEKDB_DATABASE || "stepfun_kb",
};

export const MODEL = {
  id: "bge-small-zh-v1.5",
  repo: "Xenova/bge-small-zh-v1.5",
  revision: "75c43b069aac4d136ba6bc1122f995fedcfd2781",
  dimension: 512,
  queryPrefix: "为这个句子生成表示以用于检索相关文章：",
  fp16Bytes: 47_497_427,
  int8Bytes: 24_010_842,
  fp32Bytes: 94_851_877,
};

export const DEMO_CORPUS_PATH = path.join(ROOT, "public", "demo-corpus.json");
export const MODEL_MANIFEST_PATH = path.join(ROOT, "public", "models", "manifest.json");
