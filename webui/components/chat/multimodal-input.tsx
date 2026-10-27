"use client";

import type { UseChatHelpers } from "@ai-sdk/react";
import type { UIMessage } from "ai";
import equal from "fast-deep-equal";
import { ArrowUpIcon, MicIcon, SettingsIcon, XIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import {
  type ChangeEvent,
  type Dispatch,
  memo,
  type SetStateAction,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { useLocalStorage, useWindowSize } from "usehooks-ts";
import { ModelSelectorLogo } from "@/components/ai-elements/model-selector";
import { SettingsDropdownButton } from "@/components/chat/sidebar-settings";
import { useChatModel } from "@/hooks/use-chat-model";
import { useSpeechInput } from "@/hooks/use-speech-input";
import type { Attachment, ChatMessage } from "@/lib/types";
import { cn } from "@/lib/utils";
import {
  PromptInput,
  PromptInputFooter,
  PromptInputSubmit,
  PromptInputTextarea,
  PromptInputTools,
} from "../ai-elements/prompt-input";
import { Button } from "../ui/button";
import { PaperclipIcon, StopIcon } from "./icons";
import { PreviewAttachment } from "./preview-attachment";
import {
  type SlashCommand,
  SlashCommandMenu,
  slashCommands,
} from "./slash-commands";
import { SuggestedActions } from "./suggested-actions";

function PureMultimodalInput({
  chatId,
  input,
  setInput,
  status,
  stop,
  attachments,
  setAttachments,
  messages,
  setMessages,
  sendMessage,
  className,
  editingMessage,
  onCancelEdit,
  onRequestTransfer,
  isLoading,
}: {
  chatId: string;
  input: string;
  setInput: Dispatch<SetStateAction<string>>;
  status: UseChatHelpers<ChatMessage>["status"];
  stop: () => void;
  attachments: Attachment[];
  setAttachments: Dispatch<SetStateAction<Attachment[]>>;
  messages: UIMessage[];
  setMessages: UseChatHelpers<ChatMessage>["setMessages"];
  sendMessage:
    | UseChatHelpers<ChatMessage>["sendMessage"]
    | (() => Promise<void>);
  className?: string;
  editingMessage?: ChatMessage | null;
  onCancelEdit?: () => void;
  onRequestTransfer?: () => void;
  isLoading?: boolean;
}) {
  const router = useRouter();
  const { setTheme, resolvedTheme } = useTheme();
  const { openModelSettings, isLoading: modelsLoading } = useChatModel();
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { width } = useWindowSize();
  const hasAutoFocused = useRef(false);
  useEffect(() => {
    if (!hasAutoFocused.current && width) {
      const timer = setTimeout(() => {
        textareaRef.current?.focus();
        hasAutoFocused.current = true;
      }, 100);
      return () => clearTimeout(timer);
    }
  }, [width]);

  const [localStorageInput, setLocalStorageInput] = useLocalStorage(
    "input",
    ""
  );

  useEffect(() => {
    if (textareaRef.current) {
      const domValue = textareaRef.current.value;
      const finalValue = domValue || localStorageInput || "";
      setInput(finalValue);
    }
  }, [localStorageInput, setInput]);

  useEffect(() => {
    setLocalStorageInput(input);
  }, [input, setLocalStorageInput]);

  // 银行首页快捷入口带话进来:一次性消费,消费即清。
  // bank-autosend(今日待办六卡等):预填并立即由 agent 开跑,不要求用户补发送;
  // bank-prefill:只填入输入框,用户过目后自己回车。
  useEffect(() => {
    const prefill = sessionStorage.getItem("bank-prefill");
    if (prefill) {
      sessionStorage.removeItem("bank-prefill");
      setInput(prefill);
    }
  }, [setInput]);

  useEffect(() => {
    const autosend = sessionStorage.getItem("bank-autosend");
    if (!autosend) {
      return;
    }
    // 必须等 /api/models 返回、模型偏好落位后再发:落位前 currentModelId
    // 还是编译期默认(glm-4.6),直接发会被 DeepSeek 端点 400 拒掉。
    if (modelsLoading) {
      return;
    }
    sessionStorage.removeItem("bank-autosend");
    // 与 submitForm 同口径:压 /chat/<id> 历史栈 + 清草稿/输入框 + 直发
    setLocalStorageInput("");
    setInput("");
    window.history.pushState(
      {},
      "",
      `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
    );
    sendMessage({ parts: [{ text: autosend, type: "text" }], role: "user" });
  }, [modelsLoading, setInput, setLocalStorageInput, sendMessage, chatId]);

  // 语音输入(2026-10-27,Web Speech API):转写以"开始录音时的输入"为前缀
  // 覆盖合并(识别器对当前语句发的是最新累积文本,不能追加),不自动发送
  const speechBaseRef = useRef("");
  const handleTranscript = useCallback(
    (text: string) => {
      setInput(speechBaseRef.current + text);
    },
    [setInput]
  );
  const speech = useSpeechInput(handleTranscript);
  const canListen = speech.status !== "unsupported" &&
    (status === "ready" || status === "error");
  const handleMicClick = useCallback(() => {
    if (speech.status === "idle") {
      speechBaseRef.current = input;
    }
    speech.toggle();
    textareaRef.current?.focus();
  }, [input, speech]);
  useEffect(() => {
    if (speech.error) {
      toast.error(speech.error);
    }
  }, [speech.error]);

  const fileInputRef = useRef<HTMLInputElement>(null);
  const [uploadQueue, setUploadQueue] = useState<string[]>([]);
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashQuery, setSlashQuery] = useState("");
  const [slashIndex, setSlashIndex] = useState(0);

  const handleInput = useCallback(
    (event: ChangeEvent<HTMLTextAreaElement>) => {
      const val = event.target.value;
      setInput(val);

      if (val.startsWith("/") && !val.includes(" ")) {
        setSlashOpen(true);
        setSlashQuery(val.slice(1));
        setSlashIndex(0);
      } else {
        setSlashOpen(false);
      }
    },
    [setInput]
  );

  const handleSlashSelect = useCallback(
    (cmd: SlashCommand) => {
      setSlashOpen(false);
      setInput("");
      switch (cmd.action) {
        case "new":
          router.push("/");
          break;
        case "clear":
          setMessages(() => []);
          break;
        case "rename":
          toast("Rename is available from the sidebar chat menu.");
          break;
        case "model":
          openModelSettings();
          break;
        case "theme":
          setTheme(resolvedTheme === "dark" ? "light" : "dark");
          break;
        case "delete":
          toast("Delete this chat?", {
            action: {
              label: "Delete",
              onClick: () => {
                fetch(
                  `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/chat?id=${chatId}`,
                  { method: "DELETE" }
                );
                router.push("/");
                toast.success("Chat deleted");
              },
            },
          });
          break;
        case "purge":
          toast("Delete all chats?", {
            action: {
              label: "Delete all",
              onClick: () => {
                fetch(
                  `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/history`,
                  {
                    method: "DELETE",
                  }
                );
                router.push("/");
                toast.success("All chats deleted");
              },
            },
          });
          break;
        default:
          break;
      }
    },
    [
      chatId,
      openModelSettings,
      resolvedTheme,
      router,
      setInput,
      setMessages,
      setTheme,
    ]
  );

  const submitForm = useCallback(() => {
    window.history.pushState(
      {},
      "",
      `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
    );

    sendMessage({
      parts: [
        ...attachments.map((attachment) => ({
          mediaType: attachment.contentType,
          name: attachment.name,
          type: "file" as const,
          url: attachment.url,
        })),
        {
          text: input,
          type: "text",
        },
      ],
      role: "user",
    });

    setAttachments([]);
    setLocalStorageInput("");
    setInput("");

    if (width && width > 768) {
      textareaRef.current?.focus();
    }
  }, [
    input,
    setInput,
    attachments,
    sendMessage,
    setAttachments,
    setLocalStorageInput,
    width,
    chatId,
  ]);

  const uploadFile = useCallback(async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);

    try {
      const response = await fetch(
        `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/api/files/upload`,
        {
          body: formData,
          method: "POST",
        }
      );

      if (response.ok) {
        const data = await response.json();
        const { url, pathname, contentType } = data;

        return {
          contentType,
          name: pathname,
          url,
        };
      }
      const { error } = await response.json();
      toast.error(error);
    } catch {
      toast.error("Failed to upload file, please try again!");
    }
  }, []);

  const handleFileChange = useCallback(
    async (event: ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.target.files || []);

      setUploadQueue(files.map((file) => file.name));

      try {
        const uploadPromises = files.map((file) => uploadFile(file));
        const uploadedAttachments = await Promise.all(uploadPromises);
        const successfullyUploadedAttachments = uploadedAttachments.filter(
          (attachment) => attachment !== undefined
        );

        setAttachments((currentAttachments) => [
          ...currentAttachments,
          ...successfullyUploadedAttachments,
        ]);
      } catch {
        toast.error("Failed to upload files");
      } finally {
        setUploadQueue([]);
      }
    },
    [setAttachments, uploadFile]
  );

  const handlePaste = useCallback(
    async (event: ClipboardEvent) => {
      const items = event.clipboardData?.items;
      if (!items) {
        return;
      }

      const imageItems = Array.from(items).filter((item) =>
        item.type.startsWith("image/")
      );

      if (imageItems.length === 0) {
        return;
      }

      event.preventDefault();

      setUploadQueue((prev) => [...prev, "Pasted image"]);

      try {
        const uploadPromises = imageItems
          .map((item) => item.getAsFile())
          .filter((file): file is File => file !== null)
          .map((file) => uploadFile(file));

        const uploadedAttachments = await Promise.all(uploadPromises);
        const successfullyUploadedAttachments = uploadedAttachments.filter(
          (attachment) =>
            attachment !== undefined &&
            attachment.url !== undefined &&
            attachment.contentType !== undefined
        );

        setAttachments((curr) => [
          ...curr,
          ...(successfullyUploadedAttachments as Attachment[]),
        ]);
      } catch {
        toast.error("Failed to upload pasted image(s)");
      } finally {
        setUploadQueue([]);
      }
    },
    [setAttachments, uploadFile]
  );

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }

    textarea.addEventListener("paste", handlePaste);
    return () => textarea.removeEventListener("paste", handlePaste);
  }, [handlePaste]);

  const handleCancelEditMouseDown = useCallback(
    (e: React.MouseEvent<HTMLButtonElement>) => {
      e.preventDefault();
      onCancelEdit?.();
    },
    [onCancelEdit]
  );

  const handleSlashClose = useCallback(() => {
    setSlashOpen(false);
  }, []);

  const handlePromptSubmit = useCallback(() => {
    if (input.startsWith("/")) {
      const query = input.slice(1).trim();
      const cmd = slashCommands.find((c) => c.name === query);
      if (cmd) {
        handleSlashSelect(cmd);
      }
      return;
    }
    if (!input.trim() && attachments.length === 0) {
      return;
    }
    // 手打「我要转账」与点击建议胶囊同路:先弹收款人选择器(可搜索/新建)。
    // 带金额或其他信息的转账指令原样交给后端,不打断既有用法。
    if (
      /^我要转账[。.!！]?$/.test(input.trim()) &&
      attachments.length === 0 &&
      (status === "ready" || status === "error")
    ) {
      setInput("");
      setLocalStorageInput("");
      onRequestTransfer?.();
      return;
    }
    if (status === "ready" || status === "error") {
      submitForm();
    } else {
      toast.error("Please wait for the model to finish its response!");
    }
  }, [
    attachments.length,
    handleSlashSelect,
    input,
    status,
    submitForm,
    onRequestTransfer,
    setInput,
    setLocalStorageInput,
  ]);

  // 只清输入框与本地草稿,不动会话记录
  const handleClearInput = useCallback(() => {
    setInput("");
    setLocalStorageInput("");
    textareaRef.current?.focus();
  }, [setInput, setLocalStorageInput]);

  const handleTextareaKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (slashOpen) {
        const filtered = slashCommands.filter((cmd) =>
          cmd.name.startsWith(slashQuery.toLowerCase())
        );
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSlashIndex((i) => Math.min(i + 1, filtered.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSlashIndex((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          if (filtered[slashIndex]) {
            handleSlashSelect(filtered[slashIndex]);
          }
          return;
        }
        if (e.key === "Escape") {
          e.preventDefault();
          setSlashOpen(false);
          return;
        }
      }
      if (e.key === "Escape" && editingMessage && onCancelEdit) {
        e.preventDefault();
        onCancelEdit();
      }
    },
    [
      editingMessage,
      handleSlashSelect,
      onCancelEdit,
      slashIndex,
      slashOpen,
      slashQuery,
    ]
  );

  return (
    <div className={cn("relative flex w-full flex-col gap-4", className)}>
      {editingMessage && onCancelEdit ? (
        <div className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <span>Editing message</span>
          <button
            className="rounded px-1.5 py-0.5 text-muted-foreground/50 transition-colors hover:bg-muted hover:text-foreground"
            onMouseDown={handleCancelEditMouseDown}
            type="button"
          >
            Cancel
          </button>
        </div>
      ) : null}

      {/* 建议胶囊只在桌面端保留;手机端的四个高频入口由 QuickTags + 首屏相关问题卡承担 */}
      {!editingMessage &&
        !isLoading &&
        messages.length === 0 &&
        attachments.length === 0 &&
        uploadQueue.length === 0 && (
          <div className="hidden md:block">
            <SuggestedActions chatId={chatId} sendMessage={sendMessage} />
          </div>
        )}

      <input
        className="pointer-events-none fixed -top-4 -left-4 size-0.5 opacity-0"
        multiple
        onChange={handleFileChange}
        ref={fileInputRef}
        tabIndex={-1}
        type="file"
      />

      <div className="relative">
        {slashOpen ? (
          <SlashCommandMenu
            onClose={handleSlashClose}
            onSelect={handleSlashSelect}
            query={slashQuery}
            selectedIndex={slashIndex}
          />
        ) : null}
      </div>

      <PromptInput
        className="[&>div]:rounded-[22px] [&>div]:border [&>div]:border-brand-blue/10 [&>div]:bg-card/90 [&>div]:shadow-[0_18px_44px_-28px_rgba(30,82,165,0.45),inset_0_1px_0_rgba(255,255,255,0.7)] [&>div]:backdrop-blur-xl [&>div]:transition-shadow [&>div]:duration-300 [&>div]:focus-within:border-brand-red/25 [&>div]:focus-within:shadow-[0_0_0_1px_rgba(211,46,25,0.14),0_22px_50px_-30px_rgba(30,82,165,0.55)]"
        onSubmit={handlePromptSubmit}
      >
        {(attachments.length > 0 || uploadQueue.length > 0) && (
          <div
            className="flex w-full self-start flex-row gap-2 overflow-x-auto px-3 pt-3 no-scrollbar"
            data-testid="attachments-preview"
          >
            {attachments.map((attachment) => (
              <AttachmentPreviewItem
                attachment={attachment}
                fileInputRef={fileInputRef}
                key={attachment.url}
                setAttachments={setAttachments}
              />
            ))}

            {uploadQueue.map((filename) => (
              <PreviewAttachment
                attachment={{
                  contentType: "",
                  name: filename,
                  url: "",
                }}
                isUploading={true}
                key={filename}
              />
            ))}
          </div>
        )}
        <PromptInputTextarea
          className="min-h-9 max-h-40 px-4 py-2.5 text-[13px] leading-relaxed placeholder:text-muted-foreground/40"
          data-testid="multimodal-input"
          onChange={handleInput}
          onKeyDown={handleTextareaKeyDown}
          placeholder={
            editingMessage ? "编辑你的消息…" : "请输入您的问题"
          }
          ref={textareaRef}
          value={input}
        />
        <PromptInputFooter className="px-3 pb-1.5">
          <PromptInputTools>
            {/* 手机端左下角设置入口:与侧栏左下角/门户顶栏同一份菜单(桌面端有侧栏,不重复) */}
            <div className="md:hidden">
              <SettingsDropdownButton
                align="start"
                aria-label="设置"
                className="size-7 rounded-lg text-muted-foreground hover:text-foreground"
              >
                <SettingsIcon className="size-4" />
              </SettingsDropdownButton>
            </div>
            <AttachmentsButton fileInputRef={fileInputRef} status={status} />
            {speech.status !== "unsupported" ? (
              <button
                aria-label={speech.status === "listening" ? "停止语音输入" : "语音输入"}
                className={cn(
                  "flex size-7 items-center justify-center rounded-lg border border-border/40 p-1 transition-all",
                  speech.status === "listening"
                    ? "border-red-300/60 text-red-500 animate-pulse dark:border-red-500/40"
                    : "text-muted-foreground hover:border-border/70 hover:text-foreground"
                )}
                data-testid="mic-button"
                disabled={!canListen}
                onClick={handleMicClick}
                title="语音输入：点击开始说话，再点结束；说完手动回车发送"
                type="button"
              >
                <MicIcon className="size-4" />
              </button>
            ) : null}
            <ConnectedModelInfo />
          </PromptInputTools>

          <div className="flex items-center gap-1.5">
            {/* 清空输入(手机端):只清输入框文字,不删会话记录 */}
            {!!input && (
              <button
                aria-label="清空输入"
                className="flex size-7 items-center justify-center rounded-full text-muted-foreground/50 transition-colors hover:text-brand-red active:bg-brand-red-soft md:hidden"
                onClick={handleClearInput}
                type="button"
              >
                <XIcon className="size-4" />
              </button>
            )}

            {status === "submitted" ? (
              <StopButton setMessages={setMessages} stop={stop} />
            ) : (
              <PromptInputSubmit
                className={cn(
                  "h-7 w-7 rounded-xl transition-all duration-200",
                  input.trim()
                    ? "bg-gradient-to-br from-brand-red-solid to-brand-red-deep text-white shadow-[0_6px_16px_-8px_rgba(211,46,25,0.75)] hover:brightness-110 active:scale-95"
                    : "bg-muted text-muted-foreground/25 cursor-not-allowed"
                )}
                data-testid="send-button"
                disabled={!input.trim() || uploadQueue.length > 0}
                status={status}
                variant="secondary"
              >
                <ArrowUpIcon className="size-4" />
              </PromptInputSubmit>
            )}
          </div>
        </PromptInputFooter>
      </PromptInput>
    </div>
  );
}

