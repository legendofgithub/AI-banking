"""BankAgent 微信转发技能的调用脚本:relay.py <openid> <消息文件路径>

只依赖 Python 标准库——OpenClaw 用哪个解释器调都能跑,不需要项目 .venv。
stdout 即最终回复文本(OpenClaw 逐字回给微信用户);失败时输出降级话术
并返回非 0,让技能侧能判断。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("BANKAGENT_RELAY_URL",
                             "http://127.0.0.1:8800/api/wechat/relay")
TIMEOUT_S = float(os.environ.get("BANKAGENT_RELAY_TIMEOUT", "120"))
FALLBACK = "银行服务暂时不可用,请稍后再试。"


def main() -> int:
    openid = (sys.argv[1] if len(sys.argv) > 1 else "claw-default").strip() \
        or "claw-default"
    text = ""
    if len(sys.argv) > 2:
        with open(sys.argv[2], encoding="utf-8") as fh:
            text = fh.read().strip()
    if not text:
        print(FALLBACK)
        return 1

    body = json.dumps({"openid": openid, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        DEFAULT_BASE, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    key = os.environ.get("WECHAT_RELAY_KEY", "").strip()
    if key:  # 后端开了密钥校验时带上
        req.add_header("X-Relay-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        print(FALLBACK)
        return 1
    print(payload.get("reply") or FALLBACK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
