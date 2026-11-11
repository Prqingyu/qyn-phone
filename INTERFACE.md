# PHONE 对外接口 v1

**其他项目要调手机，只需要这一个文件。** 不必知道 iLink 协议、AES、CDN 或 `state/` 的结构。

- 机器可读 schema：[`clawbot/interface.schema.json`](clawbot/interface.schema.json)
- 一致性由 `tests/test_interface.py` 钉住：拿**真实进程输出**对照 schema，不是只测文档
- 版本 `schema: 1`

---

## 0. 最小接入（照抄即可）

```bash
# 电脑 → 手机
clawbot send --text "算完了：3 个文件，1.2 GB" --json
echo "多行内容" | clawbot send --text - --json          # 长文本走 stdin
clawbot send D:\tmp\报告.pdf --json                     # 附件
clawbot send D:\tmp\图.png --image --json               # 图片

# 手机 → 电脑
clawbot inbox --json                                    # 首次：从头读
clawbot inbox --json --since <上次的 next_cursor>        # 之后：接着读
```

```jsonc
// send 的返回（成败都返回这一种对象）
{"schema":1,"ok":true,"peer_id":"peer0000…@im.wechat","message_id":1234567890123456789,
 "stage":"send","attempts":1,"error":null}

// inbox 的返回
{"schema":1,"ok":true,
 "messages":[{"schema":1,"n":43,"message_id":123456,"peer_id":"peer0000…@im.wechat",
              "kind":"text","text":"你好","seq":7,"create_time_ms":1700000000000,
              "received_at":1700000000.00}],
 "next_cursor":"43","has_more":false,"truncated":false,"first_n":1,"last_n":43}
```

| 规则 | 值 |
|---|---|
| 成败 | 只看 `ok`（`--json` 时永远有）；退出码 `0` 成功 / `1` 业务失败 / `2` 用法错误 |
| 失败原因 | `code`（`bad_usage` / `not_logged_in` / `no_peer` / `file_not_found` / `failed`）+ `error`；发送类另有 `stage`（`read` / `upload` / `send`） |
| stdout | `--json` 时**只有一行 JSON**；日志全在 stderr |
| 游标 | `next_cursor` 是不透明字符串，原样回传即可；`has_more=true` 就再读一次；`truncated=true` 表示**你漏了消息**（不是"没有新消息"） |
| 目标 | 省略 `--to` 时要求 state 里**恰好一个**对端，否则报 `no_peer`（用 `clawbot peers --json` 选） |
| 健康检查 | `clawbot status --json` → `logged_in`、`receiver.running`（接收方在不在跑）、`inbox.last_n` |

消费侧循环（唯一需要写的一段）：

```bash
cursor=""                                   # 首次为空 = 从头读
while :; do
  page=$(clawbot inbox --json --since "$cursor")
  [ "$(echo "$page" | jq -r '.ok')" = "true" ] || { echo "$page" >&2; sleep 5; continue; }
  echo "$page" | jq -c '.messages[]'                       # 处理消息
  cursor=$(echo "$page" | jq -r '.next_cursor')
  [ "$(echo "$page" | jq -r '.has_more')" = "true" ] || sleep 1
done
```

> 前提：**有人跑着接收方**（`clawbot run`）。它默认不回复任何消息，只把消息送进收件箱。
> 没在跑时 `send` 照常可用，但手机发来的消息不会进收件箱（`status --json` 的 `receiver.running` 会告诉你）。

---

## 1. 它是管道

**收**（长轮询）→ **交出去**（收件箱，有序、可多读者、不丢不重复投递）→ **发**（文本/图片/文件）→
**失败显式**（`ok=false`+`stage`，或 `truncated=true`）。

它**不做**：不解释内容、不决定回什么、不存业务状态、**默认不替消费者回话**、不主动发起消息、不绑定任何 agent 框架。
判断标准：**在搬运消息 → 进管道；在替使用者做决定 → 放消费者那边。**

| 表现 | 落点 |
|---|---|
| `clawbot run` 默认不加载钩子 → 收到消息零回复 | `--handler` 默认为空；`test_interface.py` 用真实子进程钉住"默认静默" |
| 收件箱只存传输层事实（谁/何时/什么类型/文本） | `inbox_record` schema |
| 不主动说话（无定时器、无定时发送） | 全仓无调度代码 |
| 消费者行为在管道之外 | `examples/*`、`.dsh/skills/<unit>` |

---

## 2. 调用约定（所有命令一致）

