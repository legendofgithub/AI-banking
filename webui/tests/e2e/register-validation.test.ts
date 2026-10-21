import { expect, test } from "@playwright/test";

test.describe("Register validation", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/register");
  });

  test("jumps to the invalid field and flashes it twice", async ({ page }) => {
    await page.fill("#real-name", "测试用户");
    await page.fill("#id-card", "110101199003077775"); // 18 位假证件号(校验位错)
    await page.fill("#phone", "123"); // 唯一不合格项
    await page.fill("#password", "Passw0rd!");
    await page.fill("#password-confirm", "Passw0rd!");
    await page.fill("#pay-password", "123456");
    await page.fill("#pay-password-confirm", "123456");

    const submit = page.locator('button[type="submit"]');
    await expect(submit).toBeEnabled();
    await submit.click();

    await page.waitForFunction(() => document.activeElement?.id === "phone");
    await expect(page.locator("#phone")).toHaveAttribute(
      "aria-invalid",
      "true"
    );
    const animation = await page.locator("#phone").evaluate((el) => {
      const style = getComputedStyle(el);
      return {
        iterations: style.animationIterationCount,
        name: style.animationName,
      };
    });
    expect(animation.name).toBe("field-flash");
    expect(animation.iterations).toBe("2");
  });

  test("accepts 18-character demo ID without checksum", async ({ page }) => {
    await page.fill("#id-card", "110101199003077775");
    await expect(page.getByText(/身份证号需为 18 位/)).toHaveCount(0);

    await page.fill("#id-card", "11010119900307777");
    await expect(page.getByText(/身份证号需为 18 位/)).toBeVisible();
  });
});
