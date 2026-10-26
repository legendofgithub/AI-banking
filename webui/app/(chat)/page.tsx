import { redirect } from "next/navigation";

/* 范式改造:产品第一屏从对话流改为银行首页(/home);对话入口迁至 /chat。 */
export default function Page() {
  redirect("/home");
}
