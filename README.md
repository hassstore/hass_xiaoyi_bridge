# 小艺桥接 XiaoYi Bridge

把华为**小艺**（XiaoYi / Celia）的 A2A 语音指令桥接到 Home Assistant 的 Assist 语音管道：你在手机上对小艺说话，小艺通过 WebSocket A2A 连接把语音文本转发给本集成，集成调用 Home Assistant 的 `async_converse()` 完成意图处理，再把 Home Assistant 的语音回答回传给小艺播报出来。

```text
小艺 App ──── 语音指令 ──▶ A2A WebSocket ──▶ 本集成 ──▶ HA Assist 管道
小艺 App ◀─── 语音播报 ──◀ A2A WebSocket ◀── 本集成 ◀── response.speech
```

- 集成域名（domain）：`hass_xiaoyi_bridge`
- 协议依据：[小艺开放平台 OpenClaw 模式文档](https://developer.huawei.com/consumer/cn/doc/doccenter-celia/openclaw-0000002518410344) 与参考插件 `@ynhcj/xiaoyi`
- 仅支持配置条目（config entry）方式，不支持 YAML 配置

## 功能特性

- 完整实现小艺 OpenClaw 模式 A2A 协议：双 WebSocket 服务器高可用、AK/SK HMAC-SHA256 认证、应用层心跳、JSON-RPC `message/stream`
- 语音指令直达 Home Assistant Assist 管道，保留多轮对话上下文
- 可指定任意 HA 对话代理（默认 Assist，也可换成 `conversation.openai_conversation` 等 LLM 代理）
- 处理语言可跟随 Home Assistant，或固定为 zh-Hans / zh-Hant / en / ja / ko / de / fr
- 主动推送：通过小艺 agent-webhook 向用户发送通知（如门铃响了、洗衣结束）
- 连接状态二进制传感器与脱敏诊断下载，排障不用翻日志
- 凭据失效时自动引导重新认证（reauth）
- 面向生产的可靠性：指数退避重连（±20% 抖动）、达到上限冷却 15 分钟后继续而非永久放弃、同类告警限流、“秒断”自动升级为凭据问题

## 安装

要求 Home Assistant **2025.8.0** 或更新版本。依赖的 `websockets` 库与 `conversation` 组件会自动安装/加载。

### 通过 HACS 安装

1. HACS → 集成（Integrations）→ 右上角 ⋮ → 自定义存储库（Custom repositories）
2. 添加 `https://github.com/hassstore/hass_xiaoyi_bridge`，类别选择 Integration
3. 找到并安装 “XiaoYi Bridge”，然后重启 Home Assistant

### 手动安装

1. 下载本仓库，将 `custom_components/hass_xiaoyi_bridge` 整个目录复制到 Home Assistant 配置目录的 `custom_components/` 下
2. 重启 Home Assistant

### 旧版用户注意（domain 变更）

本项目早期版本使用域名 `ha_xiaoyi_bridge`，现域名已改为 `hass_xiaoyi_bridge`。如果你安装过旧版本，请先在 设置 → 设备与服务 中**删除旧的 `ha_xiaoyi_bridge` 配置条目**，再按下面步骤添加本集成——两个域名不同，配置条目无法自动迁移。

## 配置

### 第一步：在小艺开放平台创建 OpenClaw 智能体

参考[小艺开放平台 OpenClaw 模式文档](https://developer.huawei.com/consumer/cn/doc/doccenter-celia/openclaw-0000002518410344)：

1. 登录[小艺开放平台能力门户](https://developer.huawei.com/consumer/cn/hag/abilityportal)
2. 创建一个 **OpenClaw 模式**智能体（每个账号只能创建一个 OpenClaw 模式智能体），并复制它的 Agent ID
3. 为该智能体创建凭证（AK/SK）——**仅显示一次**，请妥善保存；之后重新创建凭证会使旧凭证立即失效
4. 发布智能体，并把测试账号加入测试白名单——未上架、未加白的连接会被服务端立即关闭（见[故障排查](#故障排查)）

### 第二步：在 Home Assistant 中添加集成

设置 → 设备与服务 → 添加集成 → 搜索 “XiaoYi Bridge”，填写：

| 字段 | 说明 |
| --- | --- |
| `ws_url1` | 主 WebSocket 服务器，默认 `wss://hag.cloud.huawei.com/openclaw/v1/ws/link` |
| `ws_url2` | 备用 WebSocket 服务器（高可用），默认 `wss://116.63.174.231/openclaw/v1/ws/link` |
| `ak` | 小艺开放平台创建的 Access Key |
| `sk` | 小艺开放平台创建的 Secret Key |
| `agent_id` | OpenClaw 模式智能体的 Agent ID |

保存前，集成会真实连接小艺服务器验证这组参数，通过后创建配置条目（标题为 `小艺桥接 (<agent_id>)`）。可能的错误：

| 错误 | 含义 |
| --- | --- |
| `missing_required` | AK、SK、Agent ID 未填写 |
| `invalid_url` | WebSocket 地址不是 `ws://` 或 `wss://` 开头 |
| `invalid_auth` | 小艺服务器拒绝了这组凭证（检查 AK/SK，并确认智能体已上架） |
| `cannot_connect` | 当前无法连接小艺服务器（网络或服务端暂不可用） |

### 凭据失效：重新认证（Reauth）

当服务端拒绝凭据时，集成会自动弹出“重新配置小艺凭证”卡片；也可以随时在集成卡片的 ⋮ 菜单手动选择“重新认证”。填入新的 AK/SK，验证通过后自动保存并重载条目。

> 注意：在小艺开放平台创建新凭证会使旧凭证立即失效，请一次性完成替换。

### 更换服务器地址：重新配置（Reconfigure）

集成卡片 ⋮ 菜单 → “重新配置”（Reconfigure），可修改主/备两个 WebSocket 地址，保存前同样会先验证连通性。

### 选项设置（Options）

集成卡片齿轮图标 → “配置选项”：

| 选项 | 默认值 | 说明 |
| --- | --- | --- |
| 处理语言 `language` | 跟随 Home Assistant | 传给 Assist 管线的语言；`auto` 跟随 `hass.config.language`，也可固定为 zh-Hans / zh-Hant / en / ja / ko / de / fr |
| HA 对话代理 ID `ha_agent_id` | 空 | 指定对话代理，例如 `conversation.openai_conversation`；留空使用默认 Assist 代理 |
| 推送 API ID `api_id` | 空 | 小艺开放平台上的 API ID，`hass_xiaoyi_bridge.send_push` 服务需要它 |
| 发送处理状态更新 `status_updates` | 开 | 处理过程中向小艺回发“正在处理 / 处理完成”等状态 |
| 心跳间隔 `heartbeat_interval` | 20 秒 | 应用层心跳周期，可设 10–120 秒；小艺服务端会关闭没有应用层流量的长连接，建议不超过 30 秒 |
| 最大重连次数 `reconnect_max` | 50 | 达到该次数后暂停 15 分钟再继续重试（不会永久放弃） |

保存选项会重载配置条目（桥接会重连一次），新设置立即生效。

## 使用

配置完成后，直接在小艺中对智能体说话即可，例如“打开客厅灯”“现在家里几度”。

### 实体

每个配置条目提供一个诊断类二进制传感器 **`<设备名> 连接状态`**（`device_class: connectivity`）：至少一条 A2A 服务器连接就绪时为“开”。设备名即配置条目标题（`小艺桥接 (<agent_id>)`）。可以把它放进仪表盘，或用作自动化触发条件（例如断开时推送提醒）。

### 服务

#### `hass_xiaoyi_bridge.send_push`

通过小艺 agent-webhook 向用户主动推送通知。

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `message` | 是 | 通知正文内容 |
| `push_text` | 否 | 展示给用户的简短文本，默认取 `message` 前 50 个字符 |
| `session_id` | 否 | 推送目标的小艺会话，默认使用最近出现过的会话 |
| `api_id` | 否 | 覆盖集成选项中的推送 API ID |
| `entry_id` | 否 | 配置了多个条目时，指定要使用的条目 |

前提条件：选项里配置了推送 API ID，并且已经收到过至少一条小艺消息——小艺会在消息的 data part 里带上 push ID（`variables.systemVariables.push_id`），主动推送需要它。

#### `hass_xiaoyi_bridge.reconnect`

断开两条 A2A WebSocket 连接并重新连接。字段：`entry_id`（可选，多条目时指定）。

### 自动化示例

门铃被按下时推送到小艺：

```yaml
automation:
  - alias: "小艺推送 - 门铃被按下"
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: hass_xiaoyi_bridge.send_push
        data:
          message: "有人按了门铃，请查看门口画面。"
          push_text: "门铃响了"
```

连接意外断开时自动重连并通知（`entity_id` 请替换为实际的连接状态传感器）：

```yaml
automation:
  - alias: "小艺桥接断开时重连"
    triggers:
      - trigger: state
        entity_id: binary_sensor.xiaoyi_bridge_lian_jie_zhuang_tai  # 替换为实际实体 ID
        to: "off"
        for: "00:01:00"
    actions:
      - action: hass_xiaoyi_bridge.reconnect
```

## 工作原理

### 整体流程

```text
小艺 ─ utterance ─▶ wss A2A ─▶ 本集成 ─▶ async_converse() ─▶ HA 设备/意图处理
小艺 ◀─ speech ── wss A2A ◀─ 本集成 ◀─ result.response.speech ─┘
```

### A2A 连接与握手

- 两个服务器端点**同时连接**（高可用）：主 `wss://hag.cloud.huawei.com/openclaw/v1/ws/link`，备 `wss://116.63.174.231/openclaw/v1/ws/link`。应答始终沿“该会话来自的服务器”原路返回。
- 认证请求头：
  - `x-access-key`：AK
  - `x-sign`：`Base64(HMAC-SHA256(sk, timestamp-ms))`
  - `x-ts`：毫秒时间戳
  - `x-agent-id`：Agent ID
- 连接建立后先发送初始化帧：

  ```json
  {"msgType": "clawd_bot_init", "agentId": "your-agent-id"}
  ```

- 随后发送应用层心跳，第一条在连接成功后约 1 秒发出，之后按 `heartbeat_interval`（默认 20 秒）循环：

  ```json
  {"msgType": "heartbeat", "agentId": "your-agent-id", "timestamp": 1760000000000}
  ```

### 请求：JSON-RPC `message/stream`

小艺的语音指令以 JSON-RPC 2.0 `message/stream` 方法到达，内容放在 `parts` 里（text / file / data）：

```json
{
  "agentId": "your-agent-id",
  "jsonrpc": "2.0",
  "id": "msg-123",
  "method": "message/stream",
  "sessionId": "session-789",
  "params": {
    "id": "task-456",
    "sessionId": "session-789",
    "message": {
      "role": "user",
      "parts": [{ "kind": "text", "text": "打开客厅灯" }]
    }
  }
}
```

其中 data part 的 `variables.systemVariables.push_id` 会带上推送 ID，主动推送时需要它。

### 应答：`agent_response` 帧

回答以 `msgType` 为 `agent_response` 的帧回传，`msgDetail` 是一段 JSON-RPC artifact-update（最终分片，`append=false`、`lastChunk=true`、`final=true`）：

```json
{
  "msgType": "agent_response",
  "agentId": "your-agent-id",
  "sessionId": "session-789",
  "taskId": "task-456",
  "msgDetail": "{\"jsonrpc\":\"2.0\",\"id\":\"msg-123\",\"result\":{\"taskId\":\"task-456\",\"kind\":\"artifact-update\",\"append\":false,\"lastChunk\":true,\"final\":true,\"artifact\":{\"artifactId\":\"artifact_...\",\"parts\":[{\"kind\":\"text\",\"text\":\"已经为你打开客厅灯\"}]}}}"
}
```

选项开启时，处理期间还会发送 `kind: status-update` 状态帧（state 为 `working` / `completed`）。

### 控制消息与会话

- `clearContext`：立即应答，并删除 Home Assistant 侧的会话映射（多轮上下文清零）；小艺侧的“会话 → 服务器”路由映射安排 1 小时后的延迟清理——进行中的任务仍可能先回答完。
- `tasks/cancel`：立即应答。Home Assistant 无法取消正在进行的 Assist 会话，因此回答仍会在就绪后送达（已知限制）。
- 每个小艺会话映射到一个 Home Assistant `conversation_id`，多轮对话上下文得以保留；同一会话的消息串行处理，不同会话互不阻塞。

### 主动推送

通过小艺 agent-webhook 发送（由 `hass_xiaoyi_bridge.send_push` 服务封装）：

```text
POST https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook
```

认证复用 AK/SK 签名，负载引用选项里配置的 `apiId` 和从小艺消息中观察到的 `pushId`。

## 可靠性与日志

这一版的焦点是“长稳”与“日志干净”，每一项都对应真实使用中被报告过的问题：

**心跳节奏对齐参考实现。** 小艺服务端会关闭“长时间没有应用层流量”的长连接。集成在连接成功后约 1 秒发出第一条应用层心跳，之后默认每 20 秒一条（与参考插件 `@ynhcj/xiaoyi` 一致）；协议级 WebSocket ping/pong 被有意关闭（`ping_interval=None`）——会吞掉 pong 帧的中间网络设备反而会造成“幽灵断连”。连接存活的判定标准是“心跳发送成功”。

**指数退避 + 抖动，永不永久放弃。** 重连间隔从 2 秒起指数增长（上限 60 秒），并叠加 ±20% 随机抖动，避免两台服务器同步重试、放大服务端波动。前 3 次重连按 debug 记录，之后才输出告警。达到 `reconnect_max`（默认 50）次后不是永久放弃，而是暂停 15 分钟再重置计数继续重试——永久放弃会让桥接静默死亡，只能靠重启 Home Assistant 恢复。

**同类告警全部限流。** 所有周期性重复的告警（心跳发送失败、重连失败等）都经过限流器：首条完整记录，5 分钟内的重复只累计、不输出，窗口结束后补一条带被抑制条数的汇总行。这正是修复 “1730 occurrences of Heartbeat send failed” 这类日志刷屏的关键：

```text
[server1] Heartbeat send failed (received 1000 (OK); then sent 1000 (OK)): connection lost
[server1] Heartbeat send failed ...: connection lost (suppressed 41 similar messages in the last 300s)
```

**连续“秒断”按凭据问题处理。** 如果一台服务器连续 3 次“连上后 10 秒内就被关闭”，集成不再无限重连，而是判定为凭据/智能体问题：输出一条可操作的错误（检查 AK/SK、确认智能体已上架）、弹出重新认证卡片，并暂停重连 10 分钟，避免持续冲击服务端。

**单轮超时与会话串行。** Assist 调用 60 秒超时，卡住的意图不会永久占用会话锁；同一会话串行处理，保证多轮上下文有序。

## 故障排查

### 小艺连上后立刻断开 / 一直重连

先看日志里的 `WebSocket closed by the server ... (code=..., reason=...)`：

- **握手阶段就被拒（HTTP 401）**：见下一节「配置时报『小艺服务器拒绝了这组凭证』」。
- **握手成功、随后被关闭（`code=1000`）**：说明 **AK/SK 是有效的**，问题在智能体或平台侧，按顺序排查：
  1. 智能体已上架（「发布真机测试」），当前账号在测试白名单内；
  2. **同一智能体只能有一个客户端在线**——确认没有第二个 Home Assistant、另一台设备上的集成实例，或 OpenClaw 插件同时连接同一个 agentId；
  3. 平台限流（短时间反复重连会被拒绝一段时间）。
- 集成对这种情况**不会**再弹「重新认证」：握手已经通过，凭证是好的；它只会按指数退避重连，并输出一条节流告警说明原因。

想区分「裸连接能否保持」「是心跳触发的关闭还是本来就被关闭」，运行：

```bash
# 只连接、不发任何帧
python tools/xiaoyi_probe.py --ak <AK> --sk <SK> --agent-id <AGENT_ID> --mode hold

# 连接 + clawd_bot_init（+0.2s）
python tools/xiaoyi_probe.py ... --mode init

# 与集成完全一致：init + 1s 后首次心跳、之后每 20s
python tools/xiaoyi_probe.py ... --mode full

# 两个端点同时保持，验证是否允许同一智能体多连接
python tools/xiaoyi_probe.py ... --mode hold --dual
```

脚本会逐帧打印服务端下发的消息、关闭码与存活时长。

### 配置时报「小艺服务器拒绝了这组凭证」

小艺网关对**所有**被拒绝的握手都返回同一个响应：

```text
HTTP 401  {"code":"badRequest","desc":"Resource is not found or param error"}
```

也就是说 **401 不等于「AK/SK 一定错」**，要按开放平台的创建顺序逐项核对：

1. **创建凭证时「服务平台」必须选「Web端（JS API）」**——选成 Android / iOS 等平台会一直 401，这是最常见的原因；
2. Agent ID、AK、SK 与开放平台逐字一致，复制时没有多余空格或换行；
3. 智能体已上架（「发布真机测试」），且当前账号在该智能体的测试白名单内；
4. Home Assistant 主机时间准确（签名里带毫秒时间戳，偏差建议小于 5 分钟）。

改完后在集成卡片上执行「重新认证 / 重新配置」，或删除条目重新添加。

需要看服务器原始返回时，在任意能出网的机器上运行诊断脚本（只需 `websockets`）：

```bash
python tools/xiaoyi_probe.py --ak <AK> --sk <SK> --agent-id <AGENT_ID>
```

脚本会打印两个端点的 HTTP 状态、`Server` 头、返回体与耗时，并附带一次「不带凭证」的对照请求；如果两者的返回完全相同，说明拒绝发生在鉴权网关，重点检查上面 1–3 项。集成日志里也会给出同样信息，例如：

```text
XiaoYi refused the handshake: server1 rejected the handshake (HTTP 401: badRequest: Resource is not found or param error)
```

### 小艺没有响应

1. 查看设备页面 `<设备名> 连接状态` 二进制传感器是否为“开”（至少一条 A2A 连接就绪）
2. 开启调试日志：

   ```yaml
   logger:
     custom_components.hass_xiaoyi_bridge: debug
   ```

3. 下载诊断数据：设备页面 ⋮ → 下载诊断数据。诊断包含连接状态与会话摘要，AK/SK/API ID 已脱敏。

### 已知限制

Home Assistant 无法取消正在进行的 Assist 会话，因此 `tasks/cancel` 会被应答，但回答仍会在就绪后送达小艺。

## 安全说明

- **凭证**：AK/SK 只保存在配置条目的数据里；诊断下载中的 AK/SK 与推送 `api_id` 均已脱敏。
- **传输加密**：两个默认端点都是 `wss://`。备用端点用 IP 直连（`wss://116.63.174.231/...`），证书没有 IP SAN，因此只关闭**主机名校验**，仍保留**证书链校验**；仅当该端点的证书链本身无法验证（自签等）时才会降级为不校验，并在日志里提示一次。
- **明文端点**：配置成 `ws://` 时不使用 TLS（适合本机代理/调试），生产环境请始终用 `wss://`。
- **语音内容**：用户语音与回答正文只写入 debug 日志，默认日志级别下不落盘。

## 卸载

1. HACS → 已安装 → 小艺桥接 → 移除（或直接删除 `custom_components/hass_xiaoyi_bridge` 目录）
2. 设置 → 设备与服务 → 小艺桥接 → 删除条目（会话表、诊断数据随之清除）
3. 重启 Home Assistant

## 许可证

MIT，见 [LICENSE](LICENSE)。

## 开发与测试

```bash
pip install -r requirements_test.txt
ruff check custom_components tests tools   # lint（最严格子集见 CI）
python tools/check_translations.py          # JSON + 翻译键/占位符一致性
python tools/xiaoyi_probe.py --ak … --sk … --agent-id …   # 握手诊断（需真实凭证）
pytest -q                                   # 130 个测试
```

- 测试基于 `pytest-homeassistant-custom-component`（版本固定在 `requirements_test.txt`）
- hassfest 校验见 `.github/workflows/hassfest.yml`
- 关键回归测试：首次心跳时延（不等待整个间隔）、默认 20s 节奏、两轮冷却恢复、认证暂停自动恢复、主动断开不误判为凭证失效、`ws://` 不传 `ssl`、IP 端点保留证书链校验、同会话串行、毫秒时间戳

代码结构：

```text
custom_components/hass_xiaoyi_bridge/
├── __init__.py        # 集成入口、服务注册、A2A → Assist 桥接逻辑
├── config_flow.py     # 配置流程（user / reauth / reconfigure / options）
├── const.py           # 常量与默认值
├── binary_sensor.py   # 连接状态传感器
├── diagnostics.py     # 脱敏诊断
├── a2a/               # A2A 协议层
│   ├── auth.py        # AK/SK HMAC-SHA256 签名
│   ├── websocket.py   # 双服务器连接管理、重连、会话路由
│   ├── heartbeat.py   # 应用层心跳
│   ├── message.py     # JSON-RPC 消息解析与构造
│   ├── session.py     # 会话 / conversation_id 管理
│   ├── push.py        # agent-webhook 主动推送
│   └── log_throttle.py # 日志限流
└── translations/      # 界面翻译（zh-Hans / en）
```

## 相关文档

- 小艺开放平台 OpenClaw 模式文档：<https://developer.huawei.com/consumer/cn/doc/doccenter-celia/openclaw-0000002518410344>
- 小艺 A2A 发起会话（message/stream）文档：<https://developer.huawei.com/consumer/cn/doc/service/message-stream-0000002505761434>
- 小艺开放平台能力门户：<https://developer.huawei.com/consumer/cn/hag/abilityportal>
- 参考插件（OpenClaw Channel）：`@ynhcj/xiaoyi`
- Home Assistant Assist / conversation 组件：<https://www.home-assistant.io/integrations/conversation/>
