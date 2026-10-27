"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { suggestions } from "@/lib/constants";
import { Landmark } from "lucide-react";

function PreviewSuggestionButton({
  suggestion,
  onAction,
}: {
  suggestion: string;
  onAction: (query?: string) => void;
}) {
  const handleClick = useCallback(() => {
    onAction(suggestion);
  }, [onAction, suggestion]);

  return (
    <button
      className="rounded-xl border border-brand-blue/10 bg-white/55 px-3 py-2.5 text-left text-[11px] leading-relaxed text-muted-foreground transition-all duration-200 hover:border-brand-red/30 hover:bg-brand-red-soft/70 hover:text-brand-red-deep dark:border-white/10 dark:bg-white/5 dark:hover:text-brand-red"
      onClick={handleClick}
      type="button"
    >
      {suggestion}
    </button>
  );
}

export function Preview() {
  const router = useRouter();

  const handleAction = useCallback(
    (query?: string) => {
      const url = query ? `/?query=${encodeURIComponent(query)}` : "/";
      router.push(url);
    },
    [router]
  );

  const handleDefaultAction = useCallback(() => {
    handleAction();
  }, [handleAction]);

  return (
    <div className="glass-panel flex h-full flex-col overflow-hidden rounded-3xl">
      <div className="flex h-14 shrink-0 items-center gap-3 border-b border-white/50 px-5 dark:border-white/10">
        <div className="flex size-5 items-center justify-center rounded bg-brand-red-solid text-white ring-1 ring-brand-red/30">
          <Landmark size={10} />
        </div>
        <span className="text-[13px] text-muted-foreground">AI 银行助手</span>
      </div>

      <div className="flex flex-1 flex-col items-center justify-center gap-8 px-8">
        <div className="text-center">
          <h2 className="text-xl font-semibold tracking-tight">
            我能帮你做什么？
          </h2>
          <p className="mt-1.5 text-sm text-muted-foreground">
            转账、账单、订阅、理财，都可以直接说。
          </p>
        </div>

        <div className="grid w-full max-w-md grid-cols-2 gap-2">
          {suggestions.map((suggestion) => (
            <PreviewSuggestionButton
              key={suggestion}
              onAction={handleAction}
              suggestion={suggestion}
            />
          ))}
        </div>
      </div>

      <div className="shrink-0 px-5 pb-5">
        <button
          className="flex w-full items-center rounded-2xl border border-brand-blue/10 bg-white/55 px-4 py-3 text-left text-[13px] text-muted-foreground/60 transition-colors hover:border-brand-red/25 hover:text-muted-foreground dark:border-white/10 dark:bg-white/5"
          onClick={handleDefaultAction}
          type="button"
        >
          随便问问…
        </button>
      </div>
    </div>
  );
}
