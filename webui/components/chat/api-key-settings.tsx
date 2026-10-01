"use client";

/*
 * BYOK 设置:用户在界面自填智谱 API Key。
 * - Key 存浏览器 localStorage(zai-api-key),只随请求发给自家后端(8800),
 *   永不进入 URL/日志;清除即删。
 * - 「测试连接」调后端 /api/validate-key 拉一次模型清单校验(不耗推理 token)。
 * - 不填则整站走部署方配置的环境变量(演示默认)。
 */

import { KeyRoundIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

const STORAGE_KEY = "zai-api-key";

export function getStoredApiKey(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(STORAGE_KEY) ?? "";
}

function agentBase(): string {
  const port = process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800";
  return `${window.location.protocol}//${window.location.hostname}:${port}`;
}

export function ApiKeySettings() {
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [stored, setStored] = useState("");
  const [status, setStatus] = useState<string>("");
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    if (open) {
      setStored(getStoredApiKey());
      setValue(getStoredApiKey());
      setStatus("");
    }
  }, [open]);

  const handleSave = useCallback(() => {
    const v = value.trim();
    if (!v) {
      toast.error("请先填写 API Key");
      return;
    }
    window.localStorage.setItem(STORAGE_KEY, v);
    setStored(v);
    setStatus("已保存。刷新页面后模型清单按此 Key 显示。");
    toast.success("API Key 已保存");
  }, [value]);

  const handleTest = useCallback(async () => {
    const v = value.trim();
    if (!v) {
      setStatus("请先填写 Key 再测试");
      return;
    }
    setTesting(true);
    setStatus("验证中…");
    try {
      const res = await fetch(`${agentBase()}/api/validate-key`, {
        body: JSON.stringify({ api_key: v }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      const json = await res.json();
      if (json.ok) {
        setStatus(`有效 ✓ 该 Key 可用 ${json.model_count} 个模型`);
      } else {
        setStatus(`无效:${json.error ?? "校验失败"}`);
      }
    } catch (e) {
      setStatus(`无法连接后端:${String(e).slice(0, 80)}`);
    } finally {
      setTesting(false);
    }
  }, [value]);

  const handleClear = useCallback(() => {
    window.localStorage.removeItem(STORAGE_KEY);
    setStored("");
    setValue("");
    setStatus("已清除,将使用部署方配置的 Key");
    toast("已恢复使用部署方配置");
  }, []);

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <Button
          className="h-7 w-7 rounded-lg text-muted-foreground transition-colors hover:text-foreground"
          data-testid="api-key-settings"
          title="API Key 设置"
          variant="ghost"
        >
          <KeyRoundIcon
            className={stored ? "text-emerald-500" : undefined}
            size={14}
          />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 p-3" side="top">
        <div className="flex flex-col gap-2">
          <div className="text-[13px] font-medium">智谱 API Key(可选)</div>
          <div className="text-xs leading-relaxed text-muted-foreground">
            填写后对话使用你自己的 Key;留空使用部署方配置。
            {stored
              ? ` 当前已配置:···${stored.slice(-4)}`
              : " 当前未配置。"}
          </div>
          <Input
            className="h-8 text-xs"
            onChange={(e) => setValue(e.target.value)}
            placeholder="粘贴 API Key,形如 id.secret"
            type="password"
            value={value}
          />
          <div className="flex gap-2">
            <Button
              className="h-7 flex-1 text-xs"
              onClick={handleSave}
              size="sm"
              variant="default"
            >
              保存
            </Button>
            <Button
              className="h-7 flex-1 text-xs"
              disabled={testing}
              onClick={handleTest}
              size="sm"
              variant="outline"
            >
              {testing ? "验证中…" : "测试连接"}
            </Button>
            {stored ? (
              <Button
                className="h-7 text-xs"
                onClick={handleClear}
                size="sm"
                variant="ghost"
              >
                清除
              </Button>
            ) : null}
          </div>
          {status ? (
            <div className="text-xs text-muted-foreground">{status}</div>
          ) : null}
        </div>
      </PopoverContent>
    </Popover>
  );
}
