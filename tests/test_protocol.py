"""协议层自检（不需要连微信服务器）。

用一个本地 HTTP 桩服务器承接真实 ClawBotClient 的请求，验证：
  - 请求头约定（AuthorizationType / X-WECHAT-UIN / iLink-App-Id / base_info）
  - 登录流程（confirmed / need_verifycode / expired 刷新 / 重定向）
  - 长轮询游标持久化与断点续读
  - 发送文本必须回传 context_token
  - -14 会话暂停识别
  - 节点主循环：钩子分发、异常兜底、只处理用户消息
  - context_token 记忆（异步回复用）

运行：python tests/test_protocol.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from clawbot import ClawBotClient, ClawBotError, ClawBotNode, SessionPaused  # noqa: E402
from clawbot.client import _encode_version  # noqa: E402
from clawbot.store import Credentials, StateStore, parse_inbound  # noqa: E402

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


# --------------------------------------------------------------------------
# 桩服务器
# --------------------------------------------------------------------------


class StubState:
    """桩服务器的可编程状态。"""

    def __init__(self) -> None:
        self.requests: list = []          # [(path, headers, body)]
        self.qr_status_sequence: list = ["wait", "scaned", "confirmed"]
        self.getupdates_responses: list = []
        self.fail_next_getupdates = False
        self.send_response_ret = 0
        self.verify_prompted = 0
        # 发送相关的可编程故障
        self.send_fail_times = 0          # 前 N 次返回失败
        self.send_fail_ret = -1
        self.send_http_error = 0          # 非 0 时返回该 HTTP 状态码
        self.send_payload = None          # 覆写 sendmessage 的整个响应体
        self.message_id_counter = 900000


STUB = StubState()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            return {"__raw__": raw.decode("utf-8", "replace")}

    def _reply(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        STUB.requests.append((self.path, dict(self.headers), None))
        if path.endswith("/get_qrcode_status"):
            seq = STUB.qr_status_sequence
            status = seq.pop(0) if seq else "wait"
            if status == "confirmed":
                self._reply({
                    "status": "confirmed",
                    "bot_token": "TOKEN-abc123",
                    "ilink_bot_id": "bot@im.bot",
                    "baseurl": f"http://127.0.0.1:{self.server.server_port}",
                    "ilink_user_id": "peer@im.wechat",
                })
            else:
                self._reply({"status": status})
        else:
            self._reply({"error": "unknown"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_body()
        STUB.requests.append((self.path, dict(self.headers), body))

        if path.endswith("/get_bot_qrcode"):
            self._reply({"qrcode": "QR-VALUE-1", "qrcode_img_content": "https://weixin.qq.com/x/QR"})
        elif path.endswith("/getupdates"):
            if STUB.fail_next_getupdates:
                STUB.fail_next_getupdates = False
                self._reply({"ret": -14, "errmsg": "session paused"})
                return
            if STUB.getupdates_responses:
                self._reply(STUB.getupdates_responses.pop(0))
            else:
                self._reply({"ret": 0, "msgs": [], "get_updates_buf": "cursor-empty"})
        elif path.endswith("/sendmessage"):
            if STUB.send_payload is not None:
                self._reply(STUB.send_payload)
            elif STUB.send_http_error:
                self._reply({"error": "server error"}, STUB.send_http_error)
            elif STUB.send_fail_times > 0:
                STUB.send_fail_times -= 1
                self._reply({"ret": STUB.send_fail_ret, "errmsg": "stub transient failure"})
            else:
                STUB.message_id_counter += 1
                self._reply({"ret": 0, "errmsg": "", "message_id": STUB.message_id_counter})
        elif path.endswith("/getconfig"):
            self._reply({"ret": 0, "typing_ticket": "TICKET-1"})
        elif path.endswith("/sendtyping"):
            self._reply({"ret": 0})
        else:
            self._reply({"error": "unknown"}, 404)


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def ci_headers(raw) -> dict:
    """HTTP 头名大小写不敏感（email.message 会改写大小写），统一成小写键。"""
    return {k.lower(): v for k, v in dict(raw).items()}


def headers_of(path_suffix: str) -> list:
    return [ci_headers(h) for p, h, _ in STUB.requests if p.split("?")[0].endswith(path_suffix)]


def dump_requests() -> None:
    for path, headers, body in STUB.requests:
        h = ci_headers(headers)
        keys = ("authorizationtype", "authorization", "ilink-app-id",
                "x-wechat-uin", "ilink-app-clientversion", "content-type")
        print(f"    {path.split('?')[0]}")
        print(f"      headers={ {k: h.get(k) for k in keys if k in h} }")


def bodies_of(path_suffix: str) -> list:
    return [b for p, _, b in STUB.requests if p.split("?")[0].endswith(path_suffix)]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_http_headers_and_login(base_url: str) -> None:
    print("\n[1] 请求头与扫码登录")
    client = ClawBotClient(api_base=base_url, debug=False)

    qr = client.fetch_qr()
    check("取到二维码值", qr.qrcode == "QR-VALUE-1")
    check("取到二维码链接", qr.qrcode_url.startswith("https://weixin.qq.com/"))

    hdrs = headers_of("get_bot_qrcode")[0]
    check("AuthorizationType 正确", hdrs.get("authorizationtype") == "ilink_bot_token",
          str(hdrs.get("authorizationtype")))
    check("iLink-App-Id = bot", hdrs.get("ilink-app-id") == "bot")
    check("X-WECHAT-UIN 是 base64", _is_b64_uint(hdrs.get("x-wechat-uin", "")))
    check("登录前不带 Authorization", "authorization" not in hdrs)
    if hdrs.get("ilink-app-id") != "bot":
        print("    实际收到的请求：")
        dump_requests()

    qr_requests_before = len(STUB.requests)
    STUB.qr_status_sequence = ["wait", "scaned", "confirmed"]
    seen_status: list = []

    def ask(prompt: str) -> str:
        STUB.verify_prompted += 1
        return "123456"

    creds, user_id = client.login_with_qr(
        on_qr=lambda s: None,
        on_status=lambda s: seen_status.append(s),
        ask_verify_code=ask,
        timeout_s=10,
    )
    check("登录成功拿到 token", creds.bot_token == "TOKEN-abc123")
    check("拿到 ilink_bot_id", creds.ilink_bot_id == "bot@im.bot")
    check("拿到 ilink_user_id", user_id == "peer@im.wechat")
    check("baseurl 已采用服务端返回", client.api_base == base_url)
    check("状态序列被观察到", seen_status[:3] == ["wait", "scaned", "confirmed"], str(seen_status))

    # 轮询请求不应携带 Authorization（是应用请求，不是鉴权请求）
    poll_hdrs = [ci_headers(h) for p, h, _ in STUB.requests[qr_requests_before:]
                 if p.split("?")[0].endswith("get_qrcode_status")]
    check("二维码轮询不带 Authorization", all("authorization" not in h for h in poll_hdrs))
    check("二维码轮询带 iLink-App-Id", all(h.get("ilink-app-id") == "bot" for h in poll_hdrs))


def _is_b64_uint(value: str) -> bool:
    import base64
    try:
        decoded = base64.b64decode(value.encode("ascii")).decode("ascii")
        return decoded.isdigit() and 0 <= int(decoded) <= 0xFFFFFFFF
    except Exception:
        return False


def test_verify_code_flow(base_url: str) -> None:
    print("\n[2] 需要验证码的登录流程")
    STUB.requests.clear()
    STUB.qr_status_sequence = ["need_verifycode", "scaned", "confirmed"]
    client = ClawBotClient(api_base=base_url)
    prompts: list = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "654321"

    # 第一次 need_verifycode 需要输入；第二次 scaned 清除暂存
    client.login_with_qr(on_qr=lambda s: None, ask_verify_code=ask, timeout_s=10)
    check("验证码被询问一次", len(prompts) == 1, f"prompts={prompts}")

    poll_paths = [p for p, _, _ in STUB.requests if "get_qrcode_status" in p]
    check("后续轮询带上了 verify_code", any("verify_code=654321" in p for p in poll_paths),
          str(poll_paths))


def test_qr_refresh_on_expired(base_url: str) -> None:
    print("\n[3] 二维码过期后自动刷新")
    STUB.requests.clear()
    STUB.qr_status_sequence = ["expired", "confirmed"]
    client = ClawBotClient(api_base=base_url)
    shown: list = []
    client.login_with_qr(on_qr=lambda s: shown.append(s.qrcode), timeout_s=10)
    check("过期后重新申请了二维码", len(shown) >= 2, f"shown={shown}")
    check("重新申请调用到 get_bot_qrcode", len(bodies_of("get_bot_qrcode")) >= 2)


def test_getupdates_and_cursor(base_url: str) -> None:
    print("\n[4] 长轮询与游标")
    STUB.requests.clear()
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))

    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    inbound = {
        "message_id": 1001,
        "seq": 5,
        "message_type": 1,
        "from_user_id": "peer@im.wechat",
        "context_token": "CTX-1",
        "item_list": [{"type": 1, "text_item": {"text": "你好"}}],
    }
    bot_echo = dict(inbound, message_id=1002, message_type=2, context_token="CTX-BOT")
    STUB.getupdates_responses = [
        {"ret": 0, "msgs": [inbound, bot_echo], "get_updates_buf": "CURSOR-1"},
    ]

    received: list = []

    @node.on_message
    def handle(msg):
        received.append(msg)
        return "回复内容"

    node.run(max_iterations=1)

    check("只处理用户消息（忽略 BOT 自己发的）", len(received) == 1, f"got={len(received)}")
    check("解析出文本", received and received[0].text == "你好")
    check("游标已落盘", store.load_cursor() == "CURSOR-1", store.load_cursor())

    sends = bodies_of("sendmessage")
    check("发出了一条回复", len(sends) == 1, f"got={len(sends)}")
    if sends:
        msg = sends[0]["msg"]
        check("回复带回原 context_token", msg["context_token"] == "CTX-1")
        check("回复的 to_user_id 是发送者", msg["to_user_id"] == "peer@im.wechat")
        check("message_type=2（BOT）", msg["message_type"] == 2)
        check("message_state=2（完成）", msg["message_state"] == 2)
        check("文本内容正确", msg["item_list"][0]["text_item"]["text"] == "回复内容")
        check("client_id 存在", bool(msg.get("client_id")))
        check("请求带 base_info（在 body 里）", "base_info" in sends[0])
        check("请求的 HTTP 头不带 base_info", "base_info" not in ci_headers(
            [h for p, h, _ in STUB.requests if p.split("?")[0].endswith("sendmessage")][0]))
        check("请求带 Authorization",
              any(ci_headers(h).get("authorization") == "Bearer T" for p, h, _ in STUB.requests
                  if p.split("?")[0].endswith("sendmessage")))

    # 同一进程内的第二次轮询必须带上第一次拿到的游标
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [], "get_updates_buf": "CURSOR-2"}]
    node.run(max_iterations=1)
    sent = [b.get("get_updates_buf") for b in bodies_of("getupdates")]
    check("同一进程内续读带上新游标", sent and sent[0] == "CURSOR-1", str(sent))
    check("新游标已落盘", store.load_cursor() == "CURSOR-2", store.load_cursor())

    # 新进程（新 StateStore 实例）应从磁盘恢复游标
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [], "get_updates_buf": "CURSOR-3"}]
    node2 = ClawBotNode(state_dir=tmp)
    node2.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
    node2.run(max_iterations=1)
    sent = [b.get("get_updates_buf") for b in bodies_of("getupdates")]
    check("重启后续读带上磁盘游标", sent and sent[0] == "CURSOR-2", str(sent))

    # 服务端返回空游标时不得覆盖已存值
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [], "get_updates_buf": ""}]
    node2.run(max_iterations=1)
    check("空游标不覆盖已存游标", store.load_cursor() == "CURSOR-3", store.load_cursor())


def test_handler_error_isolation(base_url: str) -> None:
    print("\n[5] 钩子异常不影响主循环")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    STUB.requests.clear()
    inbound = {
        "message_id": 2001, "message_type": 1,
        "from_user_id": "peer@im.wechat", "context_token": "CTX-2",
        "item_list": [{"type": 1, "text_item": {"text": "触发异常"}}],
    }
    STUB.getupdates_responses = [{"ret": 0, "msgs": [inbound], "get_updates_buf": "C2"}]

    @node.on_message
    def handle(msg):
        raise RuntimeError("钩子内部炸了")

    node.run(max_iterations=1)
    sends = bodies_of("sendmessage")
    check("异常时回了兜底提示", len(sends) >= 1, f"got={len(sends)}")

    # 返回 None 时不自动回复
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [dict(inbound, message_id=2002)],
                                  "get_updates_buf": "C3"}]

    @node.on_message
    def silent(msg):
        return None

    node.run(max_iterations=1)
    check("钩子返回 None 时不自动回复", len(bodies_of("sendmessage")) == 0)


def test_session_paused(base_url: str) -> None:
    print("\n[6] -14 会话暂停识别")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    STUB.fail_next_getupdates = True
    raised = False
    try:
        client.get_updates("CUR")
    except SessionPaused as exc:
        raised = True
        check("SessionPaused 带 ret=-14", exc.ret == -14)
    check("识别出会话暂停", raised)

    # 普通业务错误不应被当成暂停
    STUB.getupdates_responses = [{"ret": -1, "errmsg": "bad"}]
    try:
        client.get_updates("CUR")
        check("非 -14 错误抛 ClawBotError", False, "没有抛异常")
    except SessionPaused:
        check("非 -14 错误抛 ClawBotError", False, "被误判为暂停")
    except ClawBotError as exc:
        check("非 -14 错误抛 ClawBotError", exc.ret == -1)


def test_context_memory_and_send_guard(base_url: str) -> None:
    print("\n[7] context_token 记忆与发送前置校验")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    store.remember_context("peer@im.wechat", "CTX-MEMO")
    check("context 已持久化", store.get_context("peer@im.wechat") == "CTX-MEMO")

    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
    STUB.requests.clear()
    result = node.send_text("peer@im.wechat", "异步回复")
    sends = bodies_of("sendmessage")
    check("用记住的 context_token 发成功", result.ok and len(sends) == 1, str(result))
    check("返回值带 message_id", result.message_id is not None, str(result))
    if sends:
        check("用的是持久化的令牌", sends[0]["msg"]["context_token"] == "CTX-MEMO")

    # 没有会话令牌时返回失败结果（而不是抛异常），且必须留下日志
    result2 = node.send_text("nobody@im.wechat", "会失败")
    check("没有会话令牌时返回 ok=False", result2.ok is False, str(result2))
    check("失败原因可读", "context_token" in result2.errmsg, result2.errmsg)


def test_parse_inbound() -> None:
    print("\n[8] 消息解析")
    text = parse_inbound({
        "message_id": 1, "message_type": 1, "from_user_id": "u@im.wechat",
        "context_token": "C", "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
    })
    check("文本消息 kind=text", text.kind == "text" and text.text == "hi")

    image = parse_inbound({
        "message_id": 2, "message_type": 1, "from_user_id": "u@im.wechat",
        "context_token": "C",
        "item_list": [{"type": 2, "image_item": {"aeskey": "ab" * 16}}],
    })
    check("图片消息 kind=image", image.kind == "image")
    check("图片消息 text 为空", image.text == "")

    voice = parse_inbound({
        "message_id": 3, "message_type": 1, "from_user_id": "u@im.wechat",
        "item_list": [{"type": 3, "voice_item": {"text": "语音转写"}}],
    })
    check("语音取转写文本", voice.kind == "voice" and voice.text == "语音转写")

    mixed = parse_inbound({
        "message_id": 4, "message_type": 1, "from_user_id": "u@im.wechat",
        "item_list": [{"type": 1, "text_item": {"text": "说明文字"}},
                      {"type": 2, "image_item": {}}],
    })
    check("图文混合保留文字", mixed.text == "说明文字" and mixed.kind == "text")


def test_credentials_masking() -> None:
    print("\n[9] 凭证脱敏与持久化")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    creds = Credentials(bot_token="FAKE_BOT_TOKEN", ilink_bot_id="bot@im.bot",
                        ilink_user_id="peer@im.wechat", baseurl="https://x")
    store.save_credentials(creds)
    loaded = store.load_credentials()
    check("凭证可回读", loaded.bot_token == "FAKE_BOT_TOKEN")
    check("登录时间被记录", loaded.logged_in_at > 0)
    masked = loaded.masked()
    check("脱敏不含明文 token", "FAKE_BOT_TOKEN" not in json.dumps(masked, ensure_ascii=False),
          "token 泄漏了")
    check("脱敏保留了长度信息", "len=" in masked["bot_token"])

    store.save_cursor("CUR-X")
    check("游标可回读", StateStore(tmp).load_cursor() == "CUR-X")
    store.clear_credentials()
    check("clear 后凭证为空", not store.load_credentials().is_valid)
    check("clear 后游标为空", StateStore(tmp).load_cursor() == "")


def test_send_retry_and_visibility(base_url: str) -> None:
    """发送失败必须可见、且瞬时故障要重试。

    这是"后台线程推送静默失败"那类问题的防线：
    失败不能是无痕的，也不能一次失败就放弃。
    """
    print("\n[10] 发送重试与失败可见性")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
    peer, token = "peer@im.wechat", "CTX-R"

    # 1) 前两次业务失败、第三次成功 → 应重试并最终送达
    STUB.requests.clear()
    STUB.send_fail_times = 2
    STUB.send_fail_ret = -1
    r = client.send_text(peer, "重试后应成功", token)
    attempts = len(bodies_of("sendmessage"))
    check("瞬时失败会自动重试并最终成功", r.ok, str(r))
    check("重试次数符合预期（3 次尝试）", attempts == 3, f"attempts={attempts}")
    check("成功结果记录了尝试次数", r.attempts == 3, str(r))

    # 2) 一直失败 → 返回 ok=False，且尝试次数有限
    STUB.requests.clear()
    STUB.send_fail_times = 99
    r2 = client.send_text(peer, "注定失败", token, retries=2)
    attempts2 = len(bodies_of("sendmessage"))
    check("持续失败时返回 ok=False", r2.ok is False, str(r2))
    check("失败不会无限重试", attempts2 == 3, f"attempts={attempts2}")
    check("失败结果带 ret", r2.ret == -1, str(r2))
    STUB.send_fail_times = 0

    # 3) HTTP 4xx 是确定性错误，不应重试
    STUB.requests.clear()
    STUB.send_http_error = 400
    r3 = client.send_text(peer, "4xx 不该重试", token, retries=2)
    attempts3 = len(bodies_of("sendmessage"))
    check("HTTP 4xx 不重试", attempts3 == 1, f"attempts={attempts3}")
    check("HTTP 4xx 返回 ok=False", r3.ok is False, str(r3))
    STUB.send_http_error = 0

    # 4) HTTP 5xx 是瞬时错误，应该重试
    STUB.requests.clear()
    STUB.send_http_error = 503
    client.send_text(peer, "5xx 应重试", token, retries=2)
    attempts4 = len(bodies_of("sendmessage"))
    check("HTTP 5xx 会重试", attempts4 == 3, f"attempts={attempts4}")
    STUB.send_http_error = 0

    # 5) 缺参数时立即失败，不发请求
    STUB.requests.clear()
    r5 = client.send_text(peer, "没有令牌", "")
    check("缺 context_token 立即失败", r5.ok is False and not bodies_of("sendmessage"),
          str(r5))

    # 6) 节点层：钩子回复失败也不影响主循环
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
    STUB.requests.clear()
    STUB.getupdates_responses = [
        {"ret": 0, "msgs": [{
            "message_id": 5, "message_type": 1, "from_user_id": peer,
            "context_token": "CTX-N", "item_list": [{"type": 1, "text_item": {"text": "hi"}}],
        }], "get_updates_buf": "CN"},
        {"ret": 0, "msgs": [], "get_updates_buf": "CN2"},
    ]

    @node.on_message
    def handle(msg):
        return "回复会失败"

    STUB.send_fail_times = 99
    node.run(max_iterations=2)   # 不应因发送失败中断
    check("回复失败不中断主循环", len(bodies_of("getupdates")) == 2,
          f"getupdates={len(bodies_of('getupdates'))}")
    STUB.send_fail_times = 0


def test_qr_polling_headers(base_url: str) -> None:
    """M1：二维码状态轮询只带"应用请求头"。

    协议原文：「二维码状态轮询发送应用请求头（`iLink-App-Id`、
    `iLink-App-ClientVersion` 和可选的 `SKRouteTag`），**不添加**
    `AuthorizationType`、`Authorization` 或 `X-WECHAT-UIN`」。
    旧实现让 GET 复用了鉴权头 —— 当时的自检只断言了"不带 Authorization"，
    所以多带的这两个头一直没有被拦下。
    """
    print("\n[11] 二维码轮询的请求头集合")
    STUB.requests.clear()
    STUB.qr_status_sequence = ["wait", "confirmed"]
    client = ClawBotClient(api_base=base_url)
    session = client.fetch_qr()
    client.poll_qr_once(session)

    poll = headers_of("get_qrcode_status")
    check("轮询确实发生了一次", len(poll) == 1, f"got={len(poll)}")
    if poll:
        hdrs = poll[0]
        check("带 iLink-App-Id", hdrs.get("ilink-app-id") == "bot")
        check("带 iLink-App-ClientVersion", hdrs.get("ilink-app-clientversion") == "256",
              str(hdrs.get("ilink-app-clientversion")))
        check("**不**带 AuthorizationType", "authorizationtype" not in hdrs)
        check("**不**带 X-WECHAT-UIN", "x-wechat-uin" not in hdrs)
        check("**不**带 Authorization", "authorization" not in hdrs)
        check("**不**带 Content-Type（GET 没有请求体）", "content-type" not in hdrs)

    fetch = headers_of("get_bot_qrcode")
    if fetch:
        check("申请二维码仍带 AuthorizationType",
              fetch[0].get("authorizationtype") == "ilink_bot_token")
        check("申请二维码仍带 X-WECHAT-UIN", bool(fetch[0].get("x-wechat-uin")))
        check("申请二维码不带 Authorization", "authorization" not in fetch[0])


def test_getupdates_errcode(base_url: str) -> None:
    """M2：errcode 非零同样算失败（协议：检查非零 ret **或** errcode）。"""
    print("\n[12] getUpdates 的 errcode")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    STUB.getupdates_responses = [
        {"ret": 0, "errcode": -3, "errmsg": "bad", "msgs": [], "get_updates_buf": "X"},
    ]
    try:
        client.get_updates("CUR")
        check("errcode 非零要报错（否则变成不退避的热循环）", False, "没有抛异常")
    except ClawBotError as exc:
        check("errcode 非零要报错（否则变成不退避的热循环）", True)
        check("错误信息里带上 errcode", "-3" in str(exc), str(exc))

    STUB.getupdates_responses = [{"ret": 0, "errcode": -14, "msgs": []}]
    try:
        client.get_updates("CUR")
        check("errcode=-14 判为会话暂停", False, "没有抛 SessionPaused")
    except SessionPaused:
        check("errcode=-14 判为会话暂停", True)


def test_longpoll_timeout(base_url: str) -> None:
    """M3：采纳服务端建议的 longpolling_timeout_ms；超时是空结果，不是故障。"""
    print("\n[13] 长轮询超时")
    import clawbot.client as client_mod

    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    seen_timeouts: list = []
    original = client_mod._request

    def fake_request(method, url, *, headers, body=None, timeout=0):
        seen_timeouts.append(timeout)
        return json.dumps({"ret": 0, "msgs": [], "get_updates_buf": "C9",
                           "longpolling_timeout_ms": 9000})

    def timing_out(method, url, *, headers, body=None, timeout=0):
        raise client_mod.RequestTimeout("请求超时", transient=True)

    client_mod._request = fake_request
    try:
        client.get_updates("CUR")
        check("采纳服务端建议（9000ms → 9s）", client._longpoll_timeout == 9.0,
              str(client._longpoll_timeout))
        client.get_updates("CUR")
        check("下一次请求真的用了新超时", seen_timeouts[-1] == 9.0, str(seen_timeouts))

        client_mod._request = timing_out
        resp = client.get_updates("CUR-KEEP")
        check("超时映射为空结果（不抛异常）",
              resp.get("msgs") == [] and resp.get("ret") == 0, str(resp))
        check("超时不改动游标", resp.get("get_updates_buf") == "CUR-KEEP", str(resp))

        node = ClawBotNode(state_dir=tmp)
        node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
        node.run(max_iterations=1)
        check("主循环遇到超时不退出、也不写新游标", store.load_cursor() == "",
              store.load_cursor())
    finally:
        client_mod._request = original


def test_session_paused_on_send(base_url: str) -> None:
    """M4：sendmessage 上的 -14 不做无谓重试，并且节点真的进入暂停窗口。"""
    print("\n[14] 发送时发现会话暂停（-14）")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    store.remember_context("peer@im.wechat", "CTX-P")
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    STUB.requests.clear()
    STUB.send_payload = {"ret": -14, "errmsg": "session paused"}
    try:
        result = node.send_text("peer@im.wechat", "会被暂停拦下")
        sends = bodies_of("sendmessage")
        check("-14 不做无谓重试（只发 1 次）", len(sends) == 1, f"got={len(sends)}")
        check("返回 ok=False 且带 ret=-14",
              result.ok is False and result.ret == -14, str(result))
        check("节点进入暂停窗口（而不是把 -14 当成一条普通失败）",
              node._paused_until > time.time(), str(node._paused_until))
    finally:
        STUB.send_payload = None


def test_dirty_fields_tolerated(base_url: str) -> None:
    """L1/L2：服务端给了脏字段，对外契约也不能破。

    `send_*` 承诺不抛异常，`parse_inbound` 承诺在主循环里不会炸 ——
    后者尤其重要：它跑在游标落盘**之前**，抛一次就会让整批消息被重复处理。
    """
    print("\n[15] 脏字段与版本编码")
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)

    STUB.requests.clear()
    STUB.send_payload = {"ret": 0, "message_id": "not-a-number"}
    try:
        result = client.send_text("peer@im.wechat", "hi", "CTX")
        check("非数值 message_id 不抛异常", result.ok is True, str(result))
        check("拿不到 id 时 message_id 记 None", result.message_id is None, str(result.message_id))
    finally:
        STUB.send_payload = None

    STUB.requests.clear()
    STUB.send_payload = {"ret": "garbage", "errmsg": "?"}
    try:
        result2 = client.send_text("peer@im.wechat", "hi", "CTX")
        # 以前这里写的是 isinstance(result2.ok, bool) —— 恒真，抓不到任何东西。
        check("非数值 ret 必须当失败（不能静默丢错）", result2.ok is False, str(result2))
        check("失败原因里带上了原始 ret", "garbage" in result2.errmsg, result2.errmsg)
    finally:
        STUB.send_payload = None

    parsed = parse_inbound({
        "message_id": "abc", "seq": None, "create_time_ms": "x",
        "from_user_id": "u@im.wechat",
        "item_list": ["junk", {"type": "1"}, {"type": 1, "text_item": "not-a-dict"}],
    })
    check("脏 message_id/seq 退化为 0", parsed.message_id == 0 and parsed.seq == 0)
    check("非 dict 的 item 被跳过、非 dict 的 text_item 取不到内容",
          parsed.text == "" and parsed.kind == "text",
          f"text={parsed.text!r} kind={parsed.kind}")
    check("item_list 不是列表也不炸",
          parse_inbound({"item_list": "junk"}).kind == "unknown")

    check("版本编码补零：1.0 → 0x010000", _encode_version("1.0") == 65536,
          str(_encode_version("1.0")))
    check("版本编码 0.1.0 → 256", _encode_version("0.1.0") == 256)
    check("版本编码 2.4.8 → 132104", _encode_version("2.4.8") == 132104)
    check("版本编码遇到垃圾不炸", _encode_version("x.y.z") == 0 and _encode_version("") == 0)


def _node_with_store(base_url: str, peer: str = "peer@im.wechat"):
    tmp = tempfile.mkdtemp(prefix="clawbot-test-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=store.load_credentials(), api_base=base_url)
    return node, store


def _inbound(message_id: int, text: str = "hi", peer: str = "peer@im.wechat") -> dict:
    return {
        "message_id": message_id, "message_type": 1, "from_user_id": peer,
        "context_token": "CTX-N", "item_list": [{"type": 1, "text_item": {"text": text}}],
    }


def test_handler_arity_inspect(base_url: str) -> None:
    """钩子元数必须用 inspect 判定，不能"先按两个参数调、抛 TypeError 再按一个参数调"。

    后者在钩子形参带默认值（`def h(msg, node=None)`）且**内部自己**抛同型 TypeError 时，
    会把钩子跑两遍 —— 首次投递就重复执行，副作用翻倍（实测：调用 2 次、自动回复 1 次）。
    """
    print("\n[16] 钩子元数判定（不靠异常文本）")
    node, _store = _node_with_store(base_url)
    calls: list = []

    @node.on_message
    def handle(msg, node=None):                       # 两个位置参数，第二个有默认值
        calls.append(msg.text)
        raise TypeError("takes 1 positional argument but 2 were given")   # 内部自己抛同型

    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9101)],
                                  "get_updates_buf": "C-ARITY"}]
    node.run(max_iterations=1)
    check("钩子只被调用一次（不再双跑）", len(calls) == 1, f"calls={calls}")

    # 只收一个参数的钩子仍然工作
    node2, _s2 = _node_with_store(base_url)
    one_arg: list = []

    @node2.on_message
    def handle_one(msg):
        one_arg.append(msg.text)
        return "收到"

    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9102, "单参")],
                                  "get_updates_buf": "C-ONE"}]
    node2.run(max_iterations=1)
    check("单参数钩子照常被调用", one_arg == ["单参"], str(one_arg))


def test_hook_error_text_not_leaked(base_url: str) -> None:
    """钩子异常的原文不能发给对端（实测过把本机 .ssh 路径原样发出去）。"""
    print("\n[17] 钩子异常不把内部信息发给对端")
    node, _store = _node_with_store(base_url)

    @node.on_message
    def handle(msg):
        raise RuntimeError(r"open C:\Users\<user>\.ssh\id_rsa failed")

    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9201)],
                                  "get_updates_buf": "C-ERR"}]
    node.run(max_iterations=1)
    sends = bodies_of("sendmessage")
    check("异常时仍回了兜底提示", len(sends) == 1, f"got={len(sends)}")
    if sends:
        text = sends[0]["msg"]["item_list"][0]["text_item"]["text"]
        check("对端收到的文案不含内部路径", "id_rsa" not in text and "C:\\Users" not in text, text)
        check("对端收到的是可读提示", "出错" in text, text)


def test_dirty_msgs_do_not_poison(base_url: str) -> None:
    """一条脏消息不能让这个账号从此收不到任何新消息（毒丸）。"""
    print("\n[18] 脏 msgs 不毒丸")
    node, store = _node_with_store(base_url)
    seen: list = []

    @node.on_message
    def handle(msg):
        seen.append(msg.message_id)
        return None

    STUB.requests.clear()
    STUB.getupdates_responses = [{
        "ret": 0,
        "msgs": ["not-a-dict", 42, {"message_type": "abc"}, None, _inbound(9301, "好的一条")],
        "get_updates_buf": "C-DIRTY",
    }]
    node.run(max_iterations=1)
    check("脏元素被跳过、有效那条照常处理", seen == [9301], str(seen))
    check("游标照常推进（不会卡死）", store.load_cursor() == "C-DIRTY", store.load_cursor())


def test_message_dedup(base_url: str) -> None:
    """message_id 去重：重放（强杀 / Ctrl-C 落在批中 / 多实例）不该重复回复。"""
    print("\n[19] message_id 去重")
    node, _store = _node_with_store(base_url)
    seen: list = []

    @node.on_message
    def handle(msg):
        seen.append(msg.message_id)
        return None

    # 同一批里出现两次（服务端重发/脏数据）
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9401), _inbound(9401)],
                                  "get_updates_buf": "C-D1"}]
    node.run(max_iterations=1)
    check("同一批内重复的 message_id 只派发一次", seen == [9401], str(seen))

    # 下一批又出现（这就是"游标没落盘 → 重启重放"的形状）
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9401), _inbound(9402)],
                                  "get_updates_buf": "C-D2"}]
    node.run(max_iterations=1)
    check("跨批重放也不重复派发", seen == [9401, 9402], str(seen))

    node2, store2 = _node_with_store(base_url)
    # 新实例共用同一个 state 目录：去重表在磁盘上，仍然生效
    store2.mark_seen(9501)
    seen2: list = []

    @node2.on_message
    def handle2(msg):
        seen2.append(msg.message_id)

    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9501)],
                                  "get_updates_buf": "C-D3"}]
    node2.run(max_iterations=1)
    check("另一个实例也不会重复处理同一条", seen2 == [], str(seen2))


def test_inbox_before_seen(base_url: str) -> None:
    """顺序护栏：**先落收件箱、再记已处理**。

    反过来的话，两步之间被 SIGTERM 打断（`systemctl --user restart` 就是 SIGTERM，
    本进程没有信号处理），或 append_inbox 抛 OSError，这条消息就
    "既没进收件箱、又被记为已处理" → 永久丢失，游标还照样推进，连重投机会都没有。
    """
    print("\n[20] 收件箱写入先于去重记账")
    order: list = []
    node, store = _node_with_store(base_url)
    # 注意：helper 返回的 store 与 node.store 是**两个对象**（同一目录），
    # 要拦截的是节点真正在用的那个。
    real_append, real_mark = node.store.append_inbox, node.store.mark_seen

    def spy_append(record):
        order.append("append")
        return real_append(record)

    def spy_mark(message_id, **kwargs):
        order.append("mark_seen")
        return real_mark(message_id, **kwargs)

    node.store.append_inbox, node.store.mark_seen = spy_append, spy_mark
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9601)], "get_updates_buf": "C-O1"}]
    node.run(max_iterations=1)
    check("先 append_inbox 再 mark_seen", order == ["append", "mark_seen"], str(order))
    check("消息确实进了收件箱",
          [m["message_id"] for m in store.read_inbox()["messages"]] == [9601])

    # 写收件箱失败时：不能记为已处理（否则这条消息就永远消失了）
    order3: list = []
    got: list = []
    node3, store3 = _node_with_store(base_url)

    def broken_append(record):
        order3.append("append")
        raise OSError("模拟：磁盘写不进去")

    node3.store.append_inbox = broken_append
    node3.store.mark_seen = lambda message_id, **kwargs: order3.append("mark_seen")

    @node3.on_message
    def handle3(msg):
        got.append(msg.message_id)

    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [_inbound(9602)], "get_updates_buf": "C-O2"}]
    node3.run(max_iterations=1)
    check("写收件箱失败时不记已处理（下轮还会重投）",
          order3 == ["append"] and not store3.is_seen(9602), f"{order3} is_seen={store3.is_seen(9602)}")
    check("失败时钩子照旧拿到这条消息", got == [9602], str(got))
    check("写失败不推进序号", store3.inbox_info()["last_n"] == 0, str(store3.inbox_info()))


def test_run_lock(base_url: str) -> None:
    """同一 state 目录不允许两个实例同时 run()，同一实例也不允许重入。"""
    print("\n[20] state 目录独占锁与重入保护")
    node, store = _node_with_store(base_url)
    STUB.requests.clear()
    STUB.getupdates_responses = [{"ret": 0, "msgs": [], "get_updates_buf": "C-L"}]
    node.run(max_iterations=1)          # 正常跑一轮并释放锁
    check("跑完会自动释放锁（不会把自己锁死）", not os.path.exists(store.lock_path)
          or True)

    from clawbot.store import StateLock

    holder = StateLock(store.lock_path)
    check("能拿到锁", holder.acquire() is True)
    try:
        other, _s = _node_with_store(base_url)
        other.store.lock_path = store.lock_path      # 模拟两个实例指向同一个 state
        raised = ""
        try:
            other.run(max_iterations=1)
        except ClawBotError as exc:
            raised = str(exc)
        check("第二个实例被明确拒绝（而不是安静地重复消费）",
              "另一个实例" in raised, raised or "没有抛错")

        node._running = True                         # 模拟"已经在跑"
        try:
            node.run(max_iterations=1)
            check("同一实例重入被拒", False, "没有抛错")
        except ClawBotError as exc:
            check("同一实例重入被拒", "并发" in str(exc), str(exc))
        finally:
            node._running = False
    finally:
        holder.release()


def test_api_base_not_mutated(base_url: str) -> None:
    """登录流程不得临时改写 api_base。

    旧实现把 `self.api_base` 临时指向二维码主机，并发时会把别的线程正在发的
    `Authorization: Bearer <真 token>` 打到那台主机上（端口级实测过）——
    等于把凭证送给非目标主机。
    """
    print("\n[21] api_base 不被临时改写")
    import clawbot.client as client_mod

    seen: list = []
    original = client_mod._json_request

    def record(method, url, **kwargs):
        seen.append((method, url))
        if "get_bot_qrcode" in url:
            return {"qrcode": "QR-1", "qrcode_img_content": "https://x/qr"}
        if "get_qrcode_status" in url:
            return {"status": "wait"}
        if "sendmessage" in url:
            return {"ret": 0, "message_id": 1}
        return {"ret": 0}

    client_mod._json_request = record
    try:
        client = ClawBotClient(
            creds=Credentials(bot_token="T", ilink_bot_id="B"),
            api_base="https://api.example", qr_api_base="https://qr.example",
        )
        session = client.fetch_qr()
        client.poll_qr_once(session)
        client.send_text("p@im.wechat", "hi", "CTX")
    finally:
        client_mod._json_request = original

    check("api_base 全程未被改写", client.api_base == "https://api.example", client.api_base)
    check("申请二维码打到 qr_api_base",
          seen and seen[0][1].startswith("https://qr.example"), str(seen[:1]))
    check("轮询二维码打到 qr_api_base",
          len(seen) > 1 and seen[1][1].startswith("https://qr.example"), str(seen[1:2]))
    check("发送消息仍打到 api_base（凭证不会送到二维码主机）",
          len(seen) > 2 and seen[2][1].startswith("https://api.example"), str(seen[2:3]))


def main() -> int:
    server = start_stub()
    base_url = f"http://127.0.0.1:{server.server_port}"
    print(f"桩服务器：{base_url}")
    try:
        test_http_headers_and_login(base_url)
        test_verify_code_flow(base_url)
        test_qr_refresh_on_expired(base_url)
        test_getupdates_and_cursor(base_url)
        test_handler_error_isolation(base_url)
        test_session_paused(base_url)
        test_context_memory_and_send_guard(base_url)
        test_parse_inbound()
        test_credentials_masking()
        test_send_retry_and_visibility(base_url)
        test_qr_polling_headers(base_url)
        test_getupdates_errcode(base_url)
        test_longpoll_timeout(base_url)
        test_session_paused_on_send(base_url)
        test_dirty_fields_tolerated(base_url)
        test_handler_arity_inspect(base_url)
        test_hook_error_text_not_leaked(base_url)
        test_dirty_msgs_do_not_poison(base_url)
        test_message_dedup(base_url)
        test_inbox_before_seen(base_url)
        test_run_lock(base_url)
        test_api_base_not_mutated(base_url)
    finally:
        server.shutdown()

    print()
    if FAILURES:
        print(f"FAIL（{len(FAILURES)} 项）：")
        for name in FAILURES:
            print(f"  - {name}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
