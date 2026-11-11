"""出站媒体的准备、加密与消息项构造 —— 纯函数，不碰网络。

发一张图片在协议上分三段，本模块负责其中两段：

    准备+加密（本模块） → getUploadUrl / CDN 上传（client.py） → 组装 item 发消息（本模块）

为什么把准备段单独拆出来：它决定了 getUploadUrl 的每一个字段
（rawsize / rawfilemd5 / filesize / aeskey）。这四个值必须与**实际要传的密文**彼此自洽。
一旦错配，服务端照样返回上传地址、CDN 照样收下密文、sendMessage 照样 ret=0，
但对方收到的是一个解不开的灰框 —— **全程没有任何报错**。
所以这里只写纯函数，便于用例把每一根钉子钉死。

协议依据：Tencent/openclaw-weixin 的 docs/protocol_zh_CN.md（CDN 媒体流程、客户端使用的媒体字段）。
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import urllib.parse

from .aes import decrypt_ecb
from dataclasses import dataclass, field
from typing import Optional, Tuple, Union

from .aes import encrypt_ecb, padded_size

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

# getUploadUrl.media_type（协议原文：1 图片、2 视频、3 文件、4 语音）
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_FILE = 3
MEDIA_TYPE_VOICE = 4

# MessageItem.type
ITEM_TYPE_IMAGE = 2
ITEM_TYPE_FILE = 4
ITEM_TYPE_VIDEO = 5

# CDNMedia.encrypt_type：本实现只产生 AES-128-ECB
ENCRYPT_TYPE_AES_128_ECB = 1

MEDIA_TYPE_BY_KIND = {
    "image": MEDIA_TYPE_IMAGE,
    "video": MEDIA_TYPE_VIDEO,
    "file": MEDIA_TYPE_FILE,
}

# 纯 Python AES 的吞吐是硬约束。给一个默认上限，是为了让"发一个 300 MB 的文件"
# 在几秒钟内以一条可读错误结束，而不是把主循环卡住几十分钟且没有任何输出。
DEFAULT_MAX_MEDIA_BYTES = 32 * 1024 * 1024


# --------------------------------------------------------------------------
# 媒体类型识别
# --------------------------------------------------------------------------

_EXT_KIND = {
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".gif": "image",
    ".bmp": "image", ".webp": "image", ".tif": "image", ".tiff": "image",
    ".heic": "image", ".avif": "image",
    ".mp4": "video", ".mov": "video", ".m4v": "video", ".avi": "video",
    ".mkv": "video", ".webm": "video", ".3gp": "video",
}

# 魔数优先于扩展名：扩展名可以随便改，字节不会说谎。
_FIXED_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image"),
    (b"\xff\xd8\xff", "image"),          # JPEG
    (b"GIF87a", "image"),
    (b"GIF89a", "image"),
    (b"BM", "image"),                    # BMP（两个字节，弱匹配，故排在 PNG/JPEG 之后）
    (b"II*\x00", "image"),               # TIFF (little-endian)
    (b"MM\x00*", "image"),               # TIFF (big-endian)
    (b"\x1aE\xdf\xa3", "video"),         # Matroska / WebM
)

# ISO-BMFF：第 4~8 字节是 "ftyp"，第 8~12 字节是 brand，决定它是图片还是视频
_ISOBMFF_IMAGE_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"avif"}


def detect_kind(filename: str, data: bytes = b"") -> str:
    """判断一个媒体该按图片、视频还是文件（附件）发送。

    先看魔数、再看扩展名，都认不出来就当**文件附件**。
    宁可发成附件（对方能点开、能另存），也不要猜成图片：猜错的图片在微信里
    是一个打不开的灰框，而附件至少是可用的。
    """
    if data:
        for magic, kind in _FIXED_MAGIC:
            if data.startswith(magic):
                return kind
        if len(data) >= 12 and data[4:8] == b"ftyp":
            return "image" if data[8:12] in _ISOBMFF_IMAGE_BRANDS else "video"
        if len(data) >= 12 and data.startswith(b"RIFF"):
            sub = data[8:12]
            if sub == b"WEBP":
                return "image"
            if sub in (b"AVI ", b"AVI\x20"):
                return "video"
    return _EXT_KIND.get(os.path.splitext(filename or "")[1].lower(), "file")


# --------------------------------------------------------------------------
# 准备（算 MD5 / 生成密钥 / 加密）
# --------------------------------------------------------------------------


def _md5_hex(data: bytes) -> str:
    """协议要求明文 MD5。某些 FIPS 模式构建会禁掉 MD5，故带一次降级。"""
    try:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()  # type: ignore[call-arg]
    except TypeError:
        return hashlib.md5(data).hexdigest()


@dataclass
class PreparedMedia:
    """一份待上传的媒体：明文信息 + 加密结果 + 本次使用的密钥。

    三样东西刻意排除在 repr 之外：`aes_key`、`aes_key_hex`（同一把密钥的两种写法，
    只藏一种等于没藏）、`filekey`、`ciphertext`。这个对象很容易被顺手打进日志，
    密钥进日志就等于凭证泄漏（同 `credentials.masked()` 的道理）。
    """

    kind: str                       # image / video / file
    filename: str                   # 附件名（file 类型才真的用得上）
    raw_size: int                   # 明文字节数 → getUploadUrl.rawsize
    raw_md5: str                    # 明文 MD5（小写十六进制）→ rawfilemd5
    cipher_size: int                # 密文字节数 → getUploadUrl.filesize / *_size
    aes_key: bytes = field(repr=False)                    # 16 字节原始密钥
    aes_key_hex: str = field(default="", repr=False)      # 32 位十六进制 → getUploadUrl.aeskey
    filekey: str = field(default="", repr=False)          # 客户端生成的文件 key
    ciphertext: bytes = field(default=b"", repr=False)    # AES-128-ECB + PKCS#7

    @property
    def aes_key_b64(self) -> str:
        """CDNMedia.aes_key 的取值（base64(十六进制字符串)，不是 base64(原始密钥)）。"""
        return aes_key_for_media(self.aes_key)


def aes_key_for_media(key: bytes) -> str:
    """把 16 字节密钥编码成 CDNMedia.aes_key。

    必须是 **base64(32 位十六进制字符串)**，不是 base64(原始 16 字节)。
    下载端两种都收，但发送端给 raw 的后果是：对方看到一个灰色方块，
    服务端与客户端都不报错（这是社区里踩过的坑，协议原文也只说下载端兼容两种）。
    """
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须 16 字节，收到 {len(key)} 字节")
    return base64.b64encode(key.hex().encode("ascii")).decode("ascii")


def prepare_media(
    data: bytes,
    filename: str = "",
    *,
    kind: Optional[str] = None,
    aes_key: Optional[bytes] = None,
    filekey: Optional[str] = None,
) -> PreparedMedia:
    """把明文准备成可以上传的形态：算 MD5、生成密钥、AES-128-ECB 加密。

    aes_key / filekey 允许传入，只为用例能构造确定性输入；生产路径一律随机。
    """
    if not data:
        raise ValueError("空文件无法发送")
    resolved_kind = kind or detect_kind(filename, data)
    if resolved_kind not in MEDIA_TYPE_BY_KIND:
        raise ValueError(f"不支持的媒体类型：{resolved_kind}")

    key = aes_key if aes_key is not None else secrets.token_bytes(16)
    if len(key) != 16:
        raise ValueError(f"AES-128 密钥必须 16 字节，收到 {len(key)} 字节")

    ciphertext = encrypt_ecb(data, key)
    expected = padded_size(len(data))
    if len(ciphertext) != expected:   # AES 实现走偏时立刻炸，而不是发出一个灰框
        raise AssertionError(f"密文长度 {len(ciphertext)} != PKCS#7 期望 {expected}")

    return PreparedMedia(
        kind=resolved_kind,
        filename=os.path.basename(filename or "") or "media.bin",
        raw_size=len(data),
        raw_md5=_md5_hex(data),
        cipher_size=len(ciphertext),
        aes_key=key,
        aes_key_hex=key.hex(),
        filekey=filekey if filekey is not None else secrets.token_hex(16),
        ciphertext=ciphertext,
    )


# --------------------------------------------------------------------------
# 消息项与 URL 构造
# --------------------------------------------------------------------------


def build_media_item(
    prepared: PreparedMedia,
    *,
    encrypt_query_param: str,
    full_url: str = "",
    filename: str = "",
) -> dict:
    """构造 sendMessage 里的那一个 item。

    每种媒体类型放的"密文大小"字段名不一样（图片 mid_size / 视频 video_size），
    文件放的是**明文**大小（len，十进制字符串）—— 协议原文如此，不要想当然统一。
    """
    if not encrypt_query_param and not full_url:
        raise ValueError("媒体引用至少要有一个：encrypt_query_param 或 full_url")
    media = {
        "encrypt_query_param": encrypt_query_param,
        "aes_key": prepared.aes_key_b64,
        "encrypt_type": ENCRYPT_TYPE_AES_128_ECB,
    }
    if full_url:
        media["full_url"] = full_url

    if prepared.kind == "image":
        return {"type": ITEM_TYPE_IMAGE,
                "image_item": {"media": media, "mid_size": prepared.cipher_size}}
    if prepared.kind == "video":
        return {"type": ITEM_TYPE_VIDEO,
                "video_item": {"media": media, "video_size": prepared.cipher_size}}
    return {"type": ITEM_TYPE_FILE,
            "file_item": {"media": media,
                          "file_name": os.path.basename(filename or prepared.filename) or "file.bin",
                          "len": str(prepared.raw_size)}}


def build_upload_url(
    cdn_base: str,
    upload_param: str,
    filekey: str,
    upload_full_url: str = "",
) -> str:
    """上传地址：优先用服务端给的完整 URL，否则按协议拼。

    拼法依据协议原文：<cdn_base>/upload?encrypted_query_param=<encode>&filekey=<encode>。
    quote(safe="") 而不是 urlencode：这两个参数是 base64 系字符（含 + / =），
    必须全部转义，且空格不能变成 +。
    """
    full = (upload_full_url or "").strip()
    if full:
        return full
    if not upload_param:
        raise ValueError("服务端既没给 upload_full_url，也没给 upload_param")
    return (
        f"{cdn_base.rstrip('/')}/upload"
        f"?encrypted_query_param={urllib.parse.quote(str(upload_param), safe='')}"
        f"&filekey={urllib.parse.quote(str(filekey), safe='')}"
    )


@dataclass
class UploadedMedia:
    """上传完成后的媒体：准备结果 + 服务端发回的下载参数。"""

    prepared: PreparedMedia
    encrypt_query_param: str = field(repr=False)   # 等于下载凭证，不进 repr
    upload_url: str = ""
    full_url: str = ""

    def __repr__(self) -> str:
        """自定义 repr：默认的 dataclass repr 会把 `encrypt_query_param` 带出去，
        那串参数拿到就能下载这份媒体（相当于一次性凭证）。"""
        return (f"UploadedMedia(kind={self.prepared.kind!r}, "
                f"filename={self.prepared.filename!r}, "
                f"cipher_size={self.prepared.cipher_size}, "
                f"has_download_param={bool(self.encrypt_query_param)})")

    def item(self, filename: str = "") -> dict:
        """直接产出可以塞进 sendMessage 的 item。"""
        return build_media_item(
            self.prepared,
            encrypt_query_param=self.encrypt_query_param,
            full_url=self.full_url,
            filename=filename,
        )


# --------------------------------------------------------------------------
# 文件读取
# --------------------------------------------------------------------------


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _limit_of(max_bytes: Optional[int]) -> int:
    """把 max_bytes 规整成一个数字上限。

    语义写清楚，因为这类参数是"静默改变行为"的高发区：
      · None → 默认上限（DEFAULT_MAX_MEDIA_BYTES）
      · 0    → **显式不限制**（调用方自己承担卡住的风险）
      · 正数 → 该上限
      · 负数 → 报错（不猜"负数是不是想表达不限制"）
    """
    if max_bytes is None:
        return DEFAULT_MAX_MEDIA_BYTES
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError(f"max_bytes 不能为负（收到 {max_bytes}）；要取消限制请显式传 0")
    return limit


def _check_limit(actual: int, limit: int, what: str) -> None:
    if limit and actual > limit:
        raise ValueError(
            f"{what} {human_size(actual)} 超过上限 {human_size(limit)}。"
            f"纯 Python AES 加密吞吐有限，需要更大的文件请显式调大 max_bytes（0 = 不限制）。"
        )


def media_download_url(descriptor: dict, cdn_base: str) -> str:
    """入站媒体的**下载**地址：优先服务端给的 `full_url`，否则按协议拼。

    `<cdn_base>/download?encrypted_query_param=<encode>` —— 与上传的 `/upload` 对称。
    `quote(safe="")`：这个参数是 base64 系字符（含 `+ / =`），必须全部转义。
    """
    full = str(descriptor.get("url") or "").strip()
    if full:
        return full
    param = str(descriptor.get("encrypt_query_param") or "").strip()
    if not param:
        raise ValueError("媒体引用里既没有 full_url，也没有 encrypt_query_param")
    return (f"{cdn_base.rstrip('/')}/download"
            f"?encrypted_query_param={urllib.parse.quote(param, safe='')}")


def aes_key_from_b64(value: str) -> bytes:
    """`CDNMedia.aes_key` → 16 字节密钥。**下载端两种都收**（协议原文）。

    · base64(32 位十六进制字符串) —— 本实现发送时用的那种（`aes_key_for_media`）
    · base64(原始 16 字节)        —— 别的客户端会用
    只认一种的后果：对方发来的图**解不开**，而两端都不报错（社区踩过的坑）。
    """
    raw = base64.b64decode((value or "").strip(), validate=False)
    if len(raw) == 32:
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            pass
    if len(raw) == 16:
        return raw
    raise ValueError(f"aes_key 解不出 16 字节密钥（base64 后 {len(raw)} 字节）")


def decrypt_media(ciphertext: bytes, aes_key_b64: str) -> bytes:
    """CDN 密文 → 明文（AES-128-ECB + PKCS#7 去填充）。"""
    return decrypt_ecb(ciphertext, aes_key_from_b64(aes_key_b64))


