"""Constants for the XiaoYi Bridge integration."""

DOMAIN = "hass_xiaoyi_bridge"

# ─── Config entry data（连接参数：影响集成能否正常工作）──────────────────────
CONF_WS_URL1 = "ws_url1"
CONF_WS_URL2 = "ws_url2"
CONF_AK = "ak"
CONF_SK = "sk"
CONF_AGENT_ID = "agent_id"

# ─── Config entry options（偏好设置）────────────────────────────────────────
CONF_LANGUAGE = "language"
CONF_HA_AGENT_ID = "ha_agent_id"
CONF_STATUS_UPDATES = "status_updates"
CONF_RECONNECT_MAX = "reconnect_max"
CONF_HEARTBEAT_INTERVAL = "heartbeat_interval"
CONF_HEARTBEAT_FIRST_DELAY = "heartbeat_first_delay"
CONF_SINGLE_SERVER = "single_server"
CONF_SKIP_INIT = "skip_init"
CONF_API_ID = "api_id"
CONF_PUSH_ID = "push_id"

# ─── Defaults ───────────────────────────────────────────────────────────────
DEFAULT_WS_URL_1 = "wss://hag.cloud.huawei.com/openclaw/v1/ws/link"
DEFAULT_WS_URL_2 = "wss://116.63.174.231/openclaw/v1/ws/link"
# 空字符串 = 跟随 Home Assistant 的语言（hass.config.language）
DEFAULT_LANGUAGE = "auto"
DEFAULT_HA_AGENT_ID = ""
DEFAULT_STATUS_UPDATES = True
DEFAULT_RECONNECT_MAX = 50
# 参照小艺 OpenClaw Channel 参考实现的应用心跳节奏（20s）。
# 小艺服务端会关闭“长时间没有应用层心跳”的长连接，30s 会被动断连，
# 这正是历史上心跳告警刷屏的根因之一。
DEFAULT_HEARTBEAT_INTERVAL = 20
# 连接建立后首拍延迟。默认 1s 让服务端尽快看到应用层流量；
# 如果平台的连接需要“静默”一段时间（诊断用），可在选项里调大。
DEFAULT_HEARTBEAT_FIRST_DELAY = 1
# 只连接主端点（server1），失败才回退到备用端点。
# 用于确认平台是否限制“同一智能体只能有一个会话”。
DEFAULT_SINGLE_SERVER = False
# 诊断用：连接后不发送 clawd_bot_init，用来判断“连上即断”是否由该帧触发。
DEFAULT_SKIP_INIT = False
DEFAULT_API_ID = ""
# 可选：静态 push_id。正常情况下 push_id 由用户消息的 data part 下发，
# 这里只作为兜底（例如平台升级后消息里暂时没有该字段）。
DEFAULT_PUSH_ID = ""

# async_converse() 接受的 HA 语言码（BCP 47）。选项值必须是小写翻译键，
# 因此内部用下划线写法，再经 LANGUAGE_MAP 映射成真正的语言码。
# "auto" = 跟随 Home Assistant 的语言设置（hass.config.language）。
LANGUAGE_OPTIONS = ["auto", "zh_hans", "zh_hant", "en", "ja", "ko", "de", "fr"]
LANGUAGE_AUTO = "auto"
LANGUAGE_MAP = {
    "auto": None,
    "zh_hans": "zh-Hans",
    "zh_hant": "zh-Hant",
    "en": "en",
    "ja": "ja",
    "ko": "ko",
    "de": "de",
    "fr": "fr",
}

# ─── A2A protocol constants ────────────────────────────────────────────────
A2A_MSG_TYPE_INIT = "clawd_bot_init"
A2A_MSG_TYPE_HEARTBEAT = "heartbeat"
A2A_MSG_TYPE_AGENT_RESPONSE = "agent_response"
A2A_METHOD_MESSAGE_STREAM = "message/stream"
A2A_METHOD_CLEAR_CONTEXT = "clearContext"
A2A_METHOD_TASKS_CANCEL = "tasks/cancel"

