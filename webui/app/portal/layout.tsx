import type { Metadata } from "next";

/*
 * /portal 独立元数据:桌面宽屏的项目展示门户(/home 的宽屏姊妹页)。
 * page.tsx 是 client 组件,metadata 必须挂在 server layout 上。
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
  return children;
}
