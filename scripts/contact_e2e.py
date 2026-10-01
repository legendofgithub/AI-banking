# 联系人录入全流程实测:驱动 8800 SSE,走完 引导问答 → 确认卡 → 入库。
# 用法:cd 项目根 && .venv/Scripts/python.exe scripts/contact_e2e.py
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8800/api/chat"


def turn(thread_id: str, text: str):
    body = json.dumps({"thread_id": thread_id,
                       "messages": [{"role": "user", "content": text}]}).encode()
    req = urllib.request.Request(BASE, data=body,
                                 headers={"Content-Type": "application/json"})
    texts, parts = [], []
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            p = json.loads(line[5:].strip())
            t = p.get("type")
            if t == "text-delta":
                texts.append(p.get("delta", ""))
            elif t and t.startswith("data-"):
                parts.append((t, p.get("data")))
            elif t == "error":
                parts.append(("error", p.get("errorText")))
    return "".join(texts), parts


def main():
    tid = sys.argv[1] if len(sys.argv) > 1 else "contact-e2e-run"
    script = [
        "添加收款人",
        "张三",
        "13800138000",
        "健身教练,每周二上课",
        "确认",
    ]
    ok = True
    for msg in script:
        text, parts = turn(tid, msg)
        print(f"\n=== 用户: {msg}")
        print(f"    助手: {text.strip()[:120]}")
        for t, d in parts:
            print(f"    部件: {t} -> {json.dumps(d, ensure_ascii=False)[:160]}")
        if any(t == "error" for t, _ in parts):
            ok = False
            break
    print("\nRESULT:", "PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
