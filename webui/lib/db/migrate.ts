import { config } from "dotenv";

config({
  path: ".env.local",
});

/*
 * 本地存储初始化（原 Postgres 迁移脚本的替代）。
 * 库文件与建表语句由 client.ts 在首次连接时幂等创建，这里负责触发一次、做一次探针查询并打印落点，
 * 供 `pnpm build` / `pnpm db:migrate` 复用同一入口。
 */
const runInit = async () => {
  const [{ db, resolveDbPath }, { chat }] = await Promise.all([
    import("./client"),
    import("./schema"),
  ]);

  // 一次真实查询，确认表结构就绪；失败就在构建阶段暴露，而不是等到用户点开页面
  const rows = await db.select({ id: chat.id }).from(chat).limit(1);

  console.log(`Local sqlite ready at ${resolveDbPath()} (rows=${rows.length})`);
};

runInit().catch((error) => {
  console.error("Local sqlite init failed");
  console.error(error);
  process.exit(1);
});
