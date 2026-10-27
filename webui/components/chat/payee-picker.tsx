"use client";

/*
 * 「我要转账」收款人选择器(2026-10-20)。
 *
 * 点击建议胶囊「我要转账」或输入同一指令时弹出:
 * - 顶部搜索栏:姓名 / 手机号 / 备注 本地即时过滤(不把搜索词打进日志);
 * - 右上角 +:新建收款人(姓名 + 1 开头 11 位手机号 + 备注),落库后回到列表;
 * - 每位收款人都显示手机号,同名联系人可直接按手机号区分。
 * 选中后由父级(use-active-chat)带着姓名+手机号发起转账,金额仍走对话闸门。
 */

import {
  ChevronLeftIcon,
  ChevronRightIcon,
  PlusIcon,
  SearchIcon,
  XIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { toast } from "@/components/chat/toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

export interface PayeeContact {
  id: number;
  name: string;
  note?: string;
  phone: string;
  relation?: string;
}

const RELATION_CN: Record<string, string> = {
  colleague: "同事",
  daughter: "女儿",
  friend: "朋友",
  landlord: "房东",
  parent: "亲属",
  spouse: "配偶",
};

function relationText(value: string | undefined): string {
  if (!value) {
    return "";
  }
  return RELATION_CN[value] ?? value;
}

function contactMeta(contact: PayeeContact): string {
  const relation = relationText(contact.relation);
  const note = (contact.note ?? "").trim();
  // 备注自带关系词(如 relation=同事, note=同事·球友)时不重复展示
  if (note && relation && note.includes(relation)) {
    return note;
  }
  return [relation, note].filter(Boolean).join(" · ");
}

function PayeeRow({
  contact,
  onPick,
}: {
  contact: PayeeContact;
  onPick: (contact: PayeeContact) => void;
}) {
  const handlePick = useCallback(() => onPick(contact), [contact, onPick]);
  const meta = contactMeta(contact);
  return (
    <button
      className="group flex items-center gap-3 rounded-lg px-3 py-2.5 text-left transition hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      data-payee-id={contact.id}
      data-testid="payee-row"
      onClick={handlePick}
      type="button"
    >
      <span
        aria-hidden
        className="flex size-9 shrink-0 items-center justify-center rounded-full bg-brand-red-soft text-[13px] font-semibold text-brand-red-deep dark:bg-brand-red/10 dark:text-brand-red"
      >
        {contact.name.slice(0, 1)}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate text-[13px] font-medium text-foreground">
          {contact.name}
        </span>
        <span className="truncate text-[11px] text-muted-foreground">
          {contact.phone}
          {meta ? ` · ${meta}` : ""}
        </span>
      </span>
      <ChevronRightIcon className="size-4 shrink-0 text-muted-foreground/60 transition group-hover:text-foreground" />
    </button>
  );
}

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 选中收款人 → 父级发起转账 */
  onPick: (contact: PayeeContact) => void;
  /** 不选收款人,直接在对话里说“我要转账” */
  onFallback: () => void;
};

