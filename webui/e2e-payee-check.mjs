// 收款人选择器端到端实测(2026-10-20):
//   点击「我要转账」→ 自动列出已记录收款人 → 搜索姓名/手机号 → 右上角 + 新建
//   → 选中收款人发起转账 → 金额追问 → 金色确认卡(支付密码)→ 扣款成功。
// 用法:cd webui && node e2e-payee-check.mjs   (需 3000/8800 已起、LLM Key 已配)
// 可重复运行:新建收款人固定手机号,重复时识别"不能重复"并改用已有记录。

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const PHONE = "13800002233";
const PASSWORD = "Demo@12345";
const PAY_PASSWORD = "888888";
const NEW_NAME = "E2E收款人";
const NEW_PHONE = "13912345678";

const shot = (n) => {
  mkdirSync("../gui-test-payee-20261020", { recursive: true });
  return `../gui-test-payee-20261020/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { height: 900, width: 1280 } });
page.setDefaultTimeout(60_000);
page.on("console", (m) => {
  if (m.type() === "error") {
    console.log("[console.error]", m.text().slice(0, 200));
  }
});
page.on("pageerror", (e) =>
  console.log("[pageerror]", String(e).slice(0, 300))
);

try {
  // ---- T1 登录 ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill(PHONE);
  await page.getByRole("textbox", { name: /登录密码/ }).fill(PASSWORD);
  await page.getByRole("button", { exact: false, name: /登录/ }).click();
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="suggested-actions"]', {
    timeout: 30_000,
  });
  await page.waitForTimeout(2000); // 等水合
  console.log("T1 登录 + 首页建议胶囊: 通过");

  // ---- T2 点「我要转账」自动弹出收款人列表 ----
  await page.getByRole("button", { exact: true, name: "我要转账" }).click();
  const dialog = page.locator('[data-testid="payee-picker"]');
  await dialog.waitFor({ timeout: 10_000 });
  const rows = page.locator('[data-testid="payee-row"]');
  await rows.first().waitFor({ timeout: 10_000 });
  const initialCount = await rows.count();
  if (initialCount < 1) {
    throw new Error("T2: 收款人列表为空");
  }
  const firstRowText = await rows.first().innerText();
  if (!/\d{11}/.test(firstRowText)) {
    throw new Error(`T2: 收款人未显示手机号: ${firstRowText}`);
  }
  await page.screenshot({ path: shot("t2_收款人列表") });
  console.log(`T2 自动列出收款人(${initialCount} 位,含手机号): 通过`);

  // ---- T3 搜索:姓名与手机号都能过滤 ----
  const search = page.getByRole("textbox", { name: "搜索收款人" });
  await search.fill("张");
  await page.waitForTimeout(300);
  const nameFiltered = await rows.allInnerTexts();
  if (!nameFiltered.some((t) => t.includes("张阿姨"))) {
    throw new Error("T3: 搜索「张」未找到张阿姨");
  }
  if (nameFiltered.some((t) => t.includes("林悦"))) {
    throw new Error("T3: 搜索「张」仍显示林悦");
  }
  await search.fill("13500003456");
  await page.waitForTimeout(300);
  const phoneFiltered = await rows.allInnerTexts();
  if (
    phoneFiltered.length !== 1 ||
    !phoneFiltered[0].includes("13500003456")
  ) {
    throw new Error(
      `T3: 手机号搜索应恰好 1 条,实际 ${phoneFiltered.length} 条`
    );
  }
  await page.screenshot({ path: shot("t3_手机号搜索") });
  console.log("T3 搜索(姓名/手机号)过滤: 通过");

  // ---- T4 右上角 + 新建收款人 ----
  await search.fill("");
  await page.locator('[data-testid="payee-add"]').click();
  await page.getByLabel("收款人姓名").fill(NEW_NAME);
  await page.getByLabel("手机号").fill(NEW_PHONE);
  await page.getByLabel("备注").fill("浏览器验证");
  await page.locator('[data-testid="payee-save"]').click();
  await page.waitForTimeout(800);
  const createError = await dialog.innerText();
  if (createError.includes("不能重复")) {
    console.log("T4 该测试收款人已存在,改用已有记录继续");
    await page.getByRole("button", { name: "取消" }).click();
  } else if (!createError.includes(NEW_NAME)) {
    await page.screenshot({ path: shot("t4_新建失败") });
    throw new Error("T4: 新建收款人后列表里没有出现新记录");
  }
  await search.fill("E2E");
  await page.waitForTimeout(300);
  const createdRow = rows.filter({ hasText: NEW_NAME }).first();
  await createdRow.waitFor({ timeout: 10_000 });
  if (!(await createdRow.innerText()).includes(NEW_PHONE)) {
    throw new Error("T4: 新收款人手机号未显示");
  }
  await page.screenshot({ path: shot("t4_新建收款人") });
  console.log("T4 新建收款人(姓名+手机号+备注)并回显: 通过");

  // ---- T5 选中收款人 → 带着姓名+手机号发起转账 ----
  await createdRow.click();
  await dialog.waitFor({ state: "hidden", timeout: 5000 });
  await page
    .locator('[data-role="user"]')
    .filter({ hasText: "转给E2E收款人" })
    .last()
    .waitFor({ state: "visible", timeout: 15_000 });
  await page.waitForSelector("text=/转多少钱|转账金额|金额/", {
    timeout: 90_000,
  });
  await page.screenshot({ path: shot("t5_选中后金额追问") });
  console.log("T5 选中收款人后发起转账并追问金额: 通过");

  // ---- T6 金额 → 金色确认卡 → 支付密码扣款 ----
  await page.fill("textarea", "1元");
  await page.keyboard.press("Enter");
  await page.waitForSelector('input[aria-label="支付密码"]', {
    timeout: 90_000,
  });
  const cardText = await page
    .locator("input[aria-label='支付密码']")
    .locator("xpath=ancestor::div[contains(@class,'rounded-2xl')][1]")
    .innerText();
  if (!cardText.includes(NEW_NAME) || !cardText.includes("1")) {
    throw new Error("T6: 确认卡未显示新收款人/金额");
  }
  await page.fill('input[aria-label="支付密码"]', PAY_PASSWORD);
  await page.getByRole("button", { exact: true, name: "确认" }).click();
  await page.waitForSelector("text=/成功|已转|已执行|完成|已扣|已向/", {
    timeout: 90_000,
  });
  await page.screenshot({ path: shot("t6_转账成功") });
  console.log("T6 支付密码确认后扣款成功: 通过");
  console.log("E2E PAYEE PICKER ALL PASSED");
} finally {
  await browser.close();
}
