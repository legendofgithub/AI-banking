import { redirect } from "next/navigation";

/* 产品第一屏:统一落展示门户 /portal(手机/桌面同版响应式)。
 * 原手机专用的 /home 已下线;对话入口在门户顶栏「进入对话」。 */
export default function Page() {
  redirect("/portal");
}
