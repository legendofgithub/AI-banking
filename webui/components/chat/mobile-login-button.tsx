"use client";

/*
 * 右上角「手机 App」按钮 + 下载二维码弹窗。
 *
 * 2026-10-27 起,弹窗里的二维码直接编码 APK 下载地址
 * (http://<局域网IP>:3000/app/bankagent.apk):手机扫码 → 浏览器打开即触发
 * 系统的 APK 下载确认,点「下载」安装即可,不再经过中间落地页。
 * 原「接力登录二维码」已从弹窗下线(后端票据接口与 /qr-login 页保留,
 * 需要恢复时见 git 历史的 mobile-login-button 实现)。
 */

import { SmartphoneIcon } from "lucide-react";
import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { bankAgentBase } from "@/hooks/use-bank-auth";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "[::1]"]);

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
  const [apkUrl, setApkUrl] = useState("");
  const [pcOrigin, setPcOrigin] = useState("");

  // 二维码不需要登录票据,弹窗打开时算一次局域网地址即可(URL 稳定,无需轮换)
  useEffect(() => {
    if (!open) {
      setApkUrl("");
      setPcOrigin("");
      return;
    }
    let cancelled = false;
    lanOrigin()
      .then((origin) => {
        if (!cancelled) {
          setPcOrigin(origin);
          setApkUrl(`${origin}/app/bankagent.apk`);
        }
      })
      .catch(() => {
        // 探测失败保持空态,弹窗显示生成中
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  const qrSrc = apkUrl
    ? `${bankAgentBase()}/api/qr?data=${encodeURIComponent(apkUrl)}`
    : "";

  const openDialog = () => setOpen(true);

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
            {apkUrl ? (
              <>
                <div
                  className="rounded-xl border border-border/60 bg-white p-3 shadow-[var(--shadow-card)]"
                  data-testid="mobile-qr"
                  data-url={apkUrl}
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

                {/* 装好 App 后要回填的电脑地址:直接展示省得演示现场翻设置 */}
                <div className="w-full rounded-lg border border-brand-blue/15 bg-brand-blue-soft/60 px-3 py-2 dark:border-white/10 dark:bg-white/[0.04]">
                  <div className="text-[11px] text-muted-foreground">
                    App 里输入的电脑地址(只需一次,App 会记住):
                  </div>
                  <div className="mt-0.5 font-mono text-[13px] font-semibold text-ink dark:text-brand-blue">
                    {pcOrigin}
                  </div>
                </div>

                <p className="w-full break-all rounded-lg bg-muted/40 px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                  下载不了?确认手机与电脑同一 Wi-Fi、演示栈已启动;或手机浏览器直接访问
                  <br />
                  {apkUrl}
                </p>

                <Link
                  className="text-[12px] text-brand-blue underline-offset-4 hover:underline dark:text-brand-blue"
                  href="/download"
                >
                  查看图文安装指引(含网页版入口)
                </Link>
              </>
            ) : (
              <p className="py-10 text-[13px] text-muted-foreground">
                正在生成二维码…
              </p>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
