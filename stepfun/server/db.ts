import { readFile } from "node:fs/promises";
import { SeekdbClient } from "seekdb";
import { MODEL_MANIFEST_PATH, SEEKDB } from "./config.js";

type Row = Record<string, any>;

let client: SeekdbClient | null = null;
let version = "unknown";

const IDENT = /^[a-zA-Z0-9_]+$/;

export function assertIdentifier(value: string, label: string): string {
  if (!IDENT.test(value) || value.length > 64) {
    throw new Error(`Invalid ${label}: ${value}`);
  }
  return value;
}

export async function initDb(): Promise<void> {
  const database = assertIdentifier(SEEKDB.database, "database name");
  const bootstrap = new SeekdbClient({
    host: SEEKDB.host,
    port: SEEKDB.port,
    user: SEEKDB.user,
    password: SEEKDB.password,
    database: "test",
  });
  try {
    await bootstrap.execute(
      `CREATE DATABASE IF NOT EXISTS \`${database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci`,
    );
  } finally {
    await bootstrap.close();
  }

  client = new SeekdbClient({
    host: SEEKDB.host,
    port: SEEKDB.port,
    user: SEEKDB.user,
    password: SEEKDB.password,
    database,
  });

  const v = await client.execute("SELECT VERSION() AS version");
  version = String((v as Row[] | null)?.[0]?.version || "unknown");

  await client.execute(`
    CREATE TABLE IF NOT EXISTS kb_registry (
      id VARCHAR(64) PRIMARY KEY,
      name VARCHAR(255) NOT NULL,
      description TEXT,
      created_at BIGINT NOT NULL,
      updated_at BIGINT NOT NULL,
      last_refresh_at BIGINT,
      model VARCHAR(128),
      dtype VARCHAR(16),
      backend VARCHAR(16)
    ) DEFAULT CHARSET = utf8mb4
  `);
  await ensureColumns(client, "kb_registry", {
    last_refresh_at: "BIGINT",
    model: "VARCHAR(128)",
    dtype: "VARCHAR(16)",
    backend: "VARCHAR(16)",
  });
  await client.execute(`
    CREATE TABLE IF NOT EXISTS kb_docs (
      kb_id VARCHAR(64) NOT NULL,
      doc_id VARCHAR(64) NOT NULL,
      title VARCHAR(255) NOT NULL,
      source VARCHAR(512),
      char_count INT NOT NULL DEFAULT 0,
      chunk_count INT NOT NULL DEFAULT 0,
      imported_at BIGINT NOT NULL,
      content_sha VARCHAR(64),
      model VARCHAR(128),
      dtype VARCHAR(16),
      PRIMARY KEY (kb_id, doc_id)
    ) DEFAULT CHARSET = utf8mb4
  `);
  await client.execute(`
    CREATE TABLE IF NOT EXISTS search_logs (
      id VARCHAR(64) PRIMARY KEY,
      kb_id VARCHAR(64) NOT NULL,
      query_text VARCHAR(1000) NOT NULL,
      mode VARCHAR(16) NOT NULL,
      top_k INT NOT NULL,
      embedding_ms DOUBLE,
      db_ms DOUBLE,
      total_ms DOUBLE,
      backend VARCHAR(16),
      model VARCHAR(128),
      dtype VARCHAR(16),
      top_ids TEXT,
      created_at BIGINT NOT NULL
    ) DEFAULT CHARSET = utf8mb4
  `);
}

async function ensureColumns(
  client: SeekdbClient,
  table: string,
  columns: Record<string, string>,
): Promise<void> {
  const rows = await client.execute(`SHOW COLUMNS FROM \`${table}\``);
  const existing = new Set((rows || []).map((row: any) => String(row.Field)));
  for (const [name, ddl] of Object.entries(columns)) {
    if (!existing.has(name)) {
      await client.execute(`ALTER TABLE \`${table}\` ADD COLUMN \`${name}\` ${ddl}`);
    }
  }
}

export function db(): SeekdbClient {
  if (!client) throw new Error("seekdb client is not initialized");
  return client;
}

export function seekdbVersion(): string {
  return version;
}

export async function closeDb(): Promise<void> {
  if (client) {
    await client.close();
    client = null;
  }
}

export async function modelManifest(): Promise<any> {
  try {
    return JSON.parse(await readFile(MODEL_MANIFEST_PATH, "utf8"));
  } catch {
    return null;
  }
}
