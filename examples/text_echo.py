"""消费者示例：把收到的消息原样回显。

**它不是管道的一部分** —— 管道默认不回话（只把消息写进收件箱）。
要这个回显行为，必须显式把本文件交给 `run`：

    clawbot login                                # 首次：扫码登录
    clawbot run --handler examples/text_echo.py  # 带钩子启动：收到什么回什么

接入你自己的 agent，只需要改 handle() 的返回值：
    return your_agent(text)
"""

from __future__ import annotations

import time

from clawbot import ClawBotNode, InboundMessage

# state_dir 省略 → 用项目根下的 state/（不随当前工作目录漂移）
NODE = ClawBotNode(debug=True)

STARTED_AT = time.time()


@NODE.on_message
def handle(msg: InboundMessage):
    """收到一条消息就回一条。返回字符串 => 自动回发。"""
    if not msg.is_text:
        return f"收到一条{msg.kind}消息。当前版本只处理文本，其余类型会显示为占位。"

    text = msg.text.strip()

    # 两个内置小命令，方便当场确认链路信息
    if text in ("/ping", "ping"):
        return "pong（来自电脑）"
    if text in ("/status", "status"):
        uptime = int(time.time() - STARTED_AT)
        return (
            f"节点在线，已运行 {uptime // 60} 分 {uptime % 60} 秒\n"
            f"你的 ID：{msg.peer_id}\n"
            f"会话令牌：{'已获取' if msg.context_token else '缺失'}"
        )

    return f"收到：{text}"
