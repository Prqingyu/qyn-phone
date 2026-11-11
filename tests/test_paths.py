"""状态目录解析自检。

锁住一个真实踩过的坑：state_dir 若是相对路径且相对 **当前工作目录** 解析，
那么"从仓库根启动"和"在 PHONE 目录里启动"会指向两个不同的 state/，
表现就是"明明登录过却找不到凭证、要求重新扫码"。

运行：python tests/test_paths.py
"""

from __future__ import annotations

import os
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from clawbot import ClawBotNode, default_state_dir, resolve_state_dir  # noqa: E402

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def main() -> int:
    print("状态目录解析自检：")

    expected = os.path.join(ROOT, "state")
    check("默认状态目录 = <项目根>/state", default_state_dir() == expected, default_state_dir())
    check("默认是绝对路径", os.path.isabs(default_state_dir()), default_state_dir())

    check("None → 默认目录", resolve_state_dir(None) == expected, resolve_state_dir(None))
    check("空串 → 默认目录", resolve_state_dir("") == expected, resolve_state_dir(""))

    # 绝对路径必须原样保留（部署到中控机时要能指到 /tmp/qyn-state-test）
    for probe in (tempfile.gettempdir(), os.path.join(tempfile.gettempdir(), "qyn-state")):
        check(f"绝对路径原样保留: {os.path.basename(probe)}",
              resolve_state_dir(probe) == probe, resolve_state_dir(probe))

    # 相对路径必须相对**项目根**，而不是 CWD
    check("相对路径相对项目根解析",
          resolve_state_dir("state") == expected, resolve_state_dir("state"))
    check("普通相对路径相对项目根解析",
          resolve_state_dir("mystate") == os.path.join(ROOT, "mystate"),
          resolve_state_dir("mystate"))

    # 最关键：换个工作目录，解析结果不能变
    original_cwd = os.getcwd()
    try:
        for probe_cwd in (os.path.dirname(ROOT), tempfile.gettempdir()):
            os.chdir(probe_cwd)
            got = resolve_state_dir("state")
            check(f"从 {os.path.basename(probe_cwd) or probe_cwd} 启动时指向不变",
                  got == expected, f"cwd={os.getcwd()} -> {got}")
            node = ClawBotNode(state_dir="state")
            check(f"从该处构造的节点 store 不变（{os.path.basename(probe_cwd) or probe_cwd}）",
                  node.store.dir == expected, node.store.dir)
    finally:
        os.chdir(original_cwd)

    # 节点默认值
    node_default = ClawBotNode()
    check("节点不传 state_dir 时用默认目录",
          node_default.store.dir == expected, node_default.store.dir)

    # 显式临时目录仍然生效（测试依赖这个）
    tmp = tempfile.mkdtemp(prefix="clawbot-path-")
    node_tmp = ClawBotNode(state_dir=tmp)
    check("显式绝对路径生效", node_tmp.store.dir == os.path.abspath(tmp), node_tmp.store.dir)

    # 凭证文件确实落在解析出来的目录里
    check("凭证路径落在 state 目录内",
          os.path.dirname(node_default.store.credentials_path) == expected,
          node_default.store.credentials_path)

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
