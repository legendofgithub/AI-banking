// 手机扫码登录端到端实测(2026-10-20):
//   电脑端点右上角「手机端登录」→ 出二维码(内嵌局域网 URL+一次性票据)→
//   用第二个浏览器上下文模拟手机 375px 扫码打开 → 免密登录成功跳首页 →
//   同一张票再扫被拒(一次性)。
// 用法:cd webui && node e2e-mobile-login-check.mjs   (需 3000/8800 已起)
// 注:真机扫码验证需手机与电脑同一 Wi-Fi,这里用第二个上下文等效覆盖
// 「扫码打开 URL → 换 token → 存登录态 → 进首页」的完整链路。

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const PHONE = "13800002233";
const PASSWORD = "Demo@12345";

const shot = (n) => {
  mkdirSync("../gui-test-qrlogin-20261020", { recursive: true });
  return `../gui-test-qrlogin-20261020/${n}.png`;
};

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { height: 850, width: 1280 } });
page.setDefaultTimeout(60_000);
page.on("pageerror", (e) =>
  console.log("[desktop pageerror]", String(e).slice(0, 300))
);

try {
  // ---- T1 电脑端登录,点「手机端登录」出二维码 ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill(PHONE);
  await page.getByRole("textbox", { name: /登录密码/ }).fill(PASSWORD);
  await page.getByRole("button", { exact: false, name: /登录/ }).click();
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector('[data-testid="mobile-login-button"]', {
    timeout: 30_000,
  });
  await page.waitForTimeout(2000); // 等水合

  // 按钮在「后台入口」左边
  const mobileBtn = page.locator('[data-testid="mobile-login-button"]');
  const adminLink = page.locator('[data-testid="admin-console-link"]');
  const mb = await mobileBtn.boundingBox();
  const ab = await adminLink.boundingBox();
  if (!mb || !ab || mb.x + mb.width > ab.x) {
    throw new Error(
      `T1: 「手机端登录」按钮不在「后台入口」左边 (mobile右缘=${mb?.x ?? "?"} admin左缘=${ab?.x ?? "?"})`
    );
  }

  await mobileBtn.click();
  const qrBox = page.locator('[data-testid="mobile-qr"]');
  await qrBox.waitFor({ timeout: 15_000 });
  const qrUrl = await qrBox.getAttribute("data-url");
  if (!qrUrl || !/\/qr-login\?ticket=/.test(qrUrl)) {
    throw new Error(`T1: 二维码 URL 异常: ${qrUrl}`);
  }
  // 电脑端用 127.0.0.1 打开时,二维码里应是局域网 IP(手机可达)
  if (/127\.0\.0\.1|localhost/.test(qrUrl)) {
    throw new Error(`T1: 二维码 URL 仍是本机回环地址,手机扫不开: ${qrUrl}`);
  }
  const qrImg = page.locator('[data-testid="mobile-qr-img"]');
  await qrImg.waitFor({ timeout: 15_000 });
  let naturalWidth = 0;
  for (let i = 0; i < 30 && !naturalWidth; i++) {
    naturalWidth = await qrImg.evaluate(
      (el) => (el instanceof HTMLImageElement ? el.naturalWidth : 0)
    );
    if (!naturalWidth) {
      await page.waitForTimeout(300);
    }
  }
  if (!naturalWidth || naturalWidth < 100) {
    throw new Error(`T1: 二维码图片未加载(naturalWidth=${naturalWidth})`);
  }
  await page.screenshot({ path: shot("t1_电脑端二维码") });
  console.log(`T1 电脑端出码(局域网 URL + PNG ${naturalWidth}px): 通过`);

  // ---- T2 模拟手机扫码:375px 上下文打开二维码里的 URL → 免密登录 ----
  const phoneCtx = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    userAgent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    viewport: { height: 812, width: 375 },
  });
  const phone = await phoneCtx.newPage();
  phone.setDefaultTimeout(60_000);
  await phone.goto(qrUrl, { waitUntil: "domcontentloaded" });
  await phone.waitForSelector('[data-testid="qr-login-ok"]', {
    timeout: 20_000,
  });
  await phone.waitForSelector("textarea", { timeout: 30_000 }); // 跳回首页
  const nickname = await phone.evaluate(() =>
    window.localStorage.getItem("bank-nickname")
  );
  if (nickname !== "陈明") {
    throw new Error(`T2: 手机端登录态昵称异常: ${nickname}`);
  }
  const token = await phone.evaluate(() =>
    window.localStorage.getItem("bank-token")
  );
  if (!token || token.length < 16) {
    throw new Error("T2: 手机端没有拿到会话 token");
  }
  await phone.screenshot({ path: shot("t2_手机扫码登录成功") });
  console.log(`T2 手机扫码免密登录(昵称=${nickname}): 通过`);

  // ---- T3 同一张票再扫:一次性,必须被拒 ----
  const phone2Ctx = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const phone2 = await phone2Ctx.newPage();
  await phone2.goto(qrUrl, { waitUntil: "domcontentloaded" });
  await phone2.waitForSelector("text=/已过期或已被使用|二维码已失效/", {
    timeout: 20_000,
  });
  await phone2.screenshot({ path: shot("t3_旧票重扫被拒") });
  console.log("T3 票据一次性(重扫被拒): 通过");

  // ---- T4 手机端登录态可用:转账入口弹收款人(登录数据源通了) ----
  await phone
    .getByRole("button", { exact: true, name: "我要转账" })
    .click();
  await phone.locator('[data-testid="payee-row"]').first().waitFor({
    timeout: 15_000,
  });
  await phone.screenshot({ path: shot("t4_手机端登录态可用") });
  console.log("T4 手机端登录态可用(收款人列表拉到): 通过");

  await phoneCtx.close();
  await phone2Ctx.close();
  console.log("E2E MOBILE LOGIN ALL PASSED");
} finally {
  await browser.close();
}
