import { expect, type Page, test } from "@playwright/test";

async function openModelSettings(page: Page) {
  await page.getByTestId("settings-button").click();
  await page.getByTestId("settings-model").click();
  await expect(page.getByTestId("model-settings-dialog")).toBeVisible();
}

test.describe("Model Settings", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chat");
  });

  test("bottom only shows the connected model info", async ({ page }) => {
    await expect(page.getByTestId("connected-model-info")).toBeVisible();
    // 旧的底部选择器与钥匙入口都不应再出现
    await expect(page.getByTestId("model-selector")).toHaveCount(0);
    await expect(page.getByTestId("api-key-settings")).toHaveCount(0);
  });

  test("opens model settings from the lower-left menu", async ({ page }) => {
    await openModelSettings(page);
    await expect(page.getByTestId("byok-key-input")).toBeVisible();
  });

  test("lists available models", async ({ page }) => {
    await openModelSettings(page);
    const options = page.getByTestId("model-option");
    expect(await options.count()).toBeGreaterThan(0);
  });

  test("selecting a model updates the bottom info", async ({ page }) => {
    await openModelSettings(page);
    const options = page.getByTestId("model-option");
    expect(await options.count()).toBeGreaterThan(0);
    const target =
      (await options.count()) > 1 ? options.nth(1) : options.first();
    const name =
      (await target.getByTestId("model-option-name").textContent())?.trim() ??
      "";
    await target.click();
    await expect(target).toHaveAttribute("aria-pressed", "true");
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("connected-model-info")).toContainText(name);
  });

  test("closes on Escape", async ({ page }) => {
    await openModelSettings(page);
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("model-settings-dialog")).not.toBeVisible();
  });
});
