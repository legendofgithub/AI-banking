// 登录短信验证码(2026-10-27 登录二次校验)端到端实测:
//   第一步账号密码 → 第二步"模拟短信"卡(演示回显码)→ 错码被拒 →
//   「填入」对码 → 进入首页;管理台 8789 的「短信记录」页签可见该条。
// 用法:cd webui && node e2e-login-otp-check.mjs   (需 3000/8800/8789 已起)
// 回退开关:后端 AGENT_LOGIN_OTP=0 时本脚本的第二步不会出现,会在 T2 失败——
// 这正是预期(开关关=单步登录)。

import { mkdirSync } from "node:fs";
import { chromium } from "@playwright/test";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const ADMIN = process.env.ADMIN ?? "http://127.0.0.1:8789";
const PHONE = "13800002233";
const PASSWORD = "Demo@12345";

mkdirSync("../gui-test-login-otp-20261027", { recursive: true });
const shot = (n) => `../gui-test-login-otp-20261027/${n}.png`;

const ok = (m) => console.log(`  ✔ ${m}`);
const fail = (m) => {
  console.error(`  ✘ ${m}`);
  process.exitCode = 1;
};

const browser = await chromium.launch({
  headless: true,
  // 本机不另装 playwright 浏览器,复用系统 Chrome(其余 e2e 同款约束)
  channel: "chrome",
});
const page = await browser.newPage();
page.on("pageerror", (e) => console.log("[pageerror]", String(e).slice(0, 300)));

try {
  // ---- T1 第一步:账号密码 → 出现第二步(不发会话) ----
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.getByRole("textbox", { name: /账号/ }).fill(PHONE);
  await page.getByRole("textbox", { name: /登录密码/ }).fill(PASSWORD);
  await page.getByRole("button", { exact: false, name: /^登录$|登录$/ }).click();
  await page.locator('[data-testid="otp-code-input"]').waitFor({ timeout: 15_000 });
  await page.locator('[data-testid="demo-sms"]').waitFor({ timeout: 5_000 });
  await page.screenshot({ path: shot("t1_第二步验证码") });
  const masked = await page.locator('[data-testid="otp-code-input"]')
    .getAttribute("placeholder");
  ok("T1 第一步后进入第二步(验证码输入 + 模拟短信卡)");

  // ---- T2 错码被拒(带剩余次数提示) ----
  await page.locator('[data-testid="otp-code-input"]').fill("000001");
  await page.getByRole("button", { name: /验证并登录/ }).click();
  await page.locator("text=验证码不正确").waitFor({ timeout: 10_000 });
  ok("T2 错码被拒(提示剩余次数)");

  // ---- T3 「填入」对码 → 登录成功(新范式:桌面端回 /home 第一屏) ----
  await page.locator('[data-testid="demo-sms-fill"]').click();
  await page.getByRole("button", { name: /验证并登录/ }).click();
  await page.waitForURL(/\/home/, { timeout: 30_000 });
  const token = await page.evaluate(
    () => localStorage.getItem("bank-token") ?? ""
  );
  if (token.length < 16) {
    fail("T3 登录成功但 bank-token 未落地");
  } else {
    await page.screenshot({ path: shot("t3_登录成功") });
    ok("T3 模拟短信「填入」→ 验证并登录成功(进入 /home,token 已落地)");
  }

  // ---- T4 管理台「短信记录」页签可见发送记录(在个人管理台二级视图) ----
  const admin = await browser.newPage();
  await admin.goto(`${ADMIN}/`, { waitUntil: "domcontentloaded" });
  await admin.locator("#usersBody .js-enter").first().click();
  await admin.getByRole("button", { name: "短信记录" }).click();
  await admin.locator("#otpBody").waitFor({ timeout: 10_000 });
  const rows = await admin.locator("#otpBody tr").count();
  if (rows < 1) {
    fail("T4 管理台短信记录为空(应至少有一条刚才的发送)");
  } else {
    await admin.screenshot({ path: shot("t4_管理台短信记录") });
    ok(`T4 管理台「短信记录」页签列出 ${rows} 条`);
  }
  await admin.close();
  console.log(`登录二次校验端到端:完成(masked=${masked ?? "?"})`);
} catch (e) {
  fail(String(e).slice(0, 400));
  await page.screenshot({ path: shot("失败现场") }).catch(() => {});
} finally {
  await browser.close();
}
