/*
 * BYOK(自带 Key)存储与模型清单请求。
 *
 * Key 只存浏览器 localStorage,随请求头 x-zai-key 发给自家 8800 代理,
 * 不进 URL/日志;清除即删。抽到 lib 里是为了让模型 Provider 和设置弹窗
 * 共用同一份读写逻辑,不再从 UI 组件反向 import。
 */

import type { ChatModel, ModelCapabilities } from "./models";

const STORAGE_KEY = "zai-api-key";

export function getStoredApiKey(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(STORAGE_KEY) ?? "";
}

export function setStoredApiKey(key: string): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(STORAGE_KEY, key);
}

export function clearStoredApiKey(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(STORAGE_KEY);
}

export type ChatModelsResponse = {
  capabilities?: Record<string, ModelCapabilities>;
  models?: ChatModel[];
};

/** 拉模型清单:有 BYOK Key 就带请求头,让后端按这把 Key 返回可用模型。 */
export function fetchModelsWithKey(url: string): Promise<ChatModelsResponse> {
  const key = getStoredApiKey();
  return fetch(url, {
    headers: key ? { "x-zai-key": key } : undefined,
  }).then((res) => res.json());
}
