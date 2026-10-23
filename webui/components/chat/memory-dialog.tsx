"use client";

/*
 * 「Agent 记忆」检阅弹窗（2026-10-23）：
 * Agent 记住了你什么，按主题"一档一文件"直接展示——参考 Anthropic
 * memory tool 的文件式记忆：可看（view）、可忘（delete）、更新时间全程留痕。
 * 数据源：8800 GET /api/memory（用户画像置顶 + 账单/理财/生日经验 +
 * 计划历史 + 长程任务轨道，四层记忆两层可浏览一次看全）。
 *
 * 入口：左下角设置 → Agent 记忆。通过 window 事件 open-memory 打开
 * （弹窗挂在 chat layout，桌面/手机侧栏收起时也能打开）。
 */

import { BrainIcon, TrashIcon } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

type MemoryModule = {
  module: string;
  label: string;
  updated_at: string;
  lines: string[];
};

type ActiveTask = {
  goal: string;
  target_yuan: string;
  saved_yuan: string;
  deadline: string;
  next_review_at: string;
  status: string;
};

type MemoryResponse = {
  modules: MemoryModule[];
  active_task: ActiveTask | null;
  error?: string;
};

const EMPTY: MemoryResponse = { modules: [], active_task: null };

function fmtStamp(iso: string): string {
  // "2026-10-23T14:22:33" → "10-23 14:22"（什么时候记的，一眼可核）
  return iso.length >= 16 ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso;
}

function yuan(text: string): string {
  const n = Number(text);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : text;
}

export function MemoryDialog() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MemoryResponse>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [needLogin, setNeedLogin] = useState(false);
  const [forgetTarget, setForgetTarget] = useState<MemoryModule | null>(null);
  const [forgetting, setForgetting] = useState(false);

  useEffect(() => {
    const openMemory = () => setOpen(true);
    window.addEventListener("open-memory", openMemory);
    return () => window.removeEventListener("open-memory", openMemory);
  }, []);

  const load = useCallback(async () => {
    if (!getStoredToken()) {
      setNeedLogin(true);
      setData(EMPTY);
      return;
    }
    setNeedLogin(false);
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/memory`, {
        headers: bankAuthHeaders(),
        cache: "no-store",
      });
      const body = await res.json();
      if (!res.ok) {
        setError(body.error || "读取记忆失败");
        return;
      }
      setData({
        modules: Array.isArray(body.modules) ? body.modules : [],
        active_task: body.active_task ?? null,
      });
    } catch {
      setError("读取记忆失败，请稍后再试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  const forget = useCallback(async () => {
    if (!forgetTarget) {
      return;
    }
    setForgetting(true);
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/memory/${forgetTarget.module}`,
        { method: "DELETE", headers: bankAuthHeaders() },
      );
      const body = await res.json();
      if (!res.ok) {
        toast.error(body.error || "忘记失败，请稍后再试");
        return;
      }
      setData({
        modules: Array.isArray(body.modules) ? body.modules : [],
        active_task: body.active_task ?? null,
      });
      toast.success(`已忘记「${forgetTarget.label}」`);
      setForgetTarget(null);
    } catch {
      toast.error("忘记失败，请稍后再试");
    } finally {
      setForgetting(false);
    }
  }, [forgetTarget]);

  const modules = data.modules;
  const task = data.active_task;
  const hasAnything = modules.length > 0 || task;

  return (
    <>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="flex max-h-[85vh] w-[92vw] max-w-md flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="border-b border-border/50 px-5 py-4">
            <DialogTitle className="flex items-center gap-2 text-base">
              <BrainIcon className="size-4 text-amber-500" />
              Agent 记忆
            </DialogTitle>
            <DialogDescription className="text-xs">
              按主题一档，可看可忘 · 更新时间全程留痕 · 只作用于你的账号
            </DialogDescription>
          </DialogHeader>

          <ScrollArea className="min-h-0 flex-1">
            <div className="space-y-3 px-4 py-4">
              {needLogin ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  登录网银账号后，才能看到 Agent 记住了你什么。
                </p>
              ) : loading ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  正在翻开记忆…
                </p>
              ) : error ? (
                <p className="py-8 text-center text-sm text-red-500">{error}</p>
              ) : !hasAnything ? (
                <p className="py-8 text-center text-sm leading-6 text-muted-foreground">
                  还没有任何记忆。
                  <br />
                  和我聊过账单分析 / 理财 / 生日计划后，这里会记下有用的经验。
                </p>
              ) : (
                <>
                  {modules.map((m) => (
                    <div
                      key={m.module}
                      className={
                        m.module === "profile"
                          ? "rounded-xl border border-amber-500/30 bg-gradient-to-br from-amber-500/10 to-amber-600/5 p-3.5"
                          : "rounded-xl border border-border/60 bg-card/60 p-3.5"
                      }
                      data-testid={`memory-module-${m.module}`}
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-[13px] font-medium">
                          {m.label}
                        </span>
                        <span className="text-[11px] text-muted-foreground">
                          {fmtStamp(m.updated_at)}
                        </span>
                        <Button
                          aria-label={`忘记${m.label}`}
                          className="ml-auto h-6 gap-1 rounded-md px-2 text-[11px] text-muted-foreground hover:text-red-500"
                          disabled={forgetting}
                          size="sm"
                          variant="ghost"
                          onClick={() => setForgetTarget(m)}
                        >
                          <TrashIcon className="size-3" />
                          忘记
                        </Button>
                      </div>
                      {m.lines.length > 0 ? (
                        <ul className="mt-2 space-y-1">
                          {m.lines.map((line) => (
                            <li
                              className="text-[13px] leading-5 text-foreground/90"
                              key={line}
                            >
                              {line}
                            </li>
                          ))}
                        </ul>
                      ) : (
                        <p className="mt-2 text-[12px] text-muted-foreground">
                          （这一档还没有可展示的内容）
                        </p>
                      )}
                    </div>
                  ))}

                  {task ? (
                    <div
                      className="rounded-xl border border-border/60 bg-card/60 p-3.5"
                      data-testid="memory-active-task"
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-[13px] font-medium">
                          长程任务
                        </span>
                        <Badge
                          className="h-5 px-1.5 text-[10px]"
                          variant="secondary"
                        >
                          进行中
                        </Badge>
                      </div>
                      <p className="mt-2 text-[13px] leading-5">
                        {task.goal}
                      </p>
                      <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
                        已存 ¥{yuan(task.saved_yuan)} / 目标 ¥
                        {yuan(task.target_yuan)} · 截止 {task.deadline}
                        {task.next_review_at
                          ? ` · 下次复盘 ${task.next_review_at}`
                          : ""}
                      </p>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          </ScrollArea>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={forgetTarget !== null}
        onOpenChange={(v) => {
          if (!v && !forgetting) {
            setForgetTarget(null);
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>忘记「{forgetTarget?.label}」？</AlertDialogTitle>
            <AlertDialogDescription>
              Agent 将不再用这一档记忆为你做建议；之后的对话产生了新经验仍会重新记。此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={forgetting}>先留着</AlertDialogCancel>
            <AlertDialogAction
              disabled={forgetting}
              onClick={(e) => {
                e.preventDefault();
                void forget();
              }}
            >
              {forgetting ? "正在忘记…" : "忘记"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
