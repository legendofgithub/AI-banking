import { ArrowLeftIcon } from "lucide-react";
import Link from "next/link";
import { Landmark } from "lucide-react";
import { Preview } from "@/components/chat/preview";

export default function AuthLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <div className="relative isolate flex h-dvh w-screen overflow-hidden bg-aurora">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10 overflow-hidden"
      >
        <div className="aurora-drift absolute -left-32 -top-40 size-[520px] rounded-full bg-[radial-gradient(circle,rgba(255,176,192,0.5),transparent_66%)]" />
        <div className="aurora-drift absolute -right-40 top-10 size-[560px] rounded-full bg-[radial-gradient(circle,rgba(150,214,255,0.45),transparent_66%)] [animation-delay:-11s]" />
      </div>

      <div className="flex w-full flex-col bg-gradient-to-b from-brand-red-soft/60 via-background/85 to-brand-blue-soft/60 p-8 backdrop-blur-xl xl:w-[600px] xl:shrink-0 xl:rounded-r-3xl xl:border-r xl:border-white/60 md:p-16 dark:from-brand-red/10 dark:via-background/90 dark:to-brand-blue/10 dark:xl:border-white/10">
        <Link
          className="flex w-fit items-center gap-1.5 text-[13px] text-muted-foreground transition-colors hover:text-brand-red"
          href="/"
        >
          <ArrowLeftIcon className="size-3.5" />
          返回
        </Link>
        <div className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center gap-10">
          <div className="flex flex-col gap-2">
              <div className="mb-2 flex size-9 items-center justify-center rounded-lg bg-brand-red-solid text-white shadow-[0_8px_20px_-10px_rgba(211,46,25,0.7)] ring-1 ring-brand-red/30">
              <Landmark size={14} />
            </div>
            {children}
          </div>
        </div>
      </div>

      <div className="hidden flex-1 flex-col overflow-hidden pl-12 xl:flex">
        <div className="flex items-center gap-1.5 pt-8 text-[13px] text-muted-foreground/50">
          <Landmark size={14} className="text-brand-red" />
          <span className="font-medium text-muted-foreground">AI 银行助手 · 智能对话银行</span>
        </div>
        <div className="flex-1 pb-8 pr-8 pt-4">
          <Preview />
        </div>
      </div>
    </div>
  );
}
