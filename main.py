"""命令行入口（兼容壳）。

真正的实现在 `clawbot/cli.py`。保留这个文件是因为 README、部署脚本和 systemd 单元
都写着 `python main.py ...`；把实现搬进包之后，同一个入口现在有四种等价写法：

    python main.py run              # 这个壳（不需要安装）
    python -m clawbot run           # 模块入口
    clawbot run                     # 装成包之后的 console script
    from clawbot.cli import main    # 当库用

壳里只做一件事：把项目根放进 `sys.path`（这样没 pip install 也能 import clawbot），
然后把参数原样交给 `clawbot.cli.main`。
"""

from __future__ import annotations

import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from clawbot.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
