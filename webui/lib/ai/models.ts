export const DEFAULT_CHAT_MODEL = "glm-4.6";

// 智谱 GLM 的 OpenAI 兼容端点(与后端 agent/llm.py 同一套 ZAI_* 环境变量)
export const ZAI_BASE_URL_DEFAULT = "https://open.bigmodel.cn/api/paas/v4/";

export type ModelCapabilities = {
  tools: boolean;
  vision: boolean;
  reasoning: boolean;
};

export type ChatModel = {
  id: string;
  name: string;
  provider: string;
  description: string;
  capabilities: ModelCapabilities;
};

// 兜底清单:仅在 /api/models 拉取失败(未配 key、断网、端点异常)时使用,
// 保证界面依然能选能发。正常情况以端点的实时清单为准,不在这里追新模型。
// deepseek-chat 在前:当前部署方配置为 DeepSeek 端点(2026-10-09 起智谱欠费)。
export const FALLBACK_CHAT_MODELS: ChatModel[] = [
  toChatModel("deepseek-chat"),
  toChatModel("glm-4.6"),
];

// 模型 id -> 展示名:glm-5.3-flash -> GLM-5.3-Flash
export function prettyModelName(id: string): string {
  return id
    .split("-")
    .map((part, i) =>
      i === 0
        ? part.toUpperCase()
        : part.charAt(0).toUpperCase() + part.slice(1)
    )
    .join("-");
}

// 智谱 /models 只返回 id,不返回能力;按命名约定推断。
// GLM 4.5 起均为混合推理模型且支持工具调用,视觉能力看 v 后缀(glm-4.5v / glm-4v 系列)。
export function inferCapabilities(id: string): ModelCapabilities {
  const vision =
    id.endsWith("v") || /-v\d/.test(id) || /-v-/.test(id) || id.endsWith("vl");
  return { reasoning: true, tools: true, vision };
}

export function describeModel(id: string, provider?: string): string {
  if (provider === "anthropic") {
    return "Anthropic Claude model with tool use";
  }
  if (provider === "openai") {
    return "OpenAI chat model with tool use";
  }
  if (provider === "deepseek" || /deepseek/.test(id)) {
    return "DeepSeek chat model with tool use";
  }
  if (inferCapabilities(id).vision) {
    return "Vision model with tool use";
  }
  if (/flash|turbo/.test(id)) {
    return "Fast and cheap model with tool use";
  }
  if (/air/.test(id)) {
    return "Lightweight model with tool use";
  }
  return "Chat model with tool use";
}

export function toChatModel(id: string, provider?: string): ChatModel {
  const resolved =
    provider ||
    (id.startsWith("deepseek")
      ? "deepseek"
      : id.startsWith("claude")
        ? "anthropic"
        : id.startsWith("gpt")
          ? "openai"
          : "zhipuai");
  return {
    capabilities: inferCapabilities(id),
    description: describeModel(id, resolved),
    id,
    name: prettyModelName(id),
    provider: resolved,
  };
}

// 供应商连接后模型清单拉取失败的兜底:至少显示该厂商的常用模型,
// 不至于串到别家的清单。真实清单仍以 /api/models 返回为准。
export function providerFallbackModels(provider?: string): ChatModel[] {
  if (provider === "deepseek") {
    return [toChatModel("deepseek-chat", "deepseek")];
  }
  if (provider === "openai") {
    return [
      toChatModel("gpt-4o-mini", "openai"),
      toChatModel("gpt-4o", "openai"),
    ];
  }
  if (provider === "anthropic") {
    return [
      toChatModel("claude-sonnet-4-5", "anthropic"),
      toChatModel("claude-haiku-4-5", "anthropic"),
    ];
  }
  return FALLBACK_CHAT_MODELS;
}

export type ModelAvailability = "healthy" | "impacted" | "unknown";

// 端点没有 per-model 健康检查接口;已配置的模型一律按 healthy 处理,
// 避免聊天过程中弹出误导性的 gateway 降级提示。(保留 Promise 返回类型
// 兼容调用方 await;函数体内无异步操作,故不加 async。)
export function getModelAvailability(
  modelId: string
): Promise<ModelAvailability> {
  return Promise.resolve(modelId ? "healthy" : "unknown");
}

// 默认选型(与后端 pick_default_model 同规则):清单已按新→旧排序,
// 优先 flash/turbo 级(编排对话要快),DeepSeek 端点时优先 deepseek-chat
// (reasoner 是深思模型,慢且输出思考过程,不适合编排抽取),否则第一个。
const FAST_MODEL_RE = /flash|turbo|^deepseek-chat$/;
export function pickDefaultModel(models: ChatModel[]): ChatModel | undefined {
  return models.find((m) => FAST_MODEL_RE.test(m.id)) ?? models[0];
}
