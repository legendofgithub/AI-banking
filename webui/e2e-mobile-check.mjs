// 手机端端到端实测:375x812 视口走完「登录 → 打字转账 → 金色确认卡(含支付密码)
// → 扣款成功」全流程,并断言两件事关手机可用性的硬指标:
//   A. 页面无横向溢出(gold 卡右边界不超出视口);
//   B. 待确认转账悬浮球完整交互链(贴边半球 → 点按气泡 → 半屏面板 →
//      箭头收回 → 拖拽中心整球渐变 → 松手回吸),且不遮挡输入区。
// 用法:cd webui && node e2e-mobile-check.mjs     (需 8800/3000 已起、LLM Key 已配)

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const PHONE = "13800002233";
const PASSWORD = "Demo@12345";
const PAY_PASSWORD = "888888";

const shot = (n) => {
  mkdirSync("../gui-test-mobile-20261014", { recursive: true });
  return `../gui-test-mobile-20261014/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext({
  hasTouch: true,
  isMobile: true,
  userAgent:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
  viewport: { height: 812, width: 375 },
});
const page = await context.newPage();
page.setDefaultTimeout(60_000);
page.on("console", (m) => {
  if (m.type() === "error") {
    console.log("[console.error]", m.text().slice(0, 200));
  }
});
page.on("pageerror", (e) =>
  console.log("[pageerror]", String(e).slice(0, 300))
);

const noHorizontalOverflow = async (label) => {
  const overflow = await page.evaluate(() => {
    const de = document.documentElement;
    return { iw: window.innerWidth, sw: de.scrollWidth };
  });
  if (overflow.sw > overflow.iw + 1) {
    throw new Error(
      `${label}: 横向溢出 scrollWidth=${overflow.sw} > innerWidth=${overflow.iw}`
    );
  }
  console.log(`${label}: 无横向溢出 (${overflow.sw}/${overflow.iw}px) 通过`);
};

try {
  // ---- T1 手机登录(动钱链路实名制要求) ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill(PHONE);
  await page.getByRole("textbox", { name: /登录密码/ }).fill(PASSWORD);
  await page.getByRole("button", { exact: false, name: /登录/ }).click();
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.waitForTimeout(3000); // 等水合
  await page.screenshot({ path: shot("mobile-t1_登录后首页") });
  await noHorizontalOverflow("T1 首页");
  console.log("T1 手机登录 + 首页: 通过");

  // ---- T2 转账指令 → 金色确认卡(支付密码版) ----
  await page.fill("textarea", "给张三转10元");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  if ((await page.locator('[data-role="user"]').count()) === 0) {
    await page.getByRole("button", { name: "Submit" }).click();
  }
  await page.waitForSelector('input[aria-label="支付密码"]', {
    timeout: 60_000,
  });
  await page.screenshot({ path: shot("mobile-t2_确认卡375") });
  await noHorizontalOverflow("T2 确认卡");
  const cardRight = await page.evaluate(() => {
    const input = document.querySelector('input[aria-label="支付密码"]');
    const card = input?.closest(".rounded-2xl");
    return card ? Math.ceil(card.getBoundingClientRect().right) : 0;
  });
  if (!cardRight || cardRight > 375) {
    throw new Error(
      `T2: 金色确认卡右边界 ${cardRight || "(未找到卡片)"} 超出 375px 视口或定位失败`
    );
  }
  console.log(`T2 金色确认卡(支付密码)出现,右边界 ${cardRight}px: 通过`);

  // ---- T3 输支付密码 → 扣款成功 ----
  await page.fill('input[aria-label="支付密码"]', PAY_PASSWORD);
  await page.getByRole("button", { exact: true, name: "确认" }).click();
  // 播报词由 LLM 生成、时延不定(实测 DeepSeek 偶发超 60s),给足 90s 并放宽词表
  await page.waitForSelector("text=/成功|已转|已执行|完成|已扣/", {
    timeout: 90_000,
  });
  await page.screenshot({ path: shot("mobile-t3_转账成功") });
  console.log("T3 支付密码确认后扣款成功播报: 通过");

  // ---- T4 待确认转账悬浮球(2026-10-14 v2:可拖拽悬浮球取代横幅) ----
  const userBefore = await page.locator('[data-role="user"]').count();
  await page.fill("textarea", "给张三转5元");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
  if ((await page.locator('[data-role="user"]').count()) === userBefore) {
    await page.getByRole("button", { name: "Submit" }).click();
  }
  // 锚定最后一条助手消息里的新卡(T3 已答的旧卡按钮是禁用的,不能误选)
  const newCard = page.locator('[data-role="assistant"]').last();
  await newCard
    .locator('input[aria-label="支付密码"]')
    .waitFor({ timeout: 60_000 });
  await page.waitForTimeout(1500); // 等待提醒轮询出球

  // T4.1 球已出现并吸附在屏幕边缘(只露半球)
  const ball = page.locator('[data-testid="pending-ball"]');
  await ball.waitFor({ timeout: 10_000 });
  const box1 = await ball.boundingBox();
  const visibleW = 375 - box1.x;
  if (visibleW <= 10 || visibleW >= 50) {
    throw new Error(
      `T4.1: 悬浮球未贴边吸附(可见宽度 ${visibleW}px,期望约半球 10~50px)`
    );
  }
  console.log(`T4.1 悬浮球贴边吸附(可见 ${visibleW.toFixed(0)}px): 通过`);
  await page.screenshot({ path: shot("mobile-t4_悬浮球贴边") });

  // T4.2 球不遮挡输入区
  const inputClickable = await page.evaluate(() => {
    const ta = document.querySelector("textarea");
    if (!ta) {
      return false;
    }
    const r = ta.getBoundingClientRect();
    const hit = document.elementFromPoint(
      r.left + r.width / 2,
      r.top + r.height / 2
    );
    return hit === ta || ta.contains(hit);
  });
  if (!inputClickable) {
    throw new Error("T4.2: 悬浮球遮挡输入区(elementFromPoint 未命中 textarea)");
  }
  console.log("T4.2 悬浮球不遮挡输入区: 通过");

  // T4.3 点按球 → 小气泡(N 笔待确认 + 查看详情);点屏幕其他区域 → 收起
  await ball.click();
  const bubble = page.locator('[data-testid="pending-bubble"]');
  await bubble.waitFor({ timeout: 5000 });
  const bubbleText = await bubble.innerText();
  if (!/\d+ 笔转账待确认/.test(bubbleText)) {
    throw new Error(`T4.3: 气泡文案异常: ${bubbleText}`);
  }
  console.log("T4.3 点按弹气泡: 通过");
  await page.locator('[data-role="assistant"]').last().click();
  await bubble.waitFor({ state: "hidden", timeout: 5000 });
  await ball.click();
  await bubble.waitFor({ timeout: 5000 });
  console.log("T4.3b 点外部收起气泡、再点球重开: 通过");

  // T4.4 查看详情 → 屏幕正中央确认卡(含订单明细,居中是硬断言)
  await page.locator('[data-testid="pending-bubble-detail"]').click();
  const sheet = page.locator('[data-testid="pending-sheet"]');
  await sheet.waitFor({ timeout: 5000 });
  if (!(await sheet.innerText()).includes("张三")) {
    throw new Error("T4.4: 面板里没有待确认订单明细");
  }
  const sb = await sheet.boundingBox();
  const cx = sb.x + sb.width / 2;
  const cy = sb.y + sb.height / 2;
  if (Math.abs(cx - 375 / 2) > 24 || Math.abs(cy - 812 / 2) > 80) {
    throw new Error(
      `T4.4: 确认卡未居中(中心 ${cx.toFixed(0)},${cy.toFixed(0)} vs 视口 187,406)`
    );
  }
  await page.screenshot({ path: shot("mobile-t4_居中确认面板") });
  console.log(
    `T4.4 屏幕正中央确认卡(中心 ${cx.toFixed(0)},${cy.toFixed(0)}): 通过`
  );

  // T4.5 左上角朝右箭头 → 收回为悬浮球
  await page.locator('[data-testid="pending-sheet-collapse"]').click();
  await sheet.waitFor({ state: "hidden", timeout: 5000 });
  console.log("T4.5 左上角箭头收回为悬浮球: 通过");

  // T4.6 拖到屏幕中心 → 整球动态渐变;松手 → 回吸边缘
  const b = await ball.boundingBox();
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(187, 350, { steps: 10 });
  const spinning = await page
    .locator('[data-testid="pending-ball"] .pending-ball-spin')
    .count();
  await page.screenshot({ path: shot("mobile-t4_拖拽中心整球渐变") });
  await page.mouse.up();
  await page.waitForTimeout(500); // 等吸附过渡动画
  const box2 = await ball.boundingBox();
  const visible2 = 375 - box2.x;
  if (spinning !== 1) {
    throw new Error("T4.6: 拖到屏幕中心未出现整球动态渐变");
  }
  if (visible2 <= 10 || visible2 >= 50) {
    throw new Error(`T4.6: 松手后未回吸边缘(可见宽度 ${visible2}px)`);
  }
  console.log("T4.6 拖拽: 中心整球渐变 + 松手回吸边缘: 通过");

  // ---- 清场:取消这笔待确认单,不留悬空资金 ----
  await page.waitForFunction(() => {
    const btns = [...document.querySelectorAll("button")];
    const last = btns.filter((x) => x.textContent.trim() === "取消").pop();
    return Boolean(last && !last.disabled);
  });
  await newCard.getByRole("button", { exact: true, name: "取消" }).click();
  await newCard.getByText(/取消/).first().waitFor({ timeout: 30_000 });
  console.log("清场: 待确认单已取消");

  console.log("MOBILE E2E ALL PASSED");
} finally {
  await browser.close();
}
