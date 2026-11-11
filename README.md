# qyn-phone —— 手机通道（微信 ClawBot 收发）

手机微信 ↔ 电脑 之间的**最小双向管道**，基于腾讯官方的微信 ClawBot（iLink）协议。

**它是一条管道，不是一套系统。** 管道只做四件事：

1. **收**：长轮询把手机发来的消息拉下来；
2. **交出去**：按序号写进收件箱（`state/inbox.jsonl`），任何进程按游标读，不丢、不重复投递；
3. **发**：把调用方给的文本/图片/文件发出去（编码、上传、重试都在管道内）；
4. **失败显式**：绝不静默丢消息 —— 失败要么体现在 `ok=false` + `stage`，要么体现在 `truncated=true`。

它**不做**的事：不解释消息含义、不决定"该回什么"、不保存业务状态、**默认不替消费者回话**、
不绑定任何 agent 框架。上层想接什么就接什么（CLI、DSH 技能、脚本、你自己的 agent）。

- 协议实现：纯 Python 标准库，**零第三方依赖**（仅二维码 PNG 输出用到 Pillow，缺失时自动降级为终端二维码）
- 协议依据：腾讯开源仓库 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 的 `docs/protocol_zh_CN.md`
- 官方条款：使用前请阅读《微信 ClawBot 功能使用条款》。腾讯仅提供消息通道，不提供 AI 服务
- 位置：`<repo-root>`（QYN 仓库内的独立组件，暂不与 AGENT 耦合）

---

## 快速开始

```bash
cd <repo-root>

clawbot login            # 首次：出二维码，用手机微信扫
clawbot run              # 启动管道泵（默认不自动回复）

# 另开一个终端：看手机发来的消息
clawbot inbox --json
```

登录成功后凭证与游标保存在 `state/` 目录，之后 `run` 不再需要扫码。

> **`state/` 不在 git 里**。它含真实 `bot_token`，已由 `PHONE/.gitignore` 排除。
> 换机器部署时**需要重新扫码登录**，不要手工拷凭证文件。
>
> 目录内容：`credentials.json`（凭证）、`cursor.json`（长轮询游标）、`contexts.json`（对端会话令牌）、
> `inbox.jsonl`（**收件箱**，管道出口）、`seen.json`（去重表）、`run.lock`（接收方独占锁）、
> `login-qr-*.png`（登录二维码，`reset` 会清）。

其他命令：

```bash
clawbot status           # 登录状态 / 接收方在不在跑 / 收件箱概况
clawbot send --text "…"  # 发文本（管道出口）
clawbot send 报告.pdf    # 发文件附件
clawbot send shot.png --image   # 按图片发送（对端唯一时可省略 --to）
clawbot reset            # 清除凭证与派生状态（换账号时用）
```

### 三种等价的启动方式

实现只有一份（`clawbot/cli.py`），`main.py` 只是一个把参数转过去的兼容壳。
**已经 `pip install -e .` 装成包**，所以在任何目录都能直接用：

```bash
clawbot run                 # console script（装在 PATH 上，推荐）
python -m clawbot run       # 模块入口（同样任何目录可用）
python main.py run          # 老写法：不需要安装，README / 部署脚本 / systemd 都用它
```

要二维码导出 PNG 再加可选依赖：`pip install -e ".[png]"`（即 Pillow；不装会降级为终端二维码）。

### 常驻接收方（管道泵）

手机 → 电脑这条方向**必须有进程一直长轮询**：`clawbot run`。同一 state 目录只允许一个
（第二个会被明确拒绝，并告诉你持有者 pid）。

**它默认不回复任何消息** —— 收到的消息只进收件箱，由消费者决定回什么。要回显/自定义回复，
显式给 `--handler`（那是消费者代码，不属于管道）：

```powershell
Start-Process -FilePath python -ArgumentList '-m','clawbot','run' `
  -WorkingDirectory <repo-root> `
  -RedirectStandardOutput <repo-root>\logs\run.log `
  -RedirectStandardError  <repo-root> -WindowStyle Hidden
