"use client";

/*
 * 手机端「待确认转账」悬浮球(md 以下渲染;桌面仍是右下角常驻卡片):
 * - 常态:吸附在屏幕左右边缘,只露半个球(黄色,带未确认笔数角标);
 * - 点按:球旁弹出小气泡「你有 N 笔转账待确认」+「查看详情」(点外部可收起);
 * - 查看详情:屏幕正中央弹起确认卡(订单明细 + 去确认入口);
 *   卡片左上角朝右小箭头 = 收回为悬浮球;
 * - 拖拽:球跟手;拖到屏幕中心附近 → 整球 + 动态渐变旋转;松手 →
 *   自动吸附到较近的左右边缘(带回弹/压扁动效),只露半球。
 * 悬空资金必须被处理的原设计不变:球不可关闭、不可划走,单子处理完自动消失。
 *
 * 实现要点:定位用 translate3d 包一层、渐变旋转转内层圆盘(transform 分层,
 * 互不覆盖);touch-action:none 吃掉滚动手势;pointer capture 保证跟手。
 */

import { ChevronRightIcon } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export interface BallOrder {
  amount_yuan: string;
  created_at: string;
  memo: string;
  order_id: number;
  scheduled_at: string;
  status: string;
  to_name: string;
}

const R = 28; // 球半径(px)
const EDGE = 6; // 吸附后球心距边缘的偏移(略为负更贴边)
const TAP_SLOP = 8; // 位移小于此值视为点按而非拖拽
const CENTER_RADIUS = 130; // 距屏幕中心小于此值时进入"整球渐变"形态

type Pt = { x: number; y: number };

