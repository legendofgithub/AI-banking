import type { NextAuthConfig } from "next-auth";

const base = process.env.NEXT_PUBLIC_BASE_PATH ?? "";

export const authConfig = {
  basePath: "/api/auth",
  // 演示裁剪:本地 HTTP 访问,会话 Cookie 不加 Secure 标记
  cookies: {
    sessionToken: {
      name: "authjs.session-token",
      options: { httpOnly: true, path: "/", sameSite: "lax", secure: false },
    },
  },
  callbacks: {},
  pages: {
    newUser: `${base}/`,
    signIn: `${base}/login`,
  },
  providers: [],
  trustHost: true,
} satisfies NextAuthConfig;
