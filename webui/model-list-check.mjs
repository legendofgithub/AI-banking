// 排查:真实 Chrome 里「设置 → 大模型」拿到的是实时清单还是兜底清单。
// 用法:cd webui && node model-list-check.mjs [baseURL]
import { chromium } from "@playwright/test";

const BASE = process.argv[2] ?? "http://localhost:3000";

const browser = await chromium.launch({ channel: "chrome", headless: true });
const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });

let modelsResponse = null;
page.on("response", async (res) => {
  if (res.url().includes("/api/models")) {
    try {
      const json = await res.json();
      modelsResponse = json;
      console.log(
        `[network] /api/models ->`,
        res.status(),
        `models: ${json?.models?.length ?? "无models字段"}`,
        json?.models ? `(${json.models.map((m) => m.id).join(", ")})` : ""
      );
    } catch (e) {
      console.log("[network] /api/models 解析失败:", String(e).slice(0, 120));
    }
  }
});
page.on("requestfailed", (req) => {
  if (req.url().includes("/api/models")) {
    console.log("[requestfailed] /api/models:", req.failure()?.errorText);
  }
});
page.on("console", (m) => {
  if (m.type() === "error") console.log("[console.error]", m.text().slice(0, 200));
});

try {
  await page.goto(`${BASE}/`, { waitUntil: "domcontentloaded" });
  await page.waitForSelector("textarea", { timeout: 20000 });
  await page.waitForTimeout(4000); // 等 SWR 拉取 + 水合

  await page.getByTestId("settings-button").click();
  await page.getByTestId("settings-model").click();
  await page.waitForSelector("[data-testid='model-settings-dialog']", { timeout: 10000 });
  await page.waitForTimeout(800);

  const legacy = await page
    .locator("[data-testid='model-selector'], [data-testid='api-key-settings']")
    .count();
  if (legacy !== 0) {
    throw new Error(`底部仍残留旧模型选择器/钥匙入口: ${legacy}`);
  }

  const options = page.getByTestId("model-option");
  const names = await options.allTextContents();
  console.log("[model settings] 选项数:", names.length);
  console.log("[model settings] 选项:", names.map((n) => n.trim().replace(/\s+/g, " ")).join(" | "));
  if (names.length === 0) {
    throw new Error("大模型设置里没有任何可选模型");
  }

  // 选第二个(或唯一一个)模型,关闭弹窗后底部信息应同步
  const target = names.length > 1 ? options.nth(1) : options.first();
  const targetName = (await target.getByTestId("model-option-name").textContent()).trim();
  await target.click();
  await page.keyboard.press("Escape");
  await page.waitForTimeout(300);
  const bottom = (await page.getByTestId("connected-model-info").textContent()).trim();
  console.log("[bottom] 当前显示:", bottom);
  if (!bottom.includes(targetName)) {
    throw new Error(`底部模型信息未同步: 期望包含 ${targetName},实际 ${bottom}`);
  }
  console.log("[check] 设置弹窗切换模型 -> 底部信息同步: 通过");
} finally {
  await browser.close();
}
