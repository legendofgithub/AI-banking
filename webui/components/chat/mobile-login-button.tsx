"use client";

/*
 * 右上角「手机端登录」按钮 + 二维码弹窗(2026-10-20)。
 *
 * 电脑端已登录时点按:向 8800 要一张一次性票据(默认 120s),拼成手机可访问
 * 的局域网 URL(本机用 127.0.0.1 打开时自动改用 /api/lan-info 探测的局域网 IP,
 * 端口沿用页面端口),再由 8800 本地渲染成 PNG 二维码——全程不出网。
 * 手机扫码打开 /qr-login?ticket=… 即免密登录同一账号。倒计时归零自动换新票
 * (旧票一次性,留着也只是扫了报过期);弹窗一关循环即停。
 */

import { SmartphoneIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** 拼手机可访问的 origin:桌面用 127.0.0.1 打开时换成局域网 IP */
async function lanOrigin(): Promise<string> {
  const { protocol, hostname, port } = window.location;
  let host = hostname;
  if (LOCAL_HOSTS.has(hostname)) {
    try {
      const res = await fetch(`${bankAgentBase()}/api/lan-info`);
      const json = (await res.json()) as { host?: string };
      const lanHost = json.host;
      if (lanHost && !LOCAL_HOSTS.has(lanHost)) {
        host = lanHost;
      }
    } catch {
      // 探测失败就用原主机(手机与电脑同机调试时仍可用)
    }
  }
  return `${protocol}//${host}${port ? `:${port}` : ""}`;
}

/** 拼手机可访问的落地页 URL:桌面用 127.0.0.1 打开时换成局域网 IP */
async function buildPhoneUrl(ticket: string): Promise<string> {
  return `${await lanOrigin()}/qr-login?ticket=${encodeURIComponent(ticket)}`;
}

export function MobileLoginButton() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [secondsLeft, setSecondsLeft] = useState(0);
  const [error, setError] = useState("");
  const [needLogin, setNeedLogin] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const [appUrl, setAppUrl] = useState("");

  // App 下载页二维码:不需要登录票据,弹窗打开时算一次局域网地址即可
  useEffect(() => {
    if (!open) {
      setAppUrl("");
      return;
    }
    let cancelled = false;
    lanOrigin()
      .then((origin) => {
        if (!cancelled) {
          setAppUrl(`${origin}/download`);
        }
      })
      .catch(() => {
        // 下载区不展示也不影响主流程
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  /** 拉一张新票并展示;返回有效期(秒),失败返回 0(展示错误/登录引导)。 */
  const loadTicket = useCallback(async (): Promise<number> => {
    if (!getStoredToken()) {
      setNeedLogin(true);
      setUrl("");
      return 0;
    }
    setError("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/auth/qr-ticket`, {
        headers: bankAuthHeaders(),
        method: "POST",
      });
      const json = (await res.json().catch(() => null)) as {
        ticket?: string;
        expires_in?: number;
        error?: string;
      } | null;
      if (res.status === 401) {
        setNeedLogin(true);
        setUrl("");
        return 0;
      }
      if (!res.ok || !json?.ticket) {
        setError(json?.error ?? `二维码生成失败(${res.status})`);
        setUrl("");
        return 0;
      }
      setUrl(await buildPhoneUrl(json.ticket));
      const ttl = json.expires_in ?? 120;
      setSecondsLeft(ttl);
      return ttl;
    } catch {
      setError("无法连接服务器,请稍后重试");
      setUrl("");
      return 0;
    }
  }, []);

  // 生命周期:弹窗打开期间 拉票→倒计时→归零自动换票 的循环;关闭/出错即停。
  // retryKey 只作"出过错后手动重试"的重入触发器。
  // biome-ignore lint/correctness/useExhaustiveDependencies: 见上
  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    const run = async () => {
      while (!cancelled) {
        // biome-ignore lint/performance/noAwaitInLoops: 串行节奏即本意(一票用完再换下一张)
        const ttl = await loadTicket();
        if (cancelled || ttl <= 0) {
          return; // 出错态由弹窗里的「重试」按钮重新点火
        }
        for (let s = ttl; s > 0 && !cancelled; s -= 1) {
          setSecondsLeft(s);
          // biome-ignore lint/performance/noAwaitInLoops: 倒计时按秒推进,故意等待
          await sleep(1000);
        }
        // 倒计时归零 → 回到循环头换新票,旧票一次性自动作废
      }
    };
    run();
    return () => {
      cancelled = true;
    };
  }, [open, retryKey]);

  // 关弹窗即清场(下次打开重新拉票)
  useEffect(() => {
    if (!open) {
      setUrl("");
      setError("");
      setNeedLogin(false);
      setSecondsLeft(0);
    }
  }, [open]);

  const openDialog = useCallback(() => setOpen(true), []);
  const retry = useCallback(() => setRetryKey((k) => k + 1), []);
  const goLogin = useCallback(() => {
    setOpen(false);
    router.push("/login");
  }, [router]);

  const qrSrc = url
    ? `${bankAgentBase()}/api/qr?data=${encodeURIComponent(url)}`
    : "";

  return (
    <>
      <button
        className="hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-brand-blue/20 bg-white/60 px-2.5 text-[12px] text-brand-blue transition-colors hover:border-brand-red/30 hover:bg-brand-red-soft hover:text-brand-red md:inline-flex dark:border-white/10 dark:bg-white/5 dark:hover:bg-brand-red/15"
        data-testid="mobile-login-button"
        onClick={openDialog}
        title="生成二维码,手机扫码直接登录手机端;弹窗内可扫码下载手机 App"
        type="button"
      >
        <SmartphoneIcon className="size-3.5" />
        <span>手机端登录</span>
      </button>

      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent
          className="gap-0 rounded-2xl p-0 sm:max-w-sm"
          data-testid="mobile-login-dialog"
        >
          <DialogHeader className="px-5 pt-4 pb-2">
            <DialogTitle className="text-[15px]">手机端登录</DialogTitle>
            <DialogDescription className="text-[12px] leading-relaxed">
              用手机浏览器或微信「扫一扫」;扫码后手机将直接登录当前账号
              (与密码登录同权,动钱仍需支付密码)。手机需与电脑连接同一网络。
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col items-center gap-3 px-5 pb-5">
            {needLogin ? (
              <div className="flex w-full flex-col items-center gap-3 py-6 text-center">
                <p className="text-[13px] text-foreground">
                  请先在电脑端登录,再生成手机登录二维码
                </p>
                <Button
                  data-testid="qr-goto-login"
                  onClick={goLogin}
                  type="button"
                >
                  去登录
                </Button>
              </div>
            ) : error ? (
              <div className="flex w-full flex-col items-center gap-3 py-6 text-center">
                <p className="text-[13px] text-red-600 dark:text-red-300">
                  {error}
                </p>
                <Button
                  onClick={retry}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  重试
                </Button>
              </div>
            ) : url ? (
              <>
                <div
                  className="rounded-xl border border-border/60 bg-white p-3 shadow-[var(--shadow-card)]"
                  data-testid="mobile-qr"
                  data-url={url}
                >
                  {/* 二维码 PNG 由 8800 本地渲染(白底保证扫码对比度)。 */}
                  {/* biome-ignore lint/performance/noImgElement: 8800 动态生成的跨端口 PNG,next/image 不代理动态远端图 */}
                  <img
                    alt="手机端登录二维码"
                    className="size-56"
                    data-testid="mobile-qr-img"
                    src={qrSrc}
                  />
                </div>
                <p className="text-[12px] text-muted-foreground">
                  {secondsLeft > 0
                    ? `二维码 ${secondsLeft} 秒内有效,过期自动刷新`
                    : "正在刷新二维码…"}
                </p>
                <p className="w-full break-all rounded-lg bg-muted/40 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                  打不开?确认手机与电脑同一 Wi-Fi,或直接在手机浏览器访问:
                  <br />
                  {url}
                </p>
              </>
            ) : (
              <p className="py-10 text-[13px] text-muted-foreground">
                正在生成二维码…
              </p>
            )}
          </div>

          {/* App 下载区:与接力登录并列,扫码打开 /download 页装 APK */}
          {appUrl ? (
            <div
              className="border-t border-border/50 px-5 py-4"
              data-testid="app-download-section"
            >
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-[13px] font-semibold text-ink dark:text-brand-blue">
                    下载手机 App
                  </p>
                  <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                    安装后 App 直连本机演示,数据与电脑实时同步;装过一次
                    无需再扫,登录态保留 7 天。
                  </p>
                </div>
                <div
                  className="shrink-0 rounded-lg border border-border/60 bg-white p-1.5 shadow-[var(--shadow-card)]"
                  data-testid="app-download-qr"
                  data-url={appUrl}
                >
                  {/* biome-ignore lint/performance/noImgElement: 8800 动态生成的跨端口 PNG,next/image 不代理动态远端图 */}
                  <img
                    alt="手机 App 下载二维码"
                    className="size-20"
                    src={`${bankAgentBase()}/api/qr?data=${encodeURIComponent(appUrl)}`}
                  />
                </div>
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </>
  );
}