export const MultimodalInput = memo(
  PureMultimodalInput,
  (prevProps, nextProps) => {
    if (prevProps.input !== nextProps.input) {
      return false;
    }
    if (prevProps.status !== nextProps.status) {
      return false;
    }
    if (!equal(prevProps.attachments, nextProps.attachments)) {
      return false;
    }
    if (prevProps.editingMessage !== nextProps.editingMessage) {
      return false;
    }
    if (prevProps.isLoading !== nextProps.isLoading) {
      return false;
    }
    if (prevProps.messages.length !== nextProps.messages.length) {
      return false;
    }

    return true;
  }
);

function PureAttachmentPreviewItem({
  attachment,
  fileInputRef,
  setAttachments,
}: {
  attachment: Attachment;
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>;
  setAttachments: Dispatch<SetStateAction<Attachment[]>>;
}) {
  const handleRemove = useCallback(() => {
    setAttachments((currentAttachments) =>
      currentAttachments.filter((a) => a.url !== attachment.url)
    );
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, [attachment.url, fileInputRef, setAttachments]);

  return <PreviewAttachment attachment={attachment} onRemove={handleRemove} />;
}

const AttachmentPreviewItem = memo(PureAttachmentPreviewItem);

function PureAttachmentsButton({
  fileInputRef,
  status,
}: {
  fileInputRef: React.MutableRefObject<HTMLInputElement | null>;
  status: UseChatHelpers<ChatMessage>["status"];
}) {
  const { capabilities, currentModelId } = useChatModel();
  const hasVision = capabilities?.[currentModelId]?.vision ?? false;
  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      event.preventDefault();
      fileInputRef.current?.click();
    },
    [fileInputRef]
  );

  return (
    <Button
      className={cn(
        "h-7 w-7 rounded-lg border border-border/40 p-1 transition-colors",
        hasVision
          ? "text-foreground hover:border-border hover:text-foreground"
          : "text-muted-foreground/30 cursor-not-allowed"
      )}
      data-testid="attachments-button"
      disabled={status !== "ready" || !hasVision}
      onClick={handleClick}
      variant="ghost"
    >
      <PaperclipIcon size={14} style={{ height: 14, width: 14 }} />
    </Button>
  );
}

