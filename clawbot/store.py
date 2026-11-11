"""本地状态持久化：登录凭证 + 长轮询游标 + 会话上下文 + 收件箱。

只做文件读写，不含任何网络逻辑，方便单独测试。

五个文件：
  credentials.json  登录后拿到的 bot_token / ilink_bot_id / baseurl
  cursor.json       长轮询游标 get_updates_buf（断点续读的关键，丢了会重复收消息）
  contexts.json     每个对端最近一次 in-flight 的 context_token（异步回复用）
  seen.json         最近处理过的 message_id（重放/多实例共用 state 时避免重复回复）
  inbox.jsonl       入站消息的 append-only 收件箱 —— **对外的标准接口就是它**
                    （别的部件按游标 n 自己读，互不加锁、互不影响）
  run.lock          run() 的独占锁（同一 state 目录只允许一个接收方）
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# 收件箱对外的记录版本。字段只增不改；破坏性变更就把它抬到 2，并对旧版本做兼容。
INBOX_SCHEMA = 1

# 收件箱轮转：超过字节上限时只保留最近这么多条。
# 之所以要轮转：它是 append-only 文件，聊天记录 + 长跑节点会一直涨；
# 之所以用"序号 n"当游标而不是字节偏移：轮转之后老读者只会看到 truncated，
# 不会读到半条或者错位。
INBOX_MAX_BYTES = 2 * 1024 * 1024
INBOX_KEEP_RECORDS = 2000


def _warn(message: str) -> None:
    """状态文件的异常**要说出来**。

    静默降级的后果是"游标为什么一直在重复收消息"这种没人能查的问题；
    一行 stderr 就够定位，也不会污染 stdout 上的节点日志格式。
    """
    try:
        sys.stderr.write(f"[clawbot.store] {message}\n")
        sys.stderr.flush()
    except Exception:  # 连 stderr 都写不了就放弃告警，不能因此影响主流程
        pass


def _atomic_write_json(path: str, payload: Dict[str, Any], mode: Optional[int] = None,
                       *, retries: int = 5) -> None:
    """先写临时文件再替换，避免进程被杀时留下半个 JSON。

    两处不是"顺手加的"：

    · `os.fsync` —— 只 close 不 fsync 时，掉电可能得到"文件名已更新、内容是空的"游标；
      而 `_read_json` 现在把坏文件降级为空 → 从头重拉 → 重复回复。
    · 重试 `os.replace` —— Windows 上目标文件被**任何**进程持着读句柄
      （备份、杀软、索引器，甚至另一个 `main.py status` 恰好读了一下）时会抛
      `PermissionError [WinError 5]`。这是暂时性冲突，重试就好；不重试的后果
      不止"这次写失败"：调用点失败会中断本批消息的处理。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        delay = 0.05
        for attempt in range(1, max(1, retries) + 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt >= max(1, retries):
                    raise
                time.sleep(delay)
                delay *= 2
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _clean_stale_tmp(directory: str, *, max_age_s: float = 3600.0) -> None:
    """清掉自己前缀的残留临时文件。

    强杀（`_exit`/断电）会留下 `.tmp-*.json`；写凭证时的那个临时文件里是
    **明文 token**，一直躺着不合适。只清自己前缀、且超过 1 小时的，
    以免误删别的进程正在写的临时文件。
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not (name.startswith(".tmp-") and (name.endswith(".json") or name.endswith(".jsonl"))):
            continue
        full = os.path.join(directory, name)
        try:
            if now - os.path.getmtime(full) > max_age_s:
                os.unlink(full)
        except OSError:
            pass


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """读一个 JSON 文件；**读不到就当"没有这个文件"**。

    不这么做的后果是真实的：state 目录是人和别的程序都可能碰的地方，
    一个手改坏的 / 拷到一半的 cursor.json 会让节点在启动时抛 JSONDecodeError，
    报的还是看不懂的解析错误。降级为"空"是安全方向 ——
    空游标只会重复收一次消息，不会丢消息位置。
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except json.JSONDecodeError as exc:
        _warn(f"{os.path.basename(path)} 不是合法 JSON（{exc}），按空处理")
        return None
    except OSError as exc:
        _warn(f"读取 {os.path.basename(path)} 失败：{exc}，按空处理")
        return None
    if not isinstance(payload, dict):
        _warn(f"{os.path.basename(path)} 的内容不是对象（{type(payload).__name__}），按空处理")
        return None
    return payload


@dataclass
class Credentials:
    """扫码登录成功后拿到的长期凭证。"""

    bot_token: str = ""
    ilink_bot_id: str = ""
    ilink_user_id: str = ""
    baseurl: str = ""
    logged_in_at: float = 0.0

    @property
    def is_valid(self) -> bool:
        return bool(self.bot_token and self.ilink_bot_id)

    def masked(self) -> Dict[str, str]:
        """用于日志打印，绝不输出明文 token。"""

        def mask(value: str) -> str:
            if not value:
                return ""
            if len(value) <= 8:
                return "***"
            return f"{value[:4]}…{value[-4:]}(len={len(value)})"

        return {
            "bot_token": mask(self.bot_token),
            "ilink_bot_id": mask(self.ilink_bot_id),
            "ilink_user_id": mask(self.ilink_user_id),
            "baseurl": self.baseurl,
        }


class StateLock:
    """state 目录的独占锁：同一个目录同时只允许一个 `run()` 在跑。

    为什么必须有：两个实例各拿各的游标去拉同一批消息，同一条消息会被
    **处理两次**（实测：钩子被调用的序列是 [555, 555]，对端收到两条一模一样的回复）。
    第二实例现在会在启动时拿到一句可读的拒绝，而不是安静地重复消费。

    锁由操作系统在**进程结束时自动释放**，所以"进程被强杀"不会留下永久锁死。

    Windows 上锁定的是**偏移 64 起的那一个字节**，而不是偏移 0：`msvcrt.locking`
    会连**读**一起挡住，锁在偏移 0 会让别人读不到文件里的 pid（排错时正需要它）。
    POSIX 的 flock 是劝告锁，不影响读。
    """

    LOCK_OFFSET = 64      # 真正的锁字节（前 64 字节留给 pid 等可读信息）
    _FILE_BYTES = LOCK_OFFSET + 1

    def __init__(self, path: str) -> None:
        self.path = path
        self._fh = None

    def _ensure_file(self) -> None:
        try:
            if not os.path.exists(self.path) or os.path.getsize(self.path) < self._FILE_BYTES:
                with open(self.path, "wb") as fh:
                    fh.write(b"." * self._FILE_BYTES)
        except OSError:
            pass

    def acquire(self, *, write_pid: bool = True) -> bool:
        if self._fh is not None:
            return True
        self._ensure_file()
        try:
            fh = open(self.path, "r+b")
        except OSError:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(self.LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        if write_pid:
            try:
                # 写 pid 供排错（`state 目录被占用` 的信息里会带上它）
                fh.seek(0)
                line = f"{os.getpid()}\n".encode("ascii")
                fh.write(line + b" " * max(0, self.LOCK_OFFSET - len(line)))
                fh.flush()
            except OSError:
                pass
        return True

    @classmethod
    def probe(cls, path: str) -> Optional[int]:
        """看一眼这个 state 目录现在有没有接收方在跑；返回它的 pid（没有则 None）。

        用"试着加锁再立刻释放"来判断，而且 **write_pid=False** —— 探测不能把
        自己的 pid 写进去，否则探测完留下的就是一个骗人的 pid。
        """
        lock = cls(path)
        if not lock.acquire(write_pid=False):
            return lock.holder_pid()
        lock.release()
        return None

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(self.LOCK_OFFSET)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            fh.close()

    def holder_pid(self) -> Optional[int]:
        """读 lock 文件里的 pid（拿不到就返回 None）。

        必须**无缓冲、定长**地读前 32 字节：Windows 上被锁的是偏移 64 那个字节，
        而带缓冲的读会一次请求整块（含被锁字节）→ 整个读被拒，pid 就永远读不到。
        """
        try:
            with open(self.path, "rb", buffering=0) as fh:
                head = fh.read(32)
            first = head.split(b"\n", 1)[0].strip()
            return int(first) if first.isdigit() else None
        except (OSError, ValueError):
            return None

    def __enter__(self) -> "StateLock":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class StateStore:
    """状态目录的读写门面。"""

    def __init__(self, directory: str) -> None:
        self.dir = os.path.abspath(directory)
        self.credentials_path = os.path.join(self.dir, "credentials.json")
        self.cursor_path = os.path.join(self.dir, "cursor.json")
        self.contexts_path = os.path.join(self.dir, "contexts.json")
        self.seen_path = os.path.join(self.dir, "seen.json")
        self.inbox_path = os.path.join(self.dir, "inbox.jsonl")
        self.lock_path = os.path.join(self.dir, "run.lock")
        self._cursor: str = ""
        _clean_stale_tmp(self.dir)

    # -- 凭证 ---------------------------------------------------------------

    def load_credentials(self) -> Credentials:
        raw = _read_json(self.credentials_path)
        if not raw:
            return Credentials()
        known = {f for f in Credentials.__dataclass_fields__}  # type: ignore[attr-defined]
        return Credentials(**{k: v for k, v in raw.items() if k in known})

    def save_credentials(self, creds: Credentials) -> None:
        if not creds.logged_in_at:
            creds.logged_in_at = time.time()
        # 0600：凭证文件只允许自己读（Windows 上该调用是 no-op）
        _atomic_write_json(self.credentials_path, asdict(creds), mode=0o600)

    def clear_credentials(self) -> None:
        """注销：清掉凭证与所有派生状态。

        连 `login-qr-*.png` 与 `seen.json` 一起清 —— 二维码图等于登录能力，
        登出后不该继续躺在 state 里；去重表属于"这一段会话"，跟着一起清。
        """
        for path in (self.credentials_path, self.cursor_path, self.contexts_path,
                     self.seen_path, self.inbox_path):
            if os.path.exists(path):
                os.unlink(path)
        try:
            for name in os.listdir(self.dir):
                if name.startswith("login-qr") and name.endswith(".png"):
                    try:
                        os.unlink(os.path.join(self.dir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    # -- 去重 ---------------------------------------------------------------

    def is_seen(self, message_id: int) -> bool:
        """只读地问"这条消息处理过吗"。与 mark_seen 分开，是为了让调用方
        能先把消息落进收件箱、**再**记为已处理 —— 顺序反了会在两步之间
        丢消息（详见 client._pump_once 的注释）。"""
        if not message_id:
            return False
        raw = _read_json(self.seen_path) or {}
        raw_ids = raw.get("ids")
        ids = raw_ids if isinstance(raw_ids, list) else []
        return message_id in ids

    def mark_seen(self, message_id: int, *, keep: int = 500) -> bool:
        """把一条入站消息记为"已处理"；返回 True 表示**之前就见过**。

        游标是在**整批**消息处理完之后才落盘的，所以下面三种情况都会让同一批
        消息被再处理一次，对端看到的就是"同一条消息被回复两遍"：
          · 进程被强杀在"处理完、还没落盘"之间
          · Ctrl-C 落在批处理中间（README 明示的正常退出方式）
          · 两个实例共用一个 state 目录
        `message_id` 是稳定的，用它去重最省事，也不用改游标语义。

        没有 message_id 的消息不记（记不住就无法去重，不能假装记过）。
        """
        if not message_id:
            return False
        if self.is_seen(message_id):
            return True
        raw = _read_json(self.seen_path) or {}
        raw_ids = raw.get("ids")
        ids = [value for value in raw_ids if isinstance(value, int)] if isinstance(raw_ids, list) else []
        ids.append(int(message_id))
        if keep and len(ids) > keep:
            ids = ids[-keep:]
        _atomic_write_json(self.seen_path, {"ids": ids, "updated_at": time.time()}, mode=0o600)
        return False

    # -- 收件箱（对外的标准接口） -------------------------------------------

    def append_inbox(self, record: Dict[str, Any]) -> int:
        """把一条入站消息追加到收件箱，返回它的序号 `n`。

        为什么是 append-only 的 jsonl，而不是数据库/多份文件：
        跨进程通道只允许**一条**路径（"多一条通信路径就多一处会不一致"）。
        append 一行小 JSON 在 OS 层是原子的，于是任意多个读者可以各按自己的
        `n` 推进游标，互不加锁、互不影响；写方也永远不需要知道有谁在读。
        """
        os.makedirs(self.dir, exist_ok=True)
        payload = dict(record)
        payload["schema"] = INBOX_SCHEMA
        payload["n"] = self._inbox_tail_n() + 1
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with open(self.inbox_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())     # 消息宁可慢一点也不能"说收到过其实没落盘"
        self._rotate_inbox()
        return int(payload["n"])

    def _inbox_tail_n(self) -> int:
        """读最后一条**完整**记录的 `n`。

        不能用固定字节窗口：一条记录可能比窗口还长（一万字节的文本就够），
        窗口里只有碎行 → 解析不出来 → 返回 0 → 下一条 `n` 从 1 重新开始。
        后果是序号不再单调，按 `n` 推进游标的部件会把撞号那条**静默跳过**。
        所以从尾部按块回退，块不够大就翻倍，直到拿到一条前面是换行（或文件开头）的完整行。
        """
        if not os.path.exists(self.inbox_path):
            return 0
        try:
            size = os.path.getsize(self.inbox_path)
            with open(self.inbox_path, "rb") as fh:
                end = size
                block = 64 * 1024
                while end > 0:
                    start = max(0, end - block)
                    fh.seek(start)
                    chunk = fh.read(end - start)
                    lines = chunk.split(b"\n")
                    if start > 0:
                        lines = lines[1:]        # 块首那行是半截，丢掉
                    for line in reversed(lines):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            record = json.loads(line.decode("utf-8"))
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        value = record.get("n") if isinstance(record, dict) else None
                        if isinstance(value, int):
                            return value
                    if start == 0:
                        break
                    end = start
                    block *= 2
        except OSError:
            return 0
        return 0

    def _read_inbox_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        if not os.path.exists(self.inbox_path):
            return records
        try:
            with open(self.inbox_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # 半条记录（比如断电）跳过，不整体报错
                    if isinstance(record, dict):
                        records.append(record)
        except OSError as exc:
            _warn(f"读取收件箱失败：{exc}")
        return records

    def read_inbox(self, *, since: Optional[int] = None, limit: int = 100,
                   peer_id: str = "") -> Dict[str, Any]:
        """按游标读收件箱。

        游标语义（对调用方是不透明的字符串，实现上是序号 `n`）：
          · `since` 为空 → 从最老的开始读
          · `since=n` → 返回所有序号 > n 的记录
          · `next_cursor` 回传即可下次继续；`has_more=True` 表示还有没读完的
          · `truncated=True` 表示你给的 `since` 已经被轮转掉了（漏了消息），
            不是"没有新消息" —— 这两种情况必须能分开，否则调用方会安静地丢消息
        """
        records = self._read_inbox_records()
        first_n = int(records[0].get("n") or 0) if records else 0
        last_n = int(records[-1].get("n") or 0) if records else 0

        pending = [r for r in records
                   if (since is None or int(r.get("n") or 0) > since)
                   and (not peer_id or r.get("peer_id") == peer_id)]
        window = pending[: max(0, int(limit))]
        has_more = len(pending) > len(window)
        # since 之后的记录本应从 since+1 开始；如果文件里最老的都比它大，
        # 说明中间那一段已经被轮转掉了 → 必须显式告诉调用方"你漏了消息"
        truncated = bool(since is not None and records and int(since) + 1 < first_n)
        next_cursor = int(window[-1].get("n") or 0) if window else (since or 0)
        return {
            "messages": window,
            "next_cursor": str(next_cursor),
            "has_more": has_more,
            "truncated": truncated,
            "first_n": first_n,
            "last_n": last_n,
        }

    def inbox_info(self) -> Dict[str, Any]:
        """收件箱概况（给 status 用）：不读全文，只看首尾与大小。"""
        records = self._read_inbox_records()
        try:
            size = os.path.getsize(self.inbox_path) if os.path.exists(self.inbox_path) else 0
        except OSError:
            size = 0
        return {
            "path": self.inbox_path,
            "records": len(records),
            "first_n": int(records[0].get("n") or 0) if records else 0,
            "last_n": int(records[-1].get("n") or 0) if records else 0,
            "bytes": size,
        }

    def _rotate_inbox(self) -> None:
        """收件箱超过字节上限时只保留最近 N 条（原子替换）。

        替换是原子的，所以并发读者只会看到"旧文件"或"新文件"，
        不会看到半个文件；而游标是序号 n，老读者最多看到 `truncated=True`。
        """
        try:
            if os.path.getsize(self.inbox_path) <= INBOX_MAX_BYTES:
                return
        except OSError:
            return
        kept = self._read_inbox_records()[-INBOX_KEEP_RECORDS:]
        directory = os.path.dirname(self.inbox_path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".jsonl")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for record in kept:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.inbox_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def peers_detail(self) -> List[Dict[str, Any]]:
        """所有记住过 context_token 的对端（给 peers 命令 / 上层选目标用）。"""
        entries = self._read_contexts()
        result = []
        for peer, entry in entries.items():
            token = entry.get("context_token")
            result.append({
                "peer_id": peer,
                "has_context_token": bool(token),
                "updated_at": entry.get("updated_at") or 0,
            })
        result.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
        return result

    # -- 游标 ---------------------------------------------------------------

    def load_cursor(self) -> str:
        """读取游标。

        每次都从磁盘读 —— 同一个状态目录可能被多个实例/组件共用，
        缓存下来会导致"别人写了新游标、我还在用旧的"。这里不做缓存。
        """
        raw = _read_json(self.cursor_path) or {}
        self._cursor = str(raw.get("get_updates_buf", ""))
        return self._cursor

    def touch_heartbeat(self) -> None:
        """每轮长轮询回来摸一下 `<state>/poll.heartbeat` —— "接收方还在轮询吗"必须看得见。

        ★ 不能拿游标 mtime 当心跳：`save_cursor` 的值没变就不写（见 save_cursor 的说明）。
          2026-10-17 实测：接收方 18:03 之后静默丢了用户的消息，而"它还在不在轮询"
          任何地方都看不出来，只能靠人翻日志。`os.utime` 每次都动，不会被"没变就跳过"。
        """
        try:
            path = os.path.join(self.dir, "poll.heartbeat")
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write("")
            os.utime(path, None)
        except OSError:
            pass

    def append_sent(self, record: dict) -> None:
        """记一条**发出**流水（`sent.jsonl`）—— "回复到底发出去了没有"必须有留痕。

        ★ 为什么：用户说"没有回应"时，发送结果只存在于那一轮的会话存档里，
          要翻六处才能确认（2026-10-16 实测）。收件箱有 `inbox.jsonl`，
          发出的却没有对应物 —— 于是"收得到"和"发得出"没法对照。
        ★ 记流水失败**不该**让发送本身算失败：吞掉 OSError。
        """
        try:
            path = os.path.join(self.dir, "sent.jsonl")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            # ★ 只吞 OSError（磁盘/权限），不吞编程错误：第一版写成 self.state_dir，
            #   属性根本不存在 ⇒ AttributeError 被 except Exception 吞掉 ⇒ 流水静默不写，
            #   而"留痕"这件事本身失效了却看不出来。宁可吵，也不要静默。
            import sys as _sys
            print(f"（发出流水写入失败：{exc}）", file=_sys.stderr)

    def save_cursor(self, value: str) -> None:
        """落盘游标。

        跳过写的判据是**磁盘上的当前值**，不是"本实例上次写过的值"。
        后者有个已实测的坏后果：A 写 C1 → 另一个实例写 C2 → A 再写 C1 会被
        "我以为还是 C1"顶成跳过 → 磁盘停在 C2（游标倒退）→ 重复收消息。
        同一个毛病在 `reset` 之后更直接：文件被删了，本实例仍以为"写过 CUR-9"，
        于是再也写不出 cursor.json。
        """
        value = str(value)
        if os.path.exists(self.cursor_path):
            on_disk = str((_read_json(self.cursor_path) or {}).get("get_updates_buf", ""))
            if value == on_disk:
                self._cursor = value
                return
        _atomic_write_json(self.cursor_path, {"get_updates_buf": value, "updated_at": time.time()})
        self._cursor = value

    # -- 会话上下文 ---------------------------------------------------------

    def _read_contexts(self) -> Dict[str, Any]:
        """从磁盘读全部会话上下文。

        与游标同一个理由：**不缓存**。同一个状态目录可能被多个实例/组件共用，
        缓存下来有两个后果，都会表现为"随机丢回复"：
          · 陈旧读：别人刚记下的对端，我这里查不到 → 发送被拒（没有 context_token）
          · 丢更新：两个实例都读过盘之后再各自写，后写的会把对方刚记的整条覆盖掉
        """
        raw = _read_json(self.contexts_path) or {}
        peers = raw.get("peers", {}) if isinstance(raw, dict) else {}
        if not isinstance(peers, dict):
            return {}
        return {peer: entry for peer, entry in peers.items() if isinstance(entry, dict)}

    def remember_context(self, peer_id: str, context_token: str, *, keep: int = 50) -> None:
        """记住某对端最新的 context_token，供异步回复使用。

        读—改—写：每次先重新读盘再合并，而不是拿本实例的内存副本去覆盖。
        （注意：这仍然不是跨进程锁。两个进程在同一毫秒级窗口里读—改—写仍可能丢一条；
        要彻底消除需要文件锁，代价与收益见 README「已知边界」。）
        """
        if not peer_id or not context_token:
            return
        peers = self._read_contexts()
        peers[peer_id] = {"context_token": context_token, "updated_at": time.time()}
        if keep and len(peers) > keep:
            ordered = sorted(
                peers.items(), key=lambda kv: kv[1].get("updated_at", 0), reverse=True
            )[:keep]
            peers = dict(ordered)
        _atomic_write_json(self.contexts_path, {"peers": peers}, mode=0o600)

    def get_context(self, peer_id: str) -> Optional[str]:
        entry = self._read_contexts().get(peer_id)
        if not entry:
            return None
        value = entry.get("context_token")
        return str(value) if value else None

    def known_peers(self) -> Dict[str, str]:
        """所有记住过 context_token 的对端 → 令牌。

        给"不想写死 peer_id"的调用方用（如命令行 send）：只有一个对端时直接用它。
        """
        return {
            peer: str(entry.get("context_token") or "")
            for peer, entry in self._read_contexts().items()
        }


@dataclass
class InboundMessage:
    """一条入站消息的规范化视图。

    这是给上层钩子看的接口 —— 上层完全不需要知道 iLink 协议长什么样。
    """

    peer_id: str                      # 回复时要用的 to_user_id
    text: str                         # 纯文本内容（非文本消息为空串）
    kind: str                         # text / image / voice / file / video / unknown
    message_id: int = 0
    seq: int = 0
    create_time_ms: int = 0
    context_token: str = ""
    # ★ 入站媒体的**引用**（url / encrypt_query_param / aes_key / kind / filename / size）——
    #   原始字节不在这里（那是接收方按 `media_dir` 落盘的事）。
    media: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_text(self) -> bool:
        return self.kind == "text"


_ITEM_KINDS = {1: "text", 2: "image", 3: "voice", 4: "file", 5: "video"}

# item 里媒体挂在哪个子对象下（协议原文的字段名）
_MEDIA_HOLDER = {"image": "image_item", "video": "video_item",
                 "file": "file_item", "voice": "voice_item"}
# 同一个东西在不同客户端/版本里可能叫不同名字 —— 都认（"下载端两种都收"的精神）
_URL_KEYS = ("full_url", "cdn_url", "url", "download_url")
_PARAM_KEYS = ("encrypt_query_param", "encrypted_query_param", "encrypt_param")
_AES_KEYS = ("aes_key", "aeskey")
_SIZE_KEYS = ("mid_size", "video_size", "len", "file_size", "size")


def _first_str(container: Any, keys: tuple) -> str:
    if not isinstance(container, dict):
        return ""
    for k in keys:
        v = container.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def media_descriptor(item: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """从一条 item 里取出**媒体引用**（纯读；取不到返回空 dict）。

    入站消息是外部输入，这里对每个字段都当"可能是脏的"：解析异常的位置在游标落盘之前，
    抛一次会让同一批消息被重复处理。
    """
    holder = item.get(_MEDIA_HOLDER.get(kind, ""))
    if not isinstance(holder, dict):
        holder = item
    media = holder.get("media") if isinstance(holder.get("media"), dict) else holder
    url = _first_str(media, _URL_KEYS) or _first_str(holder, _URL_KEYS)
    param = _first_str(media, _PARAM_KEYS) or _first_str(holder, _PARAM_KEYS)
    if not (url or param):
        return {}
    size = 0
    for k in _SIZE_KEYS:
        v = holder.get(k) if holder.get(k) is not None else media.get(k)
        if isinstance(v, str) and v.isdigit():
            size = int(v)
            break
        if isinstance(v, int):
            size = v
            break
    return {"kind": kind, "url": url, "encrypt_query_param": param,
            "aes_key": _first_str(media, _AES_KEYS) or _first_str(holder, _AES_KEYS),
            "filename": _first_str(holder, ("file_name", "filename", "name")),
            "size": size}


def _as_int(value: Any, default: int = 0) -> int:
    """服务端给的数值字段可能是字符串/None/垃圾；转不了就给默认值。

    这里不抛异常是有具体理由的：`parse_inbound` 在主循环里、在游标落盘**之前**
    调用，一个非数值的 message_id 会让整批消息解析失败、游标不推进，
    下一轮把同一批消息再处理一遍（重复回复）。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_of(container: Any, key: str) -> str:
    if not isinstance(container, dict):
        return ""
    value = container.get(key)
    return value if isinstance(value, str) else ""


def parse_inbound(msg: Dict[str, Any]) -> InboundMessage:
    """把 WeixinMessage 解析成 InboundMessage。

    对每个字段都当"可能是脏的"来处理：入站消息是**外部输入**，
    而解析失败的位置在游标落盘之前 —— 抛一次异常就会让同一批消息被重复处理。
    """
    raw_items = msg.get("item_list")
    items = raw_items if isinstance(raw_items, list) else []
    text_parts = []
    kind = "unknown"
    media: Dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        mapped = _ITEM_KINDS.get(item_type)
        if mapped is None:
            continue
        if kind == "unknown":
            kind = mapped
        if mapped == "text":
            text = _text_of(item.get("text_item"), "text")
            if text:
                text_parts.append(text)
        elif mapped == "voice":
            # 语音消息可带转写文本
            transcript = _text_of(item.get("voice_item"), "text")
            if transcript:
                text_parts.append(transcript)
        if mapped != "text" and not media:
            # ★ 入站媒体的**引用**（用户 2026-10-16：微信发来的图片传不过来）。
            #   这里只取引用，字节由接收方按 media_dir 取回来（store 不碰网络）。
            desc = media_descriptor(item, mapped)
            if desc:
                media = desc
    return InboundMessage(
        peer_id=str(msg.get("from_user_id") or ""),
        text="\n".join(text_parts).strip(),
        kind=kind,
        message_id=_as_int(msg.get("message_id")),
        seq=_as_int(msg.get("seq")),
        create_time_ms=_as_int(msg.get("create_time_ms")),
        context_token=str(msg.get("context_token") or ""),
        media=media,
        raw=msg,
    )
