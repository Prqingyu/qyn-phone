"""iLink Bot（微信 ClawBot）协议客户端 —— 零第三方依赖。

分两层：

  ClawBotClient   协议层：请求头、鉴权、扫码登录、收发消息
  ClawBotNode     节点层：长轮询主循环、游标续读、钩子接口、重连退避

上层只需要用 ClawBotNode：

    node = ClawBotNode(state_dir="state")
    @node.on_message
    def handle(msg: InboundMessage) -> str:
        return f"收到：{msg.text}"
    node.run()          # 阻塞主循环

协议依据：https://github.com/Tencent/openclaw-weixin 的 docs/protocol_zh_CN.md
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import random
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .media import (
    MEDIA_TYPE_BY_KIND,
    decrypt_media,
    guess_media_ext,
    media_download_url,
    PreparedMedia,
    UploadedMedia,
    as_media_bytes,
    build_upload_url,
    prepare_media,
)
from .store import Credentials, InboundMessage, StateLock, StateStore, parse_inbound

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

DEFAULT_API_BASE = "https://ilinkai.weixin.qq.com"
DEFAULT_BOT_TYPE = "3"
DEFAULT_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
# 入站媒体单件上限（防止一条消息把磁盘/内存吃掉）；超了记 media_error，不落盘
MAX_MEDIA_BYTES = 32 * 1024 * 1024

CHANNEL_VERSION = "0.1.0"   # 本实现的版本，进 base_info.channel_version
BOT_AGENT = "ClawBotNode"  # 仅供服务端日志归因，非鉴权凭证

# 会话暂停错误码：收到后该账号会话暂停 1 小时
ERR_SESSION_PAUSED = -14
SESSION_PAUSE_SECONDS = 3600

# 单次 HTTP 请求默认超时（长轮询要大于服务端 hold 时间）
DEFAULT_TIMEOUT = 40.0

# CDN 上传超时：传的是一整个文件，不能沿用 API 的 40 秒
DEFAULT_CDN_TIMEOUT = 120.0

# 本文件位于 <项目根>/clawbot/client.py，向上一级即项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_state_dir() -> str:
    """默认状态目录：项目根下的 state/。

    刻意返回**绝对路径**。相对路径（如 "state"）会跟着当前工作目录漂移：
    从仓库根启动、被常驻进程 拉起、被别的程序 import —— CWD 各不相同，
    结果是"明明登录过却找不到凭证、要求重新扫码"。
    """
    return os.path.join(PROJECT_ROOT, "state")


def resolve_state_dir(state_dir: Optional[str]) -> str:
    """把调用方给的状态目录规整成绝对路径。

    · None          → 项目根/state
    · 绝对路径       → 原样使用（部署时可指到 /tmp/qyn-state-test）
    · 相对路径       → 相对**项目根**，而不是 CWD
    """
    if not state_dir:
        return default_state_dir()
    if os.path.isabs(state_dir):
        return state_dir
    return os.path.join(PROJECT_ROOT, state_dir)


class ClawBotError(RuntimeError):
    """协议层错误。

    transient 是这个错误**自带的**重试倾向：True=值得重试、False=重试没用、None=没判定。
    以前只靠字符串匹配判断（"HTTP 400 …: {…upstream HTTP 500…}" 这种错误体
    会让 4xx 被当成可重试），现在由抛出的地方直接说清楚。
    """

    def __init__(
        self,
        message: str,
        *,
        ret: Optional[int] = None,
        payload: Any = None,
        transient: Optional[bool] = None,
    ) -> None:
        super().__init__(message)
        self.ret = ret
        self.payload = payload
        self.transient = transient


class RequestTimeout(ClawBotError):
    """单次 HTTP 请求超时。

    在长轮询里这是**正常现象**（服务端 hold 到点就返回），不是故障 ——
    调用方要把它映射成"空结果"，而不是当成错误去退避。
    """


def _as_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """把服务端可能给成字符串/None/垃圾的数值字段转成 int。

    服务端返回什么类型不该让本地抛 ValueError：`send_*` 承诺"不抛异常"，
    而异常一旦冒泡出 `_pump_once`，游标就不落盘 → 整批消息下一轮被重复处理。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ret_failed(value: Any) -> bool:
    """`ret` / `errcode` 是否表示失败。

    三种情况必须分开（混起来就是"静默丢错"）：
      · 字段缺失 / 空串 → **成功**：`ret` 不是每个响应都有（协议原文如此）
      · 数字 → 只有 0 才是成功
      · 非数字垃圾 → **失败**：解析不了不等于成功，不能假装这一步过了
    """
    if value is None or value == "":
        return False
    return _as_int(value) != 0


def _encode_version(version: str) -> int:
    """版本号 → 协议要求的 0x00MMNNPP 十进制值。

    必须按"三段"补齐再拼：逐段左移的写法只在恰好三段时正确，
    `"1.0"` 会被算成 0x000001（0.0.1）而不是 0x010000（1.0.0）。
    """
    parts = (version or "").split(".")
    numbers = []
    for index in range(3):
        part = parts[index] if index < len(parts) else ""
        numbers.append(int(part) & 0xFF if part.isdigit() else 0)
    return (numbers[0] << 16) | (numbers[1] << 8) | numbers[2]


class SessionPaused(ClawBotError):
    """服务端要求暂停会话（ret/errcode = -14）。"""


class MediaDownloadError(ClawBotError):
    """入站媒体取不回来（网络/HTTP/解密）—— 消息本身仍然会进收件箱。"""


class MediaUploadError(ClawBotError):
    """CDN 上传失败。带 HTTP status 便于区分"确定性失败"与"值得重试"。"""

    def __init__(self, message: str, *, status: Optional[int] = None,
                 attempts: int = 1) -> None:
        super().__init__(message, transient=(status is None or status >= 500 or status == 429))
        self.status = status
        self.attempts = attempts


@dataclass
class SendResult:
    """一次发送的结果。

    stage 说明失败发生在哪一段：send（发消息）/ upload（CDN 上传）/ read（读本地文件）。
    媒体发送有三段，光看 ok=False 分不清是"传不上去"还是"发不出去"，
    而这两种情况的处理方式完全不同。
    """

    ok: bool
    message_id: Optional[int] = None
    ret: Optional[int] = None
    errmsg: str = ""
    attempts: int = 1
    stage: str = "send"

    def __bool__(self) -> bool:
        return self.ok

    def __str__(self) -> str:
        if self.ok:
            return f"已送达 message_id={self.message_id}（第 {self.attempts} 次尝试）"
        where = {"upload": "CDN 上传阶段", "read": "读取本地文件阶段"}.get(self.stage, "")
        prefix = f"{where}失败：" if where else ""
        return f"未送达 {prefix}ret={self.ret} errmsg={self.errmsg!r}（尝试 {self.attempts} 次）"


# --------------------------------------------------------------------------
# HTTP 小工具
# --------------------------------------------------------------------------