# ─── WebSocket connection constants ────────────────────────────────────────
WS_CONNECT_TIMEOUT = 30  # seconds
WS_RECONNECT_BASE_DELAY = 2  # seconds
WS_RECONNECT_MAX_DELAY = 60  # seconds
WS_RECONNECT_JITTER = 0.2  # ±20% jitter, avoids both servers retrying in lockstep
WS_STABLE_THRESHOLD = 10  # seconds; resets the reconnect counter when stable

# 达到 reconnect_max 后不再永久放弃：冷却这么久再重置计数继续重连。
# 永久放弃会让桥接静默死亡，只能靠重启 HA 恢复。
WS_GIVE_UP_COOLDOWN = 900  # seconds (15 min)

# “连上后立刻被关闭”判定：init 发出去后在窗口内被服务端关闭即计数。
# 连续多次说明 AK/SK 无效或智能体未上架，转 reauth 而不是无限重连刷屏。
WS_FAST_CLOSE_WINDOW = 10  # seconds
WS_FAST_CLOSE_THRESHOLD = 3  # consecutive fast closes → auth/config failure
WS_AUTH_BACKOFF = 600  # seconds; pause reconnects after an auth failure
# 暂停到期后再等这么久才真正重连，避免计时抖动导致“刚好差 1ms 仍在暂停”而永久停摆。
WS_RESUME_GRACE = 1  # seconds

# ─── Heartbeat ─────────────────────────────────────────────────────────────
# 连接成功后立刻发第一条心跳（不要等满一个间隔），
# 让服务端尽快看到应用层流量。
HEARTBEAT_FIRST_DELAY = 1  # seconds
HEARTBEAT_MAX_FAILURES = 3  # transient send failures tolerated per interval

# ─── Sessions ──────────────────────────────────────────────────────────────
# clearContext 后不立即删会话（任务可能还在跑），延迟清理。
# 与小艺 OpenClaw Channel 参考实现一致（1 小时）。
SESSION_CLEANUP_TIMEOUT = 3600  # seconds

# ─── Conversation ──────────────────────────────────────────────────────────
# 单轮 Assist 处理超时；超时后回语音“处理超时”，避免会话锁被永久占用。
CONVERSE_TIMEOUT = 60  # seconds

# 卸载/重载时等待在途对话任务退出的上限（秒）：先 cancel，再最多等这么久，
# 避免保存选项（reload）或重启 HA 时被在途对话拖住最多 CONVERSE_TIMEOUT。
SHUTDOWN_TASK_GRACE = 2  # seconds

# ─── Log throttling ────────────────────────────────────────────────────────
# 同类告警的静默窗口：首条立即记录，窗口内重复只累计，窗口末输出汇总。
LOG_THROTTLE_COOLDOWN = 300  # seconds

# ─── 握手被拒时的排障提示 ──────────────────────────────────────────────────
# 小艺网关对所有被拒绝的握手都返回同一个 401
# （{"code":"badRequest","desc":"Resource is not found or param error"}），
# 因此“401”不等于“AK/SK 一定错”。按开放平台的创建顺序逐项核对最有效。
AUTH_FAILURE_HINT = (
    "请逐项确认：① Agent ID / AK / SK 与开放平台一致且没有多余空格；"
    "② 智能体已上架（发布真机测试）且当前账号在测试白名单内；"
    "③ 创建凭证时「服务平台」必须选“Web端（JS API）”——选错平台会一直 401。"
    "网关对所有拒绝都返回同样内容，需要时可运行 tools/xiaoyi_probe.py 查看原始返回。"
)

# ─── Push service (小艺主动推送) ───────────────────────────────────────────
DEFAULT_PUSH_URL = "https://hag.cloud.huawei.com/open-ability-agent/v1/agent-webhook"
PUSH_TIMEOUT = 10  # seconds

# ─── Services ──────────────────────────────────────────────────────────────
SERVICE_SEND_PUSH = "send_push"
SERVICE_RECONNECT = "reconnect"

ATTR_MESSAGE = "message"
ATTR_PUSH_TEXT = "push_text"
ATTR_SESSION_ID = "session_id"
ATTR_API_ID = "api_id"
ATTR_ENTRY_ID = "entry_id"
ATTR_PUSH_ID = "push_id"
