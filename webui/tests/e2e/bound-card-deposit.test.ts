import { expect, type Page, test } from "@playwright/test";

async function loginDemo(page: Page) {
  await page.goto("/login");
  await page.getByTestId("demo-fill-13800002233").click();
  await page.locator('button[type="submit"]').click();
  await page.waitForURL("http://localhost:3000/", { timeout: 15_000 });
}

async function openSettingsItem(page: Page, testId: string) {
  await page.getByTestId("settings-button").click();
  await page.getByTestId(testId).click();
}

test.describe("Bound bank card demo", () => {
  test("bind card, deposit from its virtual balance, and unbind", async ({
    page,
  }) => {
    const last4 = String(Date.now()).slice(-4).padStart(4, "0");
    const cardNo = `6225 8800 0000 ${last4}`;
    await loginDemo(page);

    // 绑定
    await openSettingsItem(page, "settings-bound-cards");
    await expect(page.getByTestId("bound-cards-dialog")).toBeVisible();
    await page.getByTestId("bound-bank-name").fill("招商银行");
    await page.getByTestId("bound-card-no").fill(cardNo);
    await page.getByTestId("bound-holder-name").fill("陈明");
    await page.getByTestId("bound-card-submit").click();
    await expect(
      page.getByTestId("bound-card-item").filter({ hasText: last4 })
    ).toContainText("50000.00", { timeout: 15_000 });

    // 从这张卡充钱
    await page.keyboard.press("Escape");
    await openSettingsItem(page, "settings-deposit");
    await expect(page.getByTestId("deposit-dialog")).toBeVisible();
    const source = page.getByTestId("deposit-source");
    const options = await source.locator("option").allTextContents();
    const index = options.findIndex((text) => text.includes(last4));
    expect(index).toBeGreaterThan(0);
    await source.selectOption({ index });
    await page.getByTestId("deposit-amount").fill("1");
    await page.getByTestId("deposit-submit").click();
    await expect(page.getByTestId("deposit-success")).toContainText(
      "银行卡余额 49999.00",
      { timeout: 15_000 }
    );

    // 绑卡信息里也能看到扣减后的虚拟余额
    await page.keyboard.press("Escape");
    await openSettingsItem(page, "settings-bound-cards");
    await expect(
      page.getByTestId("bound-card-item").filter({ hasText: last4 })
    ).toContainText("49999.00", { timeout: 15_000 });

    // 解绑(两段式确认)
    const row = page.getByTestId("bound-card-item").filter({ hasText: last4 });
    await row.getByTestId("bound-card-unbind").click();
    await row.getByTestId("bound-card-unbind-confirm").click();
    await expect(
      page.getByTestId("bound-card-item").filter({ hasText: last4 })
    ).toHaveCount(0, { timeout: 15_000 });
  });
});
