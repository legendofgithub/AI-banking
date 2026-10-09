import {
  type ChatModel,
  type ModelCapabilities,
  DEFAULT_CHAT_MODEL,
  FALLBACK_CHAT_MODELS,
  toChatModel,
  ZAI_BASE_URL_DEFAULT,
} from "./models";

// 服务端专用:实时探测智谱端点上的可用模型。Next fetch 缓存 1 小时,
// 即智谱上新模型后,最多 1 小时(或重启服务)就会出现在前端选择器里。

type ZaiModelsResponse = {
  data?: { created?: number; id: string }[];
};

function getZaiConfig() {
  return {
    apiKey: process.env.ZAI_API_KEY,
    baseURL:
      process.env.ZAI_BASE_URL?.replace(/\/?$/, "/") ?? ZAI_BASE_URL_DEFAULT,
  };
}

export async function getZaiChatModels(
  keyOverride?: string
): Promise<ChatModel[]> {
  const { baseURL } = getZaiConfig();
  const apiKey = keyOverride || process.env.ZAI_API_KEY;
  if (!apiKey) {
    return FALLBACK_CHAT_MODELS;
  }

  try {
    const res = await fetch(`${baseURL}models`, {
      headers: { Authorization: `Bearer ${apiKey}` },
      // 用户自填 Key 时绕过 Next 数据缓存(缓存键不含鉴权头,会把 A 的
      // 清单错发给 B);部署方环境变量路径保留 1h 缓存。
      ...(keyOverride ? { cache: "no-store" as const } : { next: { revalidate: 3600 } }),
    });
    if (!res.ok) {
      return FALLBACK_CHAT_MODELS;
    }

    const json = (await res.json()) as ZaiModelsResponse;
    const list = (json.data ?? []).filter((m) => m.id);
    if (list.length === 0) {
      return FALLBACK_CHAT_MODELS;
    }

    // 新模型排前面;没有 created 字段的保持接口原序
    list.sort((a, b) => (b.created ?? 0) - (a.created ?? 0));
    return list.map((m) => toChatModel(m.id));
  } catch {
    return FALLBACK_CHAT_MODELS;
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
