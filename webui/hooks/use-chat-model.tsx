"use client";

/*
 * 全局大模型偏好(2026-10-21 从输入框选择器收编到左下角设置):
 * - 当前模型 + 实时模型清单由本 Provider 持有,聊天发送和设置弹窗共用;
 * - 用户选择写 chat-model cookie(与旧选择器同一枚 cookie,刷新/换会话保持);
 * - 自动落位与旧逻辑一致:优先恢复 cookie,否则按后端同规则选快模型;
 * - 换 BYOK Key 后 refreshModels() 重拉清单,当前模型不在新清单时自动回落。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import useSWR from "swr";
import { fetchModelsWithKey } from "@/lib/ai/byok";
import {
  type ChatModel,
  DEFAULT_CHAT_MODEL,
  FALLBACK_CHAT_MODELS,
  type ModelCapabilities,
  pickDefaultModel,
  toChatModel,
} from "@/lib/ai/models";

const MODEL_COOKIE = "chat-model";
const MODELS_URL = `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/models`;

function readModelCookie(): string {
  if (typeof document === "undefined") {
    return "";
  }
  const row = document.cookie
    .split("; ")
    .find((item) => item.startsWith(`${MODEL_COOKIE}=`));
  return row ? decodeURIComponent(row.split("=")[1] ?? "") : "";
}

function writeModelCookie(modelId: string): void {
  const maxAge = 60 * 60 * 24 * 365;
  // biome-ignore lint/suspicious/noDocumentCookie: 与旧选择器一致,模型偏好走 cookie
  document.cookie = `${MODEL_COOKIE}=${encodeURIComponent(modelId)}; path=/; max-age=${maxAge}`;
}

type ChatModelContextValue = {
  /** 当前模型 id(发给后端的值) */
  currentModelId: string;
  /** 当前模型对象(清单里找不到时回落到默认/首个,保证 UI 有东西可显示) */
  selectedModel: ChatModel;
  models: ChatModel[];
  capabilities: Record<string, ModelCapabilities> | undefined;
  isLoading: boolean;
  /** 用户主动选择:更新状态并落 cookie */
  selectModel: (modelId: string) => void;
  /** BYOK Key 变更后重拉模型清单 */
  refreshModels: () => void;
  modelSettingsOpen: boolean;
  setModelSettingsOpen: (open: boolean) => void;
  openModelSettings: () => void;
};

const ChatModelContext = createContext<ChatModelContextValue | null>(null);

export function ChatModelProvider({ children }: { children: React.ReactNode }) {
  const { data, isLoading, mutate } = useSWR(MODELS_URL, fetchModelsWithKey, {
    dedupingInterval: 60_000,
    revalidateOnFocus: false,
  });
  const models =
    data?.models && data.models.length > 0 ? data.models : FALLBACK_CHAT_MODELS;
  const capabilities = data?.capabilities;

  const [currentModelId, setCurrentModelId] = useState(DEFAULT_CHAT_MODEL);
  const [modelSettingsOpen, setModelSettingsOpen] = useState(false);
  const adoptedRef = useRef(false);

  // 首次拿到实时清单:恢复用户上次的选择;没有(或已失效)则按快模型规则落位。
  // 自动落位不写 cookie,保持"用户选过"与"系统默认"可区分(与旧实现一致)。
  useEffect(() => {
    if (adoptedRef.current || !data?.models?.length) {
      return;
    }
    adoptedRef.current = true;
    const saved = readModelCookie();
    if (saved && data.models.some((model) => model.id === saved)) {
      setCurrentModelId(saved);
      return;
    }
    const auto = pickDefaultModel(data.models);
    if (auto) {
      setCurrentModelId(auto.id);
    }
  }, [data]);

  // 清单变化(换 Key/端点):当前模型不存在就自动回落,避免把无效 id 发后端。
  useEffect(() => {
    if (!data?.models?.length) {
      return;
    }
    if (data.models.some((model) => model.id === currentModelId)) {
      return;
    }
    const auto = pickDefaultModel(data.models);
    if (auto) {
      setCurrentModelId(auto.id);
    }
  }, [data, currentModelId]);

  const selectModel = useCallback((modelId: string) => {
    setCurrentModelId(modelId);
    writeModelCookie(modelId);
  }, []);

  const refreshModels = useCallback(() => mutate(), [mutate]);

  const openModelSettings = useCallback(() => {
    setModelSettingsOpen(true);
  }, []);

  const selectedModel = useMemo(
    () =>
      models.find((model) => model.id === currentModelId) ??
      models.find((model) => model.id === DEFAULT_CHAT_MODEL) ??
      models[0] ??
      toChatModel(DEFAULT_CHAT_MODEL),
    [models, currentModelId]
  );

  const value = useMemo<ChatModelContextValue>(
    () => ({
      capabilities,
      currentModelId,
      isLoading,
      modelSettingsOpen,
      models,
      openModelSettings,
      refreshModels,
      selectedModel,
      selectModel,
      setModelSettingsOpen,
    }),
    [
      capabilities,
      currentModelId,
      isLoading,
      modelSettingsOpen,
      models,
      openModelSettings,
      refreshModels,
      selectModel,
      selectedModel,
    ]
  );

  return (
    <ChatModelContext.Provider value={value}>
      {children}
    </ChatModelContext.Provider>
  );
}

export function useChatModel(): ChatModelContextValue {
  const context = useContext(ChatModelContext);
  if (!context) {
    throw new Error("useChatModel must be used within ChatModelProvider");
  }
  return context;
}