clawbot status --json                              # 看状态（receiver.running / receiver.pid）
Get-Content <repo-root>\logs\run.log -Tail 20
Stop-Process -Id <pid>                             # pid 从 status --json 的 receiver.pid 拿
```

日志在 `logs/`（不入库）；**开机不会自动起**，需要的话加个计划任务，或等一台常驻机器 M3 上线后 systemd 托管。

### 状态目录是可配置的

默认是 `<本目录>/state`，**用绝对路径解析，不随当前工作目录漂移**
（从仓库根、从一台常驻机器 systemd、被别的程序 import，都指向同一个地方）。

```bash
clawbot run --state <状态目录>   # 部署到一台常驻机器时
```

### 二维码扫不出来怎么办

`login` 会同时做三件事：打印终端二维码、保存 `state/login-qr-<时间戳>.png` 并用系统看图打开、
打印一个 `https://liteapp.weixin.qq.com/q/...` 链接。三者任一可用即可。

二维码文件名带时间戳，因为**旧图会过期**：固定文件名既可能让你扫到过期的二维码，
也可能因看图程序占用文件导致 Windows 覆写失败。

---

## 对外接口（其他项目调它，不要读这一节以外的东西）

**接口合同：[`INTERFACE.md`](INTERFACE.md)**（含最小接入、字段表、游标语义、错误码、兼容承诺）；
机器可读 schema：[`clawbot/interface.schema.json`](clawbot/interface.schema.json)，
由 `tests/test_interface.py` 拿真实进程输出对照校验。

```bash
clawbot send --text "会议改到三点" --json     # 电脑 → 手机
clawbot inbox --json --since <游标>            # 手机 → 电脑
clawbot status --json                          # 健康：logged_in / receiver.running / inbox.last_n
```

一句话纪律：**`--json` 时 stdout 只有一行 JSON**（日志走 stderr），成败只看 `ok`，
游标原样回传，`truncated: true` 是"你漏了消息"而不是"没有新消息"。
DSH 侧的用法约定在 `消费者代码（在仓库外）/SKILL.md`（那是消费者代码，不属于管道）。

---

## 接入你自己的 agent

只改一个函数。复制 `examples/with_agent.py`，把 `my_agent()` 换成你真实的调用方式：

```python
from clawbot import ClawBotNode, InboundMessage

NODE = ClawBotNode(state_dir="state")

def my_agent(text: str) -> str:
    ...   # 你的本地 HTTP / CLI / A2A 调用，返回要回复的文本

@NODE.on_message
def handle(msg: InboundMessage):
    return my_agent(msg.text)
```

```bash
clawbot run --handler path/to/your_node.py
```

> `--handler` 是**消费者代码**（管道不预置任何回话行为）。不加载它时，管道只把消息写进收件箱。

钩子返回值规则：

| 返回 | 行为 |
|---|---|
| 字符串 | 自动原路回复 |
| `None` | 不回复（异步场景：自己稍后调 `NODE.send_text(peer_id, text)`） |
| 抛异常 | 捕获后回复一条兜底提示，**不会**中断主循环 |

> 返回值只能发**文本**。要发图片/文件，就在钩子里直接调 `NODE.send_image()` /
> `NODE.send_file()` 然后 `return None` —— 否则除了媒体还会再多收到一条文本回复。
> 可直接跑的示例：`examples/text_echo.py`（最小回显）、`examples/async_reply.py`（长任务不阻塞）、
> `examples/with_agent.py`（接自己的 agent）。
>
> **多数消费者不需要钩子**：直接读收件箱更简单（见 [INTERFACE.md](INTERFACE.md)）。

---

## 作为库使用

```python
from clawbot import ClawBotNode

node = ClawBotNode(state_dir="state")

@node.on_message
def handle(msg, node):
    # 想异步回复就返回 None，然后另起线程调用：
    # node.send_text(msg.peer_id, "稍后给你结果")
    return f"收到：{msg.text}"

node.run()
```

`ClawBotNode` 的公开方法：

| 方法 | 作用 |
|---|---|
| `login(open_qr=True)` | 扫码登录并落盘凭证 |
| `run(max_iterations=None)` | 阻塞式长轮询主循环 |
| `stop()` | 让主循环退出 |
| `send_text(peer_id, text, *, context_token="")` | 给某个对端发文本 |
| `send_image(peer_id, image, *, caption="")` | 发图片；`image` 可以是**本地路径**或**图片字节** |
| `send_file(peer_id, path, *, caption="")` | 发文件附件（本地路径） |
| `send_media(peer_id, media, *, filename="", kind=None, caption="")` | 统一入口，`kind` 省略时按魔数/扩展名自动判断 |
| `on_message(func)` | 注册消息钩子（装饰器） |
| `logout()` | 清除本地凭证 |

