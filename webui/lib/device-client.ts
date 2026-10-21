"use client";

/*
 * 设备/标签页标识:跨设备同步用它区分"自己写的"和"另一台设备写的"。
 * sessionStorage 保证同一标签页刷新不变、不同标签页/手机是不同 client;
 * 隐私模式下不可用时退回进程内存,至少本次会话内稳定。
 */

const STORAGE_KEY = "agent-client-id";
let memoryId = "";

function randomId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `client-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function getClientId(): string {
  if (typeof window === "undefined") {
    return "";
  }
  try {
    const existing = window.sessionStorage.getItem(STORAGE_KEY);
    if (existing) {
      return existing;
    }
    const created = randomId();
    window.sessionStorage.setItem(STORAGE_KEY, created);
    return created;
  } catch {
    if (!memoryId) {
      memoryId = randomId();
    }
    return memoryId;
  }
}
