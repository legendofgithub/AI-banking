/*
 * 本机模型供应商连接(BYOK,多连接)。
 *
 * - 每个连接 = 供应商 + API Key + Base URL + 连接状态,全部存 localStorage;
 *   同一浏览器重新登录/重启电脑后仍在,不需要每次登录重填。
 * - 同一时间有一个"当前连接":聊天请求与模型清单都按它发;
 *   没有当前连接时回落部署方 .env.local 的 ZAI_* 配置。
 * - Key 只在本地读取,随请求头 x-zai-key / body api_key 发给自家 8800,
 *   不进 URL、不写日志。
 */

import type { ChatModel, ModelCapabilities } from "./models";

export type LlmProviderId = "deepseek" | "openai" | "anthropic" | "custom";
export type LlmConnectionStatus = "connected" | "unverified";

export type LlmConnection = {
  id: string;
  provider: LlmProviderId;
  label: string;
  apiKey: string;
  baseUrl: string;
  status: LlmConnectionStatus;
  modelCount?: number;
  connectedAt?: string;
  createdAt: string;
};

export const LLM_PROVIDER_PRESETS: Record<
  LlmProviderId,
  { label: string; baseUrl: string; hint: string }
> = {
  anthropic: {
    baseUrl: "https://api.anthropic.com/v1",
    hint: "Claude 原生 Messages 协议",
    label: "Claude",
  },
  custom: {
    baseUrl: "",
    hint: "任意 OpenAI 兼容网关(自建代理、其他云厂商)",
    label: "自定义 / 兼容网关",
  },
  deepseek: {
    baseUrl: "https://api.deepseek.com/v1",
    hint: "OpenAI 兼容协议,默认模型 deepseek-chat",
    label: "DeepSeek",
  },
  openai: {
    baseUrl: "https://api.openai.com/v1",
    hint: "OpenAI 官方接口,需可用额度的 API Key",
    label: "OpenAI",
  },
};

const CONNECTIONS_KEY = "llm-connections-v1";
const ACTIVE_KEY = "llm-active-connection-v1";
const LEGACY_KEY = "zai-api-key";

export function providerLabel(provider: LlmProviderId): string {
  return LLM_PROVIDER_PRESETS[provider]?.label ?? provider;
}

export function maskApiKey(key: string): string {
  if (!key) {
    return "";
  }
  return `••••••${key.slice(-4)}`;
}

function readJson<T>(key: string): T | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeJson(key: string, value: unknown): void {
  if (typeof window === "undefined") {
    return;
  }
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // 隐私模式/配额满时不阻断界面,本次连接仍可用
  }
}

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `llm-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function normalizeConnection(value: LlmConnection): LlmConnection {
  return {
    ...value,
    baseUrl: (value.baseUrl ?? "").trim().replace(/\/+$/, ""),
    label: (value.label ?? "").trim() || providerLabel(value.provider),
    provider:
      value.provider in LLM_PROVIDER_PRESETS ? value.provider : "custom",
  };
}

/** 兼容旧版单 Key 存储:有旧 Key 时迁移成一条"原有 API Key"连接。 */
function migrateLegacy(): LlmConnection[] {
  if (typeof window === "undefined") {
    return [];
  }
  const legacy = window.localStorage.getItem(LEGACY_KEY)?.trim();
  if (!legacy) {
    return [];
  }
  const connection: LlmConnection = {
    apiKey: legacy,
    baseUrl: "",
    connectedAt: new Date().toISOString(),
    createdAt: new Date().toISOString(),
    id: `legacy-${newId()}`,
    label: "原有 API Key",
    provider: "custom",
    status: "connected",
  };
  writeJson(CONNECTIONS_KEY, [connection]);
  window.localStorage.setItem(ACTIVE_KEY, connection.id);
  window.localStorage.removeItem(LEGACY_KEY);
  return [connection];
}

export function getConnections(): LlmConnection[] {
  const stored = readJson<LlmConnection[]>(CONNECTIONS_KEY);
  if (Array.isArray(stored) && stored.length > 0) {
    return stored
      .filter((item) => item && typeof item.apiKey === "string" && item.apiKey)
      .map(normalizeConnection);
  }
  return migrateLegacy();
}

export function saveConnection(connection: LlmConnection): LlmConnection {
  const next = normalizeConnection(connection);
  const list = getConnections();
  const index = list.findIndex((item) => item.id === next.id);
  if (index >= 0) {
    list[index] = next;
  } else {
    list.unshift(next);
  }
  writeJson(CONNECTIONS_KEY, list);
  return next;
}

export function removeConnection(id: string): void {
  const list = getConnections().filter((item) => item.id !== id);
  writeJson(CONNECTIONS_KEY, list);
  if (getActiveConnectionId() === id) {
    window.localStorage.removeItem(ACTIVE_KEY);
  }
}

export function getActiveConnectionId(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(ACTIVE_KEY) ?? "";
}

export function setActiveConnection(id: string | null): void {
  if (typeof window === "undefined") {
    return;
  }
  if (id) {
    window.localStorage.setItem(ACTIVE_KEY, id);
  } else {
    window.localStorage.removeItem(ACTIVE_KEY);
  }
}

export function getActiveConnection(): LlmConnection | null {
  const list = getConnections(); // 顺带触发旧 Key 迁移
  const id = getActiveConnectionId();
  if (!id) {
    return null;
  }
  return list.find((item) => item.id === id) ?? null;
}

/** 模型清单请求头:当前连接的 Key/Base URL/供应商;未连接则不带。 */
export function getModelRequestHeaders(): Record<string, string> {
  const connection = getActiveConnection();
  if (connection) {
    return {
      "x-llm-base-url": connection.baseUrl,
      "x-llm-provider": connection.provider,
      "x-zai-key": connection.apiKey,
    };
  }
  if (typeof window !== "undefined") {
    const legacy = window.localStorage.getItem(LEGACY_KEY);
    if (legacy) {
      return { "x-zai-key": legacy };
    }
  }
  return {};
}

export type ChatModelsResponse = {
  capabilities?: Record<string, ModelCapabilities>;
  models?: ChatModel[];
};

/** 拉模型清单:有当前连接就带 Key/Base URL/供应商,让后端按连接返回模型。 */
export function fetchModelsWithKey(url: string): Promise<ChatModelsResponse> {
  return fetch(url, { headers: getModelRequestHeaders() }).then((res) =>
    res.json()
  );
}