```bash
clawbot <命令> [参数] [--json] [--state <state 目录>]
```

| 约定 | 规则 |
|---|---|
| 入口 | `clawbot`（**已装成包**，PATH 上有）/ `python -m clawbot` / `python <repo-root>` —— 三者等价 |
| `--json` | **stdout 只有一行 JSON**，无任何其他输出；人类可读输出走不带 `--json` 的模式 |
| 日志 | 一律写 stderr（节点运行日志、告警都在这里，不污染 stdout） |
| 编码 | UTF-8，中文不转义（`ensure_ascii=False`） |
| 退出码 | `0` 成功；`1` 业务失败（未登录 / 发送失败 / 文件不存在 / 没有对端）；`2` 用法错误 |
| 参数错误 | 同样走 JSON（`code=bad_usage`）+ 退出码 2 |
| 时间戳 | 秒为单位的浮点数（`received_at`），毫秒字段保留协议原值（`create_time_ms`） |
| `--state` | 状态目录，**写在子命令前或后都行**。**所有部件必须指向同一个目录**，否则各自看到不同的对端与收件箱 |

> 失败也必须**可判定**：`--json` 模式下永远有 `ok` 字段；不要靠解析 stderr 判断成败。

---

## 3. 命令

### 3.1 `clawbot send`

```bash
clawbot send --text "会议改到三点" --to <peer_id> --json
echo "多行文本" | clawbot send --text - --to <peer_id> --json
clawbot send 报告.pdf --to <peer_id> --json
clawbot send 截图.png --image --caption "这是今天的截图" --json
```

| 参数 | 说明 |
|---|---|
| 位置参数 `path` | 本地文件路径（与 `--text` 二选一） |
| `--text TEXT` | 直接发文本；`--text -` 从 stdin 读（多行文本用这个，别让 shell 转义） |
| `--to PEER` | 目标对端；省略时要求 state 里**恰好只有一个**对端，否则报 `no_peer` |
| `--image` | 按图片发送（默认按文件附件发） |
| `--caption TEXT` | 先发一条独立的说明文字（微信一条消息只带一个 item，所以是两条） |

输出（[`$defs.send`](clawbot/interface.schema.json)）：

```json
{"schema":1,"ok":true,"peer_id":"peer0000…@im.wechat","message_id":1234567890123456789,"stage":"send","attempts":1,"error":null}
```

失败示例（退出码 1）：

```json
{"schema":1,"ok":false,"peer_id":"peer0000…@im.wechat","message_id":null,"stage":"upload","attempts":3,"error":"CDN 上传失败（尝试 3 次）：HTTP 503 …"}
```

`stage` 是**失败发生在哪一段**，三选一：`read`（读本地文件）/ `upload`（加密或 CDN）/ `send`（发消息本身）。
这是排错的第一现场：`read` 说明路径或大小上限有问题，`upload` 说明网络/CDN，`send` 说明协议层拒绝了。

### 3.2 `clawbot inbox`

```bash
clawbot inbox --json                       # 从头读（最多 100 条）
clawbot inbox --json --since 42 --limit 20 # 接着读
clawbot inbox --json --peer <peer_id>      # 只看某个对端
clawbot inbox --follow --json              # 阻塞等待，每来一条输出一行
```

输出（[`$defs.inbox`](clawbot/interface.schema.json)）：

```json
{"schema":1,"ok":true,
 "messages":[{"schema":1,"n":43,"message_id":123456,"peer_id":"peer0000…@im.wechat",
              "kind":"text","text":"你好","seq":7,"create_time_ms":1700000000000,
              "received_at":1700000000.00}],
 "next_cursor":"43","has_more":false,"truncated":false,"first_n":1,"last_n":43}
```

**游标语义**（这是接口里最容易用错的地方，所以写死）：

| 字段 | 含义 |
|---|---|
| `next_cursor` | 不透明字符串，下次原样传给 `--since` 即可 |
| `has_more` | `true` = 还有没读完的（`--limit` 截断了），立刻可以再读一次 |
| `truncated` | `true` = **你给的游标已经被轮转掉了**，中间有消息永久错过。必须与"没有新消息"区分开 |
| `n` | 记录序号，单调递增、跨重启不回退。收件箱轮转后从某个更大的值开始 |

消费侧的正确姿势：