export function PayeePickerDialog({
  open,
  onOpenChange,
  onPick,
  onFallback,
}: Props) {
  const router = useRouter();
  const [contacts, setContacts] = useState<PayeeContact[]>([]);
  const [loading, setLoading] = useState(false);
  const [needLogin, setNeedLogin] = useState(false);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");
  const [mode, setMode] = useState<"list" | "create">("list");
  const [reloadKey, setReloadKey] = useState(0);
  const [form, setForm] = useState({ name: "", note: "", phone: "" });
  const [formError, setFormError] = useState("");
  const [saving, setSaving] = useState(false);

  // reloadKey 只作为"重试"触发器参与依赖,effect 内不读取
  // biome-ignore lint/correctness/useExhaustiveDependencies: 见上
  useEffect(() => {
    if (!open) {
      return;
    }
    setMode("list");
    setSearch("");
    setForm({ name: "", note: "", phone: "" });
    setFormError("");
    setError("");
    setNeedLogin(false);

    if (!getStoredToken()) {
      setContacts([]);
      setNeedLogin(true);
      return;
    }

    let cancelled = false;
    setLoading(true);
    fetch(`${bankAgentBase()}/api/contacts`, { headers: bankAuthHeaders() })
      .then(async (res) => {
        const json = (await res.json()) as {
          contacts?: PayeeContact[];
          error?: string;
        };
        if (cancelled) {
          return;
        }
        if (res.status === 401) {
          setNeedLogin(true);
          setContacts([]);
          return;
        }
        if (!res.ok) {
          setError(json.error || `收款人加载失败(${res.status})`);
          return;
        }
        setContacts(json.contacts ?? []);
      })
      .catch(() => {
        if (!cancelled) {
          setError("网络异常,收款人暂时取不到");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [open, reloadKey]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    if (!needle) {
      return contacts;
    }
    return contacts.filter((contact) =>
      [
        contact.name,
        contact.phone,
        contact.note ?? "",
        relationText(contact.relation),
      ]
        .join(" ")
        .toLowerCase()
        .includes(needle)
    );
  }, [contacts, search]);

  const startCreate = useCallback(() => {
    setForm({ name: "", note: "", phone: "" });
    setFormError("");
    setMode("create");
  }, []);

  const backToList = useCallback(() => {
    setMode("list");
    setFormError("");
  }, []);

  const handleNameChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, name: e.target.value }));
  }, []);

  const handlePhoneChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, phone: e.target.value }));
  }, []);

  const handleNoteChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setForm((f) => ({ ...f, note: e.target.value }));
  }, []);

  const handleSearchChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setSearch(e.target.value);
  }, []);

  const goLogin = useCallback(() => {
    router.push("/login");
  }, [router]);

  const retry = useCallback(() => {
    setReloadKey((k) => k + 1);
  }, []);

  const handleCreate = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const name = form.name.trim();
      const phone = form.phone.replace(/[\s-]/g, "");
      if (!name) {
        setFormError("请填写收款人姓名");
        return;
      }
      if (!/^1\d{10}$/.test(phone)) {
        setFormError("手机号须为 1 开头的 11 位数字");
        return;
      }
      setSaving(true);
      setFormError("");
      try {
        const res = await fetch(`${bankAgentBase()}/api/contacts`, {
          body: JSON.stringify({ name, note: form.note.trim(), phone }),
          headers: { ...bankAuthHeaders(), "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res.json()) as {
          contact?: PayeeContact;
          error?: string;
        };
        if (res.status === 401) {
          setNeedLogin(true);
          setMode("list");
          return;
        }
        if (!res.ok || !json.contact) {
          setFormError(json.error || "保存失败,请稍后重试");
          return;
        }
        const created = json.contact;
        setContacts((prev) => [created, ...prev]);
        setSearch("");
        setMode("list");
        setForm({ name: "", note: "", phone: "" });
        toast({
          description: `已添加收款人「${created.name}」`,
          type: "success",
        });
      } catch {
        setFormError("网络异常,收款人没有保存成功");
      } finally {
        setSaving(false);
      }
    },
    [form]
  );

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent
        className="max-h-[86dvh] gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-md"
        data-testid="payee-picker"
        showCloseButton={false}
      >
        <DialogHeader className="flex-row items-center gap-2 px-5 pt-4 pb-2">
          {mode === "create" ? (
            <Button
              aria-label="返回收款人列表"
              className="-ml-2 size-8"
              onClick={backToList}
              size="icon-sm"
              type="button"
              variant="ghost"
            >
              <ChevronLeftIcon />
            </Button>
          ) : null}
          <DialogTitle className="text-[15px]">
            {mode === "create" ? "新建收款人" : "选择收款人"}
          </DialogTitle>
          <div className="ml-auto flex items-center gap-1">
            {mode === "list" && !needLogin ? (
              <Button
                aria-label="新建收款人"
                data-testid="payee-add"
                onClick={startCreate}
                size="icon-sm"
                title="新建收款人"
                type="button"
                variant="outline"
              >
                <PlusIcon />
              </Button>
            ) : null}
            <DialogClose asChild>
              <Button
                aria-label="关闭"
                size="icon-sm"
                type="button"
                variant="ghost"
              >
                <XIcon />
              </Button>
            </DialogClose>
          </div>
        </DialogHeader>
        <DialogDescription className="sr-only">
          从常用收款人中选择转账对象,支持按姓名或手机号搜索,也可新建收款人。
        </DialogDescription>

        {mode === "create" ? (
          <form
            className="flex flex-col gap-3 px-5 pt-1 pb-5"
            onSubmit={handleCreate}
          >
            <label
              className="flex flex-col gap-1.5 text-[12px] text-muted-foreground"
              htmlFor="payee-name-input"
            >
              收款人姓名
              <Input
                aria-label="收款人姓名"
                autoComplete="name"
                className="rounded-lg"
                data-testid="payee-name"
                id="payee-name-input"
                maxLength={30}
                onChange={handleNameChange}
                placeholder="例如:张三"
                value={form.name}
              />
            </label>
            <label
              className="flex flex-col gap-1.5 text-[12px] text-muted-foreground"
              htmlFor="payee-phone-input"
            >
              手机号
              <Input
                aria-label="手机号"
                autoComplete="tel"
                className="rounded-lg"
                data-testid="payee-phone"
                id="payee-phone-input"
                inputMode="numeric"
                maxLength={11}
                onChange={handlePhoneChange}
                placeholder="1 开头的 11 位手机号"
                value={form.phone}
              />
            </label>
            <label
              className="flex flex-col gap-1.5 text-[12px] text-muted-foreground"
              htmlFor="payee-note-input"
            >
              备注(选填)
              <Input
                aria-label="备注"
                className="rounded-lg"
                data-testid="payee-note"
                id="payee-note-input"
                maxLength={40}
                onChange={handleNoteChange}
                placeholder="例如:房东·收房租"
                value={form.note}
              />
            </label>
            {formError ? (
              <div className="rounded-md bg-red-50 px-3 py-2 text-[12px] text-red-600 dark:bg-red-950/40 dark:text-red-300">
                {formError}
              </div>
            ) : null}
            <div className="mt-1 flex justify-end gap-2">
              <Button onClick={backToList} type="button" variant="ghost">
                取消
              </Button>
              <Button data-testid="payee-save" disabled={saving} type="submit">
                {saving ? "保存中…" : "保存收款人"}
              </Button>
            </div>
          </form>
        ) : (
          <>
            <div className="px-5 pb-2">
              <div className="relative">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  aria-label="搜索收款人"
                  className="rounded-lg pl-9"
                  data-testid="payee-search"
                  onChange={handleSearchChange}
                  placeholder="搜索姓名或手机号"
                  value={search}
                />
              </div>
            </div>

            <div className="max-h-[48dvh] min-h-24 overflow-y-auto px-2 pb-2">
              {needLogin ? (
                <div className="flex flex-col items-center gap-3 px-5 py-8 text-center">
                  <div className="text-[13px] font-medium text-foreground">
                    登录后才能使用常用收款人
                  </div>
                  <div className="text-[12px] leading-relaxed text-muted-foreground">
                    登录后可以查看已保存的收款人、按姓名或手机号搜索,
                    新建的收款人也会保存到你的账户里。
                  </div>
                  <div className="mt-1 flex gap-2">
                    <Button
                      data-testid="payee-login"
                      onClick={goLogin}
                      type="button"
                    >
                      去登录
                    </Button>
                    <Button
                      onClick={onFallback}
                      type="button"
                      variant="outline"
                    >
                      直接在对话里转账
                    </Button>
                  </div>
                </div>
              ) : loading ? (
                <div className="px-5 py-8 text-center text-[13px] text-muted-foreground">
                  正在加载收款人…
                </div>
              ) : error ? (
                <div className="flex flex-col items-center gap-3 px-5 py-8 text-center">
                  <div className="text-[13px] text-red-600 dark:text-red-300">
                    {error}
                  </div>
                  <Button
                    onClick={retry}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    重试
                  </Button>
                </div>
              ) : filtered.length === 0 ? (
                <div className="flex flex-col items-center gap-2 px-5 py-8 text-center">
                  <div className="text-[13px] font-medium text-foreground">
                    {contacts.length === 0
                      ? "还没有常用收款人"
                      : `没有找到「${search.trim()}」`}
                  </div>
                  <div className="text-[12px] text-muted-foreground">
                    点右上角 + 新建收款人,只需姓名和手机号
                  </div>
                  <Button
                    className="mt-2"
                    onClick={startCreate}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    <PlusIcon />
                    新建收款人
                  </Button>
                </div>
              ) : (
                <div className="flex flex-col">
                  {filtered.map((contact) => (
                    <PayeeRow
                      contact={contact}
                      key={contact.id}
                      onPick={onPick}
                    />
                  ))}
                </div>
              )}
            </div>

            {needLogin ? null : (
              <div className="flex items-center justify-between gap-2 border-t border-border/40 px-5 py-2.5">
                <span className="text-[11px] text-muted-foreground">
                  {search.trim()
                    ? `${filtered.length} / ${contacts.length} 位收款人`
                    : `共 ${contacts.length} 位收款人`}
                </span>
                <button
                  className="text-[11px] text-muted-foreground underline-offset-2 transition hover:text-foreground hover:underline"
                  data-testid="payee-fallback"
                  onClick={onFallback}
                  type="button"
                >
                  不选收款人,直接转账
                </button>
              </div>
            )}
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}
