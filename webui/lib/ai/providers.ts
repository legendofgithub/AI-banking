import { createOpenAI } from "@ai-sdk/openai";
import { customProvider } from "ai";
import { isTestEnvironment } from "../constants";
import { pickTitleModelId } from "./fetch-models";
import { ZAI_BASE_URL_DEFAULT } from "./models";

// 智谱 GLM 的 OpenAI 兼容端点,与后端 agent/llm.py 共用 ZAI_API_KEY / ZAI_BASE_URL。
// 缺 ZAI_API_KEY 时不在这里抛错:服务能起,首次调用时由智谱端点返回 401 暴露问题。
function getZaiModel(modelId: string) {
  const zai = createOpenAI({
    apiKey: process.env.ZAI_API_KEY,
    baseURL: process.env.ZAI_BASE_URL ?? ZAI_BASE_URL_DEFAULT,
  });
  return zai.chat(modelId);
}

export const myProvider = isTestEnvironment
  ? (() => {
      const {
        chatModel,
        titleModel: mockTitleModel,
      } = require("./models.mock");
      return customProvider({
        languageModels: {
          "chat-model": chatModel,
          "title-model": mockTitleModel,
        },
      });
    })()
  : null;

export function getLanguageModel(modelId: string) {
  if (isTestEnvironment && myProvider) {
    return myProvider.languageModel(modelId);
  }

  return getZaiModel(modelId);
}

export async function getTitleModel() {
  if (isTestEnvironment && myProvider) {
    return myProvider.languageModel("title-model");
  }
  return getZaiModel(await pickTitleModelId());
}
