"""出站媒体（图片/文件）协议自检。

不需要联网、不需要扫码：起一个本地桩服务器同时扮演
  - Bot API（/ilink/bot/getuploadurl、/ilink/bot/sendmessage）
  - CDN（/c2c/upload）

最关键的一条断言在 [4]：**把桩服务器收到的密文用请求里声明的 aeskey 解回来，
必须与原始文件逐字节相同**。这一条同时钉住四件事 ——
加密算法、填充、aeskey 的编码、以及 rawsize/filesize 的自洽。
缺了它，其他字段全对也可能发出一个对方解不开的灰框，而且全程不报错。

运行：python tests/test_media.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from clawbot import ClawBotClient, ClawBotNode  # noqa: E402
from clawbot.aes import decrypt_ecb, encrypt_ecb, padded_size  # noqa: E402
from clawbot.media import (  # noqa: E402
    aes_key_for_media,
    aes_key_from_b64,
    build_media_item,
    build_upload_url,
    decrypt_media,
    detect_kind,
    guess_media_ext,
    media_download_url,
    prepare_media,
)
from clawbot.store import Credentials, StateStore, media_descriptor, parse_inbound  # noqa: E402

FAILURES: list = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-body" * 8


# --------------------------------------------------------------------------
# 桩服务器
# --------------------------------------------------------------------------


class StubState:
    def __init__(self) -> None:
        self.requests: list = []            # [(path, 小写头, body_bytes|None)]
        self.upload_param = "UPLOAD-PARAM-1"
        self.upload_full_url = ""           # 非空时 getUploadUrl 返回它（优先）
        self.upload_status = 200
        self.upload_param_header = "DOWNLOAD-PARAM-1"
        self.upload_missing_header_times = 0  # 前 N 次故意不带 x-encrypted-param
        self.upload_error_times = 0           # 前 N 次返回 upload_status
        self.upload_attempts = 0
        self.getuploadurl_ret = 0
        self.getuploadurl_payload = None      # 覆写响应（测缺字段）
        self.message_id_counter = 700000
        self.send_ret = 0


STUB = StubState()


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音
        pass

    # -- 读取 -------------------------------------------------------------

    def _raw_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, body) -> None:
        STUB.requests.append(
            (self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )

    # -- 响应 -------------------------------------------------------------

    def _reply_json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_empty(self, code: int = 200, extra_headers: dict | None = None) -> None:
        self.send_response(code)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- 路由 -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        # 路由规则：只有这两个是 Bot API，其余 POST 一律当 CDN 上传
        # （这样 upload_full_url 指向任意路径也能被正确接住）
        if not (path.endswith("/getuploadurl") or path.endswith("/sendmessage")):
            body = self._raw_body()
            self._record(body)
            STUB.upload_attempts += 1
            if STUB.upload_error_times > 0:
                STUB.upload_error_times -= 1
                self._reply_empty(STUB.upload_status)
                return
            if STUB.upload_missing_header_times > 0:
                STUB.upload_missing_header_times -= 1
                self._reply_empty(200)          # 200 但没有 x-encrypted-param
                return
            self._reply_empty(200, {"x-encrypted-param": STUB.upload_param_header})
            return

        body = self._raw_body()
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {"__raw__": body.decode("utf-8", "replace")}
        self._record(parsed)

        if path.endswith("/getuploadurl"):
            if STUB.getuploadurl_payload is not None:
                self._reply_json(STUB.getuploadurl_payload)
                return
            payload = {"ret": STUB.getuploadurl_ret}
            if STUB.getuploadurl_ret != 0:
                payload["errmsg"] = "stub refuses"
                self._reply_json(payload)
                return
            payload["upload_param"] = STUB.upload_param
            if STUB.upload_full_url:
                payload["upload_full_url"] = STUB.upload_full_url
            self._reply_json(payload)
        elif path.endswith("/sendmessage"):
            if STUB.send_ret != 0:
                self._reply_json({"ret": STUB.send_ret, "errmsg": "stub send failure"})
                return
            STUB.message_id_counter += 1
            self._reply_json({"ret": 0, "errmsg": "", "message_id": STUB.message_id_counter})
        else:
            self._reply_json({"error": "unknown"}, 404)


def start_stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def requests_of(suffix: str) -> list:
    return [r for r in STUB.requests if r[0].split("?")[0].endswith(suffix)]


def make_node(tmp: str, base_url: str) -> ClawBotNode:
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    node = ClawBotNode(state_dir=tmp)
    node.client = ClawBotClient(
        creds=store.load_credentials(), api_base=base_url, cdn_base=f"{base_url}/c2c"
    )
    return node


def reset_stub() -> None:
    STUB.requests.clear()
    STUB.upload_attempts = 0
    STUB.upload_missing_header_times = 0
    STUB.upload_error_times = 0
    STUB.upload_status = 200
    STUB.upload_param = "UPLOAD-PARAM-1"
    STUB.upload_param_header = "DOWNLOAD-PARAM-1"
    STUB.upload_full_url = ""
    STUB.getuploadurl_ret = 0
    STUB.getuploadurl_payload = None
    STUB.send_ret = 0


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_pure_functions() -> None:
    print("\n[1] 密钥编码、媒体类型识别、URL 拼装（纯函数）")

    key = bytes(range(16))
    encoded = aes_key_for_media(key)
    check("aes_key 是 base64(十六进制字符串)，不是 base64(原始密钥)",
          base64.b64decode(encoded) == key.hex().encode("ascii"), encoded)
    check("aes_key 解码后是 32 个字符", len(base64.b64decode(encoded)) == 32)

    try:
        aes_key_for_media(b"\x00" * 15)
        check("非 16 字节密钥被拒", False, "没有抛异常")
    except ValueError:
        check("非 16 字节密钥被拒", True)

    check("PNG 魔数识别为图片", detect_kind("noext", PNG) == "image")
    check("JPEG 魔数识别为图片", detect_kind("x.bin", b"\xff\xd8\xff\xe0abc") == "image")
    check("HEIC 是图片（ftyp 品牌）",
          detect_kind("x", b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00") == "image")
    check("mp4 是视频", detect_kind("x", b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00") == "video")
    check("按扩展名兜底", detect_kind("a.WEBP", b"") == "image")
    check("认不出来按文件附件（不猜成图片）", detect_kind("mystery.dat", b"\x01\x02\x03") == "file")

    fallback = build_upload_url("https://cdn/c2c", "a+b/c=", "ff00", "")
    check("upload_param 里的 + / = 被完整转义",
          "encrypted_query_param=a%2Bb%2Fc%3D" in fallback and "filekey=ff00" in fallback,
          fallback)
    check("upload_full_url 优先",
          build_upload_url("https://cdn/c2c", "P", "F", "https://x/up") == "https://x/up")
    try:
        build_upload_url("https://cdn/c2c", "", "F", "")
        check("两个地址都没有时报错", False, "没有抛异常")
    except ValueError:
        check("两个地址都没有时报错", True)

    video_item = build_media_item(
        prepare_media(b"\x00\x00\x00\x18ftypisom" + b"v" * 40, "v.mp4", kind="video"),
        encrypt_query_param="Q",
    )
    check("视频 item 用 video_size（不是 mid_size）",
          video_item["type"] == 5 and "video_size" in video_item["video_item"])


def test_prepare() -> None:
    print("\n[2] 准备阶段自洽性")
    data = os.urandom(5000)
    prepared = prepare_media(data, "shot.png", kind="image")

    check("raw_size 是明文长度", prepared.raw_size == len(data))
    check("raw_md5 是明文 MD5", prepared.raw_md5 == hashlib.md5(data).hexdigest())
    check("cipher_size = PKCS#7 填充后长度", prepared.cipher_size == padded_size(len(data)),
          f"{prepared.cipher_size} vs {padded_size(len(data))}")
    check("密文长度与声明一致", len(prepared.ciphertext) == prepared.cipher_size)
    check("密钥十六进制是 32 位", len(prepared.aes_key_hex) == 32)
    check("filekey 是 32 位十六进制", len(prepared.filekey) == 32)
    check("密文可用声明的密钥解回明文", decrypt_ecb(prepared.ciphertext, prepared.aes_key) == data)

    try:
        prepare_media(b"", "empty.bin")
        check("空文件被拒", False, "没有抛异常")
    except ValueError:
        check("空文件被拒", True)

    item = build_media_item(prepared, encrypt_query_param="Q")
    check("图片 item.type=2", item["type"] == 2)
    check("图片 item 带 mid_size=密文长度",
          item["image_item"]["mid_size"] == prepared.cipher_size)

    file_item = build_media_item(prepare_media(b"hello", "报告.txt", kind="file"),
                                encrypt_query_param="Q")
    check("文件 item.type=4", file_item["type"] == 4)
    check("文件 len 是**明文**长度的十进制字符串",
          file_item["file_item"]["len"] == "5", str(file_item["file_item"]["len"]))
    check("文件名保留", file_item["file_item"]["file_name"] == "报告.txt")


def test_upload_request_fields(base_url: str) -> None:
    print("\n[3] getUploadUrl 请求字段与上传地址")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)
    peer, token = "peer@im.wechat", "CTX-M"

    reset_stub()
    data = os.urandom(3000)
    result = node.send_image(peer, data, context_token=token, filename="shot.png")
    check("图片发送成功", result.ok, str(result))

    upload_asks = requests_of("getuploadurl")
    check("发起了一次 getUploadUrl", len(upload_asks) == 1, f"got={len(upload_asks)}")
    if upload_asks:
        path, headers, body = upload_asks[0]
        check("getUploadUrl 带鉴权头", headers.get("authorization") == "Bearer T")
        check("getUploadUrl 带 base_info", "base_info" in (body or {}))
        check("media_type=1（图片）", body.get("media_type") == 1, str(body.get("media_type")))
        check("to_user_id 正确", body.get("to_user_id") == peer)
        check("rawsize=明文长度", body.get("rawsize") == len(data))
        check("rawfilemd5=明文 MD5", body.get("rawfilemd5") == hashlib.md5(data).hexdigest())
        check("filesize=填充后长度", body.get("filesize") == padded_size(len(data)))
        check("no_need_thumb=true", body.get("no_need_thumb") is True)
        check("aeskey 是 32 位十六进制", len(body.get("aeskey") or "") == 32, str(body.get("aeskey")))
        check("filekey 是 32 位十六进制", len(body.get("filekey") or "") == 32)
        check("filesize == 上传用的密文长度", body.get("filesize") == padded_size(len(data)))

    uploads = requests_of("/c2c/upload")
    check("CDN 上传走的是拼出来的地址",
          uploads and "encrypted_query_param=UPLOAD-PARAM-1" in uploads[0][0]
          and "filekey=" in uploads[0][0], uploads[0][0] if uploads else "无")


def test_ciphertext_roundtrip(base_url: str) -> None:
    print("\n[4] 密文逐字节回解（最关键的一条）")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)
    peer, token = "peer@im.wechat", "CTX-M"

    reset_stub()
    data = os.urandom(12345)
    result = node.send_image(peer, data, context_token=token, filename="shot.png")
    check("发送成功", result.ok, str(result))

    upload_asks = requests_of("getuploadurl")
    uploads = requests_of("/c2c/upload")
    if upload_asks and uploads:
        declared = upload_asks[0][2]
        ciphertext = uploads[0][2]
        check("上传体长度 == 声明的 filesize", len(ciphertext) == declared["filesize"],
              f"{len(ciphertext)} vs {declared['filesize']}")
        check("上传体不是明文（确实加密了）", ciphertext != data)
        key = bytes.fromhex(declared["aeskey"])
        check("用请求里声明的 aeskey 能解回原始字节",
              decrypt_ecb(ciphertext, key) == data)
        check("上传头是 application/octet-stream",
              uploads[0][1].get("content-type") == "application/octet-stream",
              str(uploads[0][1].get("content-type")))
        check("上传不带 Bot 鉴权头（CDN 那条路不需要）",
              "authorization" not in uploads[0][1])
    else:
        check("拿到了上传请求与 getUploadUrl 请求", False)


def test_send_message_item(base_url: str) -> None:
    print("\n[5] sendMessage 的媒体 item 形状")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)
    peer, token = "peer@im.wechat", "CTX-ITEM"

    reset_stub()
    data = os.urandom(2048)
    result = node.send_image(peer, data, context_token=token, filename="shot.png")
    check("发送成功", result.ok, str(result))

    sends = requests_of("sendmessage")
    check("只发了一条消息", len(sends) == 1, f"got={len(sends)}")
    declared = requests_of("getuploadurl")[0][2]
    if sends:
        msg = sends[0][2]["msg"]
        items = msg["item_list"]
        check("item_list 里只有一项（微信一条消息一个 item）", len(items) == 1)
        item = items[0]
        media = item["image_item"]["media"]
        check("item.type=2", item["type"] == 2)
        check("encrypt_query_param 用的是 CDN 响应头里的下载参数",
              media["encrypt_query_param"] == "DOWNLOAD-PARAM-1",
              str(media.get("encrypt_query_param")))
        check("aes_key = base64(请求里那个十六进制密钥)",
              media["aes_key"] == base64.b64encode(declared["aeskey"].encode()).decode())
        check("encrypt_type=1（AES-128-ECB）", media["encrypt_type"] == 1)
        check("mid_size=密文长度", item["image_item"]["mid_size"] == declared["filesize"])
        check("context_token 原样回传", msg["context_token"] == token)
        check("message_type=2 / state=2", msg["message_type"] == 2 and msg["message_state"] == 2)

    # 文件附件
    reset_stub()
    tmp_file = os.path.join(tmp, "报告.txt")
    with open(tmp_file, "w", encoding="utf-8") as fh:
        fh.write("这是一个附件")
    result2 = node.send_file(peer, tmp_file, context_token=token)
    check("文件发送成功", result2.ok, str(result2))
    asks2 = requests_of("getuploadurl")
    check("文件用 media_type=3", asks2 and asks2[0][2]["media_type"] == 3,
          str(asks2[0][2].get("media_type")) if asks2 else "无")
    sends2 = requests_of("sendmessage")
    if sends2:
        item2 = sends2[0][2]["msg"]["item_list"][0]
        check("文件 item.type=4", item2["type"] == 4)
        check("file_name 是路径的 basename", item2["file_item"]["file_name"] == "报告.txt",
              str(item2["file_item"].get("file_name")))
        check("file_item 不带图片才有的字段",
              "mid_size" not in item2["file_item"] and "image_item" not in item2)


def test_upload_full_url_preferred(base_url: str) -> None:
    print("\n[6] 服务端给完整上传 URL 时优先用它")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)

    reset_stub()
    STUB.upload_full_url = f"{base_url}/custom-upload?token=abc"
    result = node.send_image("peer@im.wechat", b"x" * 100, context_token="CTX",
                             filename="a.png")
    check("发送成功", result.ok, str(result))
    uploads = requests_of("custom-upload")
    check("POST 打到了 upload_full_url", len(uploads) == 1, f"got={len(uploads)}")
    check("没有用到拼装地址", not requests_of("/c2c/upload"))


def test_upload_failures(base_url: str) -> None:
    print("\n[7] 上传失败路径：失败必须可见，且不静默")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)
    peer, token = "peer@im.wechat", "CTX-F"

    # 1) 200 但缺 x-encrypted-param：先缺两次，第三次补上 → 应重试成功
    reset_stub()
    STUB.upload_missing_header_times = 2
    r1 = node.send_image(peer, b"y" * 500, context_token=token, filename="a.png")
    check("缺少下载参数时会重试并最终成功", r1.ok, str(r1))
    check("尝试了 3 次上传", STUB.upload_attempts == 3, f"attempts={STUB.upload_attempts}")

    # 2) 一直缺 → 返回 ok=False，stage=upload，且**不发 sendMessage**（不能发半条消息）
    reset_stub()
    STUB.upload_missing_header_times = 99
    r2 = node.send_image(peer, b"z" * 500, context_token=token, filename="a.png")
    check("一直缺少下载参数时 ok=False", r2.ok is False, str(r2))
    check("失败阶段标为 upload", r2.stage == "upload", r2.stage)
    check("失败原因可读", "x-encrypted-param" in r2.errmsg, r2.errmsg)
    check("上传尝试次数有限（3 次）", STUB.upload_attempts == 3, f"attempts={STUB.upload_attempts}")
    check("上传失败时不发消息", not requests_of("sendmessage"))

    # 3) HTTP 4xx 是确定性失败，不重试
    reset_stub()
    STUB.upload_error_times = 99
    STUB.upload_status = 403
    r3 = node.send_image(peer, b"w" * 500, context_token=token, filename="a.png")
    check("HTTP 4xx 返回 ok=False", r3.ok is False, str(r3))
    check("HTTP 4xx 不重试（只 1 次）", STUB.upload_attempts == 1, f"attempts={STUB.upload_attempts}")

    # 4) HTTP 5xx 会重试
    reset_stub()
    STUB.upload_error_times = 99
    STUB.upload_status = 503
    node.send_image(peer, b"w" * 500, context_token=token, filename="a.png")
    check("HTTP 5xx 重试 3 次", STUB.upload_attempts == 3, f"attempts={STUB.upload_attempts}")

    # 5) getUploadUrl 业务失败 → 不进上传
    reset_stub()
    STUB.getuploadurl_ret = -3
    r5 = node.send_image(peer, b"q" * 100, context_token=token, filename="a.png")
    check("getUploadUrl ret!=0 时 ok=False", r5.ok is False, str(r5))
    check("getUploadUrl 失败后不上传", STUB.upload_attempts == 0)

    # 6) getUploadUrl 既不返回 upload_param 也不返回 upload_full_url
    reset_stub()
    STUB.getuploadurl_payload = {"ret": 0}
    r6 = node.send_image(peer, b"q" * 100, context_token=token, filename="a.png")
    check("没有上传地址时 ok=False", r6.ok is False, str(r6))
    check("没有上传地址时不上传", STUB.upload_attempts == 0)

    # 7) 图片发出去但 sendMessage 失败 → stage 是 send，不是 upload
    reset_stub()
    STUB.send_ret = -1
    client_send = node.client.send_image(peer, b"m" * 300, token, filename="a.png", retries=0)
    check("sendMessage 失败时 ok=False", client_send.ok is False, str(client_send))
    check("sendMessage 失败时 stage=send", client_send.stage == "send", client_send.stage)
    check("上传确实成功过", STUB.upload_attempts == 1, f"attempts={STUB.upload_attempts}")


def test_node_guards(base_url: str) -> None:
    print("\n[8] 节点层的读取、上限与会话令牌校验")
    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    node = make_node(tmp, base_url)
    peer, token = "peer@im.wechat", "CTX-G"

    # 没有 context_token：不猜、不发、不联网
    reset_stub()
    r1 = node.send_image("stranger@im.wechat", b"a" * 100, filename="a.png")
    check("没有会话令牌时 ok=False", r1.ok is False, str(r1))
    check("没有会话令牌时不发任何请求", not STUB.requests)

    # 文件不存在：stage=read，且不联网
    reset_stub()
    r2 = node.send_file(peer, os.path.join(tmp, "不存在.txt"), context_token=token)
    check("文件不存在时 ok=False", r2.ok is False, str(r2))
    check("失败阶段标为 read", r2.stage == "read", r2.stage)
    check("读取失败时不发任何请求", not STUB.requests)

    # 大小上限：在加密之前就拒绝
    big = os.path.join(tmp, "big.bin")
    with open(big, "wb") as fh:
        fh.write(b"0" * 4096)
    reset_stub()
    r3 = node.send_file(peer, big, context_token=token, max_bytes=1024)
    check("超过 max_bytes 时 ok=False", r3.ok is False, str(r3))
    check("上限提示里带上了两个大小",
          "KiB" in r3.errmsg and "4.0 KiB" in r3.errmsg, r3.errmsg)
    check("超限时不发任何请求", not STUB.requests)

    # caption：先一条文本，再一条媒体
    reset_stub()
    r4 = node.send_image(peer, PNG, context_token=token, filename="x.png", caption="看这个")
    check("带 caption 发送成功", r4.ok, str(r4))
    sends = requests_of("sendmessage")
    check("caption 与媒体是两条独立消息", len(sends) == 2, f"got={len(sends)}")
    if len(sends) == 2:
        first = sends[0][2]["msg"]["item_list"][0]
        second = sends[1][2]["msg"]["item_list"][0]
        check("第一条是文本、内容为 caption",
              first["type"] == 1 and first["text_item"]["text"] == "看这个")
        check("第二条是图片", second["type"] == 2)

    # 直接传字节也支持，且 kind 自动识别
    reset_stub()
    r5 = node.send_media(peer, PNG, context_token=token)
    check("传字节也能发（自动识别为图片）", r5.ok, str(r5))
    asks = requests_of("getuploadurl")
    check("自动识别走 media_type=1", asks and asks[0][2]["media_type"] == 1)

    # max_bytes 的语义要显式钉住：0 = 不限制、负数 = 报错。
    # 旧实现里 0 与负数都"静默等于不限制"，等于这个保护可以被无声关掉。
    reset_stub()
    r6 = node.send_file(peer, big, context_token=token, max_bytes=0)
    check("max_bytes=0 = 不限制（显式关闭保护）", r6.ok, str(r6))
    reset_stub()
    r7 = node.send_file(peer, big, context_token=token, max_bytes=-1)
    check("max_bytes 为负时明确报错而不是静默放行",
          r7.ok is False and "不能为负" in r7.errmsg, str(r7))
    check("负数在上传之前就被拦下", not STUB.requests)


def test_credentials_lazy_load(base_url: str) -> None:
    """节点没跑过 run() 时也必须能发。

    这条是真机实测踩到的：`node.run()` / `node.login()` 才会把磁盘凭证装进
    协议客户端，而命令行 `main.py send`（以及把节点当库用的上层）根本不跑主循环 ——
    结果磁盘上明明有有效凭证，发送却报"尚未登录：没有 bot_token"。
    """
    print("\n[9] 凭证懒加载（不跑 run() 也能发）")
    peer = "peer@im.wechat"

    tmp = tempfile.mkdtemp(prefix="clawbot-media-")
    store = StateStore(tmp)
    store.save_credentials(Credentials(bot_token="T", ilink_bot_id="B", baseurl=base_url))
    store.remember_context(peer, "CTX-LAZY")
    fresh = ClawBotNode(state_dir=tmp)
    fresh.client.cdn_base = f"{base_url}/c2c"
    check("构造后 client 手上确实没有凭证（复现现场）", not fresh.client.creds.is_valid)

    reset_stub()
    r1 = fresh.send_image(peer, PNG, filename="x.png")
    check("未跑 run() 也能从磁盘懒加载凭证并发出", r1.ok, str(r1))
    check("懒加载后请求带上了 Authorization",
          any(h.get("authorization") == "Bearer T" for _, h, _ in requests_of("getuploadurl")))
    check("懒加载后 baseurl 已采用凭证里的值",
          fresh.client.api_base == base_url, fresh.client.api_base)

    # 完全没有凭证时：可读失败，且不联网
    empty = ClawBotNode(state_dir=tempfile.mkdtemp(prefix="clawbot-media-"))
    reset_stub()
    r2 = empty.send_image(peer, PNG, filename="x.png", context_token="CTX")
    check("完全没有凭证时 ok=False", r2.ok is False, str(r2))
    check("完全没凭证时提示重新登录", "登录" in r2.errmsg, r2.errmsg)
    check("完全没凭证时不发任何请求", not STUB.requests)


# ---------------------------------------------------------------------------
# [7] 入站媒体：取回来、解密、落盘、写进收件箱
#     用户 2026-10-16：「实测：入站图片只有 kind，没有 url/路径」—— 实测收件箱里只有 kind，
#     没有 URL/文件 id/本地路径，磁盘上也没有图（INTERFACE.md §8 原把它列为"不承诺"）。
# ---------------------------------------------------------------------------
def _raises(fn) -> bool:
    try:
        fn()
    except Exception:                                 # noqa: BLE001
        return True
    return False


def test_sent_record() -> None:
    """**发出流水**：`clawbot send` 每次都要往 `<state>/sent.jsonl` 追加一条。

    ★ 为什么值得测：第一版把属性名写成 `self.state_dir`（实际是 `self.dir`），
      又被 `except Exception: pass` 吞掉 ⇒ 流水一直空着却没人知道，
      于是"回复到底发出去了没有"依然查不到（用户 2026-10-16 的原问题）。
      这条测试就是那次静默失效的守门。
    """
    import json as _json
    import tempfile

    from clawbot.store import StateStore

    d = tempfile.mkdtemp(prefix="qyn-sent-")
    store = StateStore(d)
    store.append_sent({"ts": 1.0, "ok": True, "peer_id": "p", "message_id": 42,
                       "stage": "send", "attempts": 1})
    store.append_sent({"ts": 2.0, "ok": False, "stage": "upload", "error": "boom"})
    path = os.path.join(d, "sent.jsonl")
    assert os.path.isfile(path), "sent.jsonl 没落盘（append_sent 又静默失效了？）"
    rows = [_json.loads(l) for l in open(path, encoding="utf-8").read().splitlines() if l.strip()]
    assert len(rows) == 2, rows
    assert rows[0]["ok"] is True and rows[0]["message_id"] == 42, rows[0]
    assert rows[1]["ok"] is False and rows[1]["error"] == "boom", rows[1]


def test_inbound_media() -> None:
    print("\n[7] 入站媒体：下载 / 解密 / 落盘")
    key = bytes(range(16))
    hex_form = aes_key_for_media(key)                  # base64(32 位十六进制) —— 本实现发送时用的
    raw_form = base64.b64encode(key).decode("ascii")   # base64(原始 16 字节) —— 别的客户端会这么发

    # ① aes_key 两种编码都收（协议原文：下载端两种都收；只认一种 ⇒ 对方发来的图解不开且两端不报错）
    check("aes_key：base64(十六进制) 解出原密钥", aes_key_from_b64(hex_form) == key)
    check("aes_key：base64(原始 16 字节) 也认", aes_key_from_b64(raw_form) == key)
    check("aes_key：解不出 16 字节时**报错**（不静默给错密钥）", _raises(lambda: aes_key_from_b64("!!!")))

    # ② 下载地址：优先 full_url；否则拼 <cdn>/download 并把参数整体转义
    check("下载地址：有 full_url 就用它",
          media_download_url({"url": "https://x/y"}, "https://cdn") == "https://x/y")
    u = media_download_url({"encrypt_query_param": "a+b/c="}, "https://cdn/")
    check("下载地址：拼 <cdn>/download 且转义 + / =",
          u == "https://cdn/download?encrypted_query_param=a%2Bb%2Fc%3D", u)

    # ③ 与出站那条对称：**把密文解回来必须与明文逐字节相同**
    cipher = encrypt_ecb(PNG, key)
    check("解密：CDN 密文 → 明文（AES-128-ECB + PKCS#7）", decrypt_media(cipher, hex_form) == PNG)
    check("解密：用错密钥要报错（不产出垃圾字节）",
          _raises(lambda: decrypt_media(cipher, aes_key_for_media(bytes(16)))))

    # ④ 从真实 item 形状里取引用（发送侧 build_media_item 构造的就是这个结构）
    item = {"type": 2, "image_item": {"media": {
        "full_url": "https://cdn/download?encrypted_query_param=Z",
        "aes_key": hex_form, "encrypt_type": 1}, "mid_size": len(cipher)}}
    desc = media_descriptor(item, "image")
    check("解析：从 image_item.media 取出 url/aes_key/size",
          desc.get("url", "").endswith("Z") and desc.get("aes_key") == hex_form
          and desc.get("size") == len(cipher), str(desc))
    check("解析：纯文本 item 不产出媒体引用",
          media_descriptor({"type": 1, "text_item": {"text": "x"}}, "text") == {})
    check("解析：畸形 item 不抛异常（入站消息是外部输入）",
          media_descriptor({"type": 2, "image_item": "垃圾"}, "image") == {})

    # ⑤ 扩展名按 magic 猜（服务端不给 mime）
    check("扩展名：PNG/JPG 按 magic 认出来",
          guess_media_ext("image", PNG) == ".png"
          and guess_media_ext("image", b"\xff\xd8\xff\xe0") == ".jpg")

    # ⑥ 落盘 + 记录字段：把 _get_binary 换掉，不碰网络
    import clawbot.client as C
    tmp = tempfile.mkdtemp(prefix="clawbot-inbound-")
    msg = parse_inbound({"message_id": 9001, "from_user_id": "p@im.wechat", "seq": 1,
                         "item_list": [{"type": 2, "image_item": {"media": {
                             "full_url": "https://cdn/x", "aes_key": hex_form}}}]})
    check("解析：parse_inbound 把媒体引用带出来", bool(msg.media) and msg.kind == "image",
          str(msg.media))
    saved = C._get_binary
    try:
        C._get_binary = lambda url, timeout=0, max_bytes=0: cipher
        node = ClawBotNode(state_dir=tmp, media_dir=os.path.join(tmp, "media"))
        fields = node._fetch_media(msg)
        path = fields.get("media_path", "")
        check("落盘：文件写出且**字节与明文相同**",
              bool(path) and os.path.exists(path) and open(path, "rb").read() == PNG, str(fields))
        check("落盘：记录里带 media_bytes / media_kind",
              fields.get("media_bytes") == len(PNG) and fields.get("media_kind") == "image",
              str(fields))
        # ⑦ 失败降级：图取不下来，消息也必须能进收件箱（写 media_error，不抛）
        def boom(url, timeout=0, max_bytes=0):
            raise RuntimeError("CDN 挂了")
        C._get_binary = boom
        f2 = node._fetch_media(msg)
        check("失败降级：写 media_error 而不是抛异常（消息仍入收件箱）",
              "media_error" in f2 and "media_path" not in f2, str(f2))
        # ⑧ 关掉落盘（media_dir=None）⇒ 完全不动网络
        C._get_binary = boom
        check("未配 media_dir 时不去取（只留引用）",
              ClawBotNode(state_dir=tmp). _fetch_media(msg) == {})
    finally:
        C._get_binary = saved


def main() -> int:
    server = start_stub()
    base_url = f"http://127.0.0.1:{server.server_port}"
    print(f"桩服务器：{base_url}")
    try:
        test_pure_functions()
        test_prepare()
        test_upload_request_fields(base_url)
        test_ciphertext_roundtrip(base_url)
        test_send_message_item(base_url)
        test_upload_full_url_preferred(base_url)
        test_upload_failures(base_url)
        test_node_guards(base_url)
        test_credentials_lazy_load(base_url)
        test_sent_record()
        test_inbound_media()
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
