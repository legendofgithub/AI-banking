// 排查:真实 Chrome 里模型选择器拿到的是实时清单还是兜底清单。
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

  const btn = page.getByTestId("model-selector");
  await btn.click();
  await page.waitForTimeout(800);

  const names = await page
    .getByRole("option")
    .allTextContents();
  console.log("[selector] 选项数:", names.length);
  console.log("[selector] 选项:", names.map((n) => n.trim()).join(" | "));
  console.log("[button] 按钮当前显示:", (await btn.textContent()).trim());
} finally {
  await browser.close();
}
