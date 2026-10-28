import { redirect } from "next/navigation";
import { headers } from "next/headers";

import { isMobileUserAgent } from "@/lib/device";

/* 产品第一屏按设备分流:手机(App/手机浏览器)落银行首页 /home,
 * 桌面落展示门户 /portal(/home 是手机专用排版);对话入口在各自首页。 */
export default async function Page() {
  const ua = (await headers()).get("user-agent") ?? "";
  redirect(isMobileUserAgent(ua) ? "/home" : "/portal");
}
