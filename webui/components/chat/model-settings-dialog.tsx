"use client";

/*
 * 大模型管理弹窗(2026-10-21,多供应商版):
 * - 从设置进入,管理"本机模型供应商连接":供应商/Key/Base URL/连接状态;
 * - 连接信息全部落 localStorage,重登/重启浏览器后仍在,不用反复填 Key;
 * - 右上角「+ 新增供应商」:选 DeepSeek/OpenAI/Claude/自定义,填 Key 与
 *   Base URL,后端真实拉一次模型清单,成功才保存并标记「已连接」;
 * - 当前连接决定聊天请求和模型清单;也可一键改回部署方 .env.local 配置。
 */

import {
  CheckCircle2Icon,
  KeyRoundIcon,
  PlusIcon,
  RefreshCwIcon,
  Trash2Icon,
} from "lucide-react";
import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useState,
} from "react";
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
  getActiveConnectionId,
  getConnections,
  LLM_PROVIDER_PRESETS,
  type LlmConnection,
  type LlmProviderId,
  maskApiKey,
  providerLabel,
  removeConnection,
  saveConnection,
  setActiveConnection,
} from "@/lib/ai/byok";
import type { ChatModel } from "@/lib/ai/models";
import { cn } from "@/lib/utils";

const PROVIDER_ORDER: LlmProviderId[] = [
  "deepseek",
  "openai",
  "anthropic",
  "custom",
];

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
        <CheckCircle2Icon className="size-4 shrink-0 text-amber-600 dark:text-amber-400" />
      ) : null}
    </button>
  );
}

function providerIcon(provider: LlmProviderId): string {
  return provider === "custom" ? "openai" : provider;
}

