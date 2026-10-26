import { expect, type Page, test } from "@playwright/test";

async function loginDemo(page: Page) {
  await page.goto("/login");
  await page.getByTestId("demo-fill-13800002233").click();
  await page.locator('button[type="submit"]').click();
  await page.waitForURL("http://localhost:3000/home", { timeout: 15_000 });
  await page.goto("/chat");
}

test.describe("Simulated deposit", () => {
  test("new-user style deposit from settings produces a balance", async ({
    page,
  }) => {
    await loginDemo(page);

    // 首页建议区不再放充钱按钮(功能已收进左下角设置)
    await expect(page.getByText("账户入金(演示)")).toHaveCount(0);
    await page.getByTestId("settings-button").click();
    await page.getByTestId("settings-deposit").click();
    await expect(page.getByTestId("deposit-dialog")).toBeVisible();

    await page.getByTestId("deposit-amount").fill("1");
    await page.getByTestId("deposit-submit").click();
    await expect(page.getByTestId("deposit-success")).toContainText(
      "入金成功",
      { timeout: 15_000 }
    );
    await expect(page.getByTestId("deposit-success")).toContainText("当前余额");
  });
});
