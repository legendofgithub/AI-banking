"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  type ChangeEvent,
  type CSSProperties,
  type FormEvent,
  useCallback,
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
 * 注册页(实名口径):真实姓名 + 身份证号(18 位格式) + 手机号(必填,
 * 即登录账号) + 邮箱(选填) + 登录密码(实时策略清单) + 确认登录密码
 * + 支付密码(6 位纯数字) + 确认支付密码 → POST {agentBase}/api/auth/register。
 * 前端实时校验与后端同规则(测试阶段身份证只校验 18 位形状,不校验 GB 11643
 * 校验码;登录密码 ≥8 位且大写/小写/数字/符号;支付密码 6 位纯数字)。
 * 点击注册时如仍有不合格字段:滚动聚焦到第一处,并让所有不合格字段闪烁两次。
 */

const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/;
const PHONE_RE = /^1\d{10}$/;
const ID_CARD_RE = /^\d{17}[\dXx]$/;

// 演示阶段:只校验 18 位形状(前 17 位数字,末位数字或 X),不校验校验码。
// 恢复实名 KYC 时改回 GB 11643 校验,并同步后端 agent/auth.py。
function isIdCardShape(v: string): boolean {
  return ID_CARD_RE.test(v);
}

const flashTimers = new WeakMap<HTMLElement, number>();

function flashField(element: HTMLElement | null): void {
  if (!element) {
    return;
  }
  const previous = flashTimers.get(element);
  if (previous !== undefined) {
    window.clearTimeout(previous);
  }
  element.classList.remove("field-flash");
  // 强制重排,连续点击也能重新播放两次闪烁
  element.getBoundingClientRect();
  element.classList.add("field-flash");
  flashTimers.set(
    element,
    window.setTimeout(() => {
      element.classList.remove("field-flash");
      flashTimers.delete(element);
    }, 1100)
  );
}

type RegisterResponse = {
  token?: string;
  user_id?: number;
  nickname?: string;
  error?: string;
  detail?: string;
};