function ConnectionCard({
  active,
  busy,
  connection,
  onActivate,
  onRemove,
  onTest,
}: {
  active: boolean;
  busy: boolean;
  connection: LlmConnection;
  onActivate: (id: string) => void;
  onRemove: (id: string) => void;
  onTest: (id: string) => void;
}) {
  const handleActivate = useCallback(() => {
    onActivate(connection.id);
  }, [connection.id, onActivate]);
  const handleTest = useCallback(() => {
    onTest(connection.id);
  }, [connection.id, onTest]);
  const handleRemove = useCallback(() => {
    onRemove(connection.id);
  }, [connection.id, onRemove]);

  return (
    <div
      className={cn(
        "rounded-xl border p-3 transition-colors",
        active
          ? "border-amber-500/50 bg-amber-500/[0.06]"
          : "border-border/60 bg-muted/20"
      )}
      data-active={active}
      data-connection-id={connection.id}
      data-testid="llm-connection"
    >
      <div className="flex items-start gap-2.5">
        <ModelSelectorLogo provider={providerIcon(connection.provider)} />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="truncate text-[13px] font-medium">
              {connection.label}
            </span>
            {active ? (
              <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
                当前使用
              </span>
            ) : null}
            <span
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[10px]",
                connection.status === "connected"
                  ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                  : "bg-muted text-muted-foreground"
              )}
              data-testid="llm-status"
            >
              <span
                className={cn(
                  "size-1.5 rounded-full",
                  connection.status === "connected"
                    ? "bg-emerald-500"
                    : "bg-muted-foreground/50"
                )}
              />
              {connection.status === "connected" ? "已连接" : "未验证"}
              {connection.status === "connected" &&
              typeof connection.modelCount === "number"
                ? ` · ${connection.modelCount} 个模型`
                : ""}
            </span>
          </div>
          <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
            {connection.baseUrl || "部署方默认端点"}
          </div>
          <div className="mt-0.5 flex items-center gap-1 text-[11px] text-muted-foreground">
            <KeyRoundIcon className="size-3" />
            {maskApiKey(connection.apiKey)}
          </div>
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {active ? null : (
          <Button
            className="h-7 text-[11px]"
            data-testid="llm-activate"
            disabled={busy}
            onClick={handleActivate}
            size="sm"
            type="button"
            variant="outline"
          >
            设为当前
          </Button>
        )}
        <Button
          className="h-7 text-[11px]"
          data-testid="llm-test"
          disabled={busy}
          onClick={handleTest}
          size="sm"
          type="button"
          variant="outline"
        >
          <RefreshCwIcon className={cn("size-3", busy && "animate-spin")} />
          {busy ? "测试中…" : "测试连接"}
        </Button>
        <Button
          aria-label={`删除 ${connection.label}`}
          className="ml-auto h-7 text-[11px] text-muted-foreground"
          data-testid="llm-remove"
          disabled={busy}
          onClick={handleRemove}
          size="sm"
          type="button"
          variant="ghost"
        >
          <Trash2Icon className="size-3.5" />
        </Button>
      </div>
    </div>
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

  const [connections, setConnections] = useState<LlmConnection[]>([]);
  const [activeId, setActiveId] = useState("");
  const [formOpen, setFormOpen] = useState(false);
  const [provider, setProvider] = useState<LlmProviderId>("deepseek");
  const [baseUrl, setBaseUrl] = useState(LLM_PROVIDER_PRESETS.deepseek.baseUrl);
  const [apiKey, setApiKey] = useState("");
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState("");

  const reload = useCallback(() => {
    setConnections(getConnections());
    setActiveId(getActiveConnectionId());
  }, []);

  // 每次打开都从 localStorage 重读,并收起上一次未提交的新增表单
  useEffect(() => {
    if (!modelSettingsOpen) {
      return;
    }
    reload();
    setFormOpen(false);
    setFormError("");
    setTestingId("");
  }, [modelSettingsOpen, reload]);

  const openAddForm = useCallback(() => {
    setProvider("deepseek");
    setBaseUrl(LLM_PROVIDER_PRESETS.deepseek.baseUrl);
    setApiKey("");
    setFormError("");
    setFormOpen(true);
  }, []);

  const closeAddForm = useCallback(() => {
    setFormOpen(false);
    setFormError("");
  }, []);

  const handleProviderChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      const next = event.target.value as LlmProviderId;
      setProvider(next);
      setBaseUrl(LLM_PROVIDER_PRESETS[next]?.baseUrl ?? "");
      setFormError("");
    },
    []
  );

  const handleApiKeyChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setApiKey(event.target.value);
    },
    []
  );

  const handleBaseUrlChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setBaseUrl(event.target.value);
    },
    []
  );

  const handleAddConnection = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const key = apiKey.trim();
      const base = baseUrl.trim().replace(/\/+$/, "");
      if (!key) {
        setFormError("请填写 API Key");
        return;
      }
      if (!base) {
        setFormError("请填写 Base URL");
        return;
      }
      if (!/^https?:\/\/.+/i.test(base)) {
        setFormError("Base URL 必须是 http(s) 开头的完整地址");
        return;
      }
      setSaving(true);
      setFormError("");
      try {
        const res = await fetch(`${bankAgentBase()}/api/validate-key`, {
          body: JSON.stringify({
            api_key: key,
            base_url: base,
            provider,
          }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res.json()) as {
          error?: string;
          model_count?: number;
          ok?: boolean;
        };
        if (!json.ok) {
          setFormError(json.error || "连接失败,请检查 Key 与 Base URL");
          return;
        }
        const now = new Date().toISOString();
        const connection: LlmConnection = {
          apiKey: key,
          baseUrl: base,
          connectedAt: now,
          createdAt: now,
          id:
            typeof crypto !== "undefined" && "randomUUID" in crypto
              ? crypto.randomUUID()
              : `llm-${Date.now()}`,
          label: providerLabel(provider),
          modelCount: json.model_count ?? 0,
          provider,
          status: "connected",
        };
        saveConnection(connection);
        setActiveConnection(connection.id);
        reload();
        refreshModels();
        setFormOpen(false);
        toast.success(
          `${connection.label} 已连接${
            connection.modelCount ? `,发现 ${connection.modelCount} 个模型` : ""
          }`
        );
      } catch {
        setFormError("无法连接服务器,请稍后重试");
      } finally {
        setSaving(false);
      }
    },
    [apiKey, baseUrl, provider, refreshModels, reload]
  );

  const handleActivate = useCallback(
    (id: string) => {
      setActiveConnection(id);
      reload();
      refreshModels();
      toast.success("已切换到该模型连接");
    },
    [refreshModels, reload]
  );

  const handleTest = useCallback(
    async (id: string) => {
      const connection = getConnections().find((item) => item.id === id);
      if (!connection) {
        return;
      }
      setTestingId(id);
      try {
        const res = await fetch(`${bankAgentBase()}/api/validate-key`, {
          body: JSON.stringify({
            api_key: connection.apiKey,
            base_url: connection.baseUrl,
            provider: connection.provider,
          }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res.json()) as {
          error?: string;
          model_count?: number;
          ok?: boolean;
        };
        if (!json.ok) {
          saveConnection({ ...connection, status: "unverified" });
          reload();
          toast.error(json.error || "连接失败");
          return;
        }
        saveConnection({
          ...connection,
          connectedAt: new Date().toISOString(),
          modelCount: json.model_count ?? 0,
          status: "connected",
        });
        reload();
        refreshModels();
        toast.success(`连接正常,可用 ${json.model_count ?? 0} 个模型`);
      } catch {
        saveConnection({ ...connection, status: "unverified" });
        reload();
        toast.error("无法连接服务器");
      } finally {
        setTestingId("");
      }
    },
    [refreshModels, reload]
  );

  const handleRemove = useCallback(
    (id: string) => {
      removeConnection(id);
      reload();
      refreshModels();
      toast("已删除该供应商连接");
    },
    [refreshModels, reload]
  );

  const handleUseDeployment = useCallback(() => {
    setActiveConnection(null);
    reload();
    refreshModels();
    toast("已改用部署方默认配置");
  }, [refreshModels, reload]);

  return (
    <Dialog onOpenChange={setModelSettingsOpen} open={modelSettingsOpen}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-md"
        data-testid="model-settings-dialog"
      >
        <DialogHeader>
          <DialogTitle>大模型管理</DialogTitle>
          <DialogDescription>
            接入你自己的模型供应商;连接信息保存在本机浏览器,重新登录无需再填。
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-5">
          <section>
            <div className="mb-2 flex items-center justify-between gap-2">
              <span className="text-[13px] font-medium">模型供应商</span>
              {formOpen ? null : (
                <Button
                  className="h-7 text-[11px]"
                  data-testid="llm-add-provider"
                  onClick={openAddForm}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <PlusIcon className="size-3.5" />
                  新增供应商
                </Button>
              )}
            </div>

            {formOpen ? (
              <form
                className="mb-2 flex flex-col gap-2.5 rounded-xl border border-amber-500/40 bg-amber-500/[0.04] p-3"
                data-testid="llm-add-form"
                onSubmit={handleAddConnection}
              >
                <div className="flex items-center justify-between">
                  <span className="text-[12px] font-medium">新增供应商</span>
                  <button
                    className="text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                    onClick={closeAddForm}
                    type="button"
                  >
                    取消
                  </button>
                </div>

                <label
                  className="flex flex-col gap-1 text-[11px] text-muted-foreground"
                  htmlFor="llm-provider-select"
                >
                  供应商
                  <select
                    className="h-8 w-full rounded-lg border border-input bg-input/30 px-2 text-[12px] text-foreground outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                    data-testid="llm-provider-select"
                    id="llm-provider-select"
                    onChange={handleProviderChange}
                    value={provider}
                  >
                    {PROVIDER_ORDER.map((id) => (
                      <option key={id} value={id}>
                        {LLM_PROVIDER_PRESETS[id].label}
                      </option>
                    ))}
                  </select>
                </label>

                <label
                  className="flex flex-col gap-1 text-[11px] text-muted-foreground"
                  htmlFor="llm-api-key"
                >
                  API Key
                  <Input
                    autoComplete="off"
                    className="h-8 rounded-lg text-xs"
                    data-testid="llm-api-key"
                    id="llm-api-key"
                    onChange={handleApiKeyChange}
                    placeholder="粘贴供应商的 API Key"
                    type="password"
                    value={apiKey}
                  />
                </label>

                <label
                  className="flex flex-col gap-1 text-[11px] text-muted-foreground"
                  htmlFor="llm-base-url"
                >
                  Base URL
                  <Input
                    autoComplete="off"
                    className="h-8 rounded-lg font-mono text-xs"
                    data-testid="llm-base-url"
                    id="llm-base-url"
                    onChange={handleBaseUrlChange}
                    placeholder="https://api.example.com/v1"
                    value={baseUrl}
                  />
                </label>

                <p className="text-[10px] leading-relaxed text-muted-foreground">
                  {LLM_PROVIDER_PRESETS[provider].hint}
                  。保存前会真实拉取一次模型清单验证连接。
                </p>
                {formError ? (
                  <p
                    className="rounded-md bg-red-50 px-2 py-1.5 text-[11px] text-red-600 dark:bg-red-950/40 dark:text-red-300"
                    data-testid="llm-form-error"
                  >
                    {formError}
                  </p>
                ) : null}
                <Button
                  className="h-8 w-full text-xs"
                  data-testid="llm-save"
                  disabled={saving}
                  size="sm"
                  type="submit"
                >
                  {saving ? "连接中…" : "测试并保存连接"}
                </Button>
              </form>
            ) : null}

            <div className="flex flex-col gap-2">
              {connections.map((connection) => (
                <ConnectionCard
                  active={connection.id === activeId}
                  busy={testingId === connection.id}
                  connection={connection}
                  key={connection.id}
                  onActivate={handleActivate}
                  onRemove={handleRemove}
                  onTest={handleTest}
                />
              ))}
              {connections.length === 0 ? (
                <p className="rounded-xl border border-dashed border-border/60 px-3 py-4 text-center text-[11px] leading-relaxed text-muted-foreground">
                  还没有接入自己的供应商,当前使用部署方配置。
                </p>
              ) : null}
            </div>

            {connections.length > 0 && activeId ? (
              <button
                className="mt-2 text-[11px] text-muted-foreground underline-offset-2 hover:text-foreground hover:underline"
                data-testid="llm-use-deployment"
                onClick={handleUseDeployment}
                type="button"
              >
                改用部署方默认配置
              </button>
            ) : null}
          </section>

          <Separator />

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
              清单来自当前连接;切换模型后对下一条消息生效。聊天请求会自动带上
              当前连接的 Key、Base URL 与协议。
            </p>
          </section>
        </div>
      </DialogContent>
    </Dialog>
  );
}