```bash
cursor=""            # 首次为空 = 从头读
while :; do
  page=$(clawbot inbox --json --since "$cursor")
  echo "$page" | jq -c '.messages[]'      # 处理消息
  cursor=$(echo "$page" | jq -r '.next_cursor')
  [ "$(echo "$page" | jq -r '.has_more')" = "true" ] || sleep 1
done
```

> 收到 `truncated: true` 说明你读得太慢（或很久没跑），收件箱已经轮转。
> 默认上限 2 MiB / 保留最近 2000 条；调用方要长期归档请自己落库。

### 3.3 `clawbot peers`

```json
{"schema":1,"ok":true,"peers":[{"peer_id":"peer0000…@im.wechat","has_context_token":true,"updated_at":1700000000.00}]}
```

`has_context_token=false` 表示：能收到它的消息，但**还不能主动发**（协议要求回复必须带回入站消息里的会话令牌）。
先让它发一条消息即可。

### 3.4 `clawbot status`

```json
{"schema":1,"ok":true,"state_dir":"<repo-root>/state","logged_in":true,
 "credentials":{"bot_token":"EXAM…0000(len=58)","ilink_bot_id":"EXAM….bot(len=19)","ilink_user_id":"peer0000….wechat","baseurl":"https://ilinkai.weixin.qq.com"},
 "cursor":"CURSOR-EXAMPLE","peers":["peer0000…@im.wechat"],
 "inbox":{"records":43,"first_n":1,"last_n":43,"bytes":8123,"path":"…\\inbox.jsonl"},
 "receiver":{"running":true,"pid":40956}}
```

`receiver` 告诉你**接收方（长轮询进程）现在在不在跑**：
`running=false` 时手机发来的消息不会进收件箱（`send` 仍然可用）。
不要让运维去手工解析 `run.lock` —— 那个文件的锁字节会让 `Get-Content` 之类直接报
"另一个进程锁定了文件的一部分"；pid 只从这个字段拿。

**凭证永远是脱敏值**，任何命令、任何日志都不会输出明文 `bot_token`；收件箱记录里也**不含**会话令牌
（回复时由 `send` 自己解析）。

### 3.5 `clawbot login` / `run` / `reset`

| 命令 | 说明 |
|---|---|
| `login` | 扫码登录（人机交互；只有首次或换账号时才需要） |
| `run` | **长驻接收进程**。同一个 state 目录同时只允许一个（第二个会被明确拒绝，避免重复回复） |
| `reset` | 清凭证 + 游标 + 对端 + 去重表 + **收件箱** + 二维码图片（换账号时用） |

---

## 4. 收件箱记录 schema v1

`state/inbox.jsonl` 每行一个 JSON 对象（append-only，一行一条）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema` | int | 记录版本，恒为 `1` |
| `n` | int | 记录序号（游标单位），从 1 开始单调递增 |
| `message_id` | int | 微信消息 id（**稳定的去重键**，上层可据此做精确一次） |
| `peer_id` | string | 发送者 |
| `kind` | string | `text` / `image` / `voice` / `file` / `video` / `unknown` |
| `text` | string | 文本内容；语音是转写文本；非文本消息为空串 |
| `media_kind` | string | 有媒体时才有：`image` / `file` / `video` / `voice` |
| `media_path` | string | 媒体**落盘路径**（取回并解密成功时才有） |
| `media_bytes` | int | 明文字节数（与 `media_path` 同时出现） |
| `media_error` | string | 取媒体失败的原因（**消息仍已入收件箱**，只是图没取到） |
| `seq` | int | 协议序号（可能为 0） |
| `create_time_ms` | int | 对方发送时间（毫秒） |
| `received_at` | float | 本机收到并落盘的时间（秒） |

> 记录里**没有** `context_token`：回复走 `clawbot send`，令牌由接口自己解析。
> 这样会话令牌不会扩散到更多文件里。

---

## 5. 生命周期与并发

```
手机 ──长轮询──▶ clawbot run ──追加──▶ state/inbox.jsonl ──读游标──▶ 其他部件
                     │
                     └──钩子（同进程可选）──▶ 立即回复
