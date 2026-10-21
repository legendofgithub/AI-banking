"use client";

/*
 * 大模型设置弹窗(2026-10-21):
 * 从输入框底部的「模型选择器 + 钥匙图标」整体迁到左下角设置。
 * 一个弹窗管两件事:
 *   1) 模型管理:列出当前端点可用模型,点选即全局生效(写 chat-model cookie);
 *   2) 外接模型配置:BYOK API Key 的保存/测试/清除,留空走部署方配置。
 * 底部输入区只保留「当前已连接模型」的只读信息,不再承载配置入口。
 */

import { CheckIcon, KeyRoundIcon } from "lucide-react";
import { type ChangeEvent, useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { ModelSelectorLogo } from "@/components/ai-elements/model-selector";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { bankAgentBase } from "@/hooks/use-bank-auth";
import { useChatModel } from "@/hooks/use-chat-model";
import {
  clearStoredApiKey,
  getStoredApiKey,
  setStoredApiKey,
} from "@/lib/ai/byok";
import type { ChatModel } from "@/lib/ai/models";
import { cn } from "@/lib/utils";

function ModelOption({
  isSelected,
  model,
  onSelect,
}: {
  isSelected: boolean;
  model: ChatModel;
  onSelect: (modelId: string) => void;
}) {
  const handleSelect = useCallback(() => {
    onSelect(model.id);
  }, [model.id, onSelect]);

  return (
    <button
      aria-pressed={isSelected}
      className={cn(
        "flex w-full items-center gap-2 rounded-lg border px-2.5 py-2 text-left transition-colors",
        isSelected
          ? "border-amber-500/50 bg-amber-500/10"
          : "border-transparent hover:bg-muted/60"
      )}
      data-model-id={model.id}
      data-testid="model-option"
      onClick={handleSelect}
      type="button"
    >
      <ModelSelectorLogo provider={model.provider} />
      <span className="min-w-0 flex-1">
        <span
          className="block truncate text-[13px]"
          data-testid="model-option-name"
        >
          {model.name}
        </span>
        <span className="block truncate text-[11px] text-muted-foreground">
          {model.description}
        </span>
      </span>
      {isSelected ? (
        <CheckIcon className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
      ) : null}
    </button>
  );
}

export function ModelSettingsDialog() {
  const {
    currentModelId,
    isLoading,
    modelSettingsOpen,
    models,
    refreshModels,
    selectModel,
    selectedModel,
    setModelSettingsOpen,
  } = useChatModel();

  const [keyValue, setKeyValue] = useState("");
  const [storedKey, setStoredKey] = useState("");
  const [status, setStatus] = useState("");
  const [testing, setTesting] = useState(false);

  // 每次打开都从 localStorage 重新读,避免上次未保存的输入串场
  useEffect(() => {
    if (!modelSettingsOpen) {
      return;
    }
    const stored = getStoredApiKey();
    setStoredKey(stored);
    setKeyValue(stored);
    setStatus("");
    setTesting(false);
  }, [modelSettingsOpen]);

  const handleKeyChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setKeyValue(event.target.value);
    },
    []
  );

  const handleSave = useCallback(() => {
    const value = keyValue.trim();
    if (!value) {
      toast.error("请先填写 API Key");
      return;
    }
    setStoredApiKey(value);
    setStoredKey(value);
    refreshModels();
    setStatus("已保存,正在按新 Key 刷新模型清单…");
    toast.success("API Key 已保存");
  }, [keyValue, refreshModels]);

  const handleTest = useCallback(async () => {
    const value = keyValue.trim();
    if (!value) {
      setStatus("请先填写 Key 再测试");
      return;
    }
    setTesting(true);
    setStatus("验证中…");
    try {
      const res = await fetch(`${bankAgentBase()}/api/validate-key`, {
        body: JSON.stringify({ api_key: value }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      const json = await res.json();
      if (json.ok) {
        setStatus(`有效 ✓ 该 Key 可用 ${json.model_count} 个模型`);
      } else {
        setStatus(`无效:${json.error ?? "校验失败"}`);
      }
    } catch (error) {
      setStatus(`无法连接后端:${String(error).slice(0, 80)}`);
    } finally {
      setTesting(false);
    }
  }, [keyValue]);

  const handleClear = useCallback(() => {
    clearStoredApiKey();
    setStoredKey("");
    setKeyValue("");
    refreshModels();
    setStatus("已清除,将使用部署方配置的 Key");
    toast("已恢复使用部署方配置");
  }, [refreshModels]);

  return (
    <Dialog onOpenChange={setModelSettingsOpen} open={modelSettingsOpen}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-md"
        data-testid="model-settings-dialog"
      >
        <DialogHeader>
          <DialogTitle>大模型设置</DialogTitle>
          <DialogDescription>
            选择对话使用的模型,或填写自己的外接模型 API Key;留空使用部署方配置。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5">
          <section>
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-[13px] font-medium">模型</span>
              <span className="truncate text-[11px] text-muted-foreground">
                {isLoading ? "正在读取模型清单…" : `当前:${selectedModel.name}`}
              </span>
            </div>
            <div className="flex max-h-60 flex-col gap-1 overflow-y-auto pr-1">
              {models.map((model) => (
                <ModelOption
                  isSelected={model.id === currentModelId}
                  key={model.id}
                  model={model}
                  onSelect={selectModel}
                />
              ))}
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
              清单来自当前配置的端点;切换后对下一条消息生效。
            </p>
          </section>

          <Separator />

          <section>
            <div className="mb-1.5 flex items-center gap-1.5 text-[13px] font-medium">
              <KeyRoundIcon className="size-3.5" />
              外接模型 API Key(可选)
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              填写后使用你自己的 Key 拉取模型清单并发起对话;留空使用部署方配置。
              {storedKey
                ? ` 当前已配置:···${storedKey.slice(-4)}`
                : " 当前未配置。"}
            </p>
            <Input
              className="mt-2 h-8 text-xs"
              data-testid="byok-key-input"
              onChange={handleKeyChange}
              placeholder="粘贴 API Key"
              type="password"
              value={keyValue}
            />
            <div className="mt-2 flex gap-2">
              <Button
                className="h-7 flex-1 text-xs"
                data-testid="byok-save"
                onClick={handleSave}
                size="sm"
                type="button"
              >
                保存
              </Button>
              <Button
                className="h-7 flex-1 text-xs"
                data-testid="byok-test"
                disabled={testing}
                onClick={handleTest}
                size="sm"
                type="button"
                variant="outline"
              >
                {testing ? "验证中…" : "测试连接"}
              </Button>
              {storedKey ? (
                <Button
                  className="h-7 text-xs"
                  data-testid="byok-clear"
                  onClick={handleClear}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  清除
                </Button>
              ) : null}
            </div>
            {status ? (
              <p
                className="mt-2 text-xs leading-relaxed text-muted-foreground"
                data-testid="byok-status"
              >
                {status}
              </p>
            ) : null}
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
