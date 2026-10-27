"use client";

/*
 * 手机扫码登录落地页(2026-10-20)。
 *
 * 电脑端「手机端登录」按钮生成一次性票据二维码;手机扫码打开的本页
 * 拿 ?ticket= 去换正式会话 token,成功即存登录态并回到首页——免输账号密码。
 * 票据 120 秒有效、一次性:过期/重扫都会走失败态,引导回普通登录页。
 */

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  authErrorMessage,
  bankAgentBase,
  storeBankAuth,
} from "@/hooks/use-bank-auth";

type QrLoginResponse = {
  token?: string;
  user_id?: number;
  nickname?: string;
  error?: string;
  detail?: string;
};

function QrLoginInner() {
  const router = useRouter();
  const params = useSearchParams();
  const ticket = (params.get("ticket") ?? "").trim();
  const [state, setState] = useState<"loading" | "ok" | "fail" | "none">(
    ticket ? "loading" : "none"
  );
  const [error, setError] = useState("");
  const [nickname, setNickname] = useState("");

  useEffect(() => {
    if (!ticket) {
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${bankAgentBase()}/api/auth/qr-login`, {
          body: JSON.stringify({ ticket }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res
          .json()
          .catch(() => null)) as QrLoginResponse | null;
        if (cancelled) {
          return;
        }
        if (!res.ok || !json?.token) {
          setError(authErrorMessage(json, "二维码已失效,请回电脑端刷新后重扫"));
          setState("fail");
          return;
        }
        storeBankAuth(json.token, json);
        setNickname(json.nickname ?? "");
        setState("ok");
        // 登录后第一屏统一是银行首页(余额/今日待办),与 /login 同口径;
        // 电脑端会话由 /chat 打开时自动恢复,不再显式跳 /chat/<thread>
        if (cancelled) {
          return;
        }
        // 给"登录成功"一个可见瞬间再跳转,避免白屏式的突兀
        setTimeout(() => {
          if (!cancelled) {
            router.replace("/home");
          }
        }, 600);
      } catch {
        if (!cancelled) {
          setError("无法连接服务器,请确认与电脑在同一网络后重试");
          setState("fail");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ticket, router]);

  const goHome = useCallback(() => router.replace("/"), [router]);

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">AI 银行</h1>
      <p className="text-sm text-muted-foreground">手机扫码登录</p>

      <div className="mt-6 flex flex-col gap-4" data-testid="qr-login-status">
        {state === "none" ? (
          <>
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
              链接里没有登录凭证,请回电脑端重新扫码。
            </p>
            <Link
              className="text-center text-[13px] text-muted-foreground underline-offset-4 hover:underline"
              href="/login"
            >
              改用账号密码登录
            </Link>
          </>
        ) : null}

        {state === "loading" ? (
          <p className="px-3 py-2 text-[13px] text-muted-foreground">
            正在验证二维码,请稍候…
          </p>
        ) : null}

        {state === "ok" ? (
          <p
            className="rounded-lg bg-emerald-500/10 px-3 py-2 text-[13px] text-emerald-600 dark:text-emerald-400"
            data-testid="qr-login-ok"
          >
            登录成功{nickname ? `,欢迎 ${nickname}` : ""},正在进入首页…
          </p>
        ) : null}

        {state === "fail" ? (
          <>
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
              {error}
            </p>
            <div className="flex flex-col gap-2">
              <Link href="/login">
                <Button
                  className="w-full rounded-lg bg-brand-red-solid text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] hover:bg-brand-red-deep"
                  type="button"
                >
                  改用账号密码登录
                </Button>
              </Link>
              <Button onClick={goHome} type="button" variant="outline">
                先去逛逛(观光模式)
              </Button>
            </div>
          </>
        ) : null}
      </div>
    </>
  );
}

export default function QrLoginPage() {
  return (
    <Suspense
      fallback={<p className="text-sm text-muted-foreground">正在打开…</p>}
    >
      <QrLoginInner />
    </Suspense>
  );
}
