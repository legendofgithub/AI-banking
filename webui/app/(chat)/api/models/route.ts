import { getZaiChatModels } from "@/lib/ai/fetch-models";

export async function GET(request: Request) {
  // 用户在界面接入的供应商(Key/Base URL/协议)随请求头带来:用它拉
  // "这个连接能用的模型"。不带则走部署方环境变量(带 1h 缓存)。
  const headerKey = request.headers.get("x-zai-key")?.trim() || undefined;
  const headerBase = request.headers.get("x-llm-base-url")?.trim() || undefined;
  const headerProvider =
    request.headers.get("x-llm-provider")?.trim() || undefined;
  const headers = {
    "Cache-Control":
      headerKey || headerBase || headerProvider
        ? "no-store"
        : "public, max-age=60",
  };

  const models = await getZaiChatModels(headerKey, headerBase, headerProvider);
  const capabilities = Object.fromEntries(
    models.map((m) => [m.id, m.capabilities])
  );

  return Response.json({ capabilities, models }, { headers });
}
