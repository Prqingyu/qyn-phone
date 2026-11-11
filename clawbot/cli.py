"""ClawBot 独立节点 —— 命令行入口（也就是对外的**标准接口**）。

    clawbot status [--json]            本地状态（凭证/游标/对端/收件箱概况）
    clawbot peers  [--json]            可发送的目标（对端）列表
    clawbot inbox  [--json]            读手机发来的消息（按游标，跨进程可用）
    clawbot send   [路径] [--text 文本] [--to 对端] [--image] [--caption 说明] [--json]
    clawbot login                      扫码登录（首次运行）
    clawbot run                        启动接收方（默认不自动回复，只写收件箱）
    clawbot run --handler <文件>       额外加载一个进程内钩子（消费者代码）
    clawbot reset                      清除凭证与派生状态

带 `--json` 时 **stdout 只有一行 JSON**，日志一律走 stderr，退出码 0/1/2 有固定含义。
完整字段表与兼容承诺见 INTERFACE.md。

用 python main.py / python -m clawbot / 装包后的 clawbot 三种写法完全等价。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from clawbot import ClawBotError, ClawBotNode  # noqa: E402
from clawbot.client import PROJECT_ROOT, default_state_dir  # noqa: E402
from clawbot.store import INBOX_SCHEMA  # noqa: E402

# 项目根：包内不能再拿 __file__ 当项目根（那会指向 clawbot/ 目录）。
# 唯一来源是 clawbot.client.PROJECT_ROOT（<项目根>/clawbot/client.py 往上两级）。
HERE = PROJECT_ROOT
DEFAULT_HANDLER = os.path.join(HERE, "examples", "text_echo.py")

# 接口版本：字段只增不改；破坏性变更就抬版本号，并对旧版本做兼容。
INTERFACE_VERSION = 1

EXIT_OK = 0        # 成功
EXIT_FAILED = 1    # 业务失败（未登录 / 发送失败 / 目标不存在）
EXIT_USAGE = 2     # 用法错误（参数不对）


def _emit_json(payload: dict) -> None:
    """一行 JSON 到 stdout —— 机器消费的唯一格式（日志永远走 stderr）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _send_envelope(result, peer: str) -> dict:
    """把 SendResult 变成对外契约里的固定形状。"""
    return {
        "schema": INTERFACE_VERSION,
        "ok": bool(result.ok),
        "peer_id": peer,
        "message_id": result.message_id,
        "stage": result.stage,
        "attempts": result.attempts,
        "error": None if result.ok else (result.errmsg or "发送失败"),
    }


def _fail(args: argparse.Namespace, message: str, *, code: str = "failed",
          exit_code: int = EXIT_FAILED) -> int:
    """统一的失败出口：JSON 模式下一个对象，人类模式下只打一行。"""
    if getattr(args, "json", False):
        _emit_json({"schema": INTERFACE_VERSION, "ok": False, "code": code, "error": message})
    else:
        print(f"错误：{message}")
    return exit_code


