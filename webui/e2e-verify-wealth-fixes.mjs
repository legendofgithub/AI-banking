// 浏览器实测(评审修复 2026-10-03):理财对比 / 无测评适当性闸门 / 申购幂等重放。
// 用法:cd webui && node e2e-verify-wealth-fixes.mjs
//      PARTS=C node e2e-verify-wealth-fixes.mjs     (只跑指定小节)
// 前置:8788/8789/8800/3000 四个服务已起;无测评测试用户 13900007788 已注册。
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const BASE = process.env.BASE ?? "http://127.0.0.1:3000";
const PARTS = (process.env.PARTS ?? "A,B,C").split(",").map((s) => s.trim().toUpperCase());
const NEW_USER = { id: "13900007788", pw: "Test@12345" };        // 无风险测评
const DEMO = { id: "13800002233", pw: "Demo@12345" };            // 陈明,C3 有测评

const shot = (n) => {
  mkdirSync("gui-test-screenshots", { recursive: true });
  return `gui-test-screenshots/verify_${n}.png`;
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** 等最后一条 assistant 消息满足条件,返回其文本(LLM 措辞不定,只断言关键词)。 */
async function waitReply(page, predicate, { timeout = 120000, label = "" } = {}) {
  const t0 = Date.now();
  let last = "";
  while (Date.now() - t0 < timeout) {
    const n = await page.locator('[data-role="assistant"]').count();
    if (n > 0) {
      last = await page.locator('[data-role="assistant"]').last().innerText();
      if (predicate(last)) return last;
    }
    await sleep(1500);
  }
  throw new Error(`[${label}] 等待回复超时。最后一条 assistant 消息:\n${last.slice(0, 800)}`);
}

async function send(page, text) {
  await page.fill("textarea", text);
  await page.keyboard.press("Enter");
  await sleep(2000);
  if ((await page.locator('[data-role="user"]').count()) === 0) {
    await page.getByRole("button", { name: "Submit" }).click();
  }
}

async function login(page, cred) {
  await page.goto(`${BASE}/login`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("#identifier", { timeout: 30000 });
  await page.fill("#identifier", cred.id);
  await page.fill("#password", cred.pw);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.waitForSelector("textarea", { timeout: 60000 });
  await sleep(3500); // 等水合 + bank-token 落地
}

/** 等金色确认卡上的支付密码框 → 填密码 → 点确认。
 *  注意:已确认过的旧卡片会留在 DOM 里且输入框变 disabled,必须只挑 :enabled 的,
 *  否则第二次调用会抓到上一张卡的死输入框(实测踩坑)。 */
async function payConfirm(page, password = "888888") {
  const box = page.locator('input[aria-label="支付密码"]:enabled').last();
  await box.waitFor({ timeout: 120000 });
  await box.fill(password);
  const btn = page.locator("button:enabled", { hasText: /^确认$/ }).last();
  await btn.click();
  await sleep(1500);
}

const results = [];
const ok = (name, detail = "") => {
  results.push([name, "PASS", detail]);
  console.log(`  PASS  ${name}${detail ? " — " + detail : ""}`);
};

const browser = await chromium.launch({ channel: "chrome", headless: true });

try {
  // ---------------------------------------------------------------- A. 无测评适当性闸门
  if (PARTS.includes("A")) {
    console.log("\n[A] 无风险测评用户申购 R2 产品 → 应被适当性闸门拒绝,且不弹确认卡");
    const ctxA = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const pageA = await ctxA.newPage();
    await login(pageA, NEW_USER);
    await send(pageA, "我要申购5000元的稳健纯债基金");
    const replyA = await waitReply(
      pageA, (t) => t.includes("风险测评") || t.includes("适当性"),
      { label: "A-无测评拒购" });
    const cardA = await pageA.locator('input[aria-label="支付密码"]').count();
    if (cardA !== 0) {
      throw new Error(`A 失败:被适当性拒绝的申购不该出现支付密码确认卡(实际 ${cardA} 个)`);
    }
    ok("A 无测评申购被拒 + 零确认卡", replyA.replace(/\s+/g, " ").slice(0, 90));
    await pageA.screenshot({ path: shot("A_无测评被拒"), fullPage: true });
    await ctxA.close();
  }

  // ------------------------------------------------- B/C 共用陈明会话
  if (PARTS.includes("B") || PARTS.includes("C")) {
    const ctxB = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const pageB = await ctxB.newPage();
    await login(pageB, DEMO);

    // -------------------------------------------------------------- B. 对比理财产品
    if (PARTS.includes("B")) {
      console.log("\n[B] 陈明(C3)对比理财产品 → 必须给出真实产品对比,不是空结果");
      await send(pageB, "帮我对比一下理财产品");
      // 断言"确实对比了多款产品",不绑定具体产品名(演示库产品清单会变)
      const replyB = await waitReply(
        pageB,
        (t) => t.length > 60 && (t.match(/R[1-5]/g) ?? []).length >= 2,
        { label: "B-对比理财" });
      if (/系统错误|稍后再试/.test(replyB)) {
        throw new Error(`B 失败:对比回复里出现错误字样:\n${replyB}`);
      }
      ok("B 对比理财产品返回真实产品", replyB.replace(/\s+/g, " ").slice(0, 110));
      await pageB.screenshot({ path: shot("B_对比理财"), fullPage: true });
    }

    // -------------------------------------------------------------- C. 申购 + 幂等重放
    if (PARTS.includes("C")) {
      console.log("\n[C] 申购 → 支付密码 → 扣款;随后原样重放同一条指令 → 不得二次扣款");
      const INSTR = "我要申购1000元的余额+货币基金";
      await send(pageB, INSTR);
      await payConfirm(pageB);
      const replyC1 = await waitReply(
        pageB, (t) => t.includes("申购") || t.includes("扣"),
        { label: "C1-首次申购" });
      ok("C1 首次申购执行成功", replyC1.replace(/\s+/g, " ").slice(0, 90));
      await pageB.screenshot({ path: shot("C1_申购成功"), fullPage: true });

      await send(pageB, INSTR); // 原样重放同一条指令
      await payConfirm(pageB);
      const replyC2 = await waitReply(
        pageB, (t) => /已执行过|重复|幂等|此前/.test(t),
        { label: "C2-重放幂等" });
      ok("C2 同指令重放被幂等拦截", replyC2.replace(/\s+/g, " ").slice(0, 90));
      await pageB.screenshot({ path: shot("C2_幂等拦截"), fullPage: true });
    }
    await ctxB.close();
  }

  console.log("\n=== 浏览器实测全部通过 ===");
  for (const [n, s] of results) console.log(`${s}  ${n}`);
} catch (err) {
  console.error("\n!!! 浏览器实测失败 !!!\n", err.message);
  process.exitCode = 1;
} finally {
  await browser.close();
}
