import { expect, test } from "@playwright/test";

test.describe("Login demo accounts", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/login");
  });

  test("lists demo accounts with phone and password", async ({ page }) => {
    await expect(page.getByTestId("demo-account-list")).toBeVisible();
    await expect(page.getByText("13800002233")).toBeVisible();
    await expect(page.getByText("Demo@12345")).toBeVisible();
    await expect(page.getByText("13900007788")).toBeVisible();
    await expect(page.getByText("Test@12345")).toBeVisible();
  });

  test("one-click fill populates both login fields", async ({ page }) => {
    await page.getByTestId("demo-fill-13800002233").click();
    await expect(page.locator("#identifier")).toHaveValue("13800002233");
    await expect(page.locator("#password")).toHaveValue("Demo@12345");

    await page.getByTestId("demo-fill-13900007788").click();
    await expect(page.locator("#identifier")).toHaveValue("13900007788");
    await expect(page.locator("#password")).toHaveValue("Test@12345");
  });
});
