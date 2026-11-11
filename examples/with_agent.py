"""把 ClawBot 节点接到你自己的 agent 上。

这里演示的是"独立接口"的用法：ClawBot 只负责收发，
你的 agent 通过 handle() 返回文本 —— 两者完全解耦。

替换 `my_agent()` 的实现即可接入任何后端（本地 HTTP、CLI、A2A、MQTT...）。
"""

from __future__ import annotations

import subprocess
import sys
import time

from clawbot import ClawBotNode, InboundMessage

NODE = ClawBotNode()  # state_dir 省略 → 项目根下的 state/


def my_agent(text: str) -> str:
    """你的 agent 的真实入口 —— 现在是个占位实现。

    接入方式举例（任选其一，都在同一个进程里调用）：

    # 1) 本地 HTTP 接口
    import urllib.request, json
    req = urllib.request.Request(
        "http://127.0.0.1:8000/ask",
        data=json.dumps({"text": text}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["answer"]

    # 2) 命令行
    out = subprocess.run(["python", "agent.py", text], capture_output=True, text=True)
    return out.stdout.strip()
    """
    return f"（占位 agent）收到：{text}"


@NODE.on_message
def handle(msg: InboundMessage):
    if not msg.is_text:
        return f"我暂时只处理文字，收到的是{msg.kind}。"

    started = time.time()
    try:
        reply = my_agent(msg.text)
    except Exception as exc:
        return f"agent 出错了：{exc}"
    elapsed = time.time() - started
    return f"{reply}\n\n（耗时 {elapsed:.1f}s）"


if __name__ == "__main__":
    print("这是一个示例模块，请通过以下命令运行：")
    print(f"  python main.py run --handler {__file__}")
    sys.exit(0)
