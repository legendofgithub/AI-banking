// 浏览器端到端实测:驱动系统 Chrome,走完「打字转账 → 确认卡片 → 扣款」全流程。
// 用法:cd webui && node e2e-check.mjs        (默认 127.0.0.1)
//      BASE=http://localhost:3000 node e2e-check.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";

const shot = (n) => {
  mkdirSync("gui-test-screenshots", { recursive: true });
  return `gui-test-screenshots/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
page.on("console", (m) => {
  if (m.type() === "error") console.log("[console.error]", m.text().slice(0, 200));
});
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));

try {
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("textarea", { timeout: 20000 });
  await page.waitForTimeout(3000); // 等水合完成
  await page.screenshot({ path: shot("t1_首页") });
  console.log("T1 首页加载 + 水合: 通过");

  await page.fill("textarea", "给老王转50块");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  // Enter 若未提交,回退点 Submit 按钮
  if ((await page.locator('[data-role="user"]').count()) === 0) {
    await page.getByRole("button", { name: "Submit" }).click();
    console.log("Enter 未触发,已改点 Submit 按钮");
  }

  await page.waitForSelector('button:has-text("确认")', { timeout: 60000 });
  await page.screenshot({ path: shot("t2_确认卡片"), fullPage: true });
  console.log("T2 金色确认卡片出现: 通过");

  await page.getByRole("button", { name: "确认", exact: true }).click();
  // 播报文案由 LLM 生成,措辞不定,用正则兜底
  await page.waitForSelector("text=/成功|已转|完成/", { timeout: 60000 });
  await page.screenshot({ path: shot("t3_转账成功"), fullPage: true });
  console.log("T3 确认后扣款成功播报: 通过");
  console.log("E2E ALL PASSED");
} finally {
  await browser.close();
}
