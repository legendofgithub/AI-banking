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
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import useSWR, { useSWRConfig } from "swr";
import { unstable_serialize } from "swr/infinite";
import { useDataStream } from "@/components/chat/data-stream-provider";
import {
  type PayeeContact,
  PayeePickerDialog,
} from "@/components/chat/payee-picker";
import { getChatHistoryPaginationKey } from "@/components/chat/sidebar-history";
import { toast } from "@/components/chat/toast";
import { useAutoResume } from "@/hooks/use-auto-resume";
import { getStoredToken } from "@/hooks/use-bank-auth";
import { useChatModel } from "@/hooks/use-chat-model";
import { useDeviceSync } from "@/hooks/use-device-sync";
import { getActiveConnection } from "@/lib/ai/byok";
import type { Vote } from "@/lib/db/schema";
import { getClientId } from "@/lib/device-client";
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
  isReadonly: boolean;
  isLoading: boolean;
  votes: Vote[] | undefined;
  currentModelId: string;
  /** 打开「我要转账」收款人选择器(搜索/新建都在弹窗内完成) */
  openPayeePicker: () => void;
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

  // 当前模型由全局 ChatModelProvider 持有(左下角设置与输入框信息共用),
  // 这里只保留 ref 给发送请求取最新值,避免流式回调里读到旧 state。
  const { currentModelId } = useChatModel();
  const currentModelIdRef = useRef(currentModelId);
  useEffect(() => {
    currentModelIdRef.current = currentModelId;
  }, [currentModelId]);

  const [input, setInput] = useState("");
  const [payeePickerOpen, setPayeePickerOpen] = useState(false);

  // 会话历史来自 agent 后端(服务端 SQLite 持久化):刷新/换浏览器都能恢复现场,
  // 与聊天 transport 同源同端口策略(跟随页面主机,避免代理差异)。
  // 登录态由全局 fetcher 带 x-bank-token 头(见 lib/utils.ts),URL 不带 token。
  const historyUrl = `${agentBase}/api/history?thread_id=${encodeURIComponent(chatId)}`;
  const { data: chatData, isLoading } = useSWR(
    isNewChat ? null : historyUrl,
    fetcher,
    { revalidateOnFocus: false }
  );

  const initialMessages: ChatMessage[] = isNewChat
    ? []
    : (chatData?.messages ?? []);

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

        const connection = getActiveConnection();

        return {
          body: {
            thread_id: request.id,
            // BYOK:本机当前供应商连接的 Key/端点/协议随请求生效
            ...(connection
              ? {
                  api_key: connection.apiKey,
                  base_url: connection.baseUrl,
                  provider: connection.provider,
                }
              : {}),
            // 登录态:bank-token 随请求生效(未登录不带,后端按匿名演示处理)
            ...(getStoredToken() ? { token: getStoredToken() } : {}),
            // 跨设备同步:标记本轮写入来自哪个设备/标签页
            client_id: getClientId(),
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

  // 手机 ↔ 电脑同步:跟随当前会话、拉取对方产生的新消息
  useDeviceSync({
    agentBase,
    chatId,
    isNewChat,
    setMessages,
    status,
    token: getStoredToken(),
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

  const openPayeePicker = useCallback(() => {
    setPayeePickerOpen(true);
  }, []);

  // 选中收款人(或选择直接对话发起)后走原转账入口:带着姓名+手机号发一条
  // 用户消息,金额/定时/支付密码仍由后端闸门处理,前端不碰动钱逻辑。
  const startTransfer = useCallback(
    (contact: PayeeContact | null) => {
      setPayeePickerOpen(false);
      if (status === "submitted" || status === "streaming") {
        toast({ description: "助手还在回复,请稍后再发起转账", type: "error" });
        return;
      }
      window.history.pushState(
        {},
        "",
        `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
      );
      sendMessage({
        parts: [
          {
            text: contact
              ? `我要转账,转给${contact.name},手机号${contact.phone}`
              : "我要转账",
            type: "text",
          },
        ],
        role: "user",
      });
    },
    [chatId, sendMessage, status]
  );

  const handlePayeeFallback = useCallback(() => {
    startTransfer(null);
  }, [startTransfer]);

  const value = useMemo<ActiveChatContextValue>(
    () => ({
      addToolApprovalResponse,
      chatId,
      currentModelId,
      input,
      isLoading: !isNewChat && isLoading,
      isReadonly,
      messages,
      openPayeePicker,
      regenerate,
      sendMessage,
      setInput,
      setMessages,
      status,
      stop,
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
      isReadonly,
      isNewChat,
      isLoading,
      openPayeePicker,
      votes,
      currentModelId,
    ]
  );

  return (
    <ActiveChatContext.Provider value={value}>
      {children}
      <PayeePickerDialog
        onFallback={handlePayeeFallback}
        onOpenChange={setPayeePickerOpen}
        onPick={startTransfer}
        open={payeePickerOpen}
      />
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
