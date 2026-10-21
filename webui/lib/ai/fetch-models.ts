import {
  type ChatModel,
  DEFAULT_CHAT_MODEL,
  type ModelCapabilities,
  providerFallbackModels,
  toChatModel,
  ZAI_BASE_URL_DEFAULT,
} from "./models";

// 服务端专用:实时探测当前端点上的可用模型。Next fetch 缓存 1 小时,
// 部署方环境变量路径上新模型后最多 1 小时出现;用户自带连接一律 no-store。

type ZaiModelsResponse = {
  data?: { created?: number; created_at?: string; id: string }[];
};

function getZaiConfig() {
  return {
    apiKey: process.env.ZAI_API_KEY,
    baseURL:
      process.env.ZAI_BASE_URL?.replace(/\/?$/, "/") ?? ZAI_BASE_URL_DEFAULT,
  };
}

export async function getZaiChatModels(
  keyOverride?: string,
  baseOverride?: string,
  providerOverride?: string
): Promise<ChatModel[]> {
  const { baseURL: envBaseURL } = getZaiConfig();
  const baseURL = (baseOverride?.trim() || envBaseURL).replace(/\/?$/, "/");
  const apiKey = keyOverride || process.env.ZAI_API_KEY;
  const provider = providerOverride?.trim() || undefined;
  if (!apiKey) {
    return providerFallbackModels(provider);
  }

  const headers: Record<string, string> =
    provider === "anthropic"
      ? { "anthropic-version": "2023-06-01", "x-api-key": apiKey }
      : { Authorization: `Bearer ${apiKey}` };
  const hasOverride = Boolean(keyOverride || baseOverride || provider);

  try {
    const res = await fetch(`${baseURL}models`, {
      headers,
      // 用户自带连接绕过 Next 数据缓存(缓存键不含鉴权头/端点,
      // 会把 A 的清单错发给 B);部署方环境变量路径保留 1h 缓存。
      ...(hasOverride
        ? { cache: "no-store" as const }
        : { next: { revalidate: 3600 } }),
    });
    if (!res.ok) {
      return providerFallbackModels(provider);
    }

    const json = (await res.json()) as ZaiModelsResponse;
    const list = (json.data ?? []).filter((m) => m.id);
    if (list.length === 0) {
      return providerFallbackModels(provider);
    }

    // 新模型排前面;没有 created 字段的保持接口原序
    list.sort((a, b) => (b.created ?? 0) - (a.created ?? 0));
    return list.map((m) => toChatModel(m.id, provider));
  } catch {
    return providerFallbackModels(provider);
  }
}

export async function getModelCapabilities(): Promise<
  Record<string, ModelCapabilities>
> {
  return Object.fromEntries(
    (await getZaiChatModels()).map((m) => [m.id, m.capabilities])
  );
}

// 聊天路由用:把前端传来的 selectedChatModel 收敛成端点上真实存在的 id,
// 不存在则回落到默认模型,再回落到清单第一个。
export async function resolveChatModel(selected: string): Promise<string> {
  const models = await getZaiChatModels();
  if (models.some((m) => m.id === selected)) {
    return selected;
  }
  if (models.some((m) => m.id === DEFAULT_CHAT_MODEL)) {
    return DEFAULT_CHAT_MODEL;
  }
  return models[0]?.id ?? DEFAULT_CHAT_MODEL;
}

// 标题生成用小快模型:优先 flash/turbo/air,DeepSeek 端点优先 deepseek-chat,
// 没有就用清单第一个
export async function pickTitleModelId(): Promise<string> {
  const models = await getZaiChatModels();
  return (
    models.find((m) => /flash|turbo|air|^deepseek-chat$/.test(m.id))?.id ??
    models[0]?.id ??
    DEFAULT_CHAT_MODEL
  );
}
