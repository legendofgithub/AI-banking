// 浏览器端到端实测:驱动系统 Chrome,走完「打字转账 → 确认卡片 → 扣款」全流程。
// 用法:cd webui && node e2e-check.mjs        (默认 127.0.0.1)
//      BASE=http://localhost:3000 node e2e-check.mjs

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";

const shot = (n) => {
  mkdirSync("gui-test-screenshots", { recursive: true });
  return `gui-test-screenshots/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { height: 850, width: 1280 } });
page.on("console", (m) => {
  if (m.type() === "error") {
    console.log("[console.error]", m.text().slice(0, 200));
  }
});
page.on("pageerror", (e) =>
  console.log("[pageerror]", String(e).slice(0, 300))
);

try {
  // ---- T0 登录(2026-09-28 实名制后转账必须登录;老版脚本缺这步,已跑不通) ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill("13800002233");
  await page.getByRole("textbox", { name: /登录密码/ }).fill("Demo@12345");
  await page.getByRole("button", { exact: false, name: /登录/ }).click();
  // 两步登录(2026-10-27):第二步验证码——模拟短信卡「填入」→「验证并登录」
  await page.locator('[data-testid="demo-sms-fill"]').click({ timeout: 15_000 });
  await page.getByRole("button", { name: /验证并登录/ }).click();
  await page.waitForSelector("textarea", { timeout: 30_000 });

  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("textarea", { timeout: 20_000 });
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

  await page.waitForSelector('input[aria-label="支付密码"]', {
    timeout: 60_000,
  });
  await page.fill('input[aria-label="支付密码"]', "888888");
  await page.waitForSelector('button:has-text("确认"):not([disabled])', {
    timeout: 30_000,
  });
  await page.screenshot({ fullPage: true, path: shot("t2_确认卡片") });
  console.log("T2 金色确认卡片出现(含支付密码): 通过");

  await page.getByRole("button", { exact: true, name: "确认" }).click();
  // 播报文案由 LLM 生成,措辞不定,用正则兜底。词表与 mobile 版对齐
  // (2026-10-20 实测 DeepSeek 用过「已向…转账」句式,窄词表会干等超时)
  await page.waitForSelector("text=/成功|已转|已执行|完成|已扣|已向/", {
    timeout: 90_000,
  });
  await page.screenshot({ fullPage: true, path: shot("t3_转账成功") });
  console.log("T3 确认后扣款成功播报: 通过");
  console.log("E2E ALL PASSED");
} finally {
  await browser.close();
}
