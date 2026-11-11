"""clawbot —— 手机 ↔ 电脑的通信管道（微信 ClawBot / iLink）。

零第三方依赖。**对外接口是命令行**（`clawbot send/inbox/status --json`），
字段表与游标语义见 `INTERFACE.md`；这个包只在你自己写 Python 消费者时才需要 import。

导入这一层时只承诺这几个名字稳定：

    ClawBotNode        接收方（长轮询 + 收件箱 + 可选进程内钩子）
    InboundMessage     一条入站消息
    SendResult         一次发送的结果
    ClawBotError       错误基类（SessionPaused / MediaUploadError / RequestTimeout 是子类）

其余名字（媒体准备、URL 拼装、AES）也能 import，但它们属于**实现细节**，不承诺稳定。
"""

from .client import (
    BOT_AGENT,
    CHANNEL_VERSION,
    DEFAULT_API_BASE,
    DEFAULT_CDN_BASE,
    ClawBotClient,
    ClawBotError,
    ClawBotNode,
    MediaUploadError,
    QRLoginSession,
    RequestTimeout,
    SendResult,
    SessionPaused,
    default_state_dir,
    resolve_state_dir,
)
from .media import (
    DEFAULT_MAX_MEDIA_BYTES,
    PreparedMedia,
    UploadedMedia,
    aes_key_for_media,
    build_media_item,
    build_upload_url,
    detect_kind,
    prepare_media,
    read_media_file,
)
from .store import Credentials, InboundMessage, StateStore, parse_inbound

# 版本号只有一个来源：client.CHANNEL_VERSION —— 它同时还进 base_info.channel_version
# 与 iLink-App-ClientVersion 请求头。两处各写一份迟早会漂。
__version__ = CHANNEL_VERSION

# 只把"稳定面"列进来：`from clawbot import *` 不会把这些工具类的东西倒给消费者。
# 其余名字仍然可 import（见上面 docstring），但要用就显式写 `from clawbot.media import ...`。
__all__ = [
    "ClawBotNode",
    "InboundMessage",
    "SendResult",
    "ClawBotError",
    "SessionPaused",
    "MediaUploadError",
    "RequestTimeout",
    "default_state_dir",
    "resolve_state_dir",
    "__version__",
]