def guess_media_ext(kind: str, data: bytes = b"", filename: str = "") -> str:
    """落盘用的扩展名：文件按原名，图片/视频按**magic**（服务端不给 mime）。"""
    name = os.path.basename(filename or "")
    if kind == "file" and "." in name:
        return os.path.splitext(name)[1].lower()[:12]
    head = bytes(data[:12])
    if head.startswith(b"\x89PNG"):
        return ".png"
    if head.startswith(b"\xff\xd8"):
        return ".jpg"
    if head.startswith(b"GIF8"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp":
        return ".mp4"
    return {"image": ".png", "video": ".mp4", "voice": ".silk", "file": ".bin"}.get(kind, ".bin")


def read_media_file(path: str, *, max_bytes: Optional[int] = None) -> Tuple[bytes, str]:
    """读一个本地媒体文件。

    max_bytes 的语义见 `_limit_of`。检查发生在**加密之前**，
    所以超限的文件不会被无谓地读进内存再加密好几分钟。
    """
    full = os.path.abspath(os.path.expanduser(str(path)))
    if not os.path.isfile(full):
        if os.path.isdir(full):
            raise ValueError(f"媒体路径是目录，不是文件：{full}")
        raise ValueError(f"媒体文件不存在：{full}")
    _check_limit(os.path.getsize(full), _limit_of(max_bytes), f"媒体文件（{os.path.basename(full)}）")
    with open(full, "rb") as fh:
        data = fh.read()
    return data, os.path.basename(full)


def as_media_bytes(
    source: Union[str, "os.PathLike[str]", bytes, bytearray],
    filename: str = "",
    *,
    max_bytes: Optional[int] = None,
) -> Tuple[bytes, str]:
    """把"路径或字节"统一成 (数据, 文件名)，并在这一处做统一的大小上限检查。"""
    if isinstance(source, (bytes, bytearray)):
        data = bytes(source)
        name = os.path.basename(filename or "") or "media.bin"
        _check_limit(len(data), _limit_of(max_bytes), "媒体数据")
        return data, name
    data, name = read_media_file(str(source), max_bytes=max_bytes)
    return data, os.path.basename(filename) if filename else name
