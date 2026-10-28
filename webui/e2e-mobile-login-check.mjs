// 手机 App 下载码 + 跨设备同步端到端实测(2026-10-27 改版):
//   电脑端登录 → 出 APK 下载二维码(直指 /app/bankagent.apk,浏览器打开即下载)
//   → 手机上下文密码登录(移动端登录会自动加入电脑当前会话)→ 手机看到电脑消息
//   → 手机输入新消息 → 电脑端自动看到(反向同步)。
// 原接力登录票据二维码已从弹窗下线(后端接口与 /qr-login 页保留),
// 对应用例(免密登录/票据一次性)随之移除,同步链路验证不变。
// 用法:cd webui && node e2e-mobile-login-check.mjs   (需 3000/8800 已起)

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
  // ---- T1 电脑端登录,点「手机 App」出下载二维码 ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill(PHONE);
  await page.getByRole("textbox", { name: /登录密码/ }).fill(PASSWORD);
  await page.getByRole("button", { exact: false, name: /登录/ }).click();
  // 两步登录(2026-10-27):第二步验证码——模拟短信卡「填入」→「验证并登录」
  await page.locator('[data-testid="demo-sms-fill"]').click({ timeout: 15_000 });
  await page.getByRole("button", { name: /验证并登录/ }).click();
  // 桌面端登录后落在 /portal(展示门户,无输入框);后续用例都在 /chat 里做
  await page.waitForURL(/\/portal(\/)?$|\/chat/, { timeout: 30_000 });
  await page.goto(`${BASE}/chat`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("textarea", { timeout: 30_000 });
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
      `T1: 「手机 App」按钮不在「后台入口」左边 (mobile右缘=${mb?.x ?? "?"} admin左缘=${ab?.x ?? "?"})`
    );
  }

  // ---- T1b 电脑端建立一个会话并输入内容,供手机扫码后自动加入 ----
  const suffix = String(Date.now()).slice(-6);
  const sessionName = `同步${suffix}`;
  const desktopToken = `桌面暗号${suffix}`;
  const mobileToken = `手机暗号${suffix}`;
  await page.getByRole("button", { name: /新对话/i }).first().click();
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
  if (!qrUrl || !/\/app\/bankagent\.apk$/.test(qrUrl)) {
    throw new Error(`T1: 二维码 URL 异常(应直指 APK): ${qrUrl}`);
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
  await page.screenshot({ path: shot("t1_电脑端App下载码") });
  console.log(`T1 电脑端出码(直指 APK + PNG ${naturalWidth}px): 通过`);

  // T1c 配对码:深链直指 App,server 参数必须还原出局域网 origin
  const pairBox = page.locator('[data-testid="pair-qr"]');
  await pairBox.waitFor({ timeout: 10_000 });
  const pairUrl = await pairBox.getAttribute("data-url");
  if (!pairUrl || !pairUrl.startsWith("aibanking://pair?server=")) {
    throw new Error(`T1c: 配对码 URL 异常: ${pairUrl}`);
  }
  const decoded = decodeURIComponent(pairUrl.split("server=")[1] ?? "");
  if (/127\.0\.0\.1|localhost/.test(decoded)) {
    throw new Error(`T1c: 配对码 server 仍是本机回环地址: ${decoded}`);
  }
  console.log(`T1c 配对码就绪(${decoded}): 通过`);
  // 关掉二维码弹窗,让电脑端回到可交互状态(后续还要在这里看同步消息/切会话)
  await page.keyboard.press("Escape");
  await page
    .locator('[data-testid="mobile-login-dialog"]')
    .waitFor({ state: "hidden", timeout: 5000 });

  // ---- T2 模拟手机(375px 上下文)密码登录:移动端登录会自动加入电脑当前会话 ----
  const phoneCtx = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    userAgent:
      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    viewport: { height: 812, width: 375 },
  });
  const phone = await phoneCtx.newPage();
  phone.setDefaultTimeout(60_000);
  await phone.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await phone.locator('[data-testid="demo-fill-13800002233"]').click();
  await phone.getByRole("button", { name: /登录/ }).click();
  await phone.locator('[data-testid="demo-sms-fill"]').click({
    timeout: 15_000,
  });
  await phone.getByRole("button", { name: /验证并登录/ }).click();
  // 2026-10-28 产品改版:登录统一落 /portal(展示门户),不再自动跳电脑会话;
  // 加入同一会话的路径 = 会话列表点开(这里等效为直接进线程 URL)
  await phone.waitForURL(/\/portal/, { timeout: 30_000 });
  await phone.goto(`${BASE}/chat/${desktopThreadId}`, {
    waitUntil: "domcontentloaded",
  });
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
  await phone.screenshot({ path: shot("t2_手机登录并加入电脑端会话") });
  console.log(
    `T2 手机登录后从会话列表进入电脑会话(昵称=${nickname},看到「${desktopToken}」): 通过`
  );

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
  await page.getByRole("button", { name: /新对话/i }).first().click();
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
  // 2026-10-26 起转账时机缺槽必问:先点「立即转」补槽,确认卡才会出
  await phone
    .getByRole("button", { name: /^立即转$/ })
    .click({ timeout: 60_000 });
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
  console.log("E2E MOBILE APP QR + CROSS-DEVICE SYNC ALL PASSED");
} finally {
  await browser.close();
}
