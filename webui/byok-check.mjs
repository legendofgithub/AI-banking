// BYOK 全流程实测:左下角「设置 → 大模型」→ 填 Key → 测试 → 保存 → 底部模型信息 → 对话请求带 Key/模型。
// 用法:cd webui && node byok-check.mjs
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const KEY = process.env.ZAI_API_KEY;
const shot = (n) => {
  mkdirSync("gui-test-screenshots", { recursive: true });
  return `gui-test-screenshots/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage();
let chatBody = null;
page.on("request", (req) => {
  if (req.url().includes(":8800/api/chat") && req.method() === "POST") {
    try { chatBody = JSON.parse(req.postData()); } catch {}
  }
});
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 200)));
page.on("console", (m) => {
  if (m.type() === "error") console.log("[console.error]", m.text().slice(0, 150));
});

try {
  await page.goto("http://localhost:3000/", { waitUntil: "domcontentloaded" });
  await page.waitForSelector("[data-testid='settings-button']", { timeout: 20000 });
  console.log("T1 设置入口出现: 通过");

  await page.click("[data-testid='settings-button']");
  await page.click("[data-testid='settings-model']");
  await page.waitForSelector("[data-testid='model-settings-dialog']", { timeout: 10000 });
  console.log("T1a 大模型设置弹窗: 通过");

  await page.fill("[data-testid='byok-key-input']", KEY);
  await page.getByRole("button", { name: "测试连接" }).click();
  await page.waitForFunction(() => document.body.textContent.includes("有效"), { timeout: 30000 });
  console.log("T2 测试连接: 通过");

  await page.getByRole("button", { name: "保存", exact: true })
    .click({ force: true, timeout: 8000 });
  await page.waitForTimeout(500);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(3000);
  await page.screenshot({ path: shot("BYOK_保存后") });
  console.log("T3 当前模型:", (await page.getByTestId("connected-model-info").textContent()).trim());

  await page.click("[data-testid='multimodal-input']");
  await page.fill("[data-testid='multimodal-input']", "你好");
  console.log("T4 已填入消息,按 Enter…");
  await page.keyboard.press("Enter");
  await page.waitForFunction(() => {
    const els = document.querySelectorAll("[data-role='assistant']");
    const t = els[els.length - 1]?.textContent || "";
    return els.length >= 1 && t.length > 5 && !t.includes("Waiting");
  }, { timeout: 90000 });
  await page.screenshot({ path: shot("BYOK_对话回复") });
  console.log("T5 对话收到回复");
  console.log("T6 请求带Key:", !!(chatBody && chatBody.api_key === KEY),
              "| 带模型:", chatBody ? chatBody.model : null);
} catch (e) {
  await page.screenshot({ path: shot("BYOK_失败现场") }).catch(() => {});
  console.log("FAIL:", String(e).slice(0, 300));
} finally {
  await browser.close();
}
