import { type NextRequest, NextResponse } from "next/server";

// 演示裁剪:完全移除鉴权门与游客重定向(内嵌浏览器沙箱不持久化重定向链上的
// Set-Cookie,会导致 / ↔ /api/auth/guest 无限循环)。本演示无登录流程,
// 所有页面直接放行;auth() 在布局里对空会话已有容错。
export async function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;

  if (pathname.startsWith("/ping")) {
    return new Response("pong", { status: 200 });
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
