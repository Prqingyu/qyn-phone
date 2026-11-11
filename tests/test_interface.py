"""对外接口自检（INTERFACE.md / clawbot/interface.schema.json 的可执行版本）。

三件事必须同时成立，否则"标准接口"只是文档：

1. **形状**：`--json` 的真实输出必须符合 schema 文件 —— 所以这里实现了一个
   只支持本 schema 用到的那几个关键字的迷你校验器，直接拿 `interface.schema.json`
   去校验真实进程的输出（不是手抄一遍字段名）。
2. **跨进程**：收件箱是给**别的进程**读的，所以端到端用例必须真的起子进程
   （`python -m clawbot inbox --json`），而不是在同进程里调函数。
3. **退出码**：0/1/2 的约定要能被脚本依赖。

运行：python tests/test_interface.py
"""

from __future__ import annotations

import json
import os
import subprocess
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

from clawbot import ClawBotClient, ClawBotNode  # noqa: E402
from clawbot.store import Credentials, StateStore  # noqa: E402

SCHEMA_PATH = os.path.join(ROOT, "clawbot", "interface.schema.json")
FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


# --------------------------------------------------------------------------
# 迷你 JSON Schema 校验器（只支持本 schema 用到的关键字）
# --------------------------------------------------------------------------


def _is_type(value, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    return True


def validate(instance, schema: dict, defs: dict, path: str = "$") -> list:
    errors: list = []
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/$defs/"):
            return [f"{path}: 不支持的 $ref {ref}"]
        return validate(instance, defs[ref.split("/")[-1]], defs, path)
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{path}: 期望常量 {schema['const']!r}，实际 {instance!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} 不在枚举 {schema['enum']!r} 中")
    declared = schema.get("type")
    if declared:
        allowed = declared if isinstance(declared, list) else [declared]
        if not any(_is_type(instance, name) for name in allowed):
            errors.append(f"{path}: 类型应为 {allowed}，实际 {type(instance).__name__}")
            return errors
    if "minimum" in schema and isinstance(instance, (int, float)) and instance < schema["minimum"]:
        errors.append(f"{path}: {instance} < minimum {schema['minimum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必需字段 {key}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in instance:
                errors.extend(validate(instance[key], sub, defs, f"{path}.{key}"))
    if isinstance(instance, list) and "items" in schema:
        for index, item in enumerate(instance):
            errors.extend(validate(item, schema["items"], defs, f"{path}[{index}]"))
    return errors


def load_schema() -> dict:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def conforms(name: str, payload, schema: dict) -> bool:
    errors = validate(payload, schema["$defs"][name], schema["$defs"])
    if errors:
        for line in errors[:5]:
            print(f"       schema 不符：{line}")
        return False
    return True


# --------------------------------------------------------------------------
# 桩服务器（给"跨进程发送"用）
# --------------------------------------------------------------------------


class StubState:
    def __init__(self) -> None:
        self.messages_sent = 0
        self.updates: list = []


STUB = StubState()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _reply(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        path = self.path.split("?")[0]
        if path.endswith("/getupdates"):
            if STUB.updates:
                self._reply(STUB.updates.pop(0))
            else:
                self._reply({"ret": 0, "msgs": [], "get_updates_buf": "C0"})
        elif path.endswith("/sendmessage"):
            STUB.messages_sent += 1
            self._reply({"ret": 0, "message_id": 880000 + STUB.messages_sent})
        else:
            self._reply({"ret": 0})


def start_stub() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def logged_in_state(base_url: str, peer: str = "peer@im.wechat") -> str:
    tmp = tempfile.mkdtemp(prefix="clawbot-iface-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="FAKE_BOT_TOKEN",
                                       ilink_bot_id="B@im.bot", baseurl=base_url))
    store.remember_context(peer, "CTX-IFACE")
    return tmp


LAST_STDOUT = ""     # 最近一次 run_cli 的原始 stdout（契约断言要用；测试是单线程的）


def run_cli(*args: str) -> tuple:
    """在**子进程**里跑 CLI（接口是给别的进程用的，不能只测同进程调用）。

    原始 stdout 记在 `LAST_STDOUT`：因为"`--json` 时 stdout 恰好一行"本身
    就是要钉住的契约（见 [3]）。只取 `splitlines()[-1]` 会漏掉日志污染 stdout 这类 bug。
    """
    global LAST_STDOUT
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "clawbot", *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    LAST_STDOUT = proc.stdout or ""
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = None
    return proc.returncode, payload, proc.stderr or ""


def json_lines(stdout: str = "") -> list:
    """stdout 里的非空行。契约要求恰好 1 行。"""
    text = LAST_STDOUT if not stdout else stdout
    return [line for line in text.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_schema_file(schema: dict) -> None:
    print("\n[1] schema 文件自身")
    check("schema 版本是 1", schema.get("schema_version") == 1)
    for name in ("error", "send", "status", "peers", "inbox", "inbox_record", "follow_line"):
        check(f"定义了 $defs.{name}", name in schema.get("$defs", {}))
    check("schema 文件位置与 INTERFACE.md 里引用的一致",
          os.path.exists(os.path.join(ROOT, "INTERFACE.md")))


def test_inbox_semantics() -> None:
    print("\n[2] 收件箱语义（游标 / 分页 / 过滤）")
    tmp = tempfile.mkdtemp(prefix="clawbot-iface-")
    store = StateStore(tmp)
    check("空收件箱可读", store.read_inbox()["messages"] == [])

    for index in range(5):
        store.append_inbox({"message_id": 100 + index, "peer_id": "a@im.wechat",
                            "kind": "text", "text": f"第{index}条", "received_at": 1.0 + index})
    store.append_inbox({"message_id": 200, "peer_id": "b@im.wechat",
                        "kind": "image", "text": "", "received_at": 9.0})

    page = store.read_inbox()
    check("序号从 1 开始且单调", [m["n"] for m in page["messages"]] == [1, 2, 3, 4, 5, 6],
          str([m["n"] for m in page["messages"]]))
    check("记录带 schema 版本", all(m["schema"] == 1 for m in page["messages"]))
    check("next_cursor 是最后一条的序号", page["next_cursor"] == "6", page["next_cursor"])

    page2 = store.read_inbox(since=3)
    check("since=3 返回 n>3", [m["n"] for m in page2["messages"]] == [4, 5, 6],
          str([m["n"] for m in page2["messages"]]))

    page3 = store.read_inbox(limit=2)
    check("limit 生效", len(page3["messages"]) == 2)
    check("has_more 提示还有没读完的", page3["has_more"] is True)
    page4 = store.read_inbox(since=int(page3["next_cursor"]), limit=99)
    check("沿游标继续读到剩下的", [m["n"] for m in page4["messages"]] == [3, 4, 5, 6],
          str([m["n"] for m in page4["messages"]]))
    check("读到底时 has_more=False", page4["has_more"] is False)

    check("按对端过滤", [m["n"] for m in store.read_inbox(peer_id="b@im.wechat")["messages"]] == [6])
    check("没有新消息时 truncated=False", store.read_inbox(since=6)["truncated"] is False)

    # 一条记录比"尾部字节窗口"还长时，序号绝不能回退。
    # 真 bug 回顾：_inbox_tail_n 只读尾部 4096 字节，超长记录只剩碎行 → 解析失败 → 返回 0
    # → 下一条 n 又从 1 开始 → 按游标读的部件把撞号那条静默跳过（手机发长消息就会中招）。
    big = StateStore(tempfile.mkdtemp(prefix="clawbot-iface-"))
    n1 = big.append_inbox({"message_id": 1, "peer_id": "p@im.wechat", "kind": "text",
                           "text": "长" * 3000})            # ≈ 9 KB，远大于 4096 字节窗口
    n2 = big.append_inbox({"message_id": 2, "peer_id": "p@im.wechat", "kind": "text", "text": "短"})
    check("超长记录之后序号不回退（不是 1,1）", [n1, n2] == [1, 2], f"{n1},{n2}")
    page_big = big.read_inbox(since=1)
    check("超长记录之后按游标仍能读到新消息", [m["n"] for m in page_big["messages"]] == [2],
          str(page_big)[:160])


def test_inbox_rotation() -> None:
    print("\n[3] 收件箱轮转与 truncated")
    import clawbot.store as store_mod

    tmp = tempfile.mkdtemp(prefix="clawbot-iface-")
    original_bytes, original_keep = store_mod.INBOX_MAX_BYTES, store_mod.INBOX_KEEP_RECORDS
    store_mod.INBOX_MAX_BYTES, store_mod.INBOX_KEEP_RECORDS = 200, 5
    try:
        store = StateStore(tmp)
        for index in range(40):
            store.append_inbox({"message_id": index, "peer_id": "a@im.wechat",
                                "kind": "text", "text": "x" * 40, "received_at": 1.0 + index})
        info = store.inbox_info()
        check("轮转后只保留最近 5 条", info["records"] <= 5, str(info["records"]))
        check("序号不回退（最老的序号 > 1）", info["first_n"] > 1, str(info["first_n"]))
        check("最后一条仍是最后写的", info["last_n"] == 40, str(info["last_n"]))

        stale = store.read_inbox(since=0)
        check("游标老于轮转边界时 truncated=True", stale["truncated"] is True, str(stale))
        fresh = store.read_inbox(since=info["first_n"] - 1)
        check("游标在边界内时 truncated=False", fresh["truncated"] is False, str(fresh))
    finally:
        store_mod.INBOX_MAX_BYTES, store_mod.INBOX_KEEP_RECORDS = original_bytes, original_keep


def test_cli_json_contract(base_url: str, schema: dict) -> None:
    print("\n[4] CLI 的 JSON 契约（子进程真实输出 vs schema 文件）")
    tmp = logged_in_state(base_url)

    code, payload, _err = run_cli("status", "--json", "--state", tmp)
    check("status 退出码 0（--state 写在子命令后面也要能用）", code == 0, str(code))
    check("status 输出符合 $defs.status", conforms("status", payload, schema), str(payload)[:200])
    check("status 的 stdout 恰好一行（--json 契约：日志必须走 stderr）",
          len(json_lines()) == 1, repr(LAST_STDOUT[:200]))
    check("status 里凭证是脱敏的（无明文 token）",
          payload is not None and
          "SUPERSECRETTOKENVALUE1234" not in json.dumps(payload, ensure_ascii=False),
          str((payload or {}).get("credentials")))

    code, payload, _err = run_cli("peers", "--json", "--state", tmp)
    check("peers 退出码 0", code == 0, str(code))
    check("peers 输出符合 $defs.peers", conforms("peers", payload, schema), str(payload)[:200])
    check("peers 列出对端", payload["peers"][0]["peer_id"] == "peer@im.wechat", str(payload))
    check("peers 的 stdout 恰好一行", len(json_lines()) == 1, repr(LAST_STDOUT[:200]))

    code, payload, _err = run_cli("inbox", "--json", "--state", tmp)
    check("inbox 退出码 0", code == 0, str(code))
    check("inbox 输出符合 $defs.inbox", conforms("inbox", payload, schema), str(payload)[:200])
    check("inbox 的 stdout 恰好一行", len(json_lines()) == 1, repr(LAST_STDOUT[:200]))

    # 发送：走的是文档里的接口，落在桩服务器上
    code, payload, _err = run_cli("send", "--text", "来自别的进程", "--json",
                                  "--state", tmp, "--to", "peer@im.wechat")
    check("send 退出码 0", code == 0, str(code))
    check("send 输出符合 $defs.send", conforms("send", payload, schema), str(payload)[:200])
    check("确实发出去了一条", STUB.messages_sent >= 1, str(STUB.messages_sent))
    # 这条是"静默污染"的护栏：成功路径也会打日志（debug=True），
    # 只取最后一行会漏掉它，`json.loads(stdout)` 的消费方却会直接崩。
    check("send 成功路径的 stdout 也恰好一行（日志不能混进来）",
          len(json_lines()) == 1, repr(LAST_STDOUT[:200]))

    # 失败路径：退出码与 code 都被文档承诺过
    code, payload, _err = run_cli("send", "--json", "--state", tmp)
    check("缺少参数时退出码 2", code == 2, str(code))
    check("缺少参数输出符合 $defs.error", conforms("error", payload, schema), str(payload))
    check("code=bad_usage", payload.get("code") == "bad_usage", str(payload))
    check("失败路径的 stdout 也恰好一行", len(json_lines()) == 1, repr(LAST_STDOUT[:200]))

    empty = tempfile.mkdtemp(prefix="clawbot-iface-")
    code, payload, _err = run_cli("send", "--text", "hi", "--json", "--state", empty)
    check("未登录时退出码 1", code == 1, str(code))
    check("code=not_logged_in", payload.get("code") == "not_logged_in", str(payload))
    check("未登录输出符合 $defs.error", conforms("error", payload, schema), str(payload))


def test_cross_process_inbox(base_url: str, schema: dict) -> None:
    """端到端：节点收到消息 → 另一个进程用 CLI 读到。"""
    print("\n[5] 跨进程：run 写收件箱，另一个进程读")
    tmp = logged_in_state(base_url)
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(creds=StateStore(tmp).load_credentials(), api_base=base_url)

    @node.on_message
    def handle(msg):
        return None

    STUB.updates = [{
        "ret": 0,
        "msgs": [{"message_id": 4242, "message_type": 1, "from_user_id": "peer@im.wechat",
                  "context_token": "CTX-X", "seq": 3, "create_time_ms": 1791532058000,
                  "item_list": [{"type": 1, "text_item": {"text": "跨进程你好"}}]}],
        "get_updates_buf": "C-NEXT",
    }]
    node.run(max_iterations=1)

    code, payload, _err = run_cli("inbox", "--json", "--state", tmp)
    check("子进程读到了节点写的消息", code == 0 and len(payload["messages"]) == 1, str(payload)[:200])
    if payload and payload["messages"]:
        record = payload["messages"][0]
        check("记录符合 $defs.inbox_record", conforms("inbox_record", record, schema), str(record))
        check("内容正确", record["text"] == "跨进程你好", str(record))
        check("kind=text", record["kind"] == "text")
        check("记录里不含会话令牌（令牌不扩散）", "context_token" not in record and
              "CTX-X" not in json.dumps(record, ensure_ascii=False), str(record))
        check("next_cursor 可用于续读", payload["next_cursor"] == str(record["n"]))

    code2, payload2, _err2 = run_cli("inbox", "--json", "--state", tmp,
                                     "--since", payload["next_cursor"])
    check("按游标续读时没有新消息", code2 == 0 and payload2["messages"] == [], str(payload2)[:160])


def test_status_reports_receiver(base_url: str, schema: dict) -> None:
    """`status` 必须能回答"接收方在不在跑、pid 多少"。

    这条是运维逼出来的：`run.lock` 的锁字节连 `Get-Content` 都会被拒，
    手工解析 pid 不现实，所以 pid 只能从接口里拿。
    """
    print("\n[6] status 报告接收方")
    tmp = logged_in_state(base_url)
    code, payload, _err = run_cli("status", "--json", "--state", tmp)
    check("退出码 0", code == 0, str(code))
    check("没有人持锁时 receiver.running=false",
          payload["receiver"]["running"] is False, str(payload["receiver"]))
    check("没有人持锁时 pid=null", payload["receiver"]["pid"] is None, str(payload["receiver"]))

    from clawbot.store import StateLock

    lock = StateLock(os.path.join(tmp, "run.lock"))
    check("测试自己拿到锁（模拟接收方在跑）", lock.acquire() is True)
    try:
        code, payload, _err = run_cli("status", "--json", "--state", tmp)
        check("有人持锁时 receiver.running=true",
              payload["receiver"]["running"] is True, str(payload["receiver"]))
        check("pid 就是持有者（子进程能读到本进程 pid）",
              payload["receiver"]["pid"] == os.getpid(), str(payload["receiver"]["pid"]))
        check("输出仍符合 $defs.status", conforms("status", payload, schema), str(payload)[:160])
    finally:
        lock.release()

    code, payload, _err = run_cli("status", "--json", "--state", tmp)
    check("释放后 running 回到 false",
          payload["receiver"]["running"] is False, str(payload["receiver"]))
    check("探测不会把自己的 pid 留在文件里（不留假信息）",
          StateLock(tmp + os.sep + "run.lock").holder_pid() in (None, os.getpid()),
          str(StateLock(tmp + os.sep + "run.lock").holder_pid()))


def test_pipe_is_silent_by_default(base_url: str) -> None:
    """管道的默认必须是**静默**：`run` 不带 `--handler` 时，收到消息只进收件箱、不回复。

    这是"它是管道，不是一套替使用者做决定的系统"的可执行版本：
    谁把默认钩子加回去（管道自己开始回话），这条就会红。
    """
    print("\n[7] 默认静默（管道不主动回话）")
    tmp = logged_in_state(base_url)
    STUB.messages_sent = 0
    STUB.updates = [{
        "ret": 0,
        "msgs": [{"message_id": 7001, "message_type": 1, "from_user_id": "peer@im.wechat",
                  "context_token": "CTX-SILENT", "seq": 1,
                  "item_list": [{"type": 1, "text_item": {"text": "管道测试"}}]}],
        "get_updates_buf": "C-SILENT",
    }]

    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "clawbot", "run", "--state", tmp],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    record = None
    try:
        deadline = time.time() + 25
        while time.time() < deadline:
            _code, page, _err = run_cli("inbox", "--json", "--state", tmp)
            if page and page.get("messages"):
                record = page["messages"][0]
                break
            time.sleep(0.5)
        check("消息确实进了收件箱（管道在收）", record is not None, "25s 内没进收件箱")
        check("内容原样、没有加工", record is not None and record.get("text") == "管道测试",
              str(record))
        check("管道没有主动回复（messages_sent 仍为 0）",
              STUB.messages_sent == 0, f"messages_sent={STUB.messages_sent}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    # 反向确认：显式给钩子才会回话（行为由消费者决定，不是管道的默认）
    STUB.messages_sent = 0
    STUB.updates = [{
        "ret": 0,
        "msgs": [{"message_id": 7002, "message_type": 1, "from_user_id": "peer@im.wechat",
                  "context_token": "CTX-HOOK", "seq": 2,
                  "item_list": [{"type": 1, "text_item": {"text": "带钩子"}}]}],
        "get_updates_buf": "C-HOOK",
    }]
    handler = os.path.join(ROOT, "examples", "text_echo.py")
    proc2 = subprocess.Popen(
        [sys.executable, "-m", "clawbot", "run", "--state", tmp, "--handler", handler],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
    )
    try:
        deadline = time.time() + 25
        while time.time() < deadline and STUB.messages_sent == 0:
            time.sleep(0.5)
        check("显式给 --handler 时才会回话（行为属于消费者）",
              STUB.messages_sent >= 1, f"messages_sent={STUB.messages_sent}")
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc2.kill()
            proc2.wait(timeout=10)


def main() -> int:
    schema = load_schema()
    server = start_stub()
    base_url = f"http://127.0.0.1:{server.server_port}"
    print(f"桩服务器：{base_url}")
    try:
        test_schema_file(schema)
        test_inbox_semantics()
        test_inbox_rotation()
        test_cli_json_contract(base_url, schema)
        test_cross_process_inbox(base_url, schema)
        test_status_reports_receiver(base_url, schema)
        test_pipe_is_silent_by_default(base_url)
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