export function PendingTransferBall({
  nickname,
  onGoConfirm,
  pending,
  scheduledCount,
}: {
  nickname?: string;
  onGoConfirm: () => void;
  pending: BallOrder[];
  scheduledCount: number;
}) {
  const [center, setCenter] = useState<Pt | null>(null); // 球心坐标
  const [dragging, setDragging] = useState(false);
  const [bubbleOpen, setBubbleOpen] = useState(false);
  const [sheetOpen, setSheetOpen] = useState(false);
  const [landing, setLanding] = useState(false); // 吸附落位动画中

  const startRef = useRef<{ p: Pt; ball: Pt; moved: boolean } | null>(null);

  // 初始位置:右边缘、屏幕高度 68% 处(拇指热区)
  useEffect(() => {
    if (center === null && typeof window !== "undefined") {
      setCenter({ x: window.innerWidth - EDGE, y: window.innerHeight * 0.68 });
    }
  }, [center]);

  const clampY = useCallback((y: number) => {
    const top = 88; // 让开顶栏
    const bottom = window.innerHeight - 140; // 让开输入区
    return Math.min(Math.max(y, top), Math.max(top, bottom));
  }, []);

  // ---- 拖拽(Pointer Events,鼠标/手指统一) ----
  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (center === null) {
        return;
      }
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
      startRef.current = {
        ball: center,
        moved: false,
        p: { x: e.clientX, y: e.clientY },
      };
      setBubbleOpen(false);
      setDragging(true);
    },
    [center]
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const s = startRef.current;
      if (!s || center === null) {
        return;
      }
      const dx = e.clientX - s.p.x;
      const dy = e.clientY - s.p.y;
      if (Math.abs(dx) > TAP_SLOP || Math.abs(dy) > TAP_SLOP) {
        s.moved = true;
      }
      const x = Math.min(
        Math.max(s.ball.x + dx, R * 0.4),
        window.innerWidth - R * 0.4
      );
      setCenter({ x, y: clampY(s.ball.y + dy) });
    },
    [center, clampY]
  );

  const onPointerUp = useCallback(() => {
    const s = startRef.current;
    startRef.current = null;
    setDragging(false);
    if (!s) {
      return;
    }
    if (!s.moved) {
      // 点按:弹气泡
      setBubbleOpen((v) => !v);
      return;
    }
    // 松手:吸附到较近的左右边缘,只露半球
    const side =
      center && center.x < window.innerWidth / 2
        ? EDGE
        : window.innerWidth - EDGE;
    setCenter((c) => (c ? { ...c, x: side } : c));
    setLanding(true);
    window.setTimeout(() => setLanding(false), 450);
  }, [center]);

  const openSheet = useCallback(() => {
    setBubbleOpen(false);
    setSheetOpen(true);
  }, []);

  // 气泡打开时,点按球/气泡以外的任何区域即收起气泡(点击照常落到页面上,
  // 不吞手势);capture 阶段监听,保证在其它处理器之前完成"是否外部"判定
  useEffect(() => {
    if (!bubbleOpen) {
      return;
    }
    const onDown = (e: PointerEvent) => {
      const t = e.target as HTMLElement | null;
      if (
        t?.closest(
          '[data-testid="pending-ball"], [data-testid="pending-bubble"]'
        )
      ) {
        return;
      }
      setBubbleOpen(false);
    };
    window.addEventListener("pointerdown", onDown, true);
    return () => window.removeEventListener("pointerdown", onDown, true);
  }, [bubbleOpen]);

  const goConfirm = useCallback(() => {
    setSheetOpen(false);
    onGoConfirm();
  }, [onGoConfirm]);

  if (center === null) {
    return null;
  }

  const left = center.x < window.innerWidth / 2;
  const atEdge =
    !dragging && (center.x <= R || center.x >= window.innerWidth - R);
  const nearCenter =
    Math.hypot(
      center.x - window.innerWidth / 2,
      center.y - window.innerHeight / 2
    ) < CENTER_RADIUS;

  return (
    <>
      {/* ---- 悬浮球 ---- */}
      <div
        className="fixed top-0 left-0 z-50 select-none"
        data-testid="pending-ball"
        onPointerCancel={onPointerUp}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        style={{
          touchAction: "none",
          transform: `translate3d(${center.x - R}px, ${center.y - R}px, 0)`,
          transition: dragging
            ? "none"
            : "transform 320ms cubic-bezier(0.22, 1.2, 0.36, 1)",
        }}
      >
        <div
          className={cn(
            "flex items-center justify-center rounded-full border border-white/25 shadow-[0_10px_28px_-10px_rgba(211,46,25,0.55),0_14px_36px_-18px_rgba(30,82,165,0.6)]",
            landing && "pending-ball-land",
            dragging ? "cursor-grabbing" : "cursor-grab"
          )}
          style={{ height: R * 2, width: R * 2 }}
        >
          {/* 渐变层:常态微光呼吸;拖到屏幕中心时整球动态渐变旋转 */}
          <div
            className={cn(
              "absolute inset-0 overflow-hidden rounded-full",
              nearCenter && "pending-ball-spin"
            )}
            style={
              nearCenter
                ? {
                    background:
                      "conic-gradient(#FF8F7A, #D32E19, #7B3FA8, #3E60CE, #6C97E9, #FF8F7A)",
                  }
                : {
                    background:
                      "linear-gradient(135deg,#A8170C,#7B3FA8 52%,#1E52A5)",
                  }
            }
          />
          <span className="relative text-lg font-bold text-white drop-shadow">
            ¥
          </span>
          {pending.length > 0 ? (
            <span className="absolute -top-1 -left-1 flex h-5 min-w-5 items-center justify-center rounded-full bg-white px-1 text-[11px] font-bold text-brand-red-deep shadow-[0_2px_6px_rgba(0,0,0,0.25)] ring-2 ring-brand-red/25">
              {pending.length}
            </span>
          ) : null}
        </div>
      </div>

      {/* ---- 点按弹出的小气泡 ---- */}
      {bubbleOpen && !sheetOpen ? (
        <div
          className={cn(
            "fixed z-50 w-56 rounded-2xl border border-brand-blue/15 bg-card/95 p-3 shadow-[var(--shadow-float)] backdrop-blur-xl dark:border-white/10 dark:bg-gold-card-dark",
            atEdge && (left ? "left-4" : "right-4")
          )}
          data-testid="pending-bubble"
          style={{ top: clampY(center.y - 60) }}
        >
          <div className="text-[13px] font-semibold text-ink dark:text-brand-blue">
            {nickname ? `${nickname},` : ""}你有 {pending.length} 笔转账待确认
          </div>
          <Button
            className="mt-2 h-8 w-full bg-brand-red-solid text-xs text-white shadow-[0_8px_18px_-10px_rgba(211,46,25,0.7)] hover:bg-brand-red-deep"
            data-testid="pending-bubble-detail"
            onClick={openSheet}
            size="sm"
          >
            查看详情
          </Button>
        </div>
      ) : null}

      {/* ---- 屏幕正中央确认卡(左上角朝右箭头 = 收回为悬浮球) ---- */}
      <Dialog onOpenChange={setSheetOpen} open={sheetOpen}>
        <DialogContent
          className="max-h-[70dvh] gap-0 rounded-2xl border border-brand-blue/15 border-t-2 border-t-brand-red/70 bg-card/95 p-0 ring-brand-blue/20 dark:border-white/10 dark:bg-gold-card-dark"
          data-testid="pending-sheet"
        >
          <DialogHeader className="px-5 pt-4 pb-1">
            <DialogTitle className="flex items-center gap-2 text-[15px] font-semibold text-ink dark:text-brand-blue">
              <span aria-hidden>⚠️</span>
              待确认转账 · {pending.length} 笔
            </DialogTitle>
          </DialogHeader>
          <div className="max-h-[40dvh] overflow-y-auto px-5 py-2">
            <div className="flex flex-col gap-1.5">
              {pending.slice(0, 5).map((o) => (
                <div
                  className="flex items-center justify-between gap-3 rounded-lg bg-white/70 px-2.5 py-1.5 text-[13px] dark:bg-gold-card-dark"
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
              {pending.length > 5 ? (
                <div className="px-2.5 text-[11px] text-muted-foreground">
                  …等共 {pending.length} 笔
                </div>
              ) : null}
              {scheduledCount > 0 ? (
                <div className="px-2.5 text-[11px] text-muted-foreground">
                  另有 {scheduledCount} 笔定时转账,到期后会提醒确认
                </div>
              ) : null}
            </div>
            <div className="mt-2 text-[11px] text-muted-foreground">
              动钱等你亲自确认——需输入支付密码,此提醒不可关闭
            </div>
          </div>
          <div className="px-5 pt-1 pb-5">
            <Button
              className="h-10 w-full bg-primary font-medium text-white shadow-[0_10px_22px_-12px_rgba(30,82,165,0.7)] hover:bg-primary/90"
              data-testid="pending-sheet-go"
              onClick={goConfirm}
            >
              去确认(输入支付密码执行) →
            </Button>
          </div>
          {/* 左上角朝右小箭头:点击收回为悬浮球 */}
          <button
            aria-label="收起为悬浮球"
            className="absolute top-3 left-3 flex h-8 w-8 items-center justify-center rounded-full bg-white/70 text-gold-ink transition hover:bg-gold-bg dark:bg-gold-card-dark dark:text-gold"
            data-testid="pending-sheet-collapse"
            onClick={() => setSheetOpen(false)}
            type="button"
          >
            <ChevronRightIcon className="size-4" />
          </button>
        </DialogContent>
      </Dialog>
    </>
  );
}