def _load_handler(node: ClawBotNode, path: str) -> bool:
    """加载一个"自带 node 的示例模块"，把它的钩子挪到真正要跑的节点上。成功返回 True。

    钩子是**闭包**：示例里写的是 `NODE.send_text(...)`，捕获的是示例自己的 NODE 对象。
    只把 `_handler` 拷过来、其余的不管，就会出现"轮询用一个 state 目录、发送用另一个"：
    实测 `--state` 指向别处时，钩子内的 `NODE.store.dir` 仍然是 `<PHONE>/state`，
    于是发送取不到 context_token、或把回复挂到另一个会话 —— 症状是随机丢回复，极难定位。
    所以这里把示例 NODE 的 store/client 就地换成运行节点的，闭包里的引用自然对齐。

    失败**必须让调用方知道**：显式给了 `--handler` 却加载不上，节点会"看着在跑、
    一声不响"（用户以为有人回话，实际纯静默）。返回 False，由 cmd_run 非零退出。
    """
    if not os.path.exists(path):
        print(f"未找到钩子文件：{path}", file=sys.stderr)
        return False
    spec = importlib.util.spec_from_file_location("qyn_clawbot_handler", path)
    if spec is None or spec.loader is None:
        print(f"无法加载钩子文件：{path}", file=sys.stderr)
        return False
    module = importlib.util.module_from_spec(spec)
    sys.modules["qyn_clawbot_handler"] = module
    spec.loader.exec_module(module)
    example_node = getattr(module, "NODE", None)
    if example_node is None:
        print(f"{path} 里没有 NODE 变量，钩子未注册", file=sys.stderr)
        return False
    example_store = getattr(example_node, "store", None)
    if example_store is not None and example_store.dir != node.store.dir:
        print(f"提示：{os.path.relpath(path, HERE)} 里的 NODE 原本指向 {example_store.dir}，"
              f"已改指运行节点使用的 {node.store.dir}")
    example_node.store = node.store
    example_node.client = node.client
    example_node.debug = node.debug
    # 把示例里注册的钩子挪到真正要跑的节点上
    node._handler = example_node._handler  # noqa: SLF001
    print(f"已从 {os.path.relpath(path, HERE)} 加载消息钩子")
    return True


def cmd_login(args: argparse.Namespace) -> int:
    node = ClawBotNode(state_dir=args.state, debug=True)
    try:
        creds = node.login(open_qr=not args.no_open, timeout_s=args.timeout)
    except ClawBotError as exc:
        print(f"登录失败：{exc}")
        return 1
    print("登录成功，凭证已保存：")
    for key, value in creds.masked().items():
        print(f"  {key} = {value}")
    print("\n接下来运行：clawbot run（默认不自动回复，收到的消息进收件箱）")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """启动管道泵：长轮询 → 去重 → 写收件箱（可选：再交给一个**进程内**钩子）。

    **默认不加载任何钩子**。管道不替消费者回话 —— 默认静默是这里最重要的一条默认值：
    一旦默认回显，它就不再是管道，而是一套替使用者做决定的系统。
    要回显/自定义回复，显式 `--handler <文件>`。
    """
    # ★ 入站媒体落到哪（用户 2026-10-16：微信发来的图片传不过来）。
    #   默认 <state>/media —— 默认就该能用；`--no-media` 关掉，`--media-dir` 换地方。
    media_dir = ""
    if not getattr(args, "no_media", False):
        # ★ 用 default_state_dir()，**没有** DEFAULT_STATE 这个常量 ——
        #   写错的那版让 `run` 一启动就 NameError（test_interface 的"管道在收"用例抓到）。
        media_dir = (getattr(args, "media_dir", "") or
                     os.path.join(args.state or default_state_dir(), "media"))
    node = ClawBotNode(state_dir=args.state, debug=True, media_dir=media_dir)
    if not node.store.load_credentials().is_valid:
        return _fail(args, "尚未登录：先运行 clawbot login", code="not_logged_in")
    if args.handler:
        if not _load_handler(node, args.handler):
            return _fail(args, f"钩子加载失败：{args.handler}"
                               "（--handler 要指向存在且定义了 NODE 的 Python 文件；"
                               "相对路径按当前工作目录解析，systemd 下请用绝对路径）",
                         code="bad_usage")
    else:
        print("未加载钩子：收到的消息只进收件箱（clawbot inbox --json），管道不会自动回话")
    print(f"接收方已启动（Ctrl-C 退出）· 入站媒体落盘：{media_dir or '关'}")
    try:
        node.run()
    except ClawBotError as exc:
        print(f"运行失败：{exc}")
        return 1
    except KeyboardInterrupt:
        print("\n已退出")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """发东西给对端：文本（`--text`）或本地文件/图片（位置参数）。

    这是"其他部件让手机收到东西"的唯一入口。约定：
      · `--text -` 从 stdin 读（多行文本别让 shell 去转义）
      · `--json` 时返回固定信封；失败也返回退出码 1 并给出 stage（read/upload/send）
    """
    node = ClawBotNode(state_dir=args.state, debug=True)
    if not node.store.load_credentials().is_valid:
        return _fail(args, "尚未登录：先运行 clawbot login", code="not_logged_in")

    peer = (args.to or "").strip()
    if not peer:
        peers = node.store.known_peers()
        if len(peers) == 1:
            peer = next(iter(peers))
            if not args.json:
                print(f"未指定 --to，使用 state 里唯一的对端：{peer}")
        else:
            hint = "、".join(sorted(peers)) if peers else "（还没有任何对端）"
            return _fail(args, f"未指定 --to，且 state 里没有唯一的对端：{hint}", code="no_peer")

    text = args.text
    if text == "-":
        text = sys.stdin.read()
    if text is None and not args.path:
        return _fail(args, "要么给一个文件路径，要么用 --text 给文本",
                     code="bad_usage", exit_code=EXIT_USAGE)

    if text is not None:
        result = node.send_text(peer, text)
    elif not os.path.exists(args.path):
        return _fail(args, f"文件不存在：{os.path.abspath(args.path)}", code="file_not_found")
    elif args.image:
        result = node.send_image(peer, args.path, caption=args.caption)
    else:
        result = node.send_file(peer, args.path, caption=args.caption)

    # ★ 记一条发出流水（收件箱的对应物）：排查"用户说没收到"时不用再去翻会话存档
    try:
        node.store.append_sent({
            "ts": time.time(),
            "ok": bool(result.ok),
            "peer_id": peer,
            "stage": getattr(result, "stage", None),
            "message_id": getattr(result, "message_id", None),
            "attempts": getattr(result, "attempts", None),
            "error": getattr(result, "error", None),
            "chars": len(text or ""),
            "head": (text or "")[:40],
        })
    except Exception as exc:                            # noqa: BLE001
        # 记流水绝不能影响发送结果，但也**不能静默**：第一版就是这里静默吞掉，
        # 导致 sent.jsonl 一直空着却没人知道。
        print(f"（发出流水未记录：{type(exc).__name__}: {exc}）", file=sys.stderr)

    if args.json:
        _emit_json(_send_envelope(result, peer))
    else:
        print(f"发送结果：{result}")
        if not result.ok:
            print(f"失败阶段：{result.stage}（read=读文件 / upload=CDN 上传 / send=发消息）")
    return EXIT_OK if result.ok else EXIT_FAILED


