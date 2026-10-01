"use client";

import type { UseChatHelpers } from "@ai-sdk/react";
import { useChat } from "@ai-sdk/react";
import { DefaultChatTransport } from "ai";
import { usePathname } from "next/navigation";
import {
  createContext,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import useSWR, { useSWRConfig } from "swr";
import { unstable_serialize } from "swr/infinite";
import { getStoredApiKey } from "@/components/chat/api-key-settings";
import { useDataStream } from "@/components/chat/data-stream-provider";
import { getChatHistoryPaginationKey } from "@/components/chat/sidebar-history";
import { toast } from "@/components/chat/toast";
import type { VisibilityType } from "@/components/chat/visibility-selector";
import { useAutoResume } from "@/hooks/use-auto-resume";
import { getStoredToken } from "@/hooks/use-bank-auth";
import {
  type ChatModel,
  DEFAULT_CHAT_MODEL,
  pickDefaultModel,
} from "@/lib/ai/models";
import type { Vote } from "@/lib/db/schema";
import { ChatbotError } from "@/lib/errors";
import type { ChatMessage } from "@/lib/types";
import { fetcher, fetchWithErrorHandlers, generateUUID } from "@/lib/utils";

type ActiveChatContextValue = {
  chatId: string;
  messages: ChatMessage[];
  setMessages: UseChatHelpers<ChatMessage>["setMessages"];
  sendMessage: UseChatHelpers<ChatMessage>["sendMessage"];
  status: UseChatHelpers<ChatMessage>["status"];
  stop: UseChatHelpers<ChatMessage>["stop"];
  regenerate: UseChatHelpers<ChatMessage>["regenerate"];
  addToolApprovalResponse: UseChatHelpers<ChatMessage>["addToolApprovalResponse"];
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  visibilityType: VisibilityType;
  isReadonly: boolean;
  isLoading: boolean;
  votes: Vote[] | undefined;
  currentModelId: string;
  setCurrentModelId: (id: string) => void;
};

const ActiveChatContext = createContext<ActiveChatContextValue | null>(null);

function extractChatId(pathname: string): string | null {
  const match = pathname.match(/\/chat\/([^/]+)/);
  return match ? match[1] : null;
}

export function ActiveChatProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { setDataStream, setWaitingStatus } = useDataStream();
  const { mutate } = useSWRConfig();

  const chatIdFromUrl = extractChatId(pathname);
  const isNewChat = !chatIdFromUrl;
  const newChatIdRef = useRef(generateUUID());
  const prevPathnameRef = useRef(pathname);

  if (isNewChat && prevPathnameRef.current !== pathname) {
    newChatIdRef.current = generateUUID();
  }
  prevPathnameRef.current = pathname;

  const chatId = chatIdFromUrl ?? newChatIdRef.current;

  // agent 后端基址(聊天 transport / 历史恢复 / 模型清单共用):
  // 跟随页面主机,localhost / 127.0.0.1 / 局域网 IP 打开都能用
  const agentBase = `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"}`;

  const [currentModelId, setCurrentModelId] = useState(DEFAULT_CHAT_MODEL);
  const currentModelIdRef = useRef(currentModelId);
  useEffect(() => {
    currentModelIdRef.current = currentModelId;
  }, [currentModelId]);

  // 默认模型自动落位(与后端同规则:清单里最新的 flash 级):
  // 用户没选过(cookie 无 chat-model)时,清单一到就切到快模型,
  // 避免默认 glm-4.6 把对话拖慢;选过则尊重选择。
  // 清单走 webui 自己的 /api/models(BYOK 时把 Key 放请求头,由服务端代理拉取)。
  const { data: autoModelsData } = useSWR(
    `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/models`,
    (url: string) => {
      const key = getStoredApiKey();
      return fetch(url, {
        headers: key ? { "x-zai-key": key } : undefined,
      }).then((r) => r.json());
    },
    { dedupingInterval: 60_000, revalidateOnFocus: false }
  );
  useEffect(() => {
    const models = autoModelsData?.models as ChatModel[] | undefined;
    if (!models?.length) {
      return;
    }
    const cookie = document.cookie
      .split("; ")
      .find((row) => row.startsWith("chat-model="));
    if (cookie) {
      const picked = decodeURIComponent(cookie.split("=")[1] ?? "");
      if (picked && models.some((m) => m.id === picked)) {
        setCurrentModelId(picked);
      }
      return;
    }
    const auto = pickDefaultModel(models);
    if (auto && currentModelIdRef.current === DEFAULT_CHAT_MODEL) {
      setCurrentModelId(auto.id);
    }
  }, [autoModelsData]);

  const [input, setInput] = useState("");

  // 会话历史来自 agent 后端(服务端 SQLite 持久化):刷新/换浏览器都能恢复现场,
  // 与聊天 transport 同源同端口策略(跟随页面主机,避免代理差异)。
  // 已登录时附带 bank-token,后端按登录用户隔离/归属校验。
  const historyUrl = (() => {
    const params = new URLSearchParams({ thread_id: chatId });
    const token = getStoredToken();
    if (token) {
      params.set("token", token);
    }
    return `${agentBase}/api/history?${params.toString()}`;
  })();
  const { data: chatData, isLoading } = useSWR(
    isNewChat ? null : historyUrl,
    fetcher,
    { revalidateOnFocus: false }
  );

  const initialMessages: ChatMessage[] = isNewChat
    ? []
    : (chatData?.messages ?? []);
  const visibility: VisibilityType = isNewChat
    ? "private"
    : (chatData?.visibility ?? "private");

  const {
    messages,
    setMessages,
    sendMessage,
    status,
    stop,
    regenerate,
    resumeStream,
    addToolApprovalResponse,
  } = useChat<ChatMessage>({
    generateId: generateUUID,
    id: chatId,
    messages: initialMessages,
    onData: (dataPart) => {
      if (dataPart.type === "data-waiting-status") {
        setWaitingStatus(dataPart.data);
        return;
      }
      setDataStream((ds) => (ds ? [...ds, dataPart] : []));
    },
    onError: (error) => {
      if (error instanceof ChatbotError) {
        toast({ description: error.message, type: "error" });
      } else {
        toast({
          description: error.message || "Oops, an error occurred!",
          type: "error",
        });
      }
    },
    onFinish: () => {
      mutate(unstable_serialize(getChatHistoryPaginationKey));
    },
    sendAutomaticallyWhen: ({ messages: currentMessages }) => {
      const lastMessage = currentMessages.at(-1);
      return (
        lastMessage?.parts?.some(
          (part) =>
            "state" in part &&
            part.state === "approval-responded" &&
            "approval" in part &&
            (part.approval as { approved?: boolean })?.approved === true
        ) ?? false
      );
    },
    transport: new DefaultChatTransport({
      // AI 银行后端(agent.api,FastAPI SSE)。后端契约:只取最后一条 user 消息,
      // thread_id 即会话 id(checkpointer 状态);停在人工闸门时,下一条消息自动
      // 作为 resume 值恢复执行——前端照常发消息即可,无需区分指令与闸门答复。
      // 后端地址跟随页面主机:localhost / 127.0.0.1 / 局域网 IP 打开都能用,
      // 避免系统代理或浏览器对特定主机的差异导致页面能开但接口打不通。
      api: `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${
        process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"
      }/api/chat`,
      fetch: fetchWithErrorHandlers,
      prepareSendMessagesRequest(request) {
        const toPlainMessage = (message: ChatMessage) => ({
          content: (message.parts ?? [])
            .filter((part) => part.type === "text")
            .map((part) => ("text" in part ? part.text : ""))
            .join("\n")
            .trim(),
          role: message.role,
        });

        return {
          body: {
            thread_id: request.id,
            // BYOK:界面自填的 Key 随请求生效(不填后端走部署方环境变量)
            ...(getStoredApiKey() ? { api_key: getStoredApiKey() } : {}),
            // 登录态:bank-token 随请求生效(未登录不带,后端按匿名演示处理)
            ...(getStoredToken() ? { token: getStoredToken() } : {}),
            messages: request.messages
              .map(toPlainMessage)
              .filter((message) => message.content.length > 0),
            // 模型切换:把选择器当前模型带给后端(与后端自动选型同一默认规则)
            model: currentModelIdRef.current,
          },
        };
      },
    }),
  });

  useEffect(() => {
    if (status === "submitted" || status === "ready" || status === "error") {
      setWaitingStatus(undefined);
    }
  }, [status, setWaitingStatus]);

  const loadedChatIds = useRef(new Set<string>());

  if (isNewChat && !loadedChatIds.current.has(newChatIdRef.current)) {
    loadedChatIds.current.add(newChatIdRef.current);
  }

  useEffect(() => {
    if (loadedChatIds.current.has(chatId)) {
      return;
    }
    if (chatData?.messages) {
      loadedChatIds.current.add(chatId);
      setMessages(chatData.messages);
    }
  }, [chatId, chatData?.messages, setMessages]);

  const prevChatIdRef = useRef(chatId);
  useEffect(() => {
    if (prevChatIdRef.current !== chatId) {
      prevChatIdRef.current = chatId;
      if (isNewChat) {
        setMessages([]);
      }
    }
  }, [chatId, isNewChat, setMessages]);

  useEffect(() => {
    if (chatData && !isNewChat) {
      const cookieModel = document.cookie
        .split("; ")
        .find((row) => row.startsWith("chat-model="))
        ?.split("=")[1];
      if (cookieModel) {
        setCurrentModelId(decodeURIComponent(cookieModel));
      }
    }
  }, [chatData, isNewChat]);

  const hasAppendedQueryRef = useRef(false);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const query = params.get("query");
    if (query && !hasAppendedQueryRef.current) {
      hasAppendedQueryRef.current = true;
      window.history.replaceState(
        {},
        "",
        `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
      );
      sendMessage({
        parts: [{ text: query, type: "text" }],
        role: "user" as const,
      });
    }
  }, [sendMessage, chatId]);

  useAutoResume({
    autoResume: !isNewChat && !!chatData,
    initialMessages,
    resumeStream,
    setMessages,
  });

  const isReadonly = isNewChat ? false : (chatData?.isReadonly ?? false);

  const { data: votes } = useSWR<Vote[]>(
    !isReadonly && messages.length >= 2
      ? `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/vote?chatId=${chatId}`
      : null,
    fetcher,
    { revalidateOnFocus: false }
  );

  const value = useMemo<ActiveChatContextValue>(
    () => ({
      addToolApprovalResponse,
      chatId,
      currentModelId,
      input,
      isLoading: !isNewChat && isLoading,
      isReadonly,
      messages,
      regenerate,
      sendMessage,
      setCurrentModelId,
      setInput,
      setMessages,
      status,
      stop,
      visibilityType: visibility,
      votes,
    }),
    [
      chatId,
      messages,
      setMessages,
      sendMessage,
      status,
      stop,
      regenerate,
      addToolApprovalResponse,
      input,
      visibility,
      isReadonly,
      isNewChat,
      isLoading,
      votes,
      currentModelId,
    ]
  );

  return (
    <ActiveChatContext.Provider value={value}>
      {children}
    </ActiveChatContext.Provider>
  );
}

export function useActiveChat() {
  const context = useContext(ActiveChatContext);
  if (!context) {
    throw new Error("useActiveChat must be used within ActiveChatProvider");
  }
  return context;
}
