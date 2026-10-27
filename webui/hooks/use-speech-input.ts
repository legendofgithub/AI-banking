"use client";

/*
 * 语音输入 hook(2026-10-27,浏览器 Web Speech API · 零依赖):
 * 点麦克风开始识别 → interim/final 转写通过 onTranscript 回调交给页面
 * (页面负责合并进输入框);再次点击或 onend 自动停止。
 *
 * 设计约束:
 * - 不自动发送:转写只落输入框,回车仍由用户手动触发(避免误发资金指令);
 * - 权限/兼容降级:不支持或非安全上下文(手机经局域网 http)→ status
 *   "unsupported",按钮不渲染;错误统一中文 toast 文案由页面展示;
 * - 适配层留位:识别实现集中在 _createRecognition,后续要接后端 ASR
 *   (MediaRecorder + /api/transcribe)时只替换这一段。
 */

import { useCallback, useEffect, useRef, useState } from "react";

// TS DOM 库不含 SpeechRecognition —— 就地声明用到的最小面
interface SpeechRecognitionAlternativeLike {
  transcript: string;
}
interface SpeechRecognitionResultLike {
  isFinal: boolean;
  length: number;
  [index: number]: SpeechRecognitionAlternativeLike;
}
interface SpeechRecognitionEventLike {
  resultIndex: number;
  results: { length: number; [index: number]: SpeechRecognitionResultLike };
}
interface SpeechRecognitionErrorEventLike {
  error: string;
}
interface SpeechRecognitionLike {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  abort(): void;
  onresult: ((e: SpeechRecognitionEventLike) => void) | null;
  onerror: ((e: SpeechRecognitionErrorEventLike) => void) | null;
  onend: (() => void) | null;
}
type SpeechRecognitionCtor = new () => SpeechRecognitionLike;

export type SpeechStatus = "idle" | "listening" | "unsupported";

// Web Speech 错误码 → 现场可懂的中文(浏览器只会给英文枚举)
const ERROR_TEXT: Record<string, string> = {
  "not-allowed": "麦克风权限被拒绝，请在浏览器地址栏允许后重试",
  "service-not-allowed": "语音服务不可用，请稍后重试",
  "no-speech": "没听清，请再试一次",
  "audio-capture": "没有检测到麦克风",
  network: "语音识别网络异常，请稍后重试",
};

function errorText(code: string): string {
  return (
    ERROR_TEXT[code] ??
    (code === "not-allowed" ? ERROR_TEXT["not-allowed"] : `语音识别失败(${code})`)
  );
}

function getRecognitionCtor(): SpeechRecognitionCtor | null {
  if (typeof window === "undefined") {
    return null;
  }
  const w = window as unknown as {
    SpeechRecognition?: SpeechRecognitionCtor;
    webkitSpeechRecognition?: SpeechRecognitionCtor;
  };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export function useSpeechInput(onTranscript: (text: string, isFinal: boolean) => void) {
  const [status, setStatus] = useState<SpeechStatus>("idle");
  const [error, setError] = useState("");
  const recognitionRef = useRef<SpeechRecognitionLike | null>(null);
  // 回调经 ref 转发:识别实例只建一次,回调每次渲染的新引用也能被用到
  const cbRef = useRef(onTranscript);
  useEffect(() => {
    cbRef.current = onTranscript;
  }, [onTranscript]);

  useEffect(() => {
    if (getRecognitionCtor() === null) {
      setStatus("unsupported");
    }
    return () => {
      recognitionRef.current?.abort();
      recognitionRef.current = null;
    };
  }, []);

  const stop = useCallback(() => {
    recognitionRef.current?.abort();
    recognitionRef.current = null;
    setStatus("idle");
  }, []);

  const start = useCallback(() => {
    const Ctor = getRecognitionCtor();
    if (!Ctor) {
      setStatus("unsupported");
      return;
    }
    if (recognitionRef.current) {
      return; // 已在聆听
    }
    setError("");
    const rec = new Ctor();
    rec.lang = "zh-CN";
    rec.continuous = false;
    rec.interimResults = true;
    rec.onresult = (e) => {
      let text = "";
      let isFinal = false;
      for (let i = e.resultIndex; i < e.results.length; i += 1) {
        const r = e.results[i];
        text += r[0]?.transcript ?? "";
        if (r.isFinal) {
          isFinal = true;
        }
      }
      if (text) {
        cbRef.current(text, isFinal);
      }
    };
    rec.onerror = (e) => {
      setError(errorText(e.error));
      setStatus("idle");
      recognitionRef.current = null;
    };
    rec.onend = () => {
      // 正常说完自动结束;非安全上下文的静默失败也走这里
      recognitionRef.current = null;
      setStatus((s) => (s === "listening" ? "idle" : s));
    };
    try {
      rec.start();
      recognitionRef.current = rec;
      setStatus("listening");
    } catch {
      // start 抛错(常见:上一实例未完全释放/权限被策略禁)
      setError("无法启动语音识别，请稍后重试");
      recognitionRef.current = null;
      setStatus("idle");
    }
  }, []);

  const toggle = useCallback(() => {
    if (status === "listening") {
      stop();
    } else if (status !== "unsupported") {
      start();
    }
  }, [start, status, stop]);

  return { status, error, start, stop, toggle };
}
