import fs from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { drizzle } from "drizzle-orm/sqlite-proxy";
import type { SqliteRemoteDatabase } from "drizzle-orm/sqlite-proxy";
import { SCHEMA_DDL } from "./ddl";
import * as schema from "./schema";

/*
 * 本地 SQLite 实现（node:sqlite，Node 22.5+ 内置，零外部依赖）。
 * 取代原来的 Postgres：演示机不再需要起数据库服务，删掉 data/webui-chat.db 即恢复初始态。
 * 环境变量 WEBUI_DB_PATH 可覆盖库文件路径。
 */

type LocalDb = SqliteRemoteDatabase<typeof schema>;

const DB_FILE = "webui-chat.db";

/** 仓库根下的 data/ 目录优先；找不到就退到 cwd（保证任何启动方式都不报错）。 */
function resolveDbPath(): string {
  if (process.env.WEBUI_DB_PATH) {
    return path.resolve(process.env.WEBUI_DB_PATH);
  }
  let dir = process.cwd();
  for (let i = 0; i < 4; i += 1) {
    const candidate = path.join(dir, "data");
    if (fs.existsSync(candidate) && fs.statSync(candidate).isDirectory()) {
      return path.join(candidate, DB_FILE);
    }
    const parent = path.dirname(dir);
    if (parent === dir) {
      break;
    }
    dir = parent;
  }
  return path.join(process.cwd(), DB_FILE);
}

/** node:sqlite 只接受 number/string/bigint/null/Uint8Array，这里把 drizzle 传下来的值归一化。 */
function normalizeParam(value: unknown): number | string | bigint | null | Uint8Array {
  if (value === null || value === undefined) {
    return null;
  }
  if (value instanceof Date) {
    return value.getTime();
  }
  if (typeof value === "boolean") {
    return value ? 1 : 0;
  }
  if (
    typeof value === "number" ||
    typeof value === "bigint" ||
    typeof value === "string" ||
    value instanceof Uint8Array
  ) {
    return value;
  }
  return JSON.stringify(value);
}

function createLocalDb(): LocalDb {
  const dbPath = resolveDbPath();
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });

  const sqlite = new DatabaseSync(dbPath);
  // WAL：读写不互斥；外键打开以保持与 Postgres 版一致的约束语义。
  sqlite.exec("PRAGMA journal_mode = WAL");
  sqlite.exec("PRAGMA foreign_keys = ON");
  sqlite.exec(SCHEMA_DDL);

  return drizzle(async (sql, params, method) => {
    const statement = sqlite.prepare(sql);
    const bound = (params ?? []).map((p) => normalizeParam(p));
    try {
      if (method === "run") {
        statement.run(...bound);
        return { rows: [] };
      }
      // sqlite-proxy 要求「行 = 值的数组」（drizzle 按字段顺序映射），
      // node:sqlite 返回的是对象，这里转成值数组。
      const raw =
        method === "get"
          ? (() => {
              const row = statement.get(...bound) as Record<string, unknown> | undefined;
              return row === undefined ? [] : [row];
            })()
          : (statement.all(...bound) as Record<string, unknown>[]);
      return { rows: raw.map((row) => Object.values(row)) } as unknown as {
        rows: unknown[];
      };
    } catch (error) {
      // 让上层 ChatbotError 能拿到原始错误信息（否则只剩 "Failed query"）
      throw new Error(
        `[webui-sqlite] ${(error as Error).message} | sql=${sql}`,
        { cause: error }
      );
    }
  });
}

const globalForDb = globalThis as unknown as { __webuiLocalDb?: LocalDb };

export const db: LocalDb = globalForDb.__webuiLocalDb ?? createLocalDb();

if (process.env.NODE_ENV !== "production") {
  // dev 热重载复用同一个连接，避免句柄泄漏
  globalForDb.__webuiLocalDb = db;
}

export { resolveDbPath };
