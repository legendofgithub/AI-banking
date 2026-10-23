"use client";

/*
 * 手机 ↔ 电脑会话同步(2026-10-21):
 *
 * 服务端维护每个用户"当前会话 + revision":
 * - 打开会话时 POST /api/sync/active,另一台设备轮询到后自动跟随;
 * - 每轮对话开始/结束 revision +1,另一台设备据此拉 /api/history 更新消息。
 *
 * 1.5s 轮询(个人会话量级足够,且不需要额外长连接/端口);闸门卡片随
 * pending_payload 一起写入历史,另一台设备回拉时能重建转账确认等卡片。
 * 自己写入的 revision(last_client_id 相同)不回拉,防止打断本地流式回复。
 */

import type { UseChatHelpers } from "@ai-sdk/react";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { bankAuthHeaders } from "@/hooks/use-bank-auth";
import { getClientId } from "@/lib/device-client";
import type { ChatMessage } from "@/lib/types";
import { fetcher } from "@/lib/utils";

type SyncState = {
  thread_id: string | null;
  title: string;
  updated_at: string;
  revision: number;
  last_client_id: string;
};

export function useDeviceSync({
  agentBase,
  chatId,
  isNewChat,
  setMessages,
  status,
  token,
}: {
  agentBase: string;
  chatId: string;
  isNewChat: boolean;
  setMessages: UseChatHelpers<ChatMessage>["setMessages"];
  status: UseChatHelpers<ChatMessage>["status"];
  token: string;
}) {
  const router = useRouter();
  const { data: sync } = useSWR<SyncState>(
    token ? `${agentBase}/api/sync/state` : null,
    fetcher,
    {
      dedupingInterval: 1000,
      refreshInterval: 1500,
      revalidateOnFocus: false,
      shouldRetryOnError: false,
    }
  );

  const holdNavUntilRef = useRef(0);
  const navigatingToRef = useRef("");
  const lastRevisionRef = useRef(-1);
  const syncingRef = useRef(false);
  // 服务端 active 上次看到的值:跟随只在它"发生变化"时触发。
  // 若按"≠本地就拽",新会话首条消息还在后端排队(thread 行未落库,
  // /api/sync/active 登记不上)时,服务端 active 仍指旧会话,2.5s
  // 保护窗一过就会把刚点开新会话的用户弹回旧会话。
  const lastSeenServerThreadRef = useRef<string | null>(null);
  const [navTick, setNavTick] = useState(0);

  // 打开一个已存在的会话:登记为本机当前会话(另一台设备会跟随过来)
  useEffect(() => {
    if (!token || isNewChat) {
      return;
    }
    // 刚主动切换会话时,给服务端 2.5s 消化 active 写入,避免旧轮询把页面弹回去
    holdNavUntilRef.current = Date.now() + 2500;
    const controller = new AbortController();
    // 新会话的 thread 行由 /api/chat 请求创建,稍等一拍再登记 active,
    // 避免刚 pushState 就 POST 造成无意义的 404
    const timer = setTimeout(() => {
      fetch(`${agentBase}/api/sync/active`, {
        body: JSON.stringify({ thread_id: chatId }),
        headers: { ...bankAuthHeaders(), "Content-Type": "application/json" },
        method: "POST",
        signal: controller.signal,
      }).catch(() => {
        // 会话还不存在/网络抖动:下一轮轮询仍会按服务端 active 对齐
      });
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [agentBase, chatId, isNewChat, token]);

  // 另一台设备切换了当前会话 → 跟随
  // 注意:首页(/ 新会话)不自动跟随,避免正常登录/主动开新会话被拽回旧会话;
  // 手机扫码登录页会在落地时直接跳到当前会话(见 /qr-login)。
  // 只跟随服务端 active 的"变化":值没变 ≠ 另一台设备切了会话
  // (新会话落库前服务端一直返回旧值),不拽人。
  // biome-ignore lint/correctness/useExhaustiveDependencies: navTick 仅作 hold 到期重查触发器
  useEffect(() => {
    const target = sync?.thread_id;
    if (!target) {
      lastSeenServerThreadRef.current = null;
      return;
    }
    if (isNewChat) {
      // 首页不跟随,但记住服务端当前值:从首页开新会话时它仍是旧值,
      // 不能被误判成"另一台设备刚切换"
      lastSeenServerThreadRef.current = target;
      return;
    }
    if (target === chatId) {
      lastSeenServerThreadRef.current = target;
      navigatingToRef.current = "";
      return;
    }
    if (Date.now() < holdNavUntilRef.current) {
      // 留守窗口内先不跟随(也不记录该值);窗口结束再主动检查一次,避免错过切换
      const wait = holdNavUntilRef.current - Date.now() + 50;
      const timer = setTimeout(() => setNavTick((tick) => tick + 1), wait);
      return () => clearTimeout(timer);
    }
    if (lastSeenServerThreadRef.current === target) {
      // 服务端 active 与上次看到的一致:本机在新会话等待落库等场景,
      // 不是设备间切换,保持原地
      return;
    }
    lastSeenServerThreadRef.current = target;
    if (navigatingToRef.current === target) {
      return;
    }
    navigatingToRef.current = target;
    router.push(`/chat/${target}`);
  }, [sync?.thread_id, chatId, isNewChat, router, navTick]);

  // 会话切换后重置同步游标
  // biome-ignore lint/correctness/useExhaustiveDependencies: chatId 仅作切换触发器
  useEffect(() => {
    lastRevisionRef.current = -1;
    syncingRef.current = false;
  }, [chatId]);

  // 对方产生新内容 → 拉服务端历史替换本地消息
  useEffect(() => {
    if (!token || !sync?.thread_id || sync.thread_id !== chatId) {
      return;
    }
    if (sync.revision === lastRevisionRef.current) {
      return;
    }
    // 本机流式回复中:等 finish 后下一轮轮询再同步
    if (status === "submitted" || status === "streaming") {
      return;
    }
    // 自己刚写的内容:本地已有完整消息/卡片,不回拉
    if (sync.last_client_id && sync.last_client_id === getClientId()) {
      lastRevisionRef.current = sync.revision;
      return;
    }
    if (syncingRef.current) {
      return;
    }
    syncingRef.current = true;
    const controller = new AbortController();
    fetch(`${agentBase}/api/history?thread_id=${encodeURIComponent(chatId)}`, {
      headers: bankAuthHeaders(),
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((json: { messages?: ChatMessage[] } | null) => {
        if (Array.isArray(json?.messages)) {
          setMessages(json.messages);
          lastRevisionRef.current = sync.revision;
        }
      })
      .catch(() => {
        // 下一轮轮询重试;不重置 revision,保持游标等待成功
      })
      .finally(() => {
        syncingRef.current = false;
      });
    return () => controller.abort();
  }, [agentBase, chatId, setMessages, status, sync, token]);
}
