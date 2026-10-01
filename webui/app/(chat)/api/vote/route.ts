// 演示裁剪:消息投票功能置空(不接数据库)。
export async function GET() {
  return Response.json([]);
}

export async function PATCH() {
  return new Response("Message voted", { status: 200 });
}