export default function RegisterPage() {
  const router = useRouter();
  const [realName, setRealName] = useState("");
  const [idCard, setIdCard] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [payPassword, setPayPassword] = useState("");
  const [payPasswordConfirm, setPayPasswordConfirm] = useState("");
  const [error, setError] = useState("");
  const [submitAttempted, setSubmitAttempted] = useState(false);
  const [loading, setLoading] = useState(false);

  const handleRealNameChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setRealName(e.target.value),
    []
  );
  // 身份证:统一大写(校验位 x → X),最多 18 位
  const handleIdCardChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) =>
      setIdCard(e.target.value.toUpperCase().slice(0, 18)),
    []
  );
  const handlePhoneChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setPhone(e.target.value.trim()),
    []
  );
  const handleEmailChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setEmail(e.target.value.trim()),
    []
  );
  const handlePasswordChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value),
    []
  );
  const handlePasswordConfirmChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) => setPasswordConfirm(e.target.value),
    []
  );
  // 支付密码:只留数字、最多 6 位
  const handlePayPasswordChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) =>
      setPayPassword(e.target.value.replace(/\D/g, "").slice(0, 6)),
    []
  );
  const handlePayPasswordConfirmChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) =>
      setPayPasswordConfirm(e.target.value.replace(/\D/g, "").slice(0, 6)),
    []
  );

  const trimmedPhone = phone.trim();
  const trimmedEmail = email.trim();

  // 登录密码策略清单(与后端 validate_login_password 同规则)
  const passwordChecks = [
    { label: "至少 8 位", ok: password.length >= 8 },
    { label: "含大写字母", ok: /[A-Z]/.test(password) },
    { label: "含小写字母", ok: /[a-z]/.test(password) },
    { label: "含数字", ok: /\d/.test(password) },
    { label: "含符号(如 !@#$%)", ok: /[^A-Za-z0-9]/.test(password) },
  ];
  const passwordValid = passwordChecks.every((c) => c.ok);

  const payPasswordValid = /^\d{6}$/.test(payPassword);
  const realNameValid =
    realName.trim().length >= 2 && realName.trim().length <= 20;
  const idCardDirty = idCard.length > 0;
  const idCardValid = isIdCardShape(idCard);
  const phoneDirty = trimmedPhone.length > 0;
  const phoneValid = PHONE_RE.test(trimmedPhone);
  const emailDirty = trimmedEmail.length > 0;
  const emailValid = !emailDirty || EMAIL_RE.test(trimmedEmail);
  const passwordConfirmInvalid =
    passwordConfirm.length === 0 || passwordConfirm !== password;
  const payPasswordConfirmInvalid =
    payPasswordConfirm.length === 0 || payPasswordConfirm !== payPassword;

  // 标红/闪烁范围:输入过或点过注册后,仍不合格的字段
  const realNameInvalid =
    !realNameValid && (submitAttempted || realName.length > 0);
  const idCardInvalid = !idCardValid && (submitAttempted || idCardDirty);
  const phoneInvalid = !phoneValid && (submitAttempted || phoneDirty);
  const emailInvalid = !emailValid && (submitAttempted || emailDirty);
  const passwordInvalid =
    !passwordValid && (submitAttempted || password.length > 0);
  const passwordConfirmInvalidVisible =
    passwordConfirmInvalid && (submitAttempted || passwordConfirm.length > 0);
  const payPasswordInvalid =
    !payPasswordValid && (submitAttempted || payPassword.length > 0);
  const payPasswordConfirmInvalidVisible =
    payPasswordConfirmInvalid &&
    (submitAttempted || payPasswordConfirm.length > 0);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (loading) {
      return;
    }
    const invalidFieldIds = [
      !realNameValid && "real-name",
      !idCardValid && "id-card",
      !phoneValid && "phone",
      !emailValid && "email",
      !passwordValid && "password",
      passwordConfirmInvalid && "password-confirm",
      !payPasswordValid && "pay-password",
      payPasswordConfirmInvalid && "pay-password-confirm",
    ].filter((value): value is string => Boolean(value));

    setSubmitAttempted(true);
    if (invalidFieldIds.length > 0) {
      requestAnimationFrame(() => {
        const first = document.getElementById(invalidFieldIds[0]);
        first?.scrollIntoView({ behavior: "smooth", block: "center" });
        first?.focus({ preventScroll: true });
        for (const id of invalidFieldIds) {
          flashField(document.getElementById(id));
        }
      });
      return;
    }

    setError("");
    setLoading(true);
    try {
      const res = await fetch(`${bankAgentBase()}/api/auth/register`, {
        body: JSON.stringify({
          email: trimmedEmail,
          id_card: idCard.trim(),
          login_password: password,
          pay_password: payPassword,
          phone: trimmedPhone,
          real_name: realName.trim(),
        }),
        headers: { "Content-Type": "application/json" },
        method: "POST",
      });
      const json = (await res
        .json()
        .catch(() => null)) as RegisterResponse | null;
      if (!res.ok || !json?.token) {
        setError(authErrorMessage(json, "注册失败,请检查填写内容"));
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
      <p className="text-sm text-muted-foreground">
        填写以下信息,开通你的银行账户
      </p>

      <form
        className="mt-6 flex flex-col gap-4"
        noValidate
        onSubmit={handleSubmit}
      >
        <div className="flex flex-col gap-2">
          <Label htmlFor="real-name">真实姓名</Label>
          <Input
            aria-invalid={realNameInvalid}
            id="real-name"
            maxLength={20}
            onChange={handleRealNameChange}
            placeholder="2-20 个字符,需与身份证一致"
            required
            value={realName}
          />
          {realNameInvalid ? (
            <p className="text-xs text-destructive">
              请输入 2-20 个字符的真实姓名
            </p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="id-card">身份证号</Label>
          <Input
            aria-invalid={idCardInvalid}
            autoComplete="off"
            className="font-mono"
            id="id-card"
            inputMode="numeric"
            maxLength={18}
            onChange={handleIdCardChange}
            placeholder="18 位身份证号"
            required
            value={idCard}
          />
          {idCardInvalid ? (
            <p className="text-xs text-destructive">
              身份证号需为 18 位(前 17 位数字,末位数字或 X);测试阶段不校验校验码
            </p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="phone">手机号</Label>
          <Input
            aria-invalid={phoneInvalid}
            autoComplete="tel"
            id="phone"
            inputMode="numeric"
            maxLength={11}
            onChange={handlePhoneChange}
            placeholder="11 位手机号(1 开头),将作为登录账号"
            required
            value={phone}
          />
          {phoneInvalid ? (
            <p className="text-xs text-destructive">
              请输入合法的手机号(1 开头 11 位)
            </p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="email">
            邮箱<span className="text-muted-foreground">(选填)</span>
          </Label>
          <Input
            aria-invalid={emailInvalid}
            autoComplete="email"
            id="email"
            onChange={handleEmailChange}
            placeholder="用于接收通知,可不填"
            type="email"
            value={email}
          />
          {emailInvalid ? (
            <p className="text-xs text-destructive">邮箱格式不正确</p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="password">登录密码</Label>
          <Input
            aria-invalid={passwordInvalid}
            autoComplete="new-password"
            id="password"
            onChange={handlePasswordChange}
            placeholder="至少 8 位,含大小写字母、数字与符号"
            required
            type="password"
            value={password}
          />
          <ul className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
            {passwordChecks.map((check) => (
              <li
                className={
                  check.ok
                    ? "flex items-center gap-1 text-emerald-600 dark:text-emerald-400"
                    : "flex items-center gap-1 text-muted-foreground"
                }
                key={check.label}
              >
                <span aria-hidden>{check.ok ? "✓" : "○"}</span>
                {check.label}
              </li>
            ))}
          </ul>
          {passwordInvalid ? (
            <p className="text-xs text-destructive">登录密码未满足全部要求</p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="password-confirm">确认登录密码</Label>
          <Input
            aria-invalid={passwordConfirmInvalidVisible}
            autoComplete="new-password"
            id="password-confirm"
            onChange={handlePasswordConfirmChange}
            placeholder="再次输入登录密码"
            required
            type="password"
            value={passwordConfirm}
          />
          {passwordConfirmInvalidVisible ? (
            <p className="text-xs text-destructive">
              {passwordConfirm.length === 0
                ? "请再次输入登录密码"
                : "两次输入的登录密码不一致"}
            </p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="pay-password">支付密码</Label>
          <Input
            aria-invalid={payPasswordInvalid}
            className="w-36 tracking-[0.35em]"
            id="pay-password"
            inputMode="numeric"
            maxLength={6}
            onChange={handlePayPasswordChange}
            placeholder="支付密码"
            required
            /* 支付密码不用 type=password:避免浏览器弹"保存密码/同步
               已暂停"系统提示;text+text-security 同为圆点遮罩 */
            style={{ WebkitTextSecurity: "disc" } as CSSProperties}
            type="text"
            value={payPassword}
          />
          <p className="text-xs text-muted-foreground">
            6 位纯数字,动钱与敏感操作时使用
          </p>
          {payPasswordInvalid ? (
            <p className="text-xs text-destructive">支付密码需为 6 位数字</p>
          ) : null}
        </div>

        <div className="flex flex-col gap-2">
          <Label htmlFor="pay-password-confirm">确认支付密码</Label>
          <Input
            aria-invalid={payPasswordConfirmInvalidVisible}
            className="w-36 tracking-[0.35em]"
            id="pay-password-confirm"
            inputMode="numeric"
            maxLength={6}
            onChange={handlePayPasswordConfirmChange}
            placeholder="再次输入支付密码"
            required
            style={{ WebkitTextSecurity: "disc" } as CSSProperties}
            type="text"
            value={payPasswordConfirm}
          />
          {payPasswordConfirmInvalidVisible ? (
            <p className="text-xs text-destructive">
              {payPasswordConfirm.length === 0
                ? "请再次输入支付密码"
                : "两次输入的支付密码不一致"}
            </p>
          ) : null}
        </div>

        {error ? (
          <p className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive">
            {error}
          </p>
        ) : null}

        <Button
          className="sheen rounded-lg bg-brand-red-solid text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep disabled:bg-muted disabled:text-muted-foreground disabled:shadow-none"
          disabled={loading}
          type="submit"
        >
          {loading ? "注册中…" : "注册"}
        </Button>

        <p className="text-center text-[13px] text-muted-foreground">
          {"已有账号?"}
          <Link
            className="text-foreground underline-offset-4 hover:underline"
            href="/login"
          >
            登录
          </Link>
        </p>
      </form>
    </>
  );
}
