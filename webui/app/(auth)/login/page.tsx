"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useState,
} from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  authErrorMessage,
  bankAgentBase,
  storeBankAuth,
} from "@/hooks/use-bank-auth";

/*
 * 登录页(2026-10-27 起两步):账号+密码 → 挑战(短信验证码) → 会话。
 * 演示环境无真短信:后端回显 demo_code,第二步卡片以"📱 模拟短信"展示,
 * 点「填入」自动带入输入框(AGENT_OTP_ECHO=0 时后端不回显,走真短信口径)。
 * 成功 → 存 bank-token/bank-nickname → 回首页;失败展示后端中文 error。
 */

type LoginResponse = {
  token?: string;
  user_id?: number;
  nickname?: string;
  otp_required?: boolean;
  challenge_id?: number;
  phone_masked?: string;
  expires_in?: number;
  demo_code?: string;
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

const RESEND_SECONDS = 60;

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
    <li className="flex items-center gap-2 rounded-lg border border-brand-blue/10 bg-white/60 px-2.5 py-2 dark:border-white/10 dark:bg-white/5">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[13px] font-medium text-foreground">
            {account.name}
          </span>
          <span className="rounded-full bg-brand-red-soft px-1.5 py-0.5 text-[10px] text-brand-red-deep dark:text-brand-red">
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
  // 第二步(验证码)状态:挑战信息 + 输入码 + 重发倒计时
  const [challenge, setChallenge] = useState<LoginResponse | null>(null);
  const [code, setCode] = useState("");
  const [resendLeft, setResendLeft] = useState(0);

  useEffect(() => {
    if (resendLeft <= 0) {
      return;
    }
    const timer = setInterval(() => setResendLeft((n) => n - 1), 1000);
    return () => clearInterval(timer);
  }, [resendLeft]);

  const handleIdentifierChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setIdentifier(e.target.value),
    []
  );
  const handlePasswordChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value),
    []
  );
  const handleCodeChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) =>
      setCode(e.target.value.replace(/\D/g, "").slice(0, 6)),
    []
  );
  const handleFillDemo = useCallback((account: DemoAccount) => {
    setIdentifier(account.phone);
    setPassword(account.password);
    setError("");
  }, []);

  const finishLogin = useCallback(
    (json: LoginResponse) => {
      storeBankAuth(json.token ?? "", json);
      // 登录后第一屏统一是银行首页(余额/今日待办),对话从首页「进入对话」进;
      // 手机端不再跳 /chat/<thread>(电脑端会话由 /chat 打开时自动恢复)。
      // replace 而非 push:登录成功后 /login 不得留在浏览器历史栈,
      // 手机 App 返回键(goBack)才不会从首页退回登录页;登出才是回登录页的唯一入口
      router.replace("/home");
    },
    [router]
  );

  const postLogin = useCallback(
    async (body: Record<string, unknown>): Promise<{
      ok: boolean;
      status: number;
      json: LoginResponse | null;
    }> => {
      const res = await fetch(`${bankAgentBase()}/api/auth/login`, {
        body: JSON.stringify(body),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      const json = (await res.json().catch(() => null)) as LoginResponse | null;
      return { ok: res.ok, status: res.status, json };
    },
    []
  );

  // 第一步:账号密码 → 拿验证码挑战(后端 OTP 关闭时直接返回 token)
  const requestCode = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const { ok, json } = await postLogin({
        identifier: identifier.trim(),
        password,
      });
      if (!ok || !json) {
        setError(authErrorMessage(json, "登录失败，请检查账号与密码"));
        return;
      }
      if (json.otp_required) {
        setChallenge(json);
        setCode("");
        setResendLeft(RESEND_SECONDS);
        return;
      }
      if (json.token) {
        await finishLogin(json);
      }
    } catch {
      setError("无法连接服务器，请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [finishLogin, identifier, password, postLogin]);

  // 第二步:验证码换会话
  const verifyCode = useCallback(
    async (e: FormEvent<HTMLFormElement>) => {
      e.preventDefault();
      if (loading || !challenge?.challenge_id || code.length !== 6) {
        return;
      }
      setError("");
      setLoading(true);
      try {
        const { ok, json } = await postLogin({
          identifier: identifier.trim(),
          password,
          challenge_id: challenge.challenge_id,
          code,
        });
        if (!ok || !json?.token) {
          setError(authErrorMessage(json, "验证失败，请重试"));
          return;
        }
        await finishLogin(json);
      } catch {
        setError("无法连接服务器，请稍后重试");
      } finally {
        setLoading(false);
      }
    },
    [challenge, code, finishLogin, identifier, loading, password, postLogin]
  );

  const backToStepOne = useCallback(() => {
    setChallenge(null);
    setCode("");
    setError("");
  }, []);

  const fillDemoCode = useCallback(() => {
    if (challenge?.demo_code) {
      setCode(challenge.demo_code);
    }
  }, [challenge]);

  const inOtpStep = challenge?.otp_required === true;

  return (
    <>
      <div className="mb-1 flex items-center gap-2.5">
        <div className="flex flex-col">
          <span className="text-[15px] font-semibold text-ink dark:text-brand-blue">AI 银行助手</span>
          <span className="text-[11px] text-muted-foreground">
            智能对话银行 · AI 处理常规，人负责例外
          </span>
        </div>
      </div>
      <h1 className="sr-only text-2xl font-semibold tracking-tight">AI 银行</h1>
      <p className="mt-3 text-sm text-muted-foreground">
        {inOtpStep ? "第二步：短信验证码校验" : "欢迎回来，请登录你的账户"}
      </p>

      {inOtpStep ? (
        <form className="mt-6 flex flex-col gap-4" onSubmit={verifyCode}>
          <div className="flex flex-col gap-2">
            <Label htmlFor="otp-code">
              短信验证码(已发送至 {challenge.phone_masked})
            </Label>
            <Input
              autoComplete="one-time-code"
              className="text-lg tracking-[0.5em]"
              data-testid="otp-code-input"
              id="otp-code"
              inputMode="numeric"
              onChange={handleCodeChange}
              placeholder="6 位验证码"
              required
              value={code}
            />
          </div>

          {challenge.demo_code ? (
            <div
              className="flex items-center justify-between gap-2 rounded-xl border border-brand-blue/20 bg-brand-blue-soft/70 px-3 py-2.5 dark:border-brand-blue/25 dark:bg-brand-blue/10"
              data-testid="demo-sms"
            >
              <div className="min-w-0 text-[12px] leading-relaxed text-ink dark:text-brand-blue">
                <span className="mr-1" aria-hidden>
                  📱
                </span>
                模拟短信：验证码{" "}
                <span className="font-mono text-[14px] font-semibold tracking-widest">
                  {challenge.demo_code}
                </span>
                <span className="ml-1 text-muted-foreground">
                  （演示环境，未真实发送）
                </span>
              </div>
              <Button
                className="h-7 shrink-0 rounded-lg px-2.5 text-[12px]"
                data-testid="demo-sms-fill"
                onClick={fillDemoCode}
                size="sm"
                type="button"
                variant="outline"
              >
                填入
              </Button>
            </div>
          ) : null}

          {error ? (
            <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
              {error}
            </p>
          ) : null}

          <div className="flex items-center gap-2">
            <Button
              className="sheen flex-1 rounded-lg bg-brand-red-solid text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none"
              disabled={loading || code.length !== 6}
              type="submit"
            >
              {loading ? "校验中…" : "验证并登录"}
            </Button>
            <Button
              className="rounded-lg"
              disabled={resendLeft > 0 || loading}
              onClick={requestCode}
              size="sm"
              type="button"
              variant="outline"
            >
              {resendLeft > 0 ? `重新发送 ${resendLeft}s` : "重新发送"}
            </Button>
          </div>

          <button
            className="text-left text-[12px] text-muted-foreground underline-offset-4 hover:underline"
            onClick={backToStepOne}
            type="button"
          >
            ← 返回上一步(修改账号或密码)
          </button>
        </form>
      ) : (
        <form
          className="mt-6 flex flex-col gap-4"
          onSubmit={(e) => {
            e.preventDefault();
            void requestCode();
          }}
        >
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
            className="sheen rounded-lg bg-brand-red-solid text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none"
            disabled={loading || !identifier.trim() || !password}
            type="submit"
          >
            {loading ? "登录中…" : "登录"}
          </Button>

          <p className="text-center text-[13px] text-muted-foreground">
            {"没有账号？"}
            <Link
              className="text-foreground underline-offset-4 hover:underline"
              href="/register"
            >
              注册
            </Link>
          </p>
        </form>
      )}

      <div
        className="mt-4 rounded-xl border border-white/60 bg-white/55 px-3 py-3 backdrop-blur-sm dark:border-white/10 dark:bg-white/[0.04]"
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
          登录需短信验证码(演示页会直接显示)；动钱仍需支付密码，陈明的支付密码为
          888888。
        </p>
      </div>
    </>
  );
}
