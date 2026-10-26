import { config } from "dotenv";
import { defineConfig } from "drizzle-kit";

config({
  path: ".env.local",
});

// 本地 SQLite（运行时由 node:sqlite 提供，无需外部数据库服务）
// 说明：lib/db/migrations/ 下是历史 Postgres 迁移，已不再执行；如需重新生成表结构用 `pnpm db:generate`。
export default defineConfig({
  dbCredentials: {
    url: process.env.WEBUI_DB_PATH ?? "../data/webui-chat.db",
  },
  dialect: "sqlite",
  out: "./lib/db/migrations-sqlite",
  schema: "./lib/db/schema.ts",
});