`send_*` 一律返回 `SendResult`（**不抛异常**），调用方检查 `.ok`：

```python
result = node.send_image(msg.peer_id, "chart.png", context_token=msg.context_token,
                         caption="这是今天的图")
if not result.ok:
    print(result.stage, result.errmsg)   # stage: read / upload / send
```

媒体发送有三段（读文件 → CDN 上传 → 发消息），`.stage` 告诉你失败在哪一段。
默认单文件上限 **32 MiB**（`max_bytes=` 可调）：纯 Python AES 加密约 **2 MB/s**，
与其让一条钩子把主循环无声卡上几分钟，不如立刻返回一条能看懂的错误。

---

## 它替你处理了哪些协议细节

这些都在实现里做完了，用的时候不需要知道：

| 协议细节 | 处理方式 |
|---|---|
| **扫码登录** | 二维码申请 → 状态轮询 → 凭证落盘；含 `need_verifycode`（手机显示数字）、`scaned_but_redirect`（服务端要求换 IDC 域名）、`expired`（自动刷新，最多 3 次） |
| **鉴权请求头** | 鉴权 POST 带 `AuthorizationType: ilink_bot_token`、`Authorization: Bearer <token>`、`X-WECHAT-UIN`（每次请求随机 uint32→base64，防重放）；**应用请求**（二维码状态轮询）只带 `iLink-App-Id` / `iLink-App-ClientVersion` —— 协议明说这类请求**不**带 `AuthorizationType` / `Authorization` / `X-WECHAT-UIN` |
| **长轮询游标** | `get_updates_buf` 持久化到 `state/cursor.json`，进程重启后续读；**服务端返回空游标时不覆盖已存值** |
| **长轮询超时** | 超时**不是故障**：官方把超时映射成"空结果"，本实现同样如此（游标原样带回）。当成错误处理的后果是每次正常超时都被计为故障并退避（1s→60s），越退越慢，看着像掉线 |
| **`longpolling_timeout_ms`** | 采纳服务端建议的下次超时（夹在 5–120s）。忽略它的话，服务端建议一旦大于本地固定超时，每次轮询都会先被自己掐断 |
| **失败判定** | `ret` **或** `errcode` 任一非零都算失败（协议原文如此）。只认 `ret` 的话，`{"ret":0,"errcode":-3}` 会被当成成功 → 主循环立刻再轮询，变成不打印原因的热循环 |
| **`-14` 会话暂停** | 识别为 `SessionPaused`，主循环自动等待 1 小时后继续，游标不丢；**回复时**遇到的 `-14`（`sendmessage` 返回）同样让节点进入暂停窗口，且不做无谓重试 |
| **重试的瞬时性判定** | 不靠错误文本猜，而用结构化信息（HTTP 状态码 / 错误对象自带的 `transient`）。文本猜法有真实误判：错误体里出现 "HTTP 500" 字样会把 4xx 当可重试，`尚未登录` 这种本地确定性错误也会白重发两次 |
| **脏字段容错** | 响应与入站消息里的数值字段（`message_id`/`seq`/`ret`）非数值时退化为 0/None，不抛异常 —— 解析跑在游标落盘**之前**，抛一次就会让整批消息被重复处理。`msgs` 里混进非对象元素也只会被跳过，不会让账号从此收不到消息（毒丸） |
| **重复消费防护** | `message_id` 去重表（`state/seen.json`，最近 500 条）+ state 目录**独占锁**（`state/run.lock`）。前者让"游标没落盘就重启 / Ctrl-C 落在批中间"不再重复回复；后者直接拒绝第二个实例，不让它俩各拉一份游标 |
| **写盘健壮性** | 原子写 + `fsync` + `os.replace` 退避重试（Windows 上目标文件被别的进程读着时会抛 `PermissionError`）；过期 `.tmp-*.json`（强杀产物，写凭证时含明文 token）下次启动自动清理 |
| **内部信息不外送** | 钩子异常的原文只进本地日志；发给对端的是一句固定文案 —— 异常里可能带本机路径、令牌（实测过把 `C:\Users\…\.ssh\id_rsa` 原样发给对端） |
| **凭证不外送** | 请求基址通过参数传递，**不**临时改写客户端的 `api_base`：旧写法在并发时会带着 `Authorization: Bearer <真 token>` 打到二维码那台主机上（端口级实测） |
| **`context_token`** | 每条入站消息记住，回复时原样回传；也持久化到 `state/contexts.json`，供异步回复使用 |
| **只处理用户消息** | `message_type == 1` 才分发，忽略 bot 自己发出的（`message_type == 2`） |
| **消息解析** | 文本 / 图片 / 语音(取转写) / 文件 / 视频 → 统一的 `InboundMessage` |
| **出站媒体** | `getuploadurl` 申请上传参数 → AES-128-ECB + PKCS#7 加密 → `POST` 密文到 CDN → 取响应头 `x-encrypted-param` → 组装 `image_item` / `file_item` 发 `sendmessage`（微信一条消息只带一个 item，所以说明文字是独立的一条） |
| **媒体密钥编码** | `media.aes_key` 必须是 **base64(32 位十六进制密钥串)**，不是 base64(原始 16 字节)。下载端两种都收，但发送端给 raw 的后果是：对方看到灰框，**两端都不报错** |
| **上传成功判定** | HTTP 200 **且** `x-encrypted-param` 非空；HTTP 4xx 立即放弃（重试不会变好），其余（网络/5xx/缺响应头）最多 3 次 |
| **媒体类型判定** | 魔数优先于扩展名（扩展名可以随便改）；认不出来按**文件附件**发 —— 猜成图片的代价是一个打不开的灰框，附件至少可点开 |
| **加密吞吐** | AES 是纯 Python 实现（保持零依赖），约 **2 MB/s**；因此默认单文件上限 32 MiB，超限在加密前就返回可读错误 |
| **重连退避** | 网络错误指数退避（1s → 60s 封顶），钩子异常不影响主循环 |
| **凭证安全** | `credentials.json` 原子写（不留半截 JSON）、日志只打印脱敏 token；权限 `0600` **仅 POSIX 生效**（Windows 上是 no-op，保护来自用户目录 ACL） |