const AttachmentsButton = memo(PureAttachmentsButton);

/* 底部只读信息:当前使用的模型。配置入口统一在左下角「设置 → 大模型管理」,
   这里不再放下拉选择器,也不再放 API Key 钥匙图标。 */
function ConnectedModelInfo() {
  const { selectedModel } = useChatModel();

  return (
    <div
      className="flex h-7 max-w-[180px] items-center gap-1.5 rounded-lg px-2 text-[12px] text-muted-foreground"
      data-testid="connected-model-info"
      title="当前使用的模型;在左下角「设置 → 大模型管理」中管理"
    >
      <ModelSelectorLogo provider={selectedModel.provider} />
      <span className="truncate">{selectedModel.name}</span>
    </div>
  );
}

function PureStopButton({
  stop,
  setMessages,
}: {
  stop: () => void;
  setMessages: UseChatHelpers<ChatMessage>["setMessages"];
}) {
  const handleClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>) => {
      event.preventDefault();
      stop();
      setMessages((messages) => messages);
    },
    [setMessages, stop]
  );

  return (
    <Button
      className="h-7 w-7 rounded-xl bg-gradient-to-br from-ink to-ink-3 p-1 text-white transition-all duration-200 hover:opacity-90 active:scale-95 disabled:bg-muted disabled:text-muted-foreground/25 disabled:cursor-not-allowed"
      data-testid="stop-button"
      onClick={handleClick}
    >
      <StopIcon size={14} />
    </Button>
  );
}

const StopButton = memo(PureStopButton);
