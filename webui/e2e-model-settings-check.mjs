// 大模型管理 UI 端到端实测(2026-10-21):
//   设置 → 大模型管理 → 右上角「+ 新增供应商」→ 选 OpenAI → 填 Key/Base URL
//   → 测试并保存 → 显示「已连接/当前使用」→ 刷新 + 重新登录后配置仍在。
// 校验接口用 Playwright 拦截模拟成功,不消耗真实 API Key、不联网。
// 用法:cd webui && node e2e-model-settings-check.mjs

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const PHONE = "13800002233";
const PASSWORD = "Demo@12345";
const FAKE_KEY = "sk-e2e-test-only-1234567890";

const shot = (n) => {
  mkdirSync("../gui-test-models-20261021", { recursive: true });
  return `../gui-test-models-20261021/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { height: 900, width: 1280 } });
page.setDefaultTimeout(60_000);
page.on("pageerror", (e) =>
  console.log("[pageerror]", String(e).slice(0, 300))
);

await page.route("**/api/validate-key", (route) =>
  route.fulfill({
    body: JSON.stringify({ model_count: 2, ok: true }),
    contentType: "application/json",
    status: 200,
  })
);
await page.route("**/api/models", (route) =>
  route.fulfill({
    body: JSON.stringify({
      capabilities: {
        "gpt-4o": { reasoning: true, tools: true, vision: true },
        "gpt-4o-mini": { reasoning: true, tools: true, vision: true },
      },
      models: [
        {
          capabilities: { reasoning: true, tools: true, vision: true },
          description: "OpenAI chat model with tool use",
          id: "gpt-4o-mini",
          name: "GPT-4O-Mini",
          provider: "openai",
        },
        {
          capabilities: { reasoning: true, tools: true, vision: true },
          description: "OpenAI chat model with tool use",
          id: "gpt-4o",
          name: "GPT-4O",
          provider: "openai",
        },
      ],
    }),
    contentType: "application/json",
    status: 200,
  })
);

async function login() {
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#identifier", { timeout: 15_000 });
  await page.waitForTimeout(800); // 等 React 水合,避免早期 fill 被状态初始化覆盖
  const demo = page.getByRole("button", { name: /一键填入/ });
  const identifier = page.getByRole("textbox", { name: /账号/ });
  const password = page.getByRole("textbox", { name: /登录密码/ });
  const loginBtn = page.getByRole("button", { exact: false, name: /登录/ });
  let enabled = false;
  for (let i = 0; i < 30; i += 1) {
    if ((await demo.count()) > 0) {
      await demo.click();
    } else {
      await identifier.fill(PHONE);
      await password.fill(PASSWORD);
    }
    await page.waitForTimeout(150);
    if (await loginBtn.isEnabled()) {
      enabled = true;
      break;
    }
  }
  if (!enabled) {
    throw new Error("登录按钮始终不可用(表单未完成水合?)");
  }
  await loginBtn.click();
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.waitForTimeout(1500);
}

async function openModelSettings() {
  await page.locator('[data-testid="settings-button"]').click();
  await page.locator('[data-testid="settings-model"]').click();
  await page.locator('[data-testid="model-settings-dialog"]').waitFor({
    timeout: 10_000,
  });
}

try {
  // ---- T1 登录并打开大模型管理 ----
  await login();
  await openModelSettings();
  await page.locator('[data-testid="llm-add-provider"]').waitFor({
    timeout: 10_000,
  });
  const addBtn = page.locator('[data-testid="llm-add-provider"]');
  const addText = (await addBtn.innerText()).replace(/\s+/g, "");
  if (!addText.includes("新增供应商")) {
    throw new Error(`T1: 按钮文案异常: ${addText}`);
  }
  await page.screenshot({ path: shot("t1_大模型管理") });
  console.log("T1 设置 → 大模型管理 + 右上角「+ 新增供应商」: 通过");

  // ---- T2 新增 OpenAI 供应商:预设 Base URL + Key → 连接成功 ----
  await addBtn.click();
  const form = page.locator('[data-testid="llm-add-form"]');
  await form.waitFor({ timeout: 10_000 });
  const providerSelect = page.locator('[data-testid="llm-provider-select"]');
  const options = await providerSelect.locator("option").allInnerTexts();
  for (const expected of ["DeepSeek", "OpenAI", "Claude"]) {
    if (!options.some((o) => o.includes(expected))) {
      throw new Error(`T2: 供应商下拉缺少 ${expected}: ${options}`);
    }
  }
  await providerSelect.selectOption("openai");
  const baseInput = page.locator('[data-testid="llm-base-url"]');
  if ((await baseInput.inputValue()) !== "https://api.openai.com/v1") {
    throw new Error("T2: 选择 OpenAI 后 Base URL 未预填");
  }
  await page.locator('[data-testid="llm-api-key"]').fill(FAKE_KEY);
  await page.screenshot({ path: shot("t2_新增供应商表单") });
  await page.locator('[data-testid="llm-save"]').click();

  const card = page.locator('[data-testid="llm-connection"]').first();
  await card.waitFor({ timeout: 15_000 });
  const cardText = await card.innerText();
  if (!cardText.includes("已连接") || !cardText.includes("当前使用")) {
    throw new Error(`T2: 连接状态异常: ${cardText}`);
  }
  if (!cardText.includes("••••••7890")) {
    throw new Error(`T2: 页面未脱敏展示 Key: ${cardText}`);
  }
  const stored = await page.evaluate(() =>
    window.localStorage.getItem("llm-connections-v1")
  );
  if (!stored || !stored.includes(FAKE_KEY)) {
    throw new Error("T2: 连接没有落到 localStorage");
  }
  await page.screenshot({ path: shot("t3_连接成功") });
  console.log("T2 新增供应商 + 测试连接 + 已连接/当前使用: 通过");

  // ---- T3 刷新页面后配置仍在 ----
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.waitForTimeout(1500);
  await openModelSettings();
  if ((await page.locator('[data-testid="llm-connection"]').count()) !== 1) {
    throw new Error("T3: 刷新后连接丢失");
  }
  console.log("T3 刷新页面后连接仍在: 通过");

  // ---- T4 模拟退出登录再登录,本机连接仍保留 ----
  await page.evaluate(() => {
    window.localStorage.removeItem("bank-token");
    window.localStorage.removeItem("bank-nickname");
    window.localStorage.removeItem("bank-user-id");
  });
  await login();
  await openModelSettings();
  const cardAfterRelogin = page.locator('[data-testid="llm-connection"]').first();
  await cardAfterRelogin.waitFor({ timeout: 10_000 });
  if (!(await cardAfterRelogin.innerText()).includes("当前使用")) {
    throw new Error("T4: 重新登录后连接未恢复为当前使用");
  }
  await page.screenshot({ path: shot("t4_重新登录后仍在") });
  console.log("T4 重新登录后连接状态仍保留: 通过");
  console.log("E2E MODEL SETTINGS ALL PASSED");
} finally {
  await browser.close();
}
