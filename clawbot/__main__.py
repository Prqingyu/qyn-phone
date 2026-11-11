"""`python -m clawbot ...` 入口 —— 等价于 `python main.py ...`。

    python -m clawbot login
    python -m clawbot run
    python -m clawbot send <路径>
    python -m clawbot status
    python -m clawbot reset

打包安装后同理：`clawbot login` 等（见 pyproject.toml 的 [project.scripts]）。
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
