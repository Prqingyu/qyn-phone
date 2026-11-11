"""状态持久化自检：凭证 / 游标 / 会话上下文。

单独成一个文件，因为这层的语义不是"能读能写"这么简单：

  · **不缓存** —— 同一个 state 目录可能被多个实例/组件共用（README 明说支持，
    中控机 systemd、命令行、被别的程序 import 都指向它）。缓存会同时造成
    "陈旧读"（别人刚记的对端我查不到）与"丢更新"（我拿旧副本覆盖掉对方刚写的）
  · 两种失效的共同表现都是**随机丢回复**，而且不报错，只能靠用例钉住
  · 原子写：任何时刻磁盘上都不该出现半个 JSON

运行：python tests/test_store.py
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from clawbot.store import Credentials, StateStore  # noqa: E402

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def new_dir() -> str:
    return tempfile.mkdtemp(prefix="clawbot-store-")


def read_json(path: str):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_credentials() -> None:
    print("\n[1] 凭证")
    tmp = new_dir()
    store = StateStore(tmp)
    check("没有文件时是未登录", not store.load_credentials().is_valid)

    store.save_credentials(Credentials(bot_token="FAKE_BOT_TOKEN", ilink_bot_id="B@im.bot",
                                       ilink_user_id="u@im.wechat", baseurl="https://x"))
    loaded = store.load_credentials()
    check("可回读", loaded.bot_token == "FAKE_BOT_TOKEN" and loaded.ilink_bot_id == "B@im.bot")
    check("登录时间被补上", loaded.logged_in_at > 0)
    check("脱敏不含明文 token",
          "FAKE_BOT_TOKEN" not in json.dumps(loaded.masked(), ensure_ascii=False))
    # 这条以前写成 isinstance(load_credentials(), Credentials) —— 恒真（函数只会返回它），
    # 变异体立刻证明它抓不到任何东西。改成对**行为**的断言。
    tmp2 = new_dir()
    with open(os.path.join(tmp2, "credentials.json"), "w", encoding="utf-8") as fh:
        json.dump({"future_field": {"nested": [1, 2, 3]}}, fh)
    stray = StateStore(tmp2).load_credentials()
    check("只有未知字段时视为未登录（而不是崩）", stray.is_valid is False)

    # 凭证文件里塞一个未来版本才有的字段，读取时不能炸
    raw = read_json(store.credentials_path)
    raw["future_field"] = {"nested": [1, 2, 3]}
    with open(store.credentials_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh)
    check("文件多出未知字段也能读", StateStore(tmp).load_credentials().is_valid)

    store.clear_credentials()
    check("clear 后凭证为空", StateStore(tmp).load_credentials().is_valid is False)


def test_cursor() -> None:
    print("\n[2] 游标")
    tmp = new_dir()
    a = StateStore(tmp)
    check("初始为空", a.load_cursor() == "")

    a.save_cursor("CUR-1")
    check("落盘可读", StateStore(tmp).load_cursor() == "CUR-1")
    check("跨实例可见（不缓存）", StateStore(tmp).load_cursor() == "CUR-1")

    # 另一个实例写了新游标，本实例必须读到新的，而不是内存里的旧值
    other = StateStore(tmp)
    other.save_cursor("CUR-2")
    check("别人写了新游标，我读得到", a.load_cursor() == "CUR-2", a.load_cursor())

    a.save_cursor("CUR-3")
    before = os.path.getmtime(StateStore(tmp).cursor_path)
    a.save_cursor("CUR-3")           # 同值 → 应跳过写
    check("同值重复写被跳过", os.path.getmtime(StateStore(tmp).cursor_path) == before)

    a.save_cursor("")
    check("可以写回空游标（显式重置）", read_json(os.path.join(tmp, "cursor.json"))["get_updates_buf"] == "")


def test_contexts_single_instance() -> None:
    print("\n[3] 会话上下文（单实例语义）")
    tmp = new_dir()
    store = StateStore(tmp)
    check("没记过时返回 None", store.get_context("p@im.wechat") is None)

    store.remember_context("p@im.wechat", "CTX-1")
    check("记住后可读", store.get_context("p@im.wechat") == "CTX-1")
    store.remember_context("p@im.wechat", "CTX-2")
    check("同对端覆盖为最新", store.get_context("p@im.wechat") == "CTX-2")
    check("known_peers 列出对端", list(StateStore(tmp).known_peers()) == ["p@im.wechat"])

    store.remember_context("", "CTX")
    store.remember_context("q@im.wechat", "")
    check("空对端/空令牌不落盘", len(StateStore(tmp).known_peers()) == 1)


def test_contexts_multi_instance() -> None:
    """两个实例共用同一 state 目录：既不能陈旧读，也不能丢更新。"""
    print("\n[4] 会话上下文（多实例：陈旧读与丢更新）")
    tmp = new_dir()
    seed = StateStore(tmp)
    seed.remember_context("seed@im.wechat", "CTX-SEED")

    a = StateStore(tmp)
    b = StateStore(tmp)
    a.get_context("seed@im.wechat")          # a 先读过盘（真实场景：它刚处理过一条消息）
    b.get_context("seed@im.wechat")          # b 也读过盘

    b.remember_context("peerB@im.wechat", "CTX-B")
    check("陈旧读：a 能立刻看到 b 刚记下的对端",
          a.get_context("peerB@im.wechat") == "CTX-B", str(a.get_context("peerB@im.wechat")))

    a.remember_context("peerA@im.wechat", "CTX-A")
    final = StateStore(tmp).known_peers()
    check("丢更新：交错写之后两个对端都还在",
          "peerA@im.wechat" in final and "peerB@im.wechat" in final, str(sorted(final)))
    check("先前的对端没被牵连", "seed@im.wechat" in final)

    # 两个实例来回写，最后不应少于写入过的对端数
    for index in range(10):
        a.remember_context(f"a{index}@im.wechat", f"A{index}")
        b.remember_context(f"b{index}@im.wechat", f"B{index}")
    final = StateStore(tmp).known_peers()
    # seed + peerA + peerB + 10 个 a* + 10 个 b* = 23
    check("交替写 20 条后一条不少", len(final) == 23, f"got={len(final)}")


def test_contexts_keep_limit() -> None:
    print("\n[5] 上下文条数上限")
    tmp = new_dir()
    store = StateStore(tmp)
    for index in range(60):
        store.remember_context(f"p{index}@im.wechat", f"C{index}")
    peers = StateStore(tmp).known_peers()
    check("超过 keep 时截断到 50", len(peers) == 50, f"got={len(peers)}")
    check("保留最新的那个", "p59@im.wechat" in peers)
    check("丢掉最旧的那个", "p0@im.wechat" not in peers)

    small = new_dir()
    keeper = StateStore(small)
    for index in range(5):
        keeper.remember_context(f"p{index}@im.wechat", f"C{index}", keep=2)
    check("keep 可显式调小", len(StateStore(small).known_peers()) == 2)


def test_corrupt_files() -> None:
    print("\n[6] 损坏/异构文件不致命")
    for payload in ([], {"peers": []}, {"peers": {"x@im.wechat": "not-a-dict"}}, {}, "junk"):
        tmp = new_dir()
        with open(os.path.join(tmp, "contexts.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        store = StateStore(tmp)
        try:
            got = store.known_peers()
            store.get_context("x@im.wechat")
            store.remember_context("y@im.wechat", "CTX-Y")
            ok = isinstance(got, dict) and store.get_context("y@im.wechat") == "CTX-Y"
            check(f"contexts.json={payload!r} 时仍可用", ok, str(got))
        except Exception as exc:  # noqa: BLE001
            check(f"contexts.json={payload!r} 时仍可用", False, f"{type(exc).__name__}: {exc}")

    tmp = new_dir()
    with open(os.path.join(tmp, "cursor.json"), "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 JSON")
    check("游标文件坏了返回空串（不抛异常）", StateStore(tmp).load_cursor() == "")


def test_atomic_write() -> None:
    print("\n[7] 原子写不留残渣")
    tmp = new_dir()
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B"))
    store.save_cursor("CUR")
    store.remember_context("p@im.wechat", "CTX")
    leftovers = [n for n in os.listdir(tmp) if n.startswith(".tmp-")]
    check("目录里没有 .tmp- 残留", leftovers == [], str(leftovers))
    check("三个文件都是合法 JSON",
          all(isinstance(read_json(os.path.join(tmp, name)), dict)
              for name in ("credentials.json", "cursor.json", "contexts.json")))
    # 这条以前只断言 os.path.exists —— 变异体证明"把 mode=0o600 整个删掉"它依然 PASS。
    # 现在真的检查权限：POSIX 上必须恰好是 0600；Windows 上 chmod 是 no-op，只断言文件可用。
    creds_path = store.credentials_path
    if os.name == "posix":
        check("凭证文件权限是 0600", stat.S_IMODE(os.stat(creds_path).st_mode) == 0o600,
              oct(stat.S_IMODE(os.stat(creds_path).st_mode)))
    else:
        check("凭证文件可读回（Windows 上 0600 是 no-op，不能假装它有效）",
              StateStore(tmp).load_credentials().is_valid)


def test_cursor_judgement_is_disk_based() -> None:
    """跳过写的判据必须是**磁盘上的值**，不是"本实例上次写的值"。"""
    print("\n[8] 游标：跳过写的判据")
    tmp = new_dir()
    a = StateStore(tmp)
    b = StateStore(tmp)
    a.save_cursor("C1")
    b.save_cursor("C2")
    a.save_cursor("C1")   # 旧实现：内存里以为"还是 C1"→ 跳过写 → 磁盘停在 C2（游标倒退）
    check("别人推进过之后，自己再写必须落盘",
          StateStore(tmp).load_cursor() == "C1", StateStore(tmp).load_cursor())

    a.clear_credentials()
    check("clear 后游标为空", StateStore(tmp).load_cursor() == "")
    a.save_cursor("C3")
    check("clear 之后仍能重新写出 cursor.json",
          StateStore(tmp).load_cursor() == "C3", StateStore(tmp).load_cursor())


def test_seen_dedup() -> None:
    """message_id 去重表：重放 / 多实例共用 state 时靠它避免重复回复。"""
    print("\n[9] 去重表")
    tmp = new_dir()
    store = StateStore(tmp)
    check("第一次见到返回 False（没记过）", store.mark_seen(123) is False)
    check("第二次见到返回 True（已处理过）", store.mark_seen(123) is True)
    check("另一个实例也看得到（落在磁盘上）", StateStore(tmp).mark_seen(123) is True)
    check("没有 message_id 的消息不记",
          store.mark_seen(0) is False and store.mark_seen(0) is False)
    for index in range(600):
        store.mark_seen(10000 + index)
    ids = read_json(os.path.join(tmp, "seen.json"))["ids"]
    check("去重表有上限（不会无限增长）", len(ids) == 500, str(len(ids)))
    check("保留的是最近的 id", 10599 in ids and 10000 not in ids)


def test_replace_retry_and_tmp_cleanup() -> None:
    """写盘要扛住"目标文件正被别的进程读着"（Windows 上会让 os.replace 抛错）。"""
    print("\n[10] 写盘重试与残留清理")
    tmp = new_dir()
    target = os.path.join(tmp, "cursor.json")
    store = StateStore(tmp)
    store.save_cursor("C1")

    holder = open(target, "r", encoding="utf-8")
    released = threading.Event()

    def release_later() -> None:
        time.sleep(0.15)
        holder.close()
        released.set()

    thread = threading.Thread(target=release_later)
    thread.start()
    error = ""
    try:
        store.save_cursor("C2")      # 需要退避重试到读句柄被放开
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    thread.join()
    check("目标被持读句柄时，写盘会重试而不是直接失败",
          not error and StateStore(tmp).load_cursor() == "C2", error or StateStore(tmp).load_cursor())
    check("没有 .tmp- 残留", not [n for n in os.listdir(tmp) if n.startswith(".tmp-")],
          str(os.listdir(tmp)))

    # 残留的临时文件（强杀产物）在下次构造 StateStore 时被清掉；新的不动
    stale = os.path.join(tmp, ".tmp-stale.json")
    fresh = os.path.join(tmp, ".tmp-fresh.json")
    for path in (stale, fresh):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    StateStore(tmp)
    check("清掉过期（>1h）的 .tmp- 残留", not os.path.exists(stale))
    check("不动刚写的 .tmp-（可能是别的进程正在写）", os.path.exists(fresh))


def test_lock_exclusive() -> None:
    """state 目录独占锁：跨实例互斥、释放后可用。"""
    print("\n[11] 独占锁")
    from clawbot.store import StateLock

    tmp = new_dir()
    path = os.path.join(tmp, "run.lock")
    first = StateLock(path)
    second = StateLock(path)
    check("第一个拿到锁", first.acquire() is True)
    check("第二个拿不到（同一目录不允许两个实例同时跑）", second.acquire() is False)
    first.release()
    check("释放后第二个可以拿到", second.acquire() is True)
    second.release()
    check("再释放/重复 acquire 不炸", (second.release() or first.acquire()) is True)
    first.release()


def main() -> int:
    print("状态持久化自检：")
    test_credentials()
    test_cursor()
    test_contexts_single_instance()
    test_contexts_multi_instance()
    test_contexts_keep_limit()
    test_corrupt_files()
    test_atomic_write()
    test_cursor_judgement_is_disk_based()
    test_seen_dedup()
    test_replace_retry_and_tmp_cleanup()
    test_lock_exclusive()

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
