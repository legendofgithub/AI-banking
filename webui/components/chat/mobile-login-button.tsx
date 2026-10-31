"use client";

/*
 * 右上角「手机 App」按钮 + 下载二维码弹窗。
 *
 * 2026-10-28 起,弹窗里的下载二维码直接编码 GitHub Release 公网直链
 * (releases/latest/download/bankagent.apk,永远最新构建):任何网络的手机
 * 扫码都能下载,不再依赖演示本机在线。下方「电脑地址/配对码」仍指向
 * 演示本机——App 装好后要连的是电脑端服务,同一 Wi-Fi 只在这一步需要。
 * 原「接力登录二维码」已从弹窗下线(后端票据接口与 /qr-login 页保留,
 * 需要恢复时见 git 历史的 mobile-login-button 实现)。
 */

import { SmartphoneIcon } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { bankAgentBase } from "@/hooks/use-bank-auth";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]);

/** 下载直链:GitHub Release 最新构建,发新版不用换码(见 mobile-app/README) */
const PUBLIC_APK_URL =
  "https://github.com/legendofgithub/AI-banking/releases/latest/download/bankagent.apk";

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

export function MobileLoginButton() {
  const [open, setOpen] = useState(false);
  const [pcOrigin, setPcOrigin] = useState("");

  // 下载二维码编码公网 Release 直链,不依赖本机探测;弹窗打开时算一次
  // 局域网地址,给「App 里输入的电脑地址」和配对码用
  useEffect(() => {
    if (!open) {
      setPcOrigin("");
      return;
    }
    let cancelled = false;
    lanOrigin()
      .then((origin) => {
        if (!cancelled) {
          setPcOrigin(origin);
        }
      })
      .catch(() => {
        // 探测失败不显示地址块,不影响二维码下载
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const qrSrc = `${bankAgentBase()}/api/qr?data=${encodeURIComponent(PUBLIC_APK_URL)}`;

  // 配对码:装好 App 后用系统相机扫,深链直达 App 并自动写入电脑地址(免打字)
  const pairUrl = pcOrigin
    ? `aibanking://pair?server=${encodeURIComponent(pcOrigin)}`
    : "";

  const openDialog = useCallback(() => {
    setOpen(true);
  }, []);

  return (
    <>
      <button
        className="hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-brand-blue/20 bg-white/60 px-2.5 text-[12px] text-brand-blue transition-colors hover:border-brand-red/30 hover:bg-brand-red-soft hover:text-brand-red md:inline-flex dark:border-white/10 dark:bg-white/5 dark:hover:bg-brand-red/15"
        data-testid="mobile-login-button"
        onClick={openDialog}
        title="扫码下载手机 App,装好后在 App 里填电脑地址直连演示"
        type="button"
      >
        <SmartphoneIcon className="size-3.5" />
        <span>手机 App</span>
      </button>

      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent
          className="gap-0 rounded-2xl p-0 sm:max-w-sm"
          data-testid="mobile-login-dialog"
        >
          <DialogHeader className="px-5 pt-4 pb-2">
            <DialogTitle className="text-[15px]">手机 App 下载</DialogTitle>
            <DialogDescription className="text-[12px] leading-relaxed">
              手机扫码后浏览器会直接弹出安装包下载确认,点「下载」装好即可。
              装好打开 App,输入下面显示的电脑地址,一键登录演示账号。
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col items-center gap-3 px-5 pb-5">
            <div
              className="rounded-xl border border-border/60 bg-white p-3 shadow-[var(--shadow-card)]"
              data-testid="mobile-qr"
              data-url={PUBLIC_APK_URL}
            >
              {/* 二维码 PNG 由 8800 本地渲染(白底保证扫码对比度)。 */}
              {/* biome-ignore lint/performance/noImgElement: 8800 动态生成的跨端口 PNG,next/image 不代理动态远端图 */}
              <img
                alt="手机 App 下载二维码"
                className="size-56"
                data-testid="mobile-qr-img"
                src={qrSrc}
              />
            </div>

            {/* 地址与配对码指向演示本机,只在探测到局域网地址时显示;
                下载走公网直链,不依赖本机在线 */}
            {pcOrigin ? (
              <>
                {/* 装好 App 后要回填的电脑地址:直接展示省得演示现场翻设置 */}
                <div className="w-full rounded-lg border border-brand-blue/15 bg-brand-blue-soft/60 px-3 py-2 dark:border-white/10 dark:bg-white/[0.04]">
                  <div className="text-[11px] text-muted-foreground">
                    App 里输入的电脑地址(只需一次,App 会记住):
                  </div>
                  <div className="mt-0.5 font-mono text-[13px] font-semibold text-ink dark:text-brand-blue">
                    {pcOrigin}
                  </div>
                </div>

                {/* 配对码:App 打开在搜索/配置屏时扫这张,深链写入地址免打字 */}
                {pairUrl ? (
                  <div
                    className="flex w-full items-center justify-between gap-3 rounded-xl border border-brand-blue/15 bg-brand-blue-soft/50 px-3 py-2.5 dark:border-white/10 dark:bg-white/[0.04]"
                    data-testid="pair-qr-section"
                  >
                    <div className="min-w-0">
                      <p className="text-[12px] font-semibold text-ink dark:text-brand-blue">
                        已装好 App?扫「配对码」自动连接
                      </p>
                      <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">
                        用手机系统相机(或微信)扫右侧二维码,App
                        会自动打开并填好电脑地址,无需手动输入。
                      </p>
                    </div>
                    <div
                      className="shrink-0 rounded-lg border border-border/60 bg-white p-1.5 shadow-[var(--shadow-card)]"
                      data-testid="pair-qr"
                      data-url={pairUrl}
                    >
                      {/* biome-ignore lint/performance/noImgElement: 8800 动态生成的跨端口 PNG,next/image 不代理动态远端图 */}
                      <img
                        alt="App 配对码"
                        className="size-20"
                        src={`${bankAgentBase()}/api/qr?data=${encodeURIComponent(pairUrl)}`}
                      />
                    </div>
                  </div>
                ) : null}
              </>
            ) : null}

            <p className="w-full break-all rounded-lg bg-muted/40 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
              下载走公网 Release 直链,任何网络都能下;装好 App 后连接电脑才需要
              同一 Wi-Fi。也可在手机浏览器直接访问
              <br />
              {PUBLIC_APK_URL}
            </p>

            <Link
              className="text-[12px] text-brand-blue underline-offset-4 hover:underline dark:text-brand-blue"
              href="/download"
            >
              查看图文安装指引
            </Link>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