def cmd_peers(args: argparse.Namespace) -> int:
    """列出可发送的目标。没有它，上层就得自己去解析 contexts.json。"""
    node = ClawBotNode(state_dir=args.state)
    peers = node.store.peers_detail()
    if args.json:
        _emit_json({"schema": INTERFACE_VERSION, "ok": True, "peers": peers})
        return EXIT_OK
    if not peers:
        print("还没有记住任何对端：先让对端给 ClawBot 发一条消息。")
        return EXIT_OK
    print("可发送的对端：")
    for item in peers:
        print(f"  {item['peer_id']}  会话令牌={'有' if item['has_context_token'] else '无'}")
    return EXIT_OK


def cmd_inbox(args: argparse.Namespace) -> int:
    """读手机发来的消息。跨进程可用：由 `run` 追加到 state/inbox.jsonl。"""
    node = ClawBotNode(state_dir=args.state)
    since = None
    if args.since not in ("", None):
        try:
            since = int(args.since)
        except ValueError:
            return _fail(args, f"--since 必须是整数游标（上次返回的 next_cursor）：{args.since!r}",
                         code="bad_usage", exit_code=EXIT_USAGE)
    if args.follow:
        return _follow_inbox(node, since, args)

    page = node.store.read_inbox(since=since, limit=args.limit, peer_id=args.peer or "")
    if args.json:
        envelope = {"schema": INTERFACE_VERSION, "ok": True}
        envelope.update(page)
        _emit_json(envelope)
        return EXIT_OK
    if page["truncated"]:
        print("注意：你的游标已被轮转掉，中间有消息没读到（truncated=true）")
    messages = page["messages"]
    if not messages:
        tail = f"（游标 {since}，收件箱最后一条 {page['last_n']}）" if since is not None else ""
        print(f"没有新消息。{tail}")
        return EXIT_OK
    for record in messages:
        stamp = time.strftime("%m-%d %H:%M:%S", time.localtime(record.get("received_at") or 0))
        print(f"[{record.get('n')}] {stamp} {record.get('peer_id')} {record.get('kind')}: "
              f"{record.get('text') or '（非文本）'}")
    suffix = "（还有更多，继续读）" if page["has_more"] else ""
    print(f"\n游标：{page['next_cursor']}{suffix}")
    return EXIT_OK