# 只走直连，**不读 http_proxy/https_proxy**。
# 为什么必须显式关掉：这台机器上 http_proxy 是给 apt 与局域网中继用的（DEV_WORKFLOW §7），
# 一旦被接收方继承，微信流量会静默走 Windows 中继 —— 中继一挂，节点"看着还在跑"
# 却什么都收不到；而且这种失败会被判成 transient（可重试）→ 无限重试、不报错。
# 管道要的是"要么通、要么明确失败"，所以代理必须显式断掉，而不是靠环境干净。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _b64_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 的十进制字符串再做 base64，每次请求都变。"""
    value = random.randint(0, 0xFFFFFFFF)
    return base64.b64encode(str(value).encode("ascii")).decode("ascii")


def _request(
    method: str,
    url: str,
    *,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        message = f"HTTP {exc.code} {exc.reason}"
        if detail:
            message = f"{message}: {detail}"
        # 瞬时性由状态码决定，不看响应体里出现了什么字
        raise ClawBotError(message, transient=exc.code >= 500 or exc.code == 429) from exc
    except urllib.error.URLError as exc:
        raise ClawBotError(f"网络错误: {exc.reason}", transient=True) from exc
    except (TimeoutError, socket.timeout) as exc:   # 3.9 上 socket.timeout 不是 TimeoutError
        raise RequestTimeout("请求超时", transient=True) from exc


def _is_transient(error: Any) -> bool:
    """判断一个错误是否值得重试。

    优先用结构化信息（错误对象自带的 transient / HTTP status），
    只有拿不到时才退回文本判断。文本判断有**已验证的**误判：
      · "HTTP 400 …: {"e":"upstream HTTP 500"}" → 被当成 5xx 可重试
      · "尚未登录：没有 bot_token"（本地确定性错误）→ 被当成可重试，白等 1.5 秒重发两次
    """
    flag = getattr(error, "transient", None)
    if flag is not None:
        return bool(flag)
    status = getattr(error, "status", None)
    if isinstance(status, int):
        return status >= 500 or status == 429
    text = error if isinstance(error, str) else str(error)
    if "网络错误" in text or "请求超时" in text:
        return True
    if "尚未登录" in text:
        return False
    if "HTTP " in text:
        return any(f"HTTP {code}" in text for code in range(500, 600)) or "HTTP 429" in text
    return True                   # 无法判断时按瞬时处理


def _json_request(method: str, url: str, **kwargs) -> Dict[str, Any]:
    text = _request(method, url, **kwargs)
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClawBotError(f"响应不是合法 JSON: {text[:200]}") from exc


def _get_binary(url: str, *, timeout: float, max_bytes: int = 0) -> bytes:
    """从 CDN 取**密文**（与 `_post_binary` 对称：不看 JSON、只看字节）。

    与 Bot API 那条路不同：不走鉴权头。`max_bytes` 给了就设上限，超了报错 ——
    入站媒体是**外部输入**，一条超大的消息不该把内存/磁盘吃掉。
    """
    req = urllib.request.Request(url, headers={"Accept": "*/*"}, method="GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1) if max_bytes else resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:                             # noqa: BLE001
            pass
        message = f"HTTP {exc.code} {exc.reason}"
        if detail:
            message = f"{message}: {detail}"
        raise MediaDownloadError(message, status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise MediaDownloadError(f"网络错误: {exc.reason}", status=None) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MediaDownloadError("下载超时", status=None) from exc
    if max_bytes and len(data) > max_bytes:
        raise MediaDownloadError(f"媒体超过上限（{len(data)} > {max_bytes} 字节）")
    return data


def _post_binary(url: str, body: bytes, *, timeout: float) -> tuple:
    """把密文 POST 给 CDN，返回 (status, 小写响应头, 响应体)。

    这条路径与 Bot API 不同：不走鉴权头、不要 JSON、也不看响应体 ——
    成功与否只由 HTTP 200 + 响应头 `x-encrypted-param` 决定（协议原文如此）。
    """
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/octet-stream"},
        method="POST",
    )
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        if not detail:
            detail = exc.headers.get("x-error-message", "") if exc.headers else ""
        message = f"HTTP {exc.code} {exc.reason}"
        if detail:
            message = f"{message}: {detail}"
        raise MediaUploadError(message, status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise MediaUploadError(f"网络错误: {exc.reason}", status=None) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MediaUploadError("上传超时", status=None) from exc


# --------------------------------------------------------------------------
# 协议层
# --------------------------------------------------------------------------


@dataclass
class QRLoginSession:
    """一次扫码登录会话。"""

    qrcode: str
    qrcode_url: str
    base_url: str
    verify_code: str = ""
    pending_verify_code: bool = False


class ClawBotClient:
    """iLink 协议客户端。无状态设计，凭证由调用方传入/保存。"""

    def __init__(
        self,
        creds: Optional[Credentials] = None,
        *,
        api_base: str = DEFAULT_API_BASE,
        bot_type: str = DEFAULT_BOT_TYPE,
        cdn_base: str = DEFAULT_CDN_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        cdn_timeout: float = DEFAULT_CDN_TIMEOUT,
        debug: bool = False,
        qr_api_base: Optional[str] = None,
    ) -> None:
        self.creds = creds or Credentials()
        self.api_base = (self.creds.baseurl or api_base).rstrip("/")
        # 二维码接口的固定入口；默认与 api_base 一致，测试时可单独指向桩服务器
        self.qr_api_base = (qr_api_base or api_base).rstrip("/")
        self.bot_type = bot_type
        self.cdn_base = cdn_base.rstrip("/")
        self.timeout = timeout
        self.cdn_timeout = cdn_timeout
        # 长轮询超时会被服务端返回的 longpolling_timeout_ms 更新（见 get_updates）
        self._longpoll_timeout = timeout
        self.debug = debug

    # -- 内部 ---------------------------------------------------------------

    def _log(self, message: str) -> None:
        if self.debug:
            _log(message)

    def _headers(self, *, authed: bool, app_only: bool = False) -> Dict[str, str]:
        """构造请求头。

        分两类，不能混（协议原文对二维码状态轮询有明确要求）：

          鉴权 POST（app_only=False）：AuthorizationType 恒为 ilink_bot_token
            （所以申请二维码那一步即使还没有 token 也要带）、X-WECHAT-UIN、
            Content-Type；Authorization 只在真有 token 时出现。
          应用请求（app_only=True，二维码状态轮询）：**只有** iLink-App-Id 与
            iLink-App-ClientVersion，不带 AuthorizationType / Authorization /
            X-WECHAT-UIN —— 后两者是鉴权请求的头，混进来属于协议偏离。

        公共元数据 base_info 不是 HTTP 头，而是请求体里的 JSON 字段（见 _post）。
        """
        headers = {
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": str(_encode_version(CHANNEL_VERSION)),
            "User-Agent": f"{BOT_AGENT}/{CHANNEL_VERSION}",
        }
        if app_only:
            return headers
        headers["Content-Type"] = "application/json"
        headers["X-WECHAT-UIN"] = _b64_uin()
        headers["AuthorizationType"] = "ilink_bot_token"
        if authed and self.creds.bot_token:
            headers["Authorization"] = f"Bearer {self.creds.bot_token}"
        return headers

    def _base_info(self) -> Dict[str, str]:
        return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}

    def _post(self, endpoint: str, payload: Dict[str, Any], *, authed: bool = True,
              timeout: Optional[float] = None) -> Dict[str, Any]:
        if authed and not self.creds.bot_token:
            # transient=False：没登录是本地状态问题，重发两次也不会变好
            raise ClawBotError("尚未登录：没有 bot_token", transient=False)
    def _post(self, endpoint: str, payload: Dict[str, Any], *, authed: bool = True,
              timeout: Optional[float] = None, base: Optional[str] = None) -> Dict[str, Any]:
        if authed and not self.creds.bot_token:
            # transient=False：没登录是本地状态问题，重发两次也不会变好
            raise ClawBotError("尚未登录：没有 bot_token", transient=False)
        body = dict(payload)
        if authed:
            body.setdefault("base_info", self._base_info())
        url = f"{(base or self.api_base).rstrip('/')}/{endpoint.lstrip('/')}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return _json_request(
            "POST", url, headers=self._headers(authed=authed), body=data,
            timeout=timeout or self.timeout,
        )

    def _get(self, endpoint: str, params: Dict[str, str], *, authed: bool = False,
             timeout: Optional[float] = None, base: Optional[str] = None) -> Dict[str, Any]:
        query = urllib.parse.urlencode(params)
        url = f"{(base or self.api_base).rstrip('/')}/{endpoint.lstrip('/')}?{query}"
        # 应用请求头，不是鉴权请求头（见 _headers 的说明）
        headers = self._headers(authed=authed, app_only=True)
        return _json_request("GET", url, headers=headers, timeout=timeout or self.timeout)

    # -- 扫码登录 -----------------------------------------------------------

    def fetch_qr(self) -> QRLoginSession:
        """申请一张登录二维码。

        注意：二维码接口固定打 qr_api_base（默认腾讯正式域名），不会跟随 baseurl 变化。

        base 通过**参数**传下去，绝不临时改写 `self.api_base` —— 那是个可变全局，
        并发时会把别的线程正在发的请求（带着 `Authorization: Bearer <真 token>`）
        打到二维码那台主机上，等于把凭证送给非目标主机（端口级实测过）。
        """
        payload = self._post(
            f"ilink/bot/get_bot_qrcode?bot_type={self.bot_type}",
            {"local_token_list": []},
            authed=False,
            base=self.qr_api_base,
        )
        qrcode = payload.get("qrcode")
        if not qrcode:
            raise ClawBotError(f"未取得二维码: {payload}")
        return QRLoginSession(
            qrcode=str(qrcode),
            qrcode_url=str(payload.get("qrcode_img_content") or ""),
            base_url=self.qr_api_base,
        )

    def poll_qr_once(self, session: QRLoginSession, *, timeout: float = 35.0) -> Dict[str, Any]:
        """轮询一次扫码状态。网络错误按 wait 处理，交给调用方继续轮询。"""
        params = {"qrcode": session.qrcode}
        if session.verify_code:
            params["verify_code"] = session.verify_code
        try:
            return self._get("ilink/bot/get_qrcode_status", params, authed=False,
                             timeout=timeout, base=session.base_url)
        except ClawBotError as exc:
            self._log(f"扫码状态轮询失败（按 wait 继续）：{exc}")
            return {"status": "wait"}

    def login_with_qr(
        self,
        *,
        on_qr: Callable[[QRLoginSession], None],
        on_status: Optional[Callable[[str], None]] = None,
        ask_verify_code: Optional[Callable[[str], str]] = None,
        timeout_s: float = 480.0,
        max_refresh: int = 3,
    ) -> tuple:
        """完整扫码登录流程。返回 (Credentials, user_id)。

        on_qr          展示二维码（终端/图片/链接都行）
        ask_verify_code 需要手机显示的数字时回调，返回用户输入
        """
        session = self.fetch_qr()
        on_qr(session)

        deadline = time.time() + timeout_s
        refreshes = 0
        while time.time() < deadline:
            resp = self.poll_qr_once(session)
            status = str(resp.get("status") or "wait")
            if on_status:
                on_status(status)

            if status == "confirmed":
                if not resp.get("ilink_bot_id"):
                    raise ClawBotError("服务端确认登录但未返回 ilink_bot_id")
                creds = Credentials(
                    bot_token=str(resp.get("bot_token") or ""),
                    ilink_bot_id=str(resp.get("ilink_bot_id") or ""),
                    ilink_user_id=str(resp.get("ilink_user_id") or ""),
                    baseurl=str(resp.get("baseurl") or DEFAULT_API_BASE),
                    logged_in_at=time.time(),
                )
                if not creds.bot_token:
                    raise ClawBotError("服务端确认登录但未返回 bot_token")
                self.creds = creds
                self.api_base = (creds.baseurl or DEFAULT_API_BASE).rstrip("/")
                return creds, creds.ilink_user_id

            if status == "scaned":
                session.verify_code = ""
                session.pending_verify_code = False  # 服务端接受验证码后清除暂存
            elif status == "need_verifycode":
                if not ask_verify_code:
                    raise ClawBotError("服务端要求输入验证码，但未提供 ask_verify_code 回调")
                prompt = ("验证码不正确，请重新输入手机微信显示的数字："
                          if session.pending_verify_code else
                          "请输入手机微信显示的数字：")
                session.verify_code = ask_verify_code(prompt)
                session.pending_verify_code = True
                continue
            elif status == "verify_code_blocked":
                session.verify_code = ""
                session.pending_verify_code = False
                refreshes += 1
                if refreshes > max_refresh:
                    raise ClawBotError("验证码多次错误，登录流程已停止")
                session = self.fetch_qr()
                on_qr(session)
            elif status == "expired":
                refreshes += 1
                if refreshes > max_refresh:
                    raise ClawBotError("二维码多次失效，登录流程已停止")
                session = self.fetch_qr()
                on_qr(session)
            elif status == "scaned_but_redirect":
                host = resp.get("redirect_host")
                if host:
                    session.base_url = f"https://{host}"
                    self._log(f"按服务端要求切换到 {host}")
            elif status == "binded_redirect":
                raise ClawBotError("该微信已绑定到其它实例，请先在原实例解绑")

            time.sleep(1.0)
        raise ClawBotError("登录超时")

    # -- 收发 ---------------------------------------------------------------

    def get_updates(self, cursor: str, *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """长轮询收消息。返回值里 get_updates_buf 是下次要带的新游标。

        两件事不能想当然：

        · **超时不是故障**。服务端 hold 到点就返回，官方客户端把超时映射成"空结果"。
          当成错误处理的后果是：每次正常超时都被主循环计为故障并退避（1s→60s），
          越退越慢，看上去像"掉线了"。
        · **`errcode` 也要看**。协议说 monitor 检查非零 `ret` **或** `errcode`；
          只认 ret 的话，`{"ret":0,"errcode":-3}` 会被当成成功 → 主循环立刻再轮询，
          变成不打印原因的热循环。
        """
        try:
            resp = self._post(
                "ilink/bot/getupdates",
                {"get_updates_buf": cursor},
                timeout=timeout or self._longpoll_timeout,
            )
        except RequestTimeout:
            self._log("长轮询超时（正常，按空结果继续）")
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

        ret = resp.get("ret")
        errcode = resp.get("errcode")
        if ret == ERR_SESSION_PAUSED or errcode == ERR_SESSION_PAUSED:
            raise SessionPaused(
                "服务端要求暂停会话 1 小时（默认消息也可能到达）", ret=ERR_SESSION_PAUSED,
                payload=resp,
            )
        if _ret_failed(ret) or _ret_failed(errcode):
            raise ClawBotError(
                f"getUpdates 失败 ret={ret} errcode={errcode} errmsg={resp.get('errmsg')}",
                ret=_as_int(ret), payload=resp,
            )

        suggested = _as_int(resp.get("longpolling_timeout_ms"))
        if suggested and suggested > 0:
            # 采纳服务端建议，但夹在合理区间：太小会空转，太大则 stop()/Ctrl-C 要等很久
            self._longpoll_timeout = max(5.0, min(120.0, suggested / 1000.0))
            self._log(f"采纳服务端建议的长轮询超时 {suggested}ms → {self._longpoll_timeout:.1f}s")
        return resp

    def send_text(
        self,
        to_user_id: str,
        text: str,
        context_token: str,
        *,
        message_state: int = 2,
        client_id: Optional[str] = None,
        retries: int = 2,
    ) -> SendResult:
        """发文本。context_token 必须来自对应入站消息，否则会话关联不上。

        带重试：瞬时故障（网络错误/超时/HTTP 5xx）会重试 `retries` 次；
        确定性错误（缺令牌、HTTP 4xx）立刻返回失败，不做无谓重试。

        注意：返回的是 SendResult，**不抛异常**。调用方应检查 .ok ——
        发送失败必须可见，不能静默丢掉。
        """
        if not to_user_id:
            return SendResult(ok=False, errmsg="缺少 to_user_id")
        if not context_token:
            return SendResult(ok=False, errmsg="缺少 context_token（必须回传入站消息里的那个）")
        return self._send_item(
            to_user_id,
            [{"type": 1, "text_item": {"text": text}}],
            context_token,
            message_state=message_state,
            client_id=client_id,
            retries=retries,
        )

    def _send_item(
        self,
        to_user_id: str,
        item_list: List[Dict[str, Any]],
        context_token: str,
        *,
        message_state: int = 2,
        client_id: Optional[str] = None,
        retries: int = 2,
    ) -> SendResult:
        """发一条消息（item_list 可以只有一项）。

        文本与媒体走的是同一个 sendMessage 与同一套重试策略，
        差别只在 item_list 的内容 —— 所以两者共用这一处。
        返回 SendResult，**不抛异常**；调用方检查 .ok。
        """
        msg = {
            "from_user_id": "",
            "to_user_id": to_user_id,
            "client_id": client_id or f"qyn-{int(time.time() * 1000)}-{random.randint(0, 0xFFFF)}",
            "message_type": 2,       # 2 = BOT 发出
            "message_state": message_state,  # 0 新消息 / 1 生成中 / 2 完成
            "context_token": context_token,
            "item_list": item_list,
        }

        attempts = 0
        last_err = ""
        last_ret: Optional[int] = None
        while attempts <= max(0, retries):
            attempts += 1
            try:
                resp = self._post("ilink/bot/sendmessage", {"msg": msg})
            except ClawBotError as exc:
                last_err = str(exc)
                last_ret = exc.ret
                if not _is_transient(exc):
                    break
            else:
                raw_ret = resp.get("ret")
                if not _ret_failed(raw_ret):
                    result = SendResult(
                        ok=True,
                        message_id=_as_int(resp.get("message_id")),
                        ret=0,
                        attempts=attempts,
                    )
                    self._log(f"发送成功：{result}")
                    return result
                last_ret = _as_int(raw_ret)
                last_err = str(resp.get("errmsg") or "")
                if last_ret is None:
                    # ret 存在但不是数字：当作失败上报，绝不当成功
                    last_err = f"sendMessage 返回了无法解析的 ret={raw_ret!r}"
                    break
                if last_ret == ERR_SESSION_PAUSED:
                    # 会话已被服务端暂停：重发只会继续失败。
                    # 不做无谓重试，把 -14 原样交给节点去进暂停窗口。
                    break

            if attempts <= max(0, retries):
                delay = 0.5 * (2 ** (attempts - 1))
                self._log(f"发送失败（第 {attempts} 次，{last_err or last_ret}），{delay}s 后重试")
                time.sleep(delay)

        result = SendResult(ok=False, ret=last_ret, errmsg=last_err, attempts=attempts)
        _log(f"发送最终失败：{result}")  # 恒定输出，不依赖 debug
        return result

    # -- 媒体 ---------------------------------------------------------------

    def get_upload_url(self, prepared: PreparedMedia, *, to_user_id: str) -> Dict[str, Any]:
        """申请一次 CDN 上传参数。

        只接受 PreparedMedia、不接受散装字段：rawsize / rawfilemd5 / filesize / aeskey
        必须与**真实要传的密文**一致，而服务端不会替你校验 —— 错配的结果是对方收到
        一个解不开的灰框，且全程不报错。让类型系统挡住这种错配。
        """
        payload = {
            "filekey": prepared.filekey,
            "media_type": MEDIA_TYPE_BY_KIND[prepared.kind],
            "to_user_id": to_user_id,
            "rawsize": prepared.raw_size,
            "rawfilemd5": prepared.raw_md5,
            "filesize": prepared.cipher_size,
            "no_need_thumb": True,
            "aeskey": prepared.aes_key_hex,
        }
        resp = self._post("ilink/bot/getuploadurl", payload)
        ret = resp.get("ret")
        if ret not in (0, None):
            raise ClawBotError(f"getUploadUrl 失败 ret={ret} errmsg={resp.get('errmsg')}",
                               ret=int(ret), payload=resp)
        if not (resp.get("upload_full_url") or resp.get("upload_param")):
            raise ClawBotError(f"getUploadUrl 未返回上传地址：{resp}")
        return resp

    def upload_ciphertext(self, url: str, ciphertext: bytes, *, retries: int = 2) -> str:
        """把密文 POST 到 CDN，返回下载参数 `x-encrypted-param`。

        成功判定严格按协议：HTTP 200 **且** 该响应头非空。只有响应体 200 但没头，
        等于上传没落地，不能当成功 —— 所以这种情况也计入重试。
        与发送一样：瞬时可重试，HTTP 4xx 立即放弃（重试不会变好）。
        """
        attempts = 0
        last: Optional[Exception] = None
        while attempts <= max(0, retries):
            attempts += 1
            try:
                _status, headers, _body = _post_binary(url, ciphertext, timeout=self.cdn_timeout)
            except MediaUploadError as exc:
                last = exc
                if exc.status is not None and 400 <= exc.status < 500:
                    break
            else:
                param = headers.get("x-encrypted-param") or ""
                if param:
                    self._log(f"CDN 上传成功：{len(ciphertext)} 字节，第 {attempts} 次尝试")
                    return param
                last = MediaUploadError("CDN 返回 200 但缺少 x-encrypted-param 头")

            if attempts <= max(0, retries):
                delay = 0.5 * (2 ** (attempts - 1))
                self._log(f"上传失败（第 {attempts} 次，{last}），{delay}s 后重试")
                time.sleep(delay)

        raise MediaUploadError(f"CDN 上传失败（尝试 {attempts} 次）：{last}", attempts=attempts)

    def upload_media(
        self,
        data: bytes,
        filename: str = "",
        *,
        to_user_id: str,
        kind: Optional[str] = None,
        prepared: Optional[PreparedMedia] = None,
        retries: int = 2,
    ) -> UploadedMedia:
        """加密并上传一份媒体，返回可发送的引用。失败抛 ClawBotError。"""
        if not to_user_id:
            raise ClawBotError("缺少 to_user_id，无法申请上传")
        ready = prepared if prepared is not None else prepare_media(data, filename, kind=kind)
        resp = self.get_upload_url(ready, to_user_id=to_user_id)
        url = build_upload_url(
            self.cdn_base,
            str(resp.get("upload_param") or ""),
            ready.filekey,
            str(resp.get("upload_full_url") or ""),
        )
        param = self.upload_ciphertext(url, ready.ciphertext, retries=retries)
        return UploadedMedia(prepared=ready, encrypt_query_param=param, upload_url=url)

    def send_media(
        self,
        to_user_id: str,
        data: bytes,
        filename: str = "",
        context_token: str = "",
        *,
        kind: Optional[str] = None,
        prepared: Optional[PreparedMedia] = None,
        retries: int = 2,
    ) -> SendResult:
        """发一份媒体（图片/视频/文件）。

        与 send_text 一样不抛异常：上传或加密失败会返回 stage="upload" 的
        SendResult，并且**恒定打印一行日志** —— 静默失败是这里最糟的结果。
        """
        if not to_user_id:
            return SendResult(ok=False, errmsg="缺少 to_user_id")
        if not context_token:
            return SendResult(ok=False, errmsg="缺少 context_token（必须回传入站消息里的那个）")
        try:
            uploaded = self.upload_media(
                data, filename, to_user_id=to_user_id, kind=kind,
                prepared=prepared, retries=retries,
            )
        except (ClawBotError, ValueError) as exc:
            # attempts 取上传实际尝试次数：只写 1 会让人以为"一次就放弃了"
            result = SendResult(ok=False, errmsg=str(exc), stage="upload",
                                attempts=_as_int(getattr(exc, "attempts", None), 1) or 1)
            _log(f"媒体发送失败（上传阶段）：{result.errmsg}")
            return result
        return self._send_item(
            to_user_id, [uploaded.item(filename=filename)], context_token, retries=retries
        )

    def send_image(
        self,
        to_user_id: str,
        data: bytes,
        context_token: str = "",
        *,
        filename: str = "image.png",
        retries: int = 2,
    ) -> SendResult:
        """发一张图片。data 必须是**已编码好的图片字节**（PNG/JPEG/...），不是画布对象。"""
        return self.send_media(to_user_id, data, filename or "image.png", context_token,
                               kind="image", retries=retries)

    def send_file(
        self,
        to_user_id: str,
        data: bytes,
        filename: str,
        context_token: str = "",
        *,
        retries: int = 2,
    ) -> SendResult:
        """发一个文件附件。kind 故意不写死：附件就是附件，按类型表走会更稳。"""
        return self.send_media(to_user_id, data, filename, context_token,
                               kind="file", retries=retries)

    def get_config(self, context_token: str = "") -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ilink_user_id": self.creds.ilink_user_id}
        if context_token:
            payload["context_token"] = context_token
        return self._post("ilink/bot/getconfig", payload)

    def send_typing(self, ticket: str, status: int = 1) -> Dict[str, Any]:
        """status: 1 正在输入 / 2 取消。纯体验优化，失败可忽略。"""
        return self._post(
            "ilink/bot/sendtyping",
            {"ilink_user_id": self.creds.ilink_user_id, "typing_ticket": ticket, "status": status},
        )


# --------------------------------------------------------------------------
# 节点层
# --------------------------------------------------------------------------


class ClawBotNode:
    """一个"收发节点"：长轮询 + 钩子 + 断点续读。

    最小用法：

        node = ClawBotNode(state_dir="state")

        @node.on_message
        def handle(msg):
            return f"你说的是：{msg.text}"

        node.run()
    """

    def __init__(
        self,
        state_dir: Optional[str] = None,
        *,
        client: Optional[ClawBotClient] = None,
        debug: bool = False,
        media_dir: Optional[str] = None,
    ) -> None:
        # state_dir 省略时用项目根下的 state/，绝不依赖当前工作目录
        self.store = StateStore(resolve_state_dir(state_dir))
        self.debug = debug
        # ★ 入站媒体落到哪（None = 不落盘，只把引用写进收件箱）。用户 2026-10-16：
        #   微信发来的图片传不过来 —— 这里就是"取回来"的落点。
        self.media_dir = media_dir
        self.client = client or ClawBotClient(debug=debug)
        self._handler: Optional[Callable[[InboundMessage, "ClawBotNode"], Any]] = None
        self._running = False
        self._paused_until = 0.0
        self._peer_contexts: Dict[str, str] = {}
        self._cursor: str = ""

    # -- 钩子 ---------------------------------------------------------------

    def on_message(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """注册消息处理钩子。

        钩子签名可以是 `(msg)` 或 `(msg, node)`；返回值若是字符串，自动回发。
        想异步回复就返回 None，之后自己调 `node.send_text(peer, text)`。
        """
        self._handler = func
        return func

    # -- 入站媒体 -----------------------------------------------------------

    def _fetch_media(self, msg: InboundMessage) -> Dict[str, Any]:
        """取回这条消息带的媒体（CDN 密文 → AES 解密 → 落盘），返回要并进收件箱的字段。

        ★ **失败不抛**：收件箱是上层唯一的收消息路径，图取不下来也必须让这条消息落地 ——
          记录里写 `media_error`，上层能看见"有张图但没取到"，而不是整条消息消失。
        """
        desc = msg.media or {}
        if not desc or not self.media_dir:
            return {}
        out: Dict[str, Any] = {"media_kind": str(desc.get("kind") or msg.kind)}
        try:
            os.makedirs(self.media_dir, exist_ok=True)
            # cdn_base / cdn_timeout 属于低层客户端（ClawBotNode 自己不持有）
            url = media_download_url(desc, self.client.cdn_base)
            cipher = _get_binary(url, timeout=self.client.cdn_timeout, max_bytes=MAX_MEDIA_BYTES)
            plain = decrypt_media(cipher, str(desc.get("aes_key") or ""))
            ext = guess_media_ext(str(desc.get("kind") or msg.kind), plain,
                                  str(desc.get("filename") or ""))
            path = os.path.join(self.media_dir,
                                f"{msg.message_id or int(time.time() * 1000)}{ext}")
            with open(path, "wb") as fh:
                fh.write(plain)
            out.update({"media_path": path, "media_bytes": len(plain)})
        except Exception as exc:                      # noqa: BLE001
            out["media_error"] = f"{type(exc).__name__}: {exc}"
            self._log(f"入站媒体取不下来（消息仍会入收件箱）：{out['media_error']}")
        return out

    def _dispatch(self, msg: InboundMessage) -> None:
        if self._handler is None:
            self._log(f"（未注册钩子）收到 {msg.kind}: {msg.text[:60]!r}")
            return
        try:
            if self._handler_arity() <= 1:
                result = self._handler(msg)  # type: ignore[call-arg]
            else:
                result = self._handler(msg, self)
        except Exception as exc:  # 钩子出错不能把主循环带崩
            self._log(f"钩子抛出异常：{exc!r}")
            # 对端只收固定文案：异常原文可能带本机路径、令牌等内部信息
            # （实测过把 C:\Users\…\.ssh\id_rsa 这类路径原样发给对端）。细节留本地日志。
            self._safe_reply(msg, "处理这条消息时出错了，请看电脑端的日志。")
            return
        if isinstance(result, str) and result.strip():
            self._safe_reply(msg, result)

    def _handler_arity(self) -> int:
        """钩子接受几个位置参数（1 = 只收 msg；2 = msg + node）。

        用 `inspect` 判定，而不是"先按两个参数调、抛 TypeError 再按一个参数调"：
        后者在钩子形参带默认值（`def h(msg, node=None)`）且**内部自己**抛同型
        TypeError 时，会把钩子跑两遍（实测：钩子被调用 2 次、自动回复 1 次）——
        首次投递就重复执行，副作用翻倍。
        """
        handler = self._handler
        if handler is None:
            return 0
        try:
            signature = inspect.signature(handler)
        except (TypeError, ValueError):   # 内建/无签名的可调用对象：按老行为当两参数
            return 2
        positional = 0
        for param in signature.parameters.values():
            if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
                positional += 1
            elif param.kind is param.VAR_POSITIONAL:
                return 2
        return positional

    def _safe_reply(self, msg: InboundMessage, text: str) -> Optional[SendResult]:
        """同步回复。失败必须留下痕迹 —— 静默丢消息是最糟的失败模式。

        这里**已经在主循环内部**：任何异常冒泡出去都会让 `_pump_once` 中断、
        游标不落盘，于是整批消息下一轮被重复处理（重复回复）。
        所以除了返回值检查，还要把异常一并兜住。
        """
        try:
            result = self.send_text(msg.peer_id, text, context_token=msg.context_token)
        except Exception as exc:  # noqa: BLE001 - 兜底就是为了不让异常冒泡
            _log(f"回复时异常（已兜住，避免中断主循环）：{type(exc).__name__}: {exc}")
            return None
        if not result.ok:
            _log(f"回复失败（peer={msg.peer_id}）：{result}")
        return result

    def _note_session_paused(self, result: SendResult) -> SendResult:
        """把"发送时才发现会话被暂停"提升为节点级暂停。

        否则 -14 只表现为一条失败日志，主循环仍按正常节奏轮询/回复，
        每轮都白失败一次，直到服务端放行。这里与 getUpdates 的 -14 走同一个窗口。
        """
        if not result.ok and result.ret == ERR_SESSION_PAUSED:
            self._paused_until = time.time() + SESSION_PAUSE_SECONDS
            _log(f"服务端要求暂停会话（-14）：主循环暂停 {SESSION_PAUSE_SECONDS // 60} 分钟")
        return result

    # -- 对外发送 -----------------------------------------------------------

    def send_text(self, peer_id: str, text: str, *, context_token: str = "") -> SendResult:
        """给某个对端发文本。context_token 省略时用最近一次记住的。

        返回 SendResult（不抛异常），调用方检查 .ok。
        """
        if not self._ensure_credentials():
            result = SendResult(ok=False, errmsg="尚未登录：没有可用凭证（先运行 clawbot login）")
            _log(f"发送被拒：{result}")
            return result
        token = context_token or self._peer_contexts.get(peer_id) or self.store.get_context(peer_id)
        if not token:
            result = SendResult(ok=False, errmsg=f"没有 {peer_id} 的 context_token，无法发起新会话")
            _log(f"发送被拒：{result}")
            return result
        return self._note_session_paused(self.client.send_text(peer_id, text, token))

    def _ensure_credentials(self) -> bool:
        """确保协议客户端手上有凭证，返回是否可用。

        凭证是**懒加载**的：`run()` 与 `login()` 都会把磁盘凭证装进 client，
        但节点完全可能没跑过主循环就被直接用来发送（命令行 `main.py send`、
        或上层把它当库调用）。少了这一步，发送会以"尚未登录：没有 bot_token"
        失败 —— 而磁盘上明明有有效凭证，错误信息还会把人指向重新扫码。
        """
        if self.client.creds.is_valid:
            return True
        creds = self.store.load_credentials()
        if not creds.is_valid:
            return False
        self.client.creds = creds
        self.client.api_base = (creds.baseurl or DEFAULT_API_BASE).rstrip("/")
        return True

    # -- 对外发送媒体 -------------------------------------------------------

    def send_media(
        self,
        peer_id: str,
        media: Any,
        *,
        filename: str = "",
        kind: Optional[str] = None,
        context_token: str = "",
        caption: str = "",
        max_bytes: Optional[int] = None,
        retries: int = 2,
    ) -> SendResult:
        """给某个对端发一份媒体。

        media 既可以是**本地路径**，也可以是**字节**（如内存里刚生成的截图/二维码）。
        路径会先做存在性与大小上限检查：纯 Python AES 加密几 MB 也要几秒，
        与其让一条钩子把主循环无声卡上几分钟，不如立刻返回一条能看懂的错误。

        caption 会作为**独立的一条文本消息**先发出去 —— 微信一条消息只带一个 item，
        协议客户端也是这么拆的。说明文字失败不影响媒体本身（媒体才是正文）。

        返回值：SendResult，失败时用 .stage 区分 read / upload / send 三段。
        """
        if not self._ensure_credentials():
            result = SendResult(ok=False, errmsg="尚未登录：没有可用凭证（先运行 clawbot login）")
            _log(f"发送被拒：{result}")
            return result
        token = context_token or self._peer_contexts.get(peer_id) or self.store.get_context(peer_id)
        if not token:
            result = SendResult(ok=False, errmsg=f"没有 {peer_id} 的 context_token，无法发起新会话")
            _log(f"发送被拒：{result}")
            return result

        try:
            data, name = as_media_bytes(media, filename, max_bytes=max_bytes)
        except (OSError, ValueError) as exc:
            result = SendResult(ok=False, errmsg=str(exc), attempts=1, stage="read")
            _log(f"媒体发送失败（读取阶段）：{result.errmsg}")
            return result

        if caption:
            sent = self.send_text(peer_id, caption, context_token=token)
            if not sent.ok:
                _log(f"媒体说明文字未送达（媒体将继续发送）：{sent}")

        self._log(f"准备发送 {name}（{len(data)} 字节，kind={kind or 'auto'}）→ {peer_id}")
        return self._note_session_paused(
            self.client.send_media(peer_id, data, name, token, kind=kind, retries=retries)
        )

    def send_image(
        self,
        peer_id: str,
        image: Any,
        *,
        filename: str = "",
        context_token: str = "",
        caption: str = "",
        max_bytes: Optional[int] = None,
        retries: int = 2,
    ) -> SendResult:
        """发一张图片。image 可以是本地路径或图片字节（PNG/JPEG/...）。"""
        return self.send_media(
            peer_id, image, filename=filename or "image.png", kind="image",
            context_token=context_token, caption=caption, max_bytes=max_bytes, retries=retries,
        )

    def send_file(
        self,
        peer_id: str,
        path: Any,
        *,
        filename: str = "",
        context_token: str = "",
        caption: str = "",
        max_bytes: Optional[int] = None,
        retries: int = 2,
    ) -> SendResult:
        """发一个文件附件（按本地路径）。"""
        return self.send_media(
            peer_id, path, filename=filename, kind="file",
            context_token=context_token, caption=caption, max_bytes=max_bytes, retries=retries,
        )

    # -- 登录 ---------------------------------------------------------------

    def login(self, *, open_qr: bool = True, timeout_s: float = 480.0) -> Credentials:
        """扫码登录并把凭证落盘。已登录则直接返回。"""
        existing = self.store.load_credentials()
        if existing.is_valid:
            self.client.creds = existing
            self.client.api_base = (existing.baseurl or DEFAULT_API_BASE).rstrip("/")
            return existing

        def show_qr(session: QRLoginSession) -> None:
            _show_qr(session.qrcode_url, open_qr=open_qr, state_dir=self.store.dir)

        def ask(prompt: str) -> str:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return sys.stdin.readline().strip()

        creds, user_id = self.client.login_with_qr(
            on_qr=show_qr,
            on_status=lambda s: self._log(f"扫码状态：{s}"),
            ask_verify_code=ask,
            timeout_s=timeout_s,
        )
        self.store.save_credentials(creds)
        self._log(f"登录成功：{creds.masked()}，对端 user_id={user_id}")
        return creds

    def logout(self) -> None:
        self.store.clear_credentials()
        self.client.creds = Credentials()

    # -- 主循环 -------------------------------------------------------------

    def run(self, *, max_iterations: Optional[int] = None) -> None:
        """阻塞式长轮询主循环。Ctrl-C 可中断。

        启动前先拿 state 目录的**独占锁**：同一个 state 目录不允许两个实例同时拉消息。
        没有这道锁时，两个实例各拿各的游标，同一批消息会被处理两次 ——
        实测过，对端会收到两条一模一样的回复（钩子被调用序列 [555, 555]）。
        锁拿不到就直接抛错，不闷头重复消费。
        """
        if self._running:
            raise ClawBotError("同一个节点实例不能并发 run()（两个线程共用一份游标会放大重复消费）")
        creds = self.store.load_credentials()
        if not creds.is_valid:
            raise ClawBotError("尚未登录：请先调用 login()（或运行 clawbot login）")
        self.client.creds = creds
        self.client.api_base = (creds.baseurl or DEFAULT_API_BASE).rstrip("/")

        lock = StateLock(self.store.lock_path)
        if not lock.acquire():
            holder = lock.holder_pid()
            who = f"（pid={holder}）" if holder else ""
            raise ClawBotError(
                f"state 目录正被另一个实例占用{who}：{self.store.dir}（run.lock）。"
                f"同一目录并发跑两个节点会把同一条消息处理两次，请各自用独立的 --state。"
            )
        try:
            cursor = self.store.load_cursor()
            self._cursor = cursor
            self._log(f"启动：游标={'空（首次拉取）' if not cursor else cursor[:16] + '…'}")
            self._running = True
            iterations = 0
            backoff = 1.0

            while self._running:
                if max_iterations is not None and iterations >= max_iterations:
                    break
                # 暂停窗口可能在**回复**时被设置（-14 出现在 sendmessage 上，
                # 而不是 getUpdates 上），所以每轮都要看一眼，不能只在 except 里睡。
                if self._paused_until > time.time():
                    self._sleep_until(self._paused_until)
                iterations += 1
                try:
                    self._pump_once(self._cursor)
                    backoff = 1.0
                except SessionPaused as exc:
                    self._paused_until = time.time() + SESSION_PAUSE_SECONDS
                    self._log(f"会话被服务端暂停 1 小时：{exc}")
                    self._sleep_until(self._paused_until)
                except KeyboardInterrupt:
                    # Ctrl-C 是 README 明示的正常退出方式：落在批中间时，
                    # 已处理的消息靠 message_id 去重表兜住，不会被重复回复。
                    self._log("收到 Ctrl-C，退出")
                    break
                except Exception as exc:
                    self._log(f"轮询出错，{backoff:.0f}s 后重试：{exc!r}")
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
        finally:
            lock.release()
            self._running = False

    def stop(self) -> None:
        self._running = False

    def _pump_once(self, cursor: str) -> None:
        """一次长轮询 + 处理 + 落盘游标。

        三个防御点，每一个都对应一个实测过的故障：

        · `msgs` 必须是**列表**、元素必须是 dict —— 否则 `resp["msgs"]` 里的
          一条脏数据会让每一轮都在同一处抛异常，游标永远推进不了，
          该账号从此收不到任何新消息（毒丸）。这里宁可跳过坏元素。
        · `message_id` 去重 —— 游标是整批处理完才落盘的，进程被杀 / Ctrl-C
          落在批中间 / 两个实例共用一个 state 目录，都会让同一批被再处理一次。
        · 游标**先写盘再更新内存** —— 反过来的话，写盘失败（Windows 上
          文件被别的进程读着就会 `PermissionError`）会留下"内存已推进、磁盘没动"
          的分叉，重启后重复投递已回复过的消息。
        """
        resp = self.client.get_updates(cursor)
        # ★ 心跳：轮询**回来了**才摸（超时/异常都不会走到这），所以它的新鲜度就是
        #   "接收方还在正常轮询"的证据。见 store.touch_heartbeat 的说明。
        self.store.touch_heartbeat()
        raw_msgs = resp.get("msgs")
        msgs = raw_msgs if isinstance(raw_msgs, list) else []
        new_cursor = resp.get("get_updates_buf")

        # ★ 服务端返回了什么，先记一条总览：这条日志是"消息到底有没有到本地"的唯一证据。
        #   2026-10-16 用户 16:17 的一条消息既没进收件箱、日志里也毫无痕迹，
        #   分不清是服务端没返回还是本地过滤掉了 —— 所以每一条都要留痕。
        if msgs:
            self._log("服务端返回 %d 条原始消息：%s" % (len(msgs), "、".join(
                f"[type={_as_int(m.get('message_type'), 0)} id={m.get('message_id')} "
                f"seq={m.get('seq')}]" for m in msgs if isinstance(m, dict))))

        for raw in msgs:
            if not isinstance(raw, dict):
                self._log(f"跳过无法解析的消息元素：{type(raw).__name__}")
                continue
            if _as_int(raw.get("message_type"), 0) != 1:
                # 只处理用户发来的（2 = BOT 自己发出）。★ 跳过必须留痕（原先静默 continue）。
                self._log("跳过非用户消息：type=%s id=%s seq=%s"
                          % (_as_int(raw.get("message_type"), 0),
                             raw.get("message_id"), raw.get("seq")))
                continue
            msg = parse_inbound(raw)
            if not msg.peer_id:
                # ★ 同样必须留痕：没有对端就回不了，但"丢在哪一步"要看得见。
                self._log("跳过没有对端的消息：id=%s seq=%s kind=%s"
                          % (raw.get("message_id"), raw.get("seq"), raw.get("message_type")))
                continue
            if msg.message_id and self.store.is_seen(msg.message_id):
                self._log(f"跳过已处理过的消息 message_id={msg.message_id}（重放/多实例）")
                continue
            # 顺序要紧：**先落收件箱，再记 seen**。
            # 反过来（先 mark_seen 再 append_inbox）时，两步之间被 SIGTERM/重启打断
            # （systemd stop/restart 就是 SIGTERM，本进程没有信号处理），或者 append
            # 抛 OSError，这条消息就"既没进收件箱、又被记为已处理" → 永久丢失，
            # 而且游标照样推进，连重投的机会都没有。
            # 现在这个顺序最坏只是重投一次（收件箱里多一行 same message_id）——
            # 接口本来承诺的就是"至少一次"，宁可重复也不能静默丢。
            appended = False
            # ★ 入站媒体：**先**把图/文件取回来（解密 + 落盘），再连同路径一起写进收件箱 ——
            #   一次写全，上层读到的就是"有图 + 图在哪"。取件失败不抛（见 `_fetch_media`）；
            #   取件期间崩掉也不丢消息：游标还没落盘，服务端会重投（承诺"至少一次"）。
            media_fields = self._fetch_media(msg)
            try:
                self.store.append_inbox({
                    "message_id": msg.message_id,
                    "peer_id": msg.peer_id,
                    "kind": msg.kind,
                    "text": msg.text,
                    "seq": msg.seq,
                    "create_time_ms": msg.create_time_ms,
                    "received_at": time.time(),
                    **media_fields,
                })
                appended = True
            except OSError as exc:
                # 收件箱是别的部件唯一的收消息路径，写失败必须留痕、且不能记为已处理
                self._log(f"收件箱写入失败（不记为已处理，下轮会重投）：{exc}")
            if appended and msg.message_id:
                self.store.mark_seen(msg.message_id)
            if msg.context_token:
                self._peer_contexts[msg.peer_id] = msg.context_token
                try:
                    self.store.remember_context(msg.peer_id, msg.context_token)
                except OSError as exc:
                    # 记上下文失败不该拖累这条消息的处理（它是回复的依据，但内存里已经有了）
                    self._log(f"上下文落盘失败（本次仍用内存里的令牌）：{exc}")
            self._log(f"← {msg.kind} from {msg.peer_id}: {msg.text[:80]!r}")
            self._dispatch(msg)

        # 游标只在服务端确实返回时更新：空串会把"读到哪"重置掉
        if new_cursor:
            self.store.save_cursor(str(new_cursor))
            self._cursor = str(new_cursor)

    def _sleep_until(self, deadline: float) -> None:
        while self._running and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))

    # -- 日志 ---------------------------------------------------------------

    def _log(self, message: str) -> None:
        _log(message)


def _log(message: str) -> None:
    """运行日志一律写 **stderr**。

    契约（INTERFACE.md §2）：`--json` 时 stdout 只有一行 JSON。
    日志写 stdout 会让 `json.loads(stdout)` 的消费方直接崩 —— 而且成功路径也打日志
    （`cmd_send`/`cmd_run`/`cmd_login` 都以 debug=True 建节点），
    所以不是"偶尔多一行"，是每次 `send --json` 都多一行。
    """
    stamp = time.strftime("%H:%M:%S")
    stream = sys.stderr
    if stream is None:          # pythonw / 流被关掉
        return
    try:
        stream.write(f"[{stamp}] {message}\n")
        stream.flush()
    except UnicodeEncodeError:  # GBK 控制台兜底
        stream.write(f"[{stamp}] {message.encode('ascii', 'replace').decode('ascii')}\n")


# --------------------------------------------------------------------------
# 二维码展示
# --------------------------------------------------------------------------


def _show_qr(content: str, *, open_qr: bool = True, state_dir: Optional[str] = None) -> None:
    """把二维码显示出来。

    三种手段依次尝试：终端 ASCII → PNG 并调用系统看图 → 只打印链接。
    PNG 用带时间戳的唯一文件名：二维码会过期刷新，固定文件名既会让
    看图程序锁定旧文件（Windows 覆写报 Errno 22），也会让人扫到过期的图。
    """
    from . import qr as qrlib

    state_dir = resolve_state_dir(state_dir)

    print("\n用手机微信扫描下面的二维码，确认授权：\n")
    try:
        symbol = qrlib.make_qr(content, error_correction=qrlib.ERROR_CORRECT_M)
        print(symbol.to_ascii(border=2))
    except Exception as exc:
        print(f"（终端二维码生成失败：{exc}）")

    png_path = None
    try:
        os.makedirs(state_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        png_path = os.path.join(state_dir, f"login-qr-{stamp}.png")
        qrlib.write_png(qrlib.make_qr(content, error_correction=qrlib.ERROR_CORRECT_M), png_path)
        print(f"二维码图片已保存：{os.path.abspath(png_path)}")
        _prune_old_qr(state_dir, keep=png_path)
    except Exception as exc:
        print(f"（PNG 导出失败，请使用下方链接：{exc}）")

    if png_path and open_qr:
        try:
            if sys.platform == "win32":
                os.startfile(png_path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{png_path}"')
            else:
                os.system(f'xdg-open "{png_path}" >/dev/null 2>&1')
        except Exception:
            pass

    print(f"\n若二维码无法扫描，可手动访问：\n{content}\n")


def _prune_old_qr(state_dir: str, *, keep: str) -> None:
    """清掉旧的二维码图片，避免 state 目录堆积。"""
    try:
        for name in os.listdir(state_dir):
            if not (name.startswith("login-qr") and name.endswith(".png")):
                continue
            path = os.path.join(state_dir, name)
            if os.path.abspath(path) != os.path.abspath(keep):
                try:
                    os.unlink(path)
                except OSError:
                    pass  # 正被看图程序占用，留着无妨
    except OSError:
        pass