---

## 目录结构

```
PHONE/
├── INTERFACE.md            **对外接口合同**（其他部件只依赖它，v1）
├── pyproject.toml          PEP 621 打包定义（版本号单一来源，console script = clawbot）
├── .gitignore              排除 state/（含真实 bot_token）
├── main.py                 命令行兼容壳（转调 clawbot.cli）
├── clawbot/
│   ├── __init__.py         对外接口（推荐的稳定面写在模块 docstring 里）
│   ├── cli.py              命令行实现（login / run / send / status / reset）
│   ├── __main__.py         python -m clawbot 入口
│   ├── client.py           协议层 + 节点层（ClawBotClient / ClawBotNode）
│   ├── interface.schema.json  接口的机器可读 schema（由测试对照真实输出校验）
│   ├── media.py            出站媒体的准备、加密与 item 构造（纯函数）
│   ├── aes.py              纯 Python AES-128-ECB + PKCS#7（零依赖，约 2 MB/s）
│   ├── store.py            凭证、游标、上下文、去重表的持久化 + state 目录独占锁
│   └── qr.py               零依赖 QR 编码器（扫码登录用）
├── examples/               **消费者示例**（不是管道的一部分：管道默认不回复）
│   ├── text_echo.py        最小回显（需显式 `--handler` 才生效）
│   ├── async_reply.py      长任务：钩子先返回、稍后自己推第二条
│   └── with_agent.py       接自己 agent 的模板
├── tests/
│   ├── run_all.py          一键跑全部自检（一条命令 + 一个退出码）
│   ├── test_interface.py   对外接口自检（schema 对照 + 跨进程端到端 + 默认静默）
│   ├── test_protocol.py    协议层自检（本地桩服务器，不碰微信）
│   ├── test_media.py       出站媒体自检（桩 CDN + **密文逐字节回解**）
│   ├── test_store.py       持久化自检（多实例的陈旧读 / 丢更新 / 损坏文件）
│   ├── test_aes.py         AES 自检（FIPS-197 / SP 800-38A 官方向量）
│   ├── test_paths.py       状态目录不随 CWD 漂移
│   └── test_qr_roundtrip.py QR 编码器闭环自检
├── logs/                   常驻接收方的日志（不入库）
└── state/                  运行时生成，**不入库**：凭证、游标、收件箱、锁
```

---

## 发图片 / 发文件

```python
from clawbot import ClawBotNode

NODE = ClawBotNode()

@NODE.on_message
def handle(msg):
    NODE.send_image(msg.peer_id, "chart.png", context_token=msg.context_token,
                    caption="今天的图")
    NODE.send_file(msg.peer_id, "报告.pdf", context_token=msg.context_token)
    return None        # 媒体已经自己发出去了，别再返回字符串
```