def _follow_inbox(node: ClawBotNode, since, args: argparse.Namespace) -> int:
    """持续等新消息（`--json` 时每行一条 NDJSON 记录），Ctrl-C 退出。"""
    cursor = since
    count = 0
    try:
        while True:
            page = node.store.read_inbox(since=cursor, limit=args.limit, peer_id=args.peer or "")
            for record in page["messages"]:
                if args.json:
                    _emit_json({"schema": INTERFACE_VERSION, "ok": True, "message": record})
                else:
                    stamp = time.strftime("%m-%d %H:%M:%S",
                                          time.localtime(record.get("received_at") or 0))
                    print(f"[{record.get('n')}] {stamp} {record.get('peer_id')}: "
                          f"{record.get('text') or '（非文本）'}")
                    sys.stdout.flush()
                count += 1
            cursor = int(page["next_cursor"])
            time.sleep(max(0.2, float(args.interval)))
    except KeyboardInterrupt:
        if not args.json:
            print(f"\n已停止（本次共读到 {count} 条）。")
        return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    node = ClawBotNode(state_dir=args.state)
    creds = node.store.load_credentials()
    cursor = node.store.load_cursor()
    inbox = node.store.inbox_info()
    from clawbot.store import StateLock
    receiver_pid = StateLock.probe(node.store.lock_path)
    receiver = {"running": receiver_pid is not None, "pid": receiver_pid}
    if args.json:
        _emit_json({
            "schema": INTERFACE_VERSION,
            "ok": True,
            "state_dir": node.store.dir,
            "logged_in": creds.is_valid,
            "credentials": creds.masked() if creds.is_valid else {},
            "cursor": cursor,
            "peers": [item["peer_id"] for item in node.store.peers_detail()],
            "inbox": inbox,
            "receiver": receiver,
        })
        return EXIT_OK
    print(f"状态目录：{node.store.dir}")
    print(f"文件存在：{os.path.exists(node.store.credentials_path)}")
    if creds.is_valid:
        print("凭证：")
        for key, value in creds.masked().items():
            print(f"  {key} = {value}")
    else:
        print("凭证：未登录")
    print(f"游标：{'空（下次拉取全部可用消息）' if not cursor else cursor}")
    print(f"收件箱：{inbox['records']} 条（序号 {inbox['first_n']}–{inbox['last_n']}，"
          f"{inbox['bytes']} 字节）")
    if receiver["running"]:
        print(f"接收方：在跑（pid={receiver['pid']}）")
    else:
        print("接收方：没在跑 —— 手机发来的消息不会进收件箱（用 `clawbot run` 起一个）")
    return EXIT_OK


def cmd_reset(args: argparse.Namespace) -> int:
    node = ClawBotNode(state_dir=args.state)
    node.store.clear_credentials()
    print("已清除凭证与游标。下次 run 前需要重新 login。")
    return 0


