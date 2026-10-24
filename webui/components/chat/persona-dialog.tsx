"use client";

/*
 * 「人设」编辑弹窗（2026-10-24）：
 * 用户给 Agent 设定性格/说话方式/温度（≤300 字文本 + 0-1 temperature）。
 * 人设只在播报与闲聊时注入（语气/称呼/详略），带从属规则——不得改变任何
 * 事实数字与状态；规划开始时 a_perceive 会读取并展示在思维链与计划卡🎭行。
 *
 * 入口：左下角设置 → 人设。window 事件 open-persona 打开
 * （弹窗挂在 chat layout，与充钱/绑卡/记忆同款链路）。
 */

import { SparklesIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

const MAX_CHARS = 300;

type PersonaResponse = {
  persona?: string;
  temperature?: number;
  is_default?: boolean;
  error?: string;
};

export function PersonaDialog() {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [temperature, setTemperature] = useState(0.1);
  const [isDefault, setIsDefault] = useState(true);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [needLogin, setNeedLogin] = useState(false);

  useEffect(() => {
    const openPersona = () => setOpen(true);
    window.addEventListener("open-persona", openPersona);
    return () => window.removeEventListener("open-persona", openPersona);
  }, []);

  const load = useCallback(async () => {
    if (!getStoredToken()) {
      setNeedLogin(true);
      return;
    }
    setNeedLogin(false);
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/persona`, {
        headers: bankAuthHeaders(),
        cache: "no-store",
      });
      const body: PersonaResponse = await res.json();
      if (!res.ok) {
        setError(body.error || "读取人设失败");
        return;
      }
      setText(body.persona ?? "");
      setTemperature(
        typeof body.temperature === "number" ? body.temperature : 0.1,
      );
      setIsDefault(Boolean(body.is_default));
    } catch {
      setError("读取人设失败，请稍后再试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  const save = useCallback(
    async (nextText: string, nextTemp: number) => {
      setSaving(true);
      try {
        const res = await fetch(`${bankAgentBase()}/api/persona`, {
          method: "PUT",
          headers: { "Content-Type": "application/json", ...bankAuthHeaders() },
          body: JSON.stringify({
            persona: nextText.slice(0, MAX_CHARS),
            temperature: nextTemp,
          }),
        });
        const body: PersonaResponse = await res.json();
        if (!res.ok) {
          toast.error(body.error || "保存失败，请稍后再试");
          return;
        }
        setText(body.persona ?? "");
        setIsDefault(Boolean(body.is_default));
        // 反馈必须能自证发生了什么:带内容摘要,空文本明说是"清空",
        // 绝不用含糊的"恢复默认"让用户猜有没有存上
        if (body.is_default) {
          toast.success("已清空人设，Agent 将按默认风格回复");
        } else {
          const brief = (body.persona ?? "").slice(0, 16);
          toast.success(`人设已保存：「${brief}${(body.persona ?? "").length > 16 ? "…" : ""}」，下次回复生效`);
        }
      } catch {
        toast.error("保存失败，请稍后再试");
      } finally {
        setSaving(false);
      }
    },
    [],
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="max-h-[85vh] w-[92vw] max-w-md overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <SparklesIcon className="size-4 text-amber-500" />
            人设
          </DialogTitle>
          <DialogDescription className="text-xs leading-5">
            给 Agent 设定性格、说话方式与温度。只影响语气和详略，
            <span className="text-amber-600 dark:text-amber-400">
              不会改变任何事实数字与状态
            </span>
            ；规划时会在思维链与计划卡上标明当前人设。
          </DialogDescription>
        </DialogHeader>

        {needLogin ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            登录网银账号后才能设置人设。
          </p>
        ) : loading ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            正在读取人设…
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
              当前状态：
              <Badge
                className={
                  isDefault
                    ? "h-5 px-1.5 text-[10px]"
                    : "h-5 border-amber-500/40 bg-amber-500/15 px-1.5 text-[10px] text-amber-700 dark:text-amber-300"
                }
                variant="secondary"
              >
                {isDefault ? "默认风格（未设人设）" : "自定人设生效中"}
              </Badge>
            </div>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between text-[12px] text-muted-foreground">
                <span>人设描述（空 = 不使用人设）</span>
                <span>
                  {text.length}/{MAX_CHARS}
                </span>
              </div>
              <Textarea
                className="min-h-[110px] text-[13px] leading-5"
                data-testid="persona-textarea"
                maxLength={MAX_CHARS}
                onChange={(e) => setText(e.target.value)}
                placeholder={
                  "例如：你说话像一位沉稳的银行管家，称呼我为「陈先生」，先给结论再讲细节，规划时优先稳妥方案、主动提示风险与限额。"
                }
                value={text}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between text-[12px] text-muted-foreground">
                <span>温度（低=严谨稳定，高=活泼多样）</span>
                <span className="font-medium text-foreground">
                  {temperature.toFixed(1)}
                </span>
              </div>
              <input
                aria-label="温度"
                className="h-1.5 w-full cursor-pointer appearance-none rounded-full bg-muted accent-amber-500"
                data-testid="persona-temperature"
                max={1}
                min={0}
                onChange={(e) => setTemperature(Number(e.target.value))}
                step={0.1}
                type="range"
                value={temperature}
              />
            </div>

            {error ? (
              <p className="text-center text-sm text-red-500">{error}</p>
            ) : null}

            <div className="flex items-center justify-between gap-2 pt-1">
              <Button
                className="h-8 rounded-lg px-3 text-[12px] text-muted-foreground hover:text-red-500"
                data-testid="persona-reset"
                disabled={saving || (isDefault && !text)}
                onClick={() => {
                  setText("");
                  setTemperature(0.1);
                  void save("", 0.1);
                }}
                variant="ghost"
              >
                恢复默认
              </Button>
              <Button
                className="h-8 rounded-lg bg-amber-500/90 px-4 text-[13px] text-white hover:bg-amber-500"
                data-testid="persona-save"
                disabled={saving}
                onClick={() => void save(text, temperature)}
              >
                {saving ? "保存中…" : "保存"}
              </Button>
            </div>

            <p className="text-[11px] leading-4 text-muted-foreground">
              人设按账号保存（本机演示库）；下次对话开始生效。
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