命令行实测（不需要写代码）：

```bash
clawbot send 报告.pdf --caption "附件在这"
clawbot send shot.png --image
```

**没有实测过的部分要说清楚**：视频（`item.type=5`）的 item 构造已按协议实现，
但**未在真实服务端验证**；语音（`item.type=3`，还需 `encode_type`/`sample_rate`/`playtime`
等元数据）**未实现**；缩略图（`thumb_upload_param`）本实现不产生，固定发
`no_need_thumb: true`（与官方客户端一致）。

真机实测的耗时参考（2026-10-09）：1458 B 的图片，`getUploadUrl` + CDN 上传合计约 **15 秒** ——
瓶颈在 CDN 这条路的往返，不在加密（1 MiB 加密只要 0.5 秒）。所以别把媒体发送放在
需要秒回的交互路径上；`cdn_timeout` 默认 120 秒。

---

## 自检

七套测试都不需要联网、不需要扫码：

```bash
python tests/run_all.py            # 一键跑全部，最后给一行 PASS/FAIL 与总耗时

python tests/test_interface.py     # 对外接口：schema 对照 + 收件箱游标 + 跨进程读写
python tests/test_protocol.py      # 请求头 / 登录 / 游标 / 发送 / 钩子 / 暂停 / 超时 / 脏字段
python tests/test_media.py         # getUploadUrl 字段 / 桩 CDN 上传 / item 形状 / 失败路径
python tests/test_store.py         # 凭证 / 游标 / 上下文：多实例的陈旧读与丢更新
python tests/test_aes.py           # FIPS-197 C.1 与 SP 800-38A F.1.1 官方向量
python tests/test_qr_roundtrip.py  # QR 编解码闭环
python tests/test_paths.py         # 状态目录不随 CWD 漂移
```

每套都是脚本式的：`python tests/xxx.py`，最后打印 `PASS`/`FAIL` 并用退出码表示结果。
**不要用 pytest 跑**（会当成 fixture 报错）：这不是 pytest 用例集，是有意保持"零依赖、直接可跑"。
`run_all.py` 就是给"一条命令 + 一个退出码"用的（约 25 秒）。

`test_protocol.py` 会起一个本地 HTTP 桩服务器，让真实的 `ClawBotClient` 打过去，
逐项检查请求头与请求体是否符合协议约定。

`test_media.py` 的桩服务器还要扮演 CDN：它会把收到的**密文**用请求里声明的
`aeskey` 解回来，与原始文件逐字节比对。这一条同时钉住加密算法、PKCS#7 填充、
`aes_key` 的编码方式，以及 `rawsize`/`filesize` 的自洽 —— 少了它，其他字段全对
也可能发出一个对方解不开的灰框，而且全程不报错。

---

## 已知边界

- **只支持私聊**。协议类型里有 `group_id`，但官方插件文档明确说明不保证支持群聊，本实现按私聊设计。
- **发送只支持图片 / 视频 / 文件（+ 文本）**。收消息已经能识别图片/语音/文件/视频（`InboundMessage.kind`）；
  发送端的视频 item 已按协议实现但**未在真实服务端验证**，语音（需 `encode_type`/`sample_rate`/`playtime` 等元数据）
  与缩略图（`thumb_upload_param`）**未实现** —— 固定发 `no_need_thumb: true`。
- **出站媒体已在真实服务端实测通过（2026-10-09）**：图片（PNG，1458 B → 上传 1472 B）与
  中文文件名附件（155 B → 上传 160 B）各一条，`getUploadUrl` → CDN → `sendMessage` 全部第 1 次尝试
  返回 `ret=0` 并带回 `message_id`。填充后的字节数与声明一致（1458+14、155+5）。
  手机上"显示是否正常"以肉眼所见为准；本机没法替代这一步。
- **AES 是纯 Python 实现**（为了保持零依赖），约 2 MB/s，因此默认单文件上限 32 MiB。
  要发更大的文件，要么显式调大 `max_bytes`（`0` = 取消限制，会相应变慢并阻塞调用线程），
  要么给 `clawbot/aes.py` 接一个原生后端。