def _add_state(sub_parser: argparse.ArgumentParser) -> None:
    """给每个子命令也挂上 `--state`。

    只有顶层 `--state` 时，调用方必须写 `clawbot --state X status`，
    写成 `clawbot status --state X` 会直接退出码 2 —— 对脚本太不友好。
    两边都接受：子命令上的是 `SUPPRESS` 默认值，没给就不会覆盖顶层已解析的值。
    """
    sub_parser.add_argument("--state", default=argparse.SUPPRESS,
                            help="状态目录（默认：<项目根>/state；也可写在子命令前）")


def main() -> int:
    parser = argparse.ArgumentParser(prog="clawbot", description="微信 ClawBot 独立收发节点")
    parser.add_argument("--state", default=None,
                        help="状态目录（默认：<项目根>/state）")
    sub = parser.add_subparsers(dest="command")

    p_login = sub.add_parser("login", help="扫码登录")
    _add_state(p_login)
    p_login.add_argument("--timeout", type=float, default=480.0, help="等待扫码的秒数")
    p_login.add_argument("--no-open", action="store_true", help="不用系统看图打开二维码")
    p_login.set_defaults(func=cmd_login)

    p_run = sub.add_parser("run", help="启动接收方（长轮询 → 收件箱；默认不自动回复）")
    _add_state(p_run)
    p_run.add_argument("--handler", default="",
                       help="可选：注册消息钩子的 Python 文件"
                            f"（不给 = 纯管道，只写收件箱；例如 "
                            f"{os.path.relpath(DEFAULT_HANDLER, HERE)}）")
    p_run.add_argument("--media-dir", default="",
                       help="入站图片/文件落到哪（默认 <state>/media；空串=不落盘）")
    p_run.add_argument("--no-media", action="store_true",
                       help="不取入站媒体（只把引用写进收件箱）")
    p_run.set_defaults(func=cmd_run)

    p_send = sub.add_parser("send", help="给对端发文本或本地文件/图片")
    _add_state(p_send)
    p_send.add_argument("path", nargs="?", default="", help="要发送的本地文件路径")
    p_send.add_argument("--text", default=None,
                        help="直接发文本；`--text -` 表示从 stdin 读")
    p_send.add_argument("--to", default="", help="对端 user_id（省略时用 state 里唯一的对端）")
    p_send.add_argument("--image", action="store_true", help="按图片发送（默认按文件附件）")
    p_send.add_argument("--caption", default="", help="先发一条说明文字")
    p_send.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON")
    p_send.set_defaults(func=cmd_send)

    p_peers = sub.add_parser("peers", help="列出可发送的对端")
    _add_state(p_peers)
    p_peers.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON")
    p_peers.set_defaults(func=cmd_peers)

    p_inbox = sub.add_parser("inbox", help="读手机发来的消息（按游标，跨进程可用）")
    _add_state(p_inbox)
    p_inbox.add_argument("--since", default="", help="上次返回的 next_cursor（省略则从头读）")
    p_inbox.add_argument("--limit", type=int, default=100, help="本次最多读多少条（默认 100）")
    p_inbox.add_argument("--peer", default="", help="只读某个对端的消息")
    p_inbox.add_argument("--follow", action="store_true", help="持续等待新消息（Ctrl-C 退出）")
    p_inbox.add_argument("--interval", type=float, default=1.0, help="--follow 的轮询间隔秒数")
    p_inbox.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON")
    p_inbox.set_defaults(func=cmd_inbox)

    p_status = sub.add_parser("status", help="查看本地状态")
    _add_state(p_status)
    p_status.add_argument("--json", action="store_true", help="stdout 只输出一行 JSON")
    p_status.set_defaults(func=cmd_status)

    p_reset = sub.add_parser("reset", help="清除凭证与派生状态")
    _add_state(p_reset)
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()
    # `--state` 两种位置都接受（见 _add_state），这里统一兜默认值
    if getattr(args, "state", None) is None:
        args.state = default_state_dir()
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
