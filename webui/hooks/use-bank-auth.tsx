"use client";

/*
 * 银行账号体系前端(agent 后端 :8800 /api/auth/*):
 * - 登录态 = localStorage 里的 bank-token(+昵称/用户 ID 供界面展示);
 * - 会话 token 随聊天 / 历史请求带给后端(见 use-active-chat / sidebar-history);
 * - 登出 = POST /api/auth/logout 撤销会话 → 清本地 → 刷新页面回到未登录态。
 */

import { useCallback, useEffect, useState } from "react";

const TOKEN_KEY = "bank-token";
const NICKNAME_KEY = "bank-nickname";
const USER_ID_KEY = "bank-user-id";

export type BankAuthUser = {
  nickname: string;
  user_id: string;
};

export function bankAgentBase(): string {
  const port = process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800";
  return `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${port}`;
}

export function getStoredToken(): string {
  if (typeof window === "undefined") {
    return "";
  }
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}

/** 登录/注册成功后落库:token + 昵称 + 用户 ID(后端返回结构原样可传) */
export function storeBankAuth(
  token: string,
  info: { nickname?: string; user_id?: number | string }
): void {
  if (typeof window === "undefined" || !token) {
    return;
  }
  window.localStorage.setItem(TOKEN_KEY, token);
  if (info.nickname !== undefined) {
    window.localStorage.setItem(NICKNAME_KEY, info.nickname);
  }
  if (info.user_id !== undefined) {
    window.localStorage.setItem(USER_ID_KEY, String(info.user_id));
  }
}

export function clearBankAuth(): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(NICKNAME_KEY);
  window.localStorage.removeItem(USER_ID_KEY);
}

/** 从后端响应里提取可展示的中文错误:优先 {error},其次字符串 {detail}(FastAPI 校验错误) */
export function authErrorMessage(json: unknown, fallback: string): string {
  const j = json as { error?: unknown; detail?: unknown } | null;
  if (typeof j?.error === "string" && j.error) {
    return j.error;
  }
  if (typeof j?.detail === "string" && j.detail) {
    return j.detail;
  }
  return fallback;
}

function readUser(): BankAuthUser | null {
  if (typeof window === "undefined") {
    return null;
  }
  const token = window.localStorage.getItem(TOKEN_KEY);
  if (!token) {
    return null;
  }
  return {
    nickname: window.localStorage.getItem(NICKNAME_KEY) ?? "",
    user_id: window.localStorage.getItem(USER_ID_KEY) ?? "",
  };
}

export function useBankAuth() {
  // 首帧 SSR 与客户端一致渲染为未登录,挂载后再读 localStorage,避免水合错位
  const [user, setUser] = useState<BankAuthUser | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setUser(readUser());
    setReady(true);
  }, []);

  const login = useCallback(
    (token: string, info: { nickname?: string; user_id?: number | string }) => {
      storeBankAuth(token, info);
      setUser(readUser());
    },
    []
  );

  const logout = useCallback(async () => {
    const token = getStoredToken();
    // 后端撤销会话;即使请求失败也照常清本地登录态并刷新
    try {
      if (token) {
        await fetch(`${bankAgentBase()}/api/auth/logout`, {
          body: JSON.stringify({ token }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        });
      }
    } catch {
      // 网络异常不阻断本地登出
    }
    clearBankAuth();
    window.location.href = `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/`;
  }, []);

  return { login, logout, ready, token: ready ? getStoredToken() : "", user };
}