- **消息是"至少一次"语义，但重复会被挡掉**：游标在整批消息处理**之后**才落盘，所以强杀 / 崩溃 / Ctrl-C 落在批中间
  都会导致重放；`state/seen.json` 按 `message_id` 去重（最近 500 条），重放不会再重复回复。
  超出 500 条窗口的极老消息若被重放，仍可能重复一次 —— 要更精确得由上层按 `message_id` 长期去重。
- **同一个 state 目录只能有一个 `run()`**：第二个实例会**拿不到锁并明确报错**，不会安静地重复消费。
  这是有意的：两个实例各拉一份游标必然重复回复。要跑多实例请各自用独立 `--state`。
- **`contexts.json` 不是跨进程锁**：读写都是"读—改—写"，两个进程在同一毫秒级窗口里仍可能丢一条记录。
  单进程（推荐用法）不受影响；真要并发跑多个实例，请各自用独立 `--state` 目录。
- **`credentials.json` 的 0600 只在 POSIX 生效**：Windows 上 `os.chmod` 是 no-op，
  保护来自用户目录 ACL。别把它当成"加密存储"。
- **协议里未实现的部分**（没做，也没在文档里宣称过）：`SKRouteTag` 路由头、`run_id`、
  `notifyStart`/`notifyStop` 生命周期通知、`local_token_list` 的历史 token 复用；
  入站媒体的**下载解密**（收件箱只给类型与文本，不给图片/文件字节）、
  输入中状态（`ClawBotClient.get_config()`/`send_typing()` 在，但节点没接线）。
- **群聊/多账号**未做路由，单账号单会话场景最直接。
- **速率限制未公开**，官方条款保留随时调整连接规模与频率的权利，需要实测。
- **后端协议是私有契约**（官方文档自己也强调：TypeScript 里的可选字段不代表服务端行为），会有变更。所有调用都收敛在 `ClawBotClient` 内，变更好改。
- 服务端**不存储**你的消息内容，但会收集 IP 与操作记录用于安全审计。
- 腾讯有权变更、中断或终止该功能，**不要把核心业务完全压在这条通道上**。

---

## 换账号 / 重新登录

```bash
clawbot reset
clawbot login
```

## 排错

| 现象 | 排查 |
|---|---|
| `登录失败：登录超时` | 二维码没在 8 分钟内扫；重跑 `login` |
| `登录失败：该微信已绑定到其它实例` | 先在原实例解绑，或 `reset` 后重登 |
| `尚未登录` | 先跑 `login`，或检查 `state/credentials.json` 是否存在 |
| 收到消息但没回复 | 用 `--handler` 指定的文件里必须有 `NODE` 变量且注册了钩子；看启动日志有没有"已加载消息钩子" |
| `会话被服务端暂停 1 小时` | 正常容错，主循环会等到期后自动继续，不用干预 |
| 长时间收不到消息 | 看 `clawbot status --json` 的游标与 `receiver.running` 是否正常 |
| 图片发出去是**灰色方块** | 九成是 `media.aes_key` 编码错了：必须是 base64(32 位十六进制密钥串)，不能是 base64(原始 16 字节)。再看 CDN 上传体长度是否等于 `getuploadurl` 里声明的 `filesize` |
| 媒体没发出去 | 看日志里的失败阶段：**读取**=路径不存在/超过 `max_bytes`；**上传**=加密或 CDN（含 HTTP 状态）；**发消息**=`sendmessage` 的 ret 非 0 |
| 发大文件很慢/报超限 | 纯 Python AES 约 2 MB/s，默认上限 32 MiB；用 `max_bytes=` 调大（`0` = 不限制，会相应变慢） |
| 同一条消息被回复了两次 | 现在有 `message_id` 去重表挡着；若仍出现，看日志有没有 `跳过已处理过的消息`，以及是不是两个实例在跑（第二个实例启动时会被锁拒绝，日志里会写明） |
| `state 目录正被另一个实例占用` | 有意的：同一目录并发跑两个节点必然重复回复。各自用独立 `--state`，或先停掉另一个 |
| 日志出现 `[clawbot.store] … 不是合法 JSON` | `state/` 里的文件被手改坏或拷了一半。该文件按"空"处理（最坏是重复收一次消息），删掉它重跑即可 |
| 明明在跑，却像是"掉线"很久没反应 | 看有没有 `会话被服务端暂停`；另外单次长轮询最长会占住 `longpolling_timeout_ms`（夹在 5–120 秒），`Ctrl-C`/`stop()` 要等这一轮结束 |
