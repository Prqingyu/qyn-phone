"""一键跑全部自检 —— 封装/交接用。

    python tests/run_all.py

为什么需要它：
  · 六套自检是**脚本式**的（`python tests/xxx.py` + `PASS`/退出码），不是 pytest 用例集，
    用 pytest 跑会被当成 fixture 报错；
  · 交接 / CI / 上线前想要的是一条命令 + 一个明确的退出码，而不是六条命令挨个看。

零第三方依赖：只用 subprocess 起同一个解释器。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
SUITES = (
    ("test_interface.py", "对外接口：schema 一致性 / 收件箱游标 / 跨进程读写"),
    ("test_protocol.py", "协议层：请求头 / 登录 / 游标 / 发送 / 钩子 / 暂停 / 超时 / 脏字段"),
    ("test_media.py", "出站媒体：getUploadUrl / 桩 CDN / item 形状 / 失败路径"),
    ("test_store.py", "持久化：凭证 / 游标 / 上下文（多实例陈旧读与丢更新）"),
    ("test_aes.py", "AES-128-ECB：FIPS-197 / SP 800-38A 官方向量"),
    ("test_qr_roundtrip.py", "QR 编解码闭环"),
    ("test_paths.py", "状态目录不随 CWD 漂移"),
)


def main() -> int:
    print(f"Python {sys.version.split()[0]} | {os.path.dirname(HERE)}")
    print()
    results = []
    for filename, description in SUITES:
        path = os.path.join(HERE, filename)
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, path],
            cwd=os.path.dirname(HERE),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.perf_counter() - started
        output = (proc.stdout or "") + (proc.stderr or "")
        failures = [line.strip() for line in output.splitlines()
                    if line.strip().startswith("FAIL")]
        ok = proc.returncode == 0
        results.append((filename, description, ok, elapsed, failures))

        print(f"[{'PASS' if ok else 'FAIL'}] {filename:<22} {elapsed:5.2f}s  {description}")
        if not ok:
            tail = [line for line in output.strip().splitlines()[-12:]]
            for line in tail:
                print(f"        {line}")

    print()
    failed = [name for name, _, ok, _, _ in results if not ok]
    total = sum(elapsed for *_, elapsed, _ in results)
    if failed:
        print(f"FAIL：{len(failed)}/{len(results)} 套未通过 → {', '.join(failed)}（共 {total:.2f}s）")
        return 1
    print(f"PASS：{len(results)}/{len(results)} 套全部通过（共 {total:.2f}s）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
