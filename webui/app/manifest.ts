import type { MetadataRoute } from "next";

/*
 * PWA manifest:手机浏览器访问 :3000 时可"添加到主屏幕",获得类 App 体验
 * (独立窗口/启动画面/图标)。Next 会自动挂 <link rel="manifest">。
 *
 * 刻意不写 Service Worker:演示阶段 SW 缓存会把旧代码钉在手机上,
 * 换机器演示/改代码后难以自愈;安装体验按浏览器能力降级
 * (Android Chrome 可安装,iOS 走 apple-touch-icon 快捷方式)。
 * theme_color 与 layout.tsx 的 LIGHT_THEME_COLOR 保持一致(白色地址栏),
 * dark 模式由运行时 theme-color 脚本接管。
 */

export default function Manifest(): MetadataRoute.Manifest {
  return {
    background_color: "#ffffff",
    description:
      "自然语言银行助手:转账、账单、订阅、理财、卡片、跨场景联动,动钱必过人工闸门",
    display: "standalone",
    icons: [
      {
        sizes: "192x192",
        src: "/images/icon-192.png",
        type: "image/png",
      },
      {
        sizes: "512x512",
        src: "/images/icon-512.png",
        type: "image/png",
      },
      {
        purpose: "maskable",
        sizes: "192x192",
        src: "/images/icon-maskable-192.png",
        type: "image/png",
      },
      {
        purpose: "maskable",
        sizes: "512x512",
        src: "/images/icon-maskable-512.png",
        type: "image/png",
      },
    ],
    name: "AI Banking 智能银行助手",
    short_name: "AI Banking",
    start_url: "/",
    theme_color: "#ffffff",
  };
}