其他部件 ──clawbot send──▶ 微信
```

| 规则 | 说明 |
|---|---|
| 接收方单实例 | 同一 state 目录只允许一个 `run()`（`state/run.lock`）。第二个实例启动即报错**并带上持有者 pid**，不会安静地重复消费 |
| 发送方可并发 | 多个进程可以同时 `send`；会话令牌的读写是"读—改—写"，极端并发下仍可能互相覆盖（已知边界） |
| 收件箱多读者 | 各自记自己的 `n`，互不加锁。文件轮转时老读者会收到 `truncated: true` |
| 至少一次 | 消息是"至少一次"语义；节点内按 `message_id` 去重（最近 500 条）挡掉重放。要精确一次请上层长期记 `message_id` |
| 不需要同时启动 | 收件箱是文件，`run` 不在线时 `inbox` 也能读到历史 |

### 5.1 常驻接收方怎么运维

```powershell
# 起（Windows，后台 + 日志）。默认不加载钩子 = 纯管道：消息只进收件箱，不自动回复
Start-Process -FilePath python -ArgumentList '-m','clawbot','run' `
  -WorkingDirectory <repo-root> `
  -RedirectStandardOutput <repo-root>\logs\run.log `
  -RedirectStandardError  <repo-root> -WindowStyle Hidden

# 看活着没 / 看日志
clawbot status --json                 # receiver.running / receiver.pid
Get-Content <repo-root>\logs\run.log -Tail 20

# 停（pid 从 status 拿，别去解析 run.lock）
Stop-Process -Id <pid>
```

| 事实 | 说明 |
|---|---|
| 日志 | `PHONE/logs/run.log`（stdout）与 `run.err.log`（stderr），已进 `.gitignore` |
| 持有者 | `clawbot status --json` 的 `receiver.pid`（`run.lock` 被锁字节挡着，shell 工具读不了它） |
| 崩溃/重启 | 锁由操作系统在进程退出时释放，**不会锁死**；开机不会自动起（需要的话加计划任务或等中控机 systemd） |
| 没在跑会怎样 | 手机发来的消息不会进收件箱（`send` 仍然可用，因为它不需要接收方） |

---

## 6. 错误码

| `code` | 退出码 | 触发 |
|---|---|---|
| `bad_usage` | 2 | 参数不对（既没给路径也没给 `--text`、`--since` 不是整数…） |
| `not_logged_in` | 1 | 没有有效凭证 → 先 `clawbot login` |
| `no_peer` | 1 | 没给 `--to` 且 state 里不是唯一对端 → 用 `clawbot peers` 选一个 |
| `file_not_found` | 1 | `send` 的文件路径不存在 |
| `failed` | 1 | 其它（含发送失败；细节看 `stage` 与 `error`） |

---

## 7. 兼容承诺

1. `schema: 1` 期间**只增字段、不改语义**：新增字段一定给默认值，消费方按需读取即可。
2. 破坏性变更（删字段、改含义、改退出码）→ `schema` 抬到 `2`，并在本文件里写迁移说明；旧版本至少保留一个大版本。
3. 命令名、参数名、`code` 枚举值属于接口；内部实现（模块/类/函数）不属于。

## 8. 明确不承诺的

- **群聊 / 多账号路由**：按单账号私聊设计。
- **语音发送**（缺 `encode_type`/`sample_rate`/`playtime` 等元数据）与**缩略图**：未实现。
- **视频发送**：item 已按协议实现，但未在真实服务端验证过。
- **入站媒体的下载解密**：~~未实现~~ → **已实现（2026-10-16）**：收到图片/文件/视频时，接收方会去 CDN 取密文、用消息里带的 `aes_key` 做 AES-128-ECB 解密，落到 `<state>/media/<message_id>.<ext>`，并把 `media_path` 等字段写进收件箱记录。落盘目录默认 `<state>/media`（`run --media-dir <dir>` 换地方，`--no-media` 关掉）；**取不下来不丢消息**：记录里写 `media_error`，消息照常入收件箱。
- **精确一次**：见 §5。
- **速率**：服务端未公开限制，官方条款保留调整权力。

---

## 9. 部署形态

本接口与机器无关：状态目录默认 `<repo-root>/state`，可用 `--state <目录>` 指到任意位置。
常驻方式随意（systemd 用户单元、计划任务、容器），只要保证**同时只有一个接收方实例**
在轮询同一份状态目录即可。

# 开发机：打包 + 上传 + 远端装配
powershell -File <你的发布脚本>
```

```bash
# 中控机：切换接收方（★ 先停掉旧机器上的 clawbot run）
systemctl --user enable --now <unit>
clawbot status --json                      # logged_in / receiver.running / inbox.last_n
tail -n 20 <repo-root>/logs/phone.log      # 或 journalctl --user -u <unit> -n 20

# 回滚（状态在外置目录，不受影响）
ln -sfn <部署目录>/current && systemctl --user restart <unit>
```
