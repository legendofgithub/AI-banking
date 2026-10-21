// 手机扫码登录 + 跨设备同步端到端实测(2026-10-21):
//   电脑端建会话并发消息 → 出二维码 → 手机扫码登录后自动加入同一会话并看到
//   电脑消息 → 手机输入新消息 → 电脑端自动看到(反向同步)→ 旧票重扫被拒。
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

  // ---- T1b 电脑端建立一个会话并输入内容,供手机扫码后自动加入 ----
  const suffix = String(Date.now()).slice(-6);
  const sessionName = `同步${suffix}`;
  const desktopToken = `桌面暗号${suffix}`;
  const mobileToken = `手机暗号${suffix}`;
  await page.getByRole("button", { name: /New chat/i }).first().click();
  const newChatDialog = page.locator('[data-testid="pc-new-chat-dialog"]');
  await newChatDialog.waitFor({ timeout: 10_000 });
  await page.locator('[data-testid="pc-session-name-input"]').fill(sessionName);
  await page.locator('[data-testid="pc-session-create-button"]').click();
  await newChatDialog.waitFor({ state: "hidden", timeout: 10_000 });
  await page.waitForURL(/\/chat\//, { timeout: 15_000 });
  await page.waitForSelector("textarea", { timeout: 30_000 });
  await page.waitForTimeout(1000);
  const desktopThreadId = page.url().split("/chat/")[1];
  await page.fill("textarea", `请记住 ${desktopToken} 这个暗号`);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(6000); // 等消息落 checkpoint(手机端初始历史要用)
  console.log(`T1b 电脑端会话就绪(${sessionName}/${desktopThreadId.slice(0, 8)}): 通过`);

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
  // 关掉二维码弹窗,让电脑端回到可交互状态(后续还要在这里看同步消息/切会话)
  await page.keyboard.press("Escape");
  await page
    .locator('[data-testid="mobile-login-dialog"]')
    .waitFor({ state: "hidden", timeout: 5000 });

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
  // 扫码登录后应自动落到电脑端当前会话(而不是停在首页)
  await phone.waitForURL(new RegExp(`/chat/${desktopThreadId}`), {
    timeout: 30_000,
  });
  await phone.waitForSelector("textarea", { timeout: 30_000 });
  await phone
    .locator('[data-role="user"]')
    .filter({ hasText: desktopToken })
    .first()
    .waitFor({ timeout: 20_000 });
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
  await phone.screenshot({ path: shot("t2_手机扫码登录并加入电脑端会话") });
  console.log(
    `T2 扫码登录后自动加入电脑端会话(昵称=${nickname},看到「${desktopToken}」): 通过`
  );

  // ---- T3 同一张票再扫:一次性,必须被拒 ----
  const phone2Ctx = await browser.newContext({ viewport: { width: 375, height: 812 } });
  const phone2 = await phone2Ctx.newPage();
  await phone2.goto(qrUrl, { waitUntil: "domcontentloaded" });
  await phone2.waitForSelector("text=/已过期或已被使用|二维码已失效/", {
    timeout: 20_000,
  });
  await phone2.screenshot({ path: shot("t3_旧票重扫被拒") });
  console.log("T3 票据一次性(重扫被拒): 通过");

  // ---- T4 反向同步:手机输入 → 电脑端看到;桌面留在同一会话 ----
  await phone.fill("textarea", `请记住 ${mobileToken} 这个暗号`);
  await phone.keyboard.press("Enter");
  await phone
    .locator('[data-role="user"]')
    .filter({ hasText: mobileToken })
    .first()
    .waitFor({ timeout: 15_000 });
  await page
    .locator('[data-role="user"]')
    .filter({ hasText: mobileToken })
    .first()
    .waitFor({ timeout: 30_000 });
  await page.screenshot({ path: shot("t4_电脑端看到手机输入") });
  console.log(
    `T4 手机输入「${mobileToken}」已同步到电脑端: 通过`
  );

  // ---- T5 电脑端切换/新建会话 → 手机自动跟随 ----
  await page.waitForTimeout(1500);
  const switchedName = `切换${suffix}`;
  await page.getByRole("button", { name: /New chat/i }).first().click();
  await newChatDialog.waitFor({ timeout: 10_000 });
  await page.locator('[data-testid="pc-session-name-input"]').fill(switchedName);
  await page.locator('[data-testid="pc-session-create-button"]').click();
  await newChatDialog.waitFor({ state: "hidden", timeout: 10_000 });
  await page.waitForURL(/\/chat\//, { timeout: 15_000 });
  const switchedThreadId = page.url().split("/chat/")[1];
  await phone.waitForURL(new RegExp(`/chat/${switchedThreadId}`), {
    timeout: 15_000,
  });
  await phone.screenshot({ path: shot("t5_手机跟随电脑切换会话") });
  console.log("T5 电脑切换会话,手机自动跟随: 通过");

  // ---- T6 手机发起转账 → 电脑端同步出现转账确认卡(含支付密码框) ----
  await phone.fill("textarea", "给张三转1元");
  await phone.keyboard.press("Enter");
  await page.waitForSelector('input[aria-label="支付密码"]', {
    timeout: 60_000,
  });
  await page.locator('input[aria-label="支付密码"]').waitFor({
    state: "visible",
    timeout: 10_000,
  });
  await page.screenshot({ path: shot("t6_电脑端同步转账确认卡") });
  // 取消这笔测试转账,避免留下悬空单
  await page.getByRole("button", { exact: true, name: "取消" }).click();
  await page.waitForSelector("text=/取消|已取消/", { timeout: 30_000 });
  console.log("T6 手机发起转账,电脑端同步出现确认卡并可取消: 通过");

  await phoneCtx.close();
  await phone2Ctx.close();
  console.log("E2E MOBILE LOGIN + CROSS-DEVICE SYNC ALL PASSED");
} finally {
  await browser.close();
}
