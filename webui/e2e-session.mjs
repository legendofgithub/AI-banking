// 会话持久化端到端实测:发消息 → 刷新页面 → 侧边栏点回该会话 → 消息恢复。
// 用法:cd webui && node e2e-session.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const shot = (n) => {
  mkdirSync("gui-test-screenshots", { recursive: true });
  return `gui-test-screenshots/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 850 } });
// 新上下文里侧边栏默认折叠(sidebar_state 无 cookie 时 defaultOpen=false),预置展开
await context.addCookies([
  { name: "sidebar_state", value: "true", url: BASE },
]);
const page = await context.newPage();
const MARK = "给王秀兰转20块";

try {
  // 1. 打开首页:不应出现登录提示,侧边栏应有会话目录
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3000);
  const loginPrompt = await page.getByText("Login to save").count();
  if (loginPrompt > 0) throw new Error("仍显示登录提示");
  console.log("T1 免登录:登录提示已消失");

  // 2. 发一条带标识的消息,走到确认卡片(会话已在后端登记)
  await page.waitForSelector("textarea", { timeout: 20000 });
  await page.fill("textarea", MARK);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  if ((await page.locator('[data-role="user"]').count()) === 0) {
    await page.getByRole("button", { name: "Submit" }).click();
  }
  await page.waitForSelector('button:has-text("确认")', { timeout: 60000 });
  console.log("T2 消息已发送,确认卡片出现(会话已登记)");

  // 3. 刷新页面
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForTimeout(3500);

  // 4. 侧边栏应出现该会话(标题=首条消息;历史测试轮次可能留下同名会话,取第一个)
  const item = page.getByRole("link", { name: new RegExp(MARK) }).first();
  await item.waitFor({ state: "visible", timeout: 15000 });
  console.log("T3 刷新后侧边栏出现该会话目录项");

  // 5. 点进去,消息应从后端恢复
  await item.click();
  await page.waitForTimeout(3000);
  await page.waitForSelector(`text=${MARK}`, { timeout: 15000 });
  await page.screenshot({ path: shot("s1_会话恢复"), fullPage: true });
  const restored = await page.locator('[data-role="user"]').count();
  if (restored === 0) throw new Error("会话消息未恢复");
  console.log(`T4 会话恢复:点击目录项后消息完整恢复(${restored} 条用户消息)`);
  console.log("SESSION E2E ALL PASSED");
} finally {
  await browser.close();
}
