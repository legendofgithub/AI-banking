"use client";

/*
 * 右上角「微信 ClawBot」接入按钮 + 动态二维码弹窗。
 *
 * 与「手机 App」按钮的区别:那个码是静态公网下载链接;这个码是**一次性
 * 登录凭证**——由本机 OpenClaw 现场向微信申请,一两分钟失效、CLI 自动
 * 换新几次。所以弹窗打开时先 POST /start 拉起会话,再 2s 轮询 /state
 * 拿最新 qr_url,图片仍由 8800 的 /api/qr 本地渲染(白底,不出网)。
 *
 * 连上之后用户微信聊天列表里会出现 ClawBot 对话,发「绑定 手机号」即可
 * 把该微信账号绑到演示账户,之后的业务对话由 OpenClaw 技能转发到
 * /api/wechat/relay(见 docs/openclaw-bankagent-relay-技能.md)。
 */

import { Loader2Icon, MessageCircleIcon, RefreshCwIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { bankAgentBase } from "@/hooks/use-bank-auth";

type Status =
  | "idle"
  | "starting"
  | "waiting"
  | "connected"
  | "failed"
  | "unavailable";

type ClawbotState = {
  status: Status;
  qr_url: string;
  gen: number;
  message: string;
  cli_ready: boolean;
  alive: boolean;
  log: string[];
};

const EMPTY: ClawbotState = {
  status: "idle",
  qr_url: "",
  gen: 0,
  message: "",
  cli_ready: true,
  alive: false,
  log: [],
};

export function WechatClawbotButton() {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<ClawbotState>(EMPTY);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = useCallback(async () => {
    try {
      const res = await fetch(`${bankAgentBase()}/api/wechat/clawbot/state`);
      if (res.ok) {
        setState((await res.json()) as ClawbotState);
      }
    } catch {
      // 后端未起/网络抖动:保留上一帧,下一轮再试
    }
  }, []);

  const start = useCallback(
    async (force = false) => {
      setBusy(true);
      try {
        const res = await fetch(
          `${bankAgentBase()}/api/wechat/clawbot/start${force ? "?force=true" : ""}`,
          { method: "POST" }
        );
        if (res.ok) {
          setState((await res.json()) as ClawbotState);
        }
      } catch {
        setState((s) => ({
          ...s,
          status: "failed",
          message: "后端不可达,请确认演示服务(8800)已启动",
        }));
      } finally {
        setBusy(false);
      }
    },
    []
  );

  // 打开弹窗即拉起会话 + 轮询;关闭时停轮询并清空本地状态
  useEffect(() => {
    if (!open) {
      if (timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
      setState(EMPTY);
      return;
    }
    start(false);
    timer.current = setInterval(poll, 2000);
    return () => {
      if (timer.current) {
        clearInterval(timer.current);
        timer.current = null;
      }
    };
  }, [open, poll, start]);

  // 已连接 / 申请中 时不显示二维码图
  const showQr = state.status === "waiting" && state.qr_url.length > 0;
  const qrSrc = showQr
    ? `${bankAgentBase()}/api/qr?data=${encodeURIComponent(state.qr_url)}&gen=${state.gen}`
    : "";

  return (
    <>
      <button
        className="hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-brand-blue/20 bg-white/60 px-2.5 text-[12px] text-brand-blue transition-colors hover:border-brand-red/30 hover:bg-brand-red-soft hover:text-brand-red md:inline-flex dark:border-white/10 dark:bg-white/5 dark:hover:bg-brand-red/15"
        data-testid="wechat-clawbot-button"
        onClick={() => setOpen(true)}
        title="扫码把 BankAgent 接入微信 ClawBot,在微信聊天列表里直接对话"
        type="button"
      >
        <MessageCircleIcon className="size-3.5" />
        <span>微信 ClawBot</span>
      </button>

      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent
          className="gap-0 rounded-2xl p-0 sm:max-w-sm"
          data-testid="wechat-clawbot-dialog"
        >
          <DialogHeader className="px-5 pt-4 pb-2">
            <DialogTitle className="text-[15px]">微信 ClawBot 接入</DialogTitle>
            <DialogDescription className="text-[12px] leading-relaxed">
              用手机微信扫下方二维码,聊天列表里会出现 ClawBot——之后发
              「绑定 手机号」即可绑定演示账户,直接在微信里查余额、看账单。
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col items-center gap-3 px-5 pb-5">
            {state.status === "unavailable" ? (
              <p
                className="w-full rounded-lg border border-brand-red/20 bg-brand-red-soft/50 px-3 py-2.5 text-[12px] leading-relaxed text-brand-red"
                data-testid="wechat-clawbot-unavailable"
              >
                本机未找到 OpenClaw:请先安装并连通微信插件
                (<span className="font-mono">npx -y @tencent-weixin/openclaw-weixin-cli install</span>),
                或设置环境变量 CLAWBOT_CLI 指向 openclaw 的 dist/index.js。
              </p>
            ) : state.status === "connected" ? (
              <div
                className="w-full rounded-xl border border-emerald-500/30 bg-emerald-50 px-3 py-3 dark:border-emerald-400/20 dark:bg-emerald-500/10"
                data-testid="wechat-clawbot-connected"
              >
                <p className="text-[13px] font-semibold text-emerald-700 dark:text-emerald-300">
                  ✓ 微信已连接
                </p>
                <p className="mt-1 text-[12px] leading-relaxed text-emerald-800/80 dark:text-emerald-200/70">
                  打开微信聊天列表里的 ClawBot,发「绑定 13800002233」绑定演示账户,
                  再发「查一下我的余额」验证。
                </p>
              </div>
            ) : showQr ? (
              <>
                <div
                  className="rounded-xl border border-border/60 bg-white p-3 shadow-[var(--shadow-card)]"
                  data-testid="wechat-clawbot-qr"
                  data-url={state.qr_url}
                  data-gen={state.gen}
                >
                  {/* biome-ignore lint/performance/noImgElement: 8800 动态生成的跨端口 PNG,next/image 不代理动态远端图 */}
                  <img
                    alt="微信 ClawBot 连接二维码"
                    className="size-52"
                    data-testid="wechat-clawbot-qr-img"
                    src={qrSrc}
                  />
                </div>
                <p className="text-[11px] leading-relaxed text-muted-foreground">
                  二维码约 1 分钟有效,过期会自动换新;也可在手机微信
                  「我 → 设置 → 插件 → 微信 ClawBot → 开始扫一扫」扫码。
                </p>
              </>
            ) : (
              <div className="flex h-52 w-full flex-col items-center justify-center gap-2 text-muted-foreground">
                <Loader2Icon className="size-6 animate-spin" />
                <span className="text-[12px]">
                  {state.message || "正在向微信申请二维码…"}
                </span>
              </div>
            )}

            {state.status === "failed" ? (
              <p className="w-full rounded-lg bg-muted/40 px-3 py-2 text-[11px] text-muted-foreground">
                {state.message}
              </p>
            ) : null}

            {/* 已连接时让用户能重新生成(换手机/换微信账号);其余状态用于刷新失效码 */}
            {state.status !== "unavailable" ? (
              <Button
                className="w-full"
                data-testid="wechat-clawbot-refresh"
                disabled={busy}
                onClick={() => start(true)}
                size="sm"
                type="button"
                variant="outline"
              >
                {busy ? (
                  <Loader2Icon className="size-3.5 animate-spin" />
                ) : (
                  <RefreshCwIcon className="size-3.5" />
                )}
                {state.status === "connected" ? "重新生成二维码" : "刷新二维码"}
              </Button>
            ) : null}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
