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

type DemoAccount = {
  name: string;
  phone: string;
  password: string;
  badge: string;
  note?: string;
};

// 演示账号清单：只放"手机号 + 已知密码"的账号，便于现场一键填入。
// 陈明来自 seed + ensure_demo_auth，任何一次播种都存在；测试无测评是本机
// 脚本注册的测试账号（无风险测评），重新播种后会消失，因此单独标注。
const DEMO_ACCOUNTS: DemoAccount[] = [
  {
    badge: "主演示",
    name: "陈明",
    password: "Demo@12345",
    phone: "13800002233",
  },
  {
    badge: "本机测试",
    name: "测试无测评",
    note: "本机已有；重新播种演示库后需重新注册",
    password: "Test@12345",
    phone: "13900007788",
  },
];

function DemoAccountRow({
  account,
  onFill,
}: {
  account: DemoAccount;
  onFill: (account: DemoAccount) => void;
}) {
  const handleFill = useCallback(() => onFill(account), [account, onFill]);

  return (
    <li className="flex items-center gap-2 rounded-lg border border-border/40 bg-background/50 px-2.5 py-2">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[13px] font-medium text-foreground">
            {account.name}
          </span>
          <span className="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-300">
            {account.badge}
          </span>
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] text-muted-foreground">
          <span>
            手机号{" "}
            <span className="font-mono text-foreground/80">
              {account.phone}
            </span>
          </span>
          <span>
            密码{" "}
            <span className="font-mono text-foreground/80">
              {account.password}
            </span>
          </span>
        </div>
        {account.note ? (
          <p className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground/80">
            {account.note}
          </p>
        ) : null}
      </div>
      <Button
        className="h-7 shrink-0 rounded-lg px-2.5 text-[12px]"
        data-testid={`demo-fill-${account.phone}`}
        onClick={handleFill}
        size="sm"
        type="button"
        variant="outline"
      >
        一键填入
      </Button>
    </li>
  );
}

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
  const handleFillDemo = useCallback((account: DemoAccount) => {
    setIdentifier(account.phone);
    setPassword(account.password);
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
      // 手机登录后加入电脑端当前会话(与扫码登录同一口径);电脑端正常回首页
      let target = "/";
      if (typeof window !== "undefined" && window.innerWidth < 768) {
        try {
          const syncRes = await fetch(`${bankAgentBase()}/api/sync/state`, {
            headers: { "x-bank-token": json.token },
          });
          const sync = (await syncRes.json().catch(() => null)) as {
            thread_id?: string | null;
          } | null;
          if (sync?.thread_id) {
            target = `/chat/${encodeURIComponent(sync.thread_id)}`;
          }
        } catch {
          // 同步接口不可用时仍回首页,不阻断登录
        }
      }
      router.push(target);
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

      <div
        className="mt-4 rounded-xl border border-border/60 bg-muted/30 px-3 py-3"
        data-testid="demo-account-list"
      >
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-1">
          <span className="text-[12px] font-medium text-foreground">
            演示账号
          </span>
          <span className="text-[11px] text-muted-foreground">
            点「一键填入」自动填手机号和密码
          </span>
        </div>
        <ul className="flex flex-col gap-2">
          {DEMO_ACCOUNTS.map((account) => (
            <DemoAccountRow
              account={account}
              key={account.phone}
              onFill={handleFillDemo}
            />
          ))}
        </ul>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
          登录后动钱仍需支付密码；陈明的支付密码为 888888。
        </p>
      </div>
    </>
  );
}
