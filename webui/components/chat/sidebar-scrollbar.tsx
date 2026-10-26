"use client";

/*
 * 侧栏自绘常驻滚动条(2026-10-26)。
 * 为什么不用原生滚动条:新版 Chrome/Edge 在 Windows 上是 Fluent 悬浮式
 * 滚动条——静止时不渲染也不占位,::-webkit-scrollbar 自定义样式同样改不了
 * (实测 offsetWidth===clientWidth),用户只能靠滚轮"盲滚"会话列表。
 *
 * 结构:外层 sticky 零高包裹层放在容器内容末尾,用 bottom-0 钉在可视区
 * 底边(内容末尾的元素天然位于视口下方,sticky 会全程把它顶在可视区内;
 * 滚到底时自然落位,视觉位置不变)。内层轨道绝对定位、高度由 update()
 * 按容器实测 clientHeight 设置。无溢出时淡出且不响应点击;支持拖拽快滑
 * 与点击轨道翻页;滚轮/键盘滚动不受影响,原生滚动条本体由 SidebarContent
 * 的 no-scrollbar 隐藏,避免双条重叠。
 * 本组件纯视觉(aria-hidden),可访问性走容器原有的滚轮/键盘滚动。
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";

const MIN_THUMB_H = 32;
// 轨道上下各留 4px(top-1 / 底部 4px),从容器可视高里扣掉
const TRACK_INSET = 8;

export function SidebarScrollbar() {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const trackRef = useRef<HTMLDivElement | null>(null);
  const thumbRef = useRef<HTMLDivElement | null>(null);
  const dragState = useRef<{ startY: number; startScrollTop: number } | null>(
    null
  );
  const [visible, setVisible] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [trackH, setTrackH] = useState(0);

  // 包裹层是 SidebarContent 的最后一个子元素,父元素即滚动容器
  const getContainer = useCallback(
    () => wrapRef.current?.parentElement ?? null,
    []
  );

  /** 按容器当前滚动状态同步可见性与滑块几何(直接写 DOM,不走渲染) */
  const update = useCallback(() => {
    const el = getContainer();
    const track = trackRef.current;
    const thumb = thumbRef.current;
    if (!el || !track || !thumb) return;
    const { scrollTop, scrollHeight, clientHeight } = el;
    const h = clientHeight - TRACK_INSET;
    setTrackH((prev) => (prev === h ? prev : h));
    if (scrollHeight <= clientHeight + 1 || h <= 0) {
      setVisible(false);
      return;
    }
    setVisible(true);
    const thumbH = Math.max(MIN_THUMB_H, (clientHeight / scrollHeight) * h);
    const maxY = h - thumbH;
    const y = (scrollTop / Math.max(1, scrollHeight - clientHeight)) * maxY;
    thumb.style.height = `${thumbH}px`;
    thumb.style.transform = `translateY(${y}px)`;
  }, [getContainer]);

  useEffect(() => {
    const el = getContainer();
    if (!el) return;
    update();
    el.addEventListener("scroll", update, { passive: true });
    // 列表异步加载/会话增删/窗口与收展变化都会改 scrollHeight,统一在此兜住
    const ro = new ResizeObserver(update);
    ro.observe(el);
    const mo = new MutationObserver(update);
    mo.observe(el, { childList: true, subtree: true });
    return () => {
      el.removeEventListener("scroll", update);
      ro.disconnect();
      mo.disconnect();
    };
  }, [getContainer, update]);

  const handleThumbPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const el = getContainer();
      if (!el) return;
      event.stopPropagation(); // 别触发轨道的翻页逻辑
      event.currentTarget.setPointerCapture(event.pointerId);
      dragState.current = {
        startY: event.clientY,
        startScrollTop: el.scrollTop,
      };
      setDragging(true);
    },
    [getContainer]
  );

  const handleThumbPointerMove = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const el = getContainer();
      const drag = dragState.current;
      if (!el || !drag) return;
      // 滑块走 1px ≈ 内容走 scrollHeight/clientHeight px
      const ratio = el.scrollHeight / el.clientHeight;
      el.scrollTop =
        drag.startScrollTop + (event.clientY - drag.startY) * ratio;
    },
    [getContainer]
  );

  const endDrag = useCallback(() => {
    dragState.current = null;
    setDragging(false);
  }, []);

  const handleTrackPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      const el = getContainer();
      const thumb = thumbRef.current;
      if (!el || !thumb) return;
      // 点中滑块本身交给滑块逻辑;点轨道空白处 = 朝点击方向翻一页
      const rect = thumb.getBoundingClientRect();
      const page = el.clientHeight * 0.9;
      el.scrollTop += event.clientY < rect.top ? -page : page;
    },
    [getContainer]
  );

  return (
    <div
      aria-hidden="true"
      className="sticky bottom-0 top-auto z-20 h-0"
      data-dragging={dragging || undefined}
      ref={wrapRef}
    >
      <div
        className={`absolute bottom-1 right-[3px] w-2 rounded-full transition-opacity hover:bg-sidebar-foreground/5 ${
          visible ? "opacity-100" : "pointer-events-none opacity-0"
        }`}
        data-testid="sidebar-scrollbar"
        onPointerDown={handleTrackPointerDown}
        ref={trackRef}
        style={{ height: trackH }}
      >
        <div
          className="w-full cursor-default rounded-full bg-sidebar-foreground/20 transition-colors hover:bg-sidebar-foreground/35 data-dragging:bg-sidebar-foreground/45"
          data-testid="sidebar-scrollbar-thumb"
          onPointerDown={handleThumbPointerDown}
          onPointerMove={handleThumbPointerMove}
          onPointerUp={endDrag}
          ref={thumbRef}
          style={{ height: MIN_THUMB_H }}
        />
      </div>
    </div>
  );
}
