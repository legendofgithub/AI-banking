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

  test("mic button hidden on insecure context even when API exists", async ({
    page,
  }) => {
    // 手机经局域网 http 访问的形态:API 在,但非安全上下文拿不到麦克风
    await page.addInitScript(() => {
      class FakeSpeechRecognition {}
      const w = window as unknown as Record<string, unknown>;
      w.SpeechRecognition = FakeSpeechRecognition;
      w.webkitSpeechRecognition = FakeSpeechRecognition;
      Object.defineProperty(window, "isSecureContext", {
        value: false,
        configurable: true,
      });
    });
    await page.goto("/chat");
    await expect(page.getByTestId("mic-button")).toHaveCount(0);
  });

  test("service abort before any transcript shows typing guidance", async ({
    page,
  }) => {
    // Edge 全版本/大陆网络 Chrome 的真实形态:start 后立刻 aborted
    await page.addInitScript(() => {
      class DeadSpeechRecognition {
        lang = "zh-CN";
        continuous = false;
        interimResults = true;
        onresult: ((e: unknown) => void) | null = null;
        onerror: ((e: unknown) => void) | null = null;
        onend: (() => void) | null = null;
        start() {
          setTimeout(() => {
            this.onerror?.({ error: "aborted" });
            this.onend?.();
          }, 50);
        }
        abort() {
          this.onend?.();
        }
      }
      const w = window as unknown as Record<string, unknown>;
      w.SpeechRecognition = DeadSpeechRecognition;
      w.webkitSpeechRecognition = DeadSpeechRecognition;
    });
    await page.goto("/chat");
    const mic = page.getByTestId("mic-button");
    await expect(mic).toBeVisible();
    await mic.click();
    await expect(page.getByText("当前浏览器无法使用语音识别")).toBeVisible();
    // 失败后按钮回到可再次点击的空闲态
    await expect(mic).toHaveAttribute("aria-label", "语音输入");
  });

  test("manual stop does not raise an error toast", async ({ page }) => {
    await page.addInitScript(() => {
      class ListeningSpeechRecognition {
        lang = "zh-CN";
        continuous = false;
        interimResults = true;
        onresult: ((e: unknown) => void) | null = null;
        onerror: ((e: unknown) => void) | null = null;
        onend: (() => void) | null = null;
        start() {
          /* 一直聆听,直到 abort */
        }
        abort() {
          // Chrome 真机行为:手动 abort 也会回调 onerror("aborted")
          this.onerror?.({ error: "aborted" });
          this.onend?.();
        }
      }
      const w = window as unknown as Record<string, unknown>;
      w.SpeechRecognition = ListeningSpeechRecognition;
      w.webkitSpeechRecognition = ListeningSpeechRecognition;
    });
    await page.goto("/chat");
    const mic = page.getByTestId("mic-button");
    await mic.click();
    await expect(mic).toHaveAttribute("aria-label", "停止语音输入");
    await mic.click();
    await expect(mic).toHaveAttribute("aria-label", "语音输入");
    await expect(page.getByText("语音识别失败")).toHaveCount(0);
    await expect(page.getByText("无法使用语音识别")).toHaveCount(0);
  });
});
