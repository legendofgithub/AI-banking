import { expect, test } from "@playwright/test";

/*
 * 语音输入(2026-10-27,Web Speech API)测试:
 * - 注入假 webkitSpeechRecognition(识别器活在页面上下文,只能用 init script 替身):
 *   点麦克风 → onresult 依次给 interim/final → 文本以"录音前前缀+识别结果"
 *   覆盖合并进输入框;未自动发送(无新消息、发送按钮仍使能)、结束即复位;
 * - 不支持 SpeechRecognition 的环境:麦克风按钮不渲染。
 */

const SPOKEN = "帮我看看上个月的账单";

test.describe("voice input", () => {
  test("transcript fills input without auto-send", async ({ page }) => {
    await page.addInitScript((spoken: string) => {
      class FakeSpeechRecognition {
        lang = "zh-CN";
        continuous = false;
        interimResults = true;
        onresult: ((e: unknown) => void) | null = null;
        onerror: ((e: unknown) => void) | null = null;
        onend: (() => void) | null = null;
        start() {
          // 异步吐一段 interim + 一段 final,模拟真实识别节奏
          setTimeout(() => {
            this.onresult?.({
              resultIndex: 0,
              results: [
                {
                  isFinal: false,
                  length: 1,
                  0: { transcript: spoken.slice(0, 4) },
                },
              ],
            });
          }, 50);
          setTimeout(() => {
            this.onresult?.({
              resultIndex: 0,
              results: [{ isFinal: true, length: 1, 0: { transcript: spoken } }],
            });
            this.onend?.();
          }, 150);
        }
        abort() {
          this.onend?.();
        }
      }
      const w = window as unknown as Record<string, unknown>;
      // Chrome 原生自带 SpeechRecognition,hook 优先用它——假识别器必须两个键都盖掉
      w.SpeechRecognition = FakeSpeechRecognition;
      w.webkitSpeechRecognition = FakeSpeechRecognition;
    }, SPOKEN);

    await page.goto("/chat");
    const mic = page.getByTestId("mic-button");
    await expect(mic).toBeVisible();

    // 录音前先有前缀,转写应"前缀+识别文本"覆盖合并(识别器对当前语句
    // 发的是最新累积文本,组件侧不能追加,否则 interim 会叠成乱串)
    const input = page.getByTestId("multimodal-input");
    await input.fill("测试:");
    await mic.click();
    await expect(input).toHaveValue(`测试:${SPOKEN}`);

    // 未自动发送:发送按钮仍可用(若已自动发出,输入清空且按钮禁用)
    await expect(page.getByTestId("send-button")).toBeEnabled();

    // onend 后按钮退出聆听态(红色脉冲消失 → aria-label 回到"语音输入")
    await expect(mic).toHaveAttribute("aria-label", "语音输入");
  });

  test("mic button hidden when speech unsupported", async ({ page }) => {
    await page.addInitScript(() => {
      const w = window as unknown as Record<string, unknown>;
      delete w.SpeechRecognition;
      delete w.webkitSpeechRecognition;
    });
    await page.goto("/chat");
    await expect(page.getByTestId("mic-button")).toHaveCount(0);
  });
});
