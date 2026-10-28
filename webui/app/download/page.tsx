"use client";

/*
 * 手机 App 下载页(手机浏览器扫码后落在)。
 * 主按钮指向 webui/public/app/bankagent.apk(由 mobile-app/ 的 GitHub Actions
 * 构建产物放入,不入库);页面动态展示电脑端局域网地址,方便装好 App 后回填。
 * 不需要登录;未放 APK 时按钮降级为「未就绪」并保留网页版入口。
 */

import {
  ArrowRightIcon,
  DownloadIcon,
  Landmark,
  ShieldCheckIcon,
  SmartphoneIcon,
  WifiIcon,
} from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { bankAgentBase } from "@/hooks/use-bank-auth";

const APK_HREF = "app/bankagent.apk";

const STEPS: { title: string; detail: string }[] = [
  {
    detail: "浏览器提示「可能有害」属正常(演示包未上架商店),点「仍然安装」。",
    title: "1 · 允许安装",
  },
  {
    detail:
      "App 会自动搜索同一 Wi-Fi 下的电脑;没搜到就扫电脑端弹窗的「配对码」,或手动输入下面地址。",
    title: "2 · 连接电脑",
  },
  {
    detail: "登录页点「一键填入」演示账号,验证码点「填入」即可。",
    title: "3 · 一键登录",
  },
  {
    detail: "手机与电脑同账号同会话,消息、确认卡双向实时同步。",
    title: "4 · 与电脑联动",
  },
];

export default function DownloadPage() {
  const [lanHost, setLanHost] = useState("");
  const [apkReady, setApkReady] = useState(false);
  const [apkChecked, setApkChecked] = useState(false);
  const [isAndroid, setIsAndroid] = useState(true);

  useEffect(() => {
    setIsAndroid(/android/i.test(navigator.userAgent));
    fetch(`${bankAgentBase()}/api/lan-info`)
      .then((r) => r.json())
      .then((j: { host?: string }) => setLanHost(j.host ?? ""))
      .catch(() => setLanHost(""));
    // 相对路径跟随 basePath;HEAD 探测 APK 是否已由构建产物放入
    fetch(APK_HREF, { method: "HEAD" })
      .then((r) => setApkReady(r.ok))
      .catch(() => setApkReady(false))
      .finally(() => setApkChecked(true));
  }, []);

  const pcOrigin = lanHost ? `http://${lanHost}:3000` : "";

  return (
    <div className="relative isolate flex min-h-dvh flex-col overflow-hidden bg-aurora">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[420px] overflow-hidden"
      >
        <div className="aurora-drift absolute -left-24 -top-28 size-[360px] rounded-full bg-[radial-gradient(circle,rgba(255,176,192,0.5),transparent_66%)]" />
        <div className="aurora-drift absolute -right-32 top-4 size-[400px] rounded-full bg-[radial-gradient(circle,rgba(150,214,255,0.46),transparent_66%)] [animation-delay:-9s]" />
      </div>

      <main className="mx-auto flex w-full max-w-md flex-1 flex-col gap-4 px-5 pb-10 pt-12">
        {/* 品牌区 */}
        <div className="flex items-center gap-3">
          <div className="flex size-11 items-center justify-center rounded-2xl bg-brand-red-solid text-white shadow-[0_10px_24px_-10px_rgba(211,46,25,0.7)]">
            <Landmark className="size-5" />
          </div>
          <div>
            <h1 className="text-[19px] font-semibold text-ink dark:text-brand-blue">
              小小微银行助手 App
            </h1>
            <p className="text-[12px] text-muted-foreground">
              装到手机上的智能银行助手,数据与电脑端实时联动
            </p>
          </div>
        </div>

        {/* 下载主卡 */}
        <section className="glass-panel gradient-hairline rounded-2xl p-5">
          <div className="flex items-center gap-1.5 text-[12px] text-muted-foreground">
            <WifiIcon className="size-3.5" />
            手机与电脑需连接同一 Wi-Fi
          </div>

          {apkChecked && !apkReady ? (
            <div className="mt-3 rounded-xl bg-warning/10 px-3 py-2.5 text-[13px] leading-relaxed text-warning">
              安装包还没就绪(需要先在电脑端完成一次云端构建,见
              mobile-app/README)。可以先用下面的「手机网页版」体验同样的功能。
            </div>
          ) : (
            <a
              className={`sheen mt-3 flex h-12 items-center justify-center gap-2 rounded-xl bg-brand-red-solid text-[15px] font-medium text-white shadow-[0_12px_28px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep ${
                apkChecked && !apkReady ? "pointer-events-none opacity-50" : ""
              }`}
              download="AI-Banking.apk"
              href={APK_HREF}
            >
              <DownloadIcon className="size-4" />
              下载 Android App(APK)
            </a>
          )}

          {!isAndroid ? (
            <p className="mt-3 text-[12px] leading-relaxed text-muted-foreground">
              当前设备不像 Android:APK 仅支持 Android 安装;iPhone 可点下方
              「手机网页版」使用全部功能。
            </p>
          ) : null}

          <div className="mt-4 rounded-xl border border-brand-blue/15 bg-brand-blue-soft/60 px-3 py-2.5 dark:border-white/10 dark:bg-white/[0.04]">
            <div className="text-[11px] text-muted-foreground">
              装好 App 后,在 App 里输入的电脑地址:
            </div>
            <div className="mt-1 font-mono text-[15px] font-semibold text-ink dark:text-brand-blue">
              {pcOrigin || "电脑端未探测到局域网地址"}
            </div>
          </div>

          {/* 已装 App 的手机点这里:深链拉起 App 并自动写入地址,免打字 */}
          {pcOrigin && isAndroid ? (
            <a
              className="mt-3 flex h-11 items-center justify-center gap-2 rounded-xl border border-brand-blue/25 bg-white/70 text-[14px] font-medium text-brand-blue transition-colors hover:border-brand-red/30 hover:text-brand-red dark:border-white/10 dark:bg-white/5"
              href={`aibanking://pair?server=${encodeURIComponent(pcOrigin)}`}
            >
              <SmartphoneIcon className="size-4" />
              手机已装 App?点此一键连接
            </a>
          ) : null}
        </section>

        {/* 四步指引 */}
        <section aria-label="安装步骤" className="glass-panel rounded-2xl p-5">
          <h2 className="flex items-center gap-1.5 text-[14px] font-semibold text-foreground">
            <ShieldCheckIcon className="size-4 text-brand-red" />
            安装与连接
          </h2>
          <ol className="mt-3 flex flex-col gap-3">
            {STEPS.map((s) => (
              <li className="flex flex-col gap-0.5" key={s.title}>
                <span className="text-[13px] font-medium text-foreground">
                  {s.title}
                </span>
                <span className="text-[12px] leading-relaxed text-muted-foreground">
                  {s.detail}
                </span>
              </li>
            ))}
          </ol>
        </section>

        {/* 兜底:网页版 */}
        <Link
          className="flex items-center justify-between rounded-2xl border border-brand-blue/15 bg-card/80 px-4 py-3 text-[13px] text-foreground transition-colors hover:border-brand-red/25 hover:text-brand-red-deep dark:border-white/10 dark:bg-white/[0.04] dark:hover:text-brand-red"
          href="/"
        >
          <span className="flex items-center gap-2">
            <SmartphoneIcon className="size-4 text-brand-blue" />
            先不装 App,用手机网页版(功能相同)
          </span>
          <ArrowRightIcon className="size-4" />
        </Link>
      </main>
    </div>
  );
}
