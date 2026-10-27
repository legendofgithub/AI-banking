import type { Metadata } from "next";
import { BoundCardsDialog } from "@/components/chat/bound-cards-dialog";
import { DepositDialog } from "@/components/chat/deposit-dialog";
import { MemoryDialog } from "@/components/chat/memory-dialog";
import { ModelSettingsDialog } from "@/components/chat/model-settings-dialog";
import { PersonaDialog } from "@/components/chat/persona-dialog";
import { ChatModelProvider } from "@/hooks/use-chat-model";

/*
 * /portal 独立元数据:桌面宽屏的项目展示门户(/home 的宽屏姊妹页)。
 * page.tsx 是 client 组件,metadata 必须挂在 server layout 上。
 *
 * 顶栏账号下拉与聊天页设置菜单同款(设置项含充钱/绑卡/记忆/人设/大模型管理),
 * 因此这里要挂 ChatModelProvider + 五个弹窗(与 (chat)/layout 同款链路):
 * 前四者走 window 事件打开,后者依赖 ChatModelProvider。
 */
export const metadata: Metadata = {
  description:
    "小小微银行助手——基于大模型自主规划与 MCP 工具协议的银行智能体:转账、订阅、理财、账单一站式服务",
  title: "小小微银行助手 · 服务门户",
};

export default function PortalLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <ChatModelProvider>
      {children}
      <ModelSettingsDialog />
      <DepositDialog />
      <BoundCardsDialog />
      <MemoryDialog />
      <PersonaDialog />
    </ChatModelProvider>
  );
}
