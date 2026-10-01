"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type ChangeEvent, type FormEvent, useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  authErrorMessage,
  bankAgentBase,
  storeBankAuth,
} from "@/hooks/use-bank-auth";

/*
 * 登录页:账号(手机号/邮箱) + 登录密码 → POST {agentBase}/api/auth/login。
 * 成功 → 存 bank-token/bank-nickname → 回首页;失败展示后端中文 error。
 */

type LoginResponse = {
  token?: string;
  user_id?: number;
  nickname?: string;
  error?: string;
  detail?: string;
};

export default function LoginPage() {
  const router = useRouter();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleIdentifierChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setIdentifier(e.target.value),
    []
  );
  const handlePasswordChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value),
    []
  );
  const handleFillDemo = useCallback(() => {
    setIdentifier("13800002233");
    setPassword("Demo@12345");
    setError("");
  }, []);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (loading) {
      return;
    }
    setError("");
    setLoading(true);
    try {
      const res = await fetch(`${bankAgentBase()}/api/auth/login`, {
        body: JSON.stringify({
          identifier: identifier.trim(),
          password,
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      const json = (await res.json().catch(() => null)) as LoginResponse | null;
      if (!res.ok || !json?.token) {
        setError(authErrorMessage(json, "登录失败,请检查账号与密码"));
        return;
      }
      storeBankAuth(json.token, json);
      router.push("/");
    } catch {
      setError("无法连接服务器,请稍后重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">AI 银行</h1>
      <p className="text-sm text-muted-foreground">欢迎回来,请登录你的账户</p>

      <form className="mt-6 flex flex-col gap-4" onSubmit={handleSubmit}>
        <div className="flex flex-col gap-2">
          <Label htmlFor="identifier">账号(手机号 / 邮箱)</Label>
          <Input
            autoComplete="username"
            id="identifier"
            onChange={handleIdentifierChange}
            placeholder="手机号或邮箱"
            required
            value={identifier}
          />
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="password">登录密码</Label>
          <Input
            autoComplete="current-password"
            id="password"
            onChange={handlePasswordChange}
            placeholder="登录密码"
            required
            type="password"
            value={password}
          />
        </div>

        {error ? (
          <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
            {error}
          </p>
        ) : null}

        <Button
          className="rounded-lg bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50"
          disabled={loading || !identifier.trim() || !password}
          type="submit"
        >
          {loading ? "登录中…" : "登录"}
        </Button>

        <p className="text-center text-[13px] text-muted-foreground">
          {"没有账号?"}
          <Link
            className="text-foreground underline-offset-4 hover:underline"
            href="/register"
          >
            注册
          </Link>
        </p>
      </form>

      <div className="mt-4 rounded-xl border border-border/60 bg-muted/30 px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
        演示账号:手机号 13800002233 / 密码 Demo@12345
        <button
          className="ml-2 text-amber-600 underline-offset-2 hover:underline dark:text-amber-400"
          onClick={handleFillDemo}
          type="button"
        >
          一键填入
        </button>
      </div>
    </>
  );
}
