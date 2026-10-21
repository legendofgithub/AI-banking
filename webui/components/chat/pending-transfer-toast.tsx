"use client";

/*
 * 悬空转账强制提醒(右下角弹窗,无法关闭):
 * - 登录用户名下有 pending_confirm/scheduled 转账单时,右下角常驻提醒卡,
 *   没有关闭按钮——按设计,悬空资金必须被处理(确认/取消),提醒不许被划掉;
 * - 单子确认完(对话里或落地页上)列表清空,弹窗自动消失;
 * - 数据源 GET ${agentBase}/api/pending-orders(带登录 token);挂载时与
 *   每轮对话结束(status 回到 ready)各查一次;
 * - 右侧「去确认」按钮跳 /pending-transfers 落地页(输支付密码执行)。
 */

import { GripHorizontalIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import {
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { PendingTransferBall } from "@/components/chat/pending-transfer-ball";
import { useActiveChat } from "@/hooks/use-active-chat";
import {
  bankAgentBase,
  bankAuthHeaders,
  useBankAuth,
} from "@/hooks/use-bank-auth";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

const EDGE = 8;

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

/** fixed 元素的定位基准:有 transform/定位祖先时按祖先盒子算,否则按视口。 */
function containingBox(element: HTMLElement) {
  const parent = element.offsetParent as HTMLElement | null;
  const rect = parent?.getBoundingClientRect();
  return {
    height: rect?.height ?? window.innerHeight,
    width: rect?.width ?? window.innerWidth,
    x: rect?.left ?? 0,
    y: rect?.top ?? 0,
  };
}

interface PendingOrder {
  amount_yuan: string;
  created_at: string;
  memo: string;
  order_id: number;
  scheduled_at: string;
  status: string;
  to_name: string;
}

export function PendingTransferToast() {
  const router = useRouter();
  const { ready, token, user } = useBankAuth();
  const { status } = useActiveChat();
  const isMobile = useIsMobile();
  const [orders, setOrders] = useState<PendingOrder[]>([]);
  const askedRef = useRef("");
  const cardRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{
    dx: number;
    dy: number;
    height: number;
    maxX: number;
    maxY: number;
    originX: number;
    originY: number;
    width: number;
  } | null>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  const load = useCallback(async () => {
    if (!token) {
      setOrders([]);
      return;
    }
    try {
      const res = await fetch(`${bankAgentBase()}/api/pending-orders`, {
        headers: bankAuthHeaders(),
      });
      const json = (await res.json().catch(() => null)) as {
        orders?: PendingOrder[];
      } | null;
      setOrders(json?.orders ?? []);
    } catch {
      // 网络异常保持现状(下次状态变化再查),不打扰用户
    }
  }, [token]);

  // 挂载(登录态就绪)后查一次;之后每轮对话结束(status 回 ready)复查,
  // 建单停闸门 → 弹窗立即出现;确认执行 → 弹窗立即消失
  useEffect(() => {
    if (!ready) {
      return;
    }
    const key = `${token ?? ""}|${user?.user_id ?? ""}`;
    if (askedRef.current === key && status !== "ready") {
      return;
    }
    askedRef.current = key;
    load();
  }, [load, ready, status, token, user?.user_id]);

  const goConfirm = useCallback(
    () => router.push("/pending-transfers"),
    [router]
  );

  const handleDragStart = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const card = cardRef.current;
      if (!card) {
        return;
      }
      const rect = card.getBoundingClientRect();
      const box = containingBox(card);
      dragRef.current = {
        dx: event.clientX - rect.left,
        dy: event.clientY - rect.top,
        height: rect.height,
        maxX: box.width - rect.width - EDGE,
        maxY: box.height - rect.height - EDGE,
        originX: box.x,
        originY: box.y,
        width: rect.width,
      };
      // CSS left/top 相对定位基准(有 transform 祖先时不是视口原点)
      setPos({ x: rect.left - box.x, y: rect.top - box.y });
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    []
  );

  const handleDragMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const drag = dragRef.current;
      if (!drag) {
        return;
      }
      setPos({
        x: clamp(event.clientX - drag.dx - drag.originX, EDGE, drag.maxX),
        y: clamp(event.clientY - drag.dy - drag.originY, EDGE, drag.maxY),
      });
    },
    []
  );

  const handleDragEnd = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      dragRef.current = null;
      if (event.currentTarget.hasPointerCapture(event.pointerId)) {
        event.currentTarget.releasePointerCapture(event.pointerId);
      }
    },
    []
  );

  // 窗口缩放后把卡片拉回可视区(拖动过才需要)
  useEffect(() => {
    const onResize = () => {
      setPos((current) => {
        const card = cardRef.current;
        if (!current || !card) {
          return current;
        }
        const box = containingBox(card);
        return {
          x: clamp(current.x, EDGE, box.width - card.offsetWidth - EDGE),
          y: clamp(current.y, EDGE, box.height - card.offsetHeight - EDGE),
        };
      });
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const pending = orders.filter((o) => o.status === "pending_confirm");
  const scheduled = orders.filter((o) => o.status === "scheduled");
  if (pending.length + scheduled.length === 0) {
    return null;
  }

  // 手机端:可拖拽悬浮球(边缘吸附半球 → 点按气泡 → 半屏确认面板);
  // 桌面端:可拖动的常驻卡片(无关闭按钮,按住标题拖动;默认放右上避开输入框)
  if (isMobile) {
    return (
      <PendingTransferBall
        nickname={user?.nickname}
        onGoConfirm={goConfirm}
        pending={pending}
        scheduledCount={scheduled.length}
      />
    );
  }

  return (
    <div
      aria-live="assertive"
      /* 桌面默认右上角:原右下角会盖住输入框发送按钮(实测 PC 端)。
         拖动过之后改用 left/top 定位,窗口缩放时自动拉回可视区。 */
      className={cn(
        "fixed inset-x-3 top-16 z-50 rounded-2xl border border-amber-300/80 bg-gradient-to-br from-amber-50 to-yellow-50 p-4 shadow-[var(--shadow-float)] dark:border-amber-500/50 dark:from-amber-950/60 dark:to-yellow-950/40 md:inset-x-auto md:top-20 md:right-4 md:bottom-auto md:w-[min(92vw,360px)]",
        pos && "md:right-auto"
      )}
      data-testid="pending-transfer-card"
      ref={cardRef}
      role="alert"
      style={pos ? { left: pos.x, top: pos.y } : undefined}
    >
      <div className="mb-2 flex items-center justify-between gap-3">
        <div
          className="flex min-w-0 cursor-move touch-none items-center gap-1.5 text-[13px] font-semibold text-amber-700 select-none dark:text-amber-300"
          data-testid="pending-transfer-drag-handle"
          onPointerCancel={handleDragEnd}
          onPointerDown={handleDragStart}
          onPointerMove={handleDragMove}
          onPointerUp={handleDragEnd}
          title="按住这里拖动提醒卡片"
        >
          <GripHorizontalIcon className="size-3.5 shrink-0 opacity-50" />
          <span aria-hidden>⚠️</span>
          <span className="truncate">
            {user?.nickname
              ? `${user.nickname},你有 ${pending.length} 笔转账未确认`
              : `你有 ${pending.length} 笔转账未确认`}
          </span>
        </div>
        <button
          className="shrink-0 rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-amber-600"
          onClick={goConfirm}
          type="button"
        >
          去确认 →
        </button>
      </div>
      <div className="flex flex-col gap-1.5">
        {pending.slice(0, 3).map((o) => (
          <div
            className="flex items-center justify-between gap-3 rounded-lg bg-white/70 px-2.5 py-1.5 text-[12px] dark:bg-amber-900/30"
            key={o.order_id}
          >
            <span className="min-w-0 truncate text-foreground">
              给 {o.to_name}
              {o.memo ? ` · ${o.memo}` : ""}
            </span>
            <span className="shrink-0 font-semibold text-foreground">
              ¥{o.amount_yuan}
            </span>
          </div>
        ))}
        {scheduled.length > 0 ? (
          <div className="px-2.5 text-[11px] text-muted-foreground">
            另有 {scheduled.length} 笔定时转账,到期后会提醒确认
          </div>
        ) : null}
        {pending.length > 3 ? (
          <div className="px-2.5 text-[11px] text-muted-foreground">
            …等共 {pending.length} 笔,点「去确认」查看全部
          </div>
        ) : null}
      </div>
      <div className="mt-2 text-[11px] text-amber-700/80 dark:text-amber-300/80">
        按住标题可拖动 · 去确认需输入支付密码 · 提醒不可关闭
      </div>
    </div>
  );
}
