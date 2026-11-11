"""异步回复示例 —— 长任务的正确形态。

你的 agent 跑一次可能要几十秒（claude --print 会阻塞），而长轮询一轮只有 35 秒。
所以钩子不能傻等：先秒回"收到"，再在后台线程里干完活，主动推第二条。

这就是"钩子返回 None + 稍后自己 send_text"的用法。

    python main.py run --handler examples/async_reply.py

在手机微信里发：
    /slow     → 立刻回"收到"，约 8 秒后推第二条结果
    其它文字   → 同样异步处理，也是约 8 秒后推第二条
"""

from __future__ import annotations

import threading
import time

from clawbot import ClawBotNode, InboundMessage

NODE = ClawBotNode()  # state_dir 省略 → 项目根下的 state/


def do_work(text: str) -> str:
    """模拟一个耗时的 agent 调用。换成你真实的调用即可。"""
    time.sleep(8)
    return f"【后台算完了】你刚才发的是：{text}"


def reply_later(peer_id: str, context_token: str, text: str) -> None:
    """在后台线程里干活，完成后主动推送第二条消息。"""
    try:
        result = do_work(text)
    except Exception as exc:
        result = f"后台任务出错：{exc}"
    try:
        NODE.send_text(peer_id, result, context_token=context_token)
    except Exception as exc:  # 推送失败只记日志，不能影响主循环
        print(f"[异步回复失败] {exc}")


@NODE.on_message
def handle(msg: InboundMessage):
    if not msg.is_text:
        return f"暂时只处理文字，收到的是{msg.kind}。"

    text = msg.text.strip()

    if text == "/slow":
        # 关键：先把 peer / context_token 抓下来交给线程，
        # 钩子立刻返回 None —— 主循环马上去处理下一条消息，不被阻塞。
        threading.Thread(
            target=reply_later,
            args=(msg.peer_id, msg.context_token, text),
            daemon=True,
        ).start()
        return "收到，我先去算，算完单独推给你。"

    # 普通消息也走异步，验证"不阻塞"这个性质
    threading.Thread(
        target=reply_later,
        args=(msg.peer_id, msg.context_token, text),
        daemon=True,
    ).start()
    return f"已受理（{time.strftime('%H:%M:%S')}），3 秒后给你结果。"
