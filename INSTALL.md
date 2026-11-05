# Claude Code → 钉钉 完成通知（自动安装指南）

> 本文档面向前来配置本通知脚本的智能体，例如另一台机器上的 Claude Code。
> 按照本文档，你可以把「每轮任务结束自动把状态/耗时/Token/结果推到钉钉」这项能力装到当前机器上。
> 所有步骤都不需要你阅读 Python 源码，也不需要用户手工填表。

---

## 0. 这个能力是什么

用户希望：**Claude Code 每次回答结束，自动往钉钉推一条消息**，内容包含：

- 状态（已完成 / 已中断）
- 耗时
- Token 用量（输入 / 输出 / 缓存）
- 工具调用统计
- 用户本轮提问
- 结果摘要

实现方式：一个独立的 Python 脚本 `notify-dingtalk.py`，挂在 Claude Code 的 `Stop` 和 `SessionStart` hook 上。脚本不依赖任何第三方库，只用标准库，Python 3.9+ 即可。

它**不通过 MCP server 运行**，而是由 Claude Code 内核直接 exec。MCP 只是钉钉一侧提供的 HTTP 接口。

---

## 1. 你需要先确认的三件事

在动手前，逐项确认。任何一项不满足，都要先问用户。

| # | 检查项 | 检查方式 |
|---|---|---|
| 1 | 机器上是否有 Python 3.9+ | `python3 --version` / `python --version` / `py -3 --version` |
| 2 | 是否有钉钉推送通道 | 问用户：有企业机器人 robotCode 吗？有群 webhook 吗？ |
| 3 | 是否有用户的钉钉 userId | 问用户。这是内部 staffId，不是手机号 |

**Python 路径要记下来**，后面写 hook 命令要用绝对路径。Windows 上通常类似 `C:/Python313/python.exe`；macOS/Linux 通常是 `/usr/bin/python3` 或 `python3`。

---

## 2. 确定用哪种通道

脚本支持三条通道，优先级从上到下，**填全了哪条就走哪条**：

| 通道 | 需要什么 | 特点 | 推荐度 |
|---|---|---|---|
| MCP | `mcp_url` + `mcp_robot_code` + `mcp_user_ids`（或群 ID） | 单聊，消息只进自己；一个 key 搞定，不需要 AppSecret | 首选 |
| 企业机器人 API | `app_key` + `app_secret` + `robot_code` + `user_id` | 单聊；需要在开发者后台开「机器人发单聊消息」权限 | 次选 |
| 群机器人 webhook | `webhook_url`（可选 `webhook_secret`） | **发到群里**，群成员都能看到 | 仅在用户明确接受打扰他人时使用 |

**决策顺序**：

1. 用户已经给了 MCP 地址 → 用 MCP 通道
2. 用户有 AppKey/AppSecret → 用企业机器人通道
3. 用户只想在自己的群/测试群看到 → 用 webhook

如果用户什么都没给，**先停下来问**，不要自己编造凭据，也不要尝试调用 `create_robot` 在用户组织里创建应用（那会真的新建一个企业内部应用）。

---

## 3. 安装步骤

### 3.1 放置文件

把这个目录下的两个文件复制到目标位置：

```
<目标目录>/notify-dingtalk.py
<目标目录>/notify-dingtalk-config.example.json   → 改名为 notify-dingtalk-config.json 后再填
```

**推荐位置**：`~/.claude/`（即 `$HOME/.claude`，Windows 是 `C:/Users/<用户名>/.claude`）。

脚本按以下顺序找配置，找到第一个就用：

1. 环境变量 `NOTIFY_DINGTALK_CONFIG` 指向的路径
2. 脚本同目录的 `notify-dingtalk-config.json`
3. `~/.claude/notify-dingtalk-config.json`

**推荐 `~/.claude/`**：这样脚本和配置都在同一个地方，多项目共用一份。

### 3.2 写配置文件

复制示例并填写。**只填用户实际拥有的通道，其余留空**。

```bash
cp notify-dingtalk-config.example.json notify-dingtalk-config.json
```

然后编辑 `notify-dingtalk-config.json`。

**MCP 通道示例**：

```json
{
  "mcp_url": "https://mcp-gw.dingtalk.com/server/<一串hash>?key=<key>",
  "mcp_robot_code": "dingxxxxxxxx",
  "mcp_user_ids": ["你的userId"],
  "mcp_open_conversation_id": ""
}
```

**关键类型约束**（写错会导致静默失败）：

| 字段 | 类型 | 常见错误 |
|---|---|---|
| `mcp_user_ids` | **字符串数组** | 写成 `"38124"` 字符串 → 校验会报错 |
| `mcp_robot_code` | 字符串 | — |
| `mcp_open_conversation_id` | 字符串 | 和 `mcp_user_ids` 二选一；两个都填时**优先单聊** |
| 所有布尔开关 | `true` / `false` | 不要写成 `"true"` 字符串 |

**凭据处理**：如果用户不想把密钥写在文件里，配置项可以写成 `${ENV_NAME}`，脚本启动时从同名环境变量读取：

```json
{ "app_key": "${DINGTALK_APP_KEY}", "app_secret": "${DINGTALK_APP_SECRET}" }
```

然后在 `~/.claude/settings.json` 的 `env` 块里设置这些变量。这样配置文件可以进 git。

### 3.3 挂 hook

编辑 `~/.claude/settings.json`，在 `hooks` 里加三条。**必须保留文件里已有的其它字段**（`env`、`permissions` 等），不要整体覆盖。

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<PYTHON> -I \"<SCRIPT>\" start",
            "timeout": 10
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "<PYTHON> -I \"<SCRIPT>\"",
            "timeout": 20,
            "async": true
          }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": "permission_prompt|idle_prompt|agent_needs_input",
        "hooks": [
          {
            "type": "command",
            "command": "<PYTHON> -I \"<SCRIPT>\" notify",
            "timeout": 20,
            "async": true
          }
        ]
      }
    ]
  }
}
```

替换两个占位：

- `<PYTHON>`：第 1 步查到的 Python 绝对路径。**Windows 用正斜杠**，例如 `C:/Python313/python.exe`
- `<SCRIPT>`：脚本绝对路径，例如 `C:/Users/alice/.claude/notify-dingtalk.py`

**为什么 `Stop` 要加 `"async": true`**：推送要发网络请求，最长 6 秒超时。同步执行会让用户每轮都等这几秒。异步跑，失败也不影响主流程。

**为什么 `-I`**：隔离模式，不加载用户 site-packages，避免目标机器上某个同名模块污染。

### 3.4 验证

按顺序执行三步，**不要跳步**：

```bash
# 步骤 A：配置自检（不联网、不发消息）
<PYTHON> -I -X utf8 "<SCRIPT>" check

# 步骤 B：连通性自检（联网，但不发消息）
<PYTHON> -I -X utf8 "<SCRIPT>" probe

# 步骤 C：真发一条
<PYTHON> -I -X utf8 "<SCRIPT>" test
```

**期望结果**：

- `check` 输出「✓ 配置完整，可以发送」，退出码 0
- `probe` 输出 `MCP 连通 ✓`，并列出可见机器人
- `test` 输出「发送成功」

如果 `test` 成功，让用户去钉钉确认收到消息，**再继续**。

---

## 4. 排错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| `check` 报「未找到配置文件」 | 文件没放对位置 | 确认在 `~/.claude/` 或脚本同目录 |
| `check` 报「没有任何一条通道配置完整」 | 通道字段没填全 | 对照第 2 节，MCP 通道需要 `mcp_url` + `mcp_robot_code` + target |
| `check` 报「mcp_user_ids 必须是数组」 | 写成了字符串 | 改成 `["xxx"]` |
| `check` 提示「环境变量 X 未设置」 | `${X}` 占位符没对应变量 | 在 settings.json 的 `env` 里设上，或改成字面量 |
| `probe` 报 `MCP 连通 ✓` 但机器人列表为空 | robotCode 不属于当前账号 | 让用户去开发者后台确认 robotCode |
| `test` 报 `invalidClientIdOrSecret` | AppKey/AppSecret 错误 | 企业机器人通道的凭据有问题 |
| `test` 报 `460101 body 大小不合法` | 单次请求超过 20000 字节 | 降低 `max_message_bytes` |
| `test` 成功但 hook 从不触发 | hook 配置没生效 | 让用户敲一次 `/hooks` 重新加载，或重启 Claude Code |
| hook 触发了但没收到消息 | 看日志 | `~/.claude/notify-dingtalk.log` |
| 日志里出现 `stdin 不是合法 JSON` | 输入编码问题 | 该问题已在当前版本修复（走 `sys.stdin.buffer` 解码 UTF-8），确认脚本是最新版 |

**日志位置**：`~/.claude/notify-dingtalk.log`，追加写入，超过 1MB 自动清空。排查时先看最后几行。

---

## 5. 消息长什么样

```
**项目名**

✅ 已完成　·　⏱ 1m23s　·　14:05　·　⎇ main

---

**Token**　输入 2.6K　·　输出 1.5K　·　缓存 842.6K　·　5 次请求

**工具**　Bash×2　·　Read×1　·　Edit×1

---

**提问**

用户这轮问了什么

---

**结果**

Claude 最后一段回复的摘要（默认 20 行 / 900 字）
```

---

## 6. 可调参数

全部在 `notify-dingtalk-config.json` 里，改完不用重启 Claude Code（每次 hook 触发都会重新读）。

| 参数 | 默认 | 说明 |
|---|---|---|
| `min_duration_seconds` | `0` | 任务短于这个秒数不推。设成 `60` 可过滤简短问答 |
| `min_interval_seconds` | `10` | 同一会话两次推送最小间隔，防重复 |
| `include_last_reply` | `true` | 是否带结果摘要 |
| `include_question` | `true` | 是否带用户提问 |
| `max_question_chars` | `120` | 提问最多显示多少字 |
| `max_reply_chars` | `900` | 结果摘要最多多少字 |
| `max_reply_lines` | `20` | 结果摘要最多多少行 |
| `include_git_branch` | `true` | 在 git 仓库里时显示分支 |
| `include_timestamp` | `true` | 显示完成时间 |
| `max_message_bytes` | `6000` | 正文最大字节数 |
| `notify_on` | `["permission_prompt"]` | 哪些 Notification 类型要提醒，见下 |

### 关于 `notify_on`（等待授权提醒）

Claude Code 在需要人介入时会触发 `Notification` hook，`notification_type` 的取值有：

| 值 | 含义 | 建议 |
|---|---|---|
| `permission_prompt` | 有工具调用在等你授权 | **开**（默认） |
| `idle_prompt` | 空闲等你输入 | 关（容易和 Stop 完成通知重复） |
| `agent_needs_input` | 子 agent 需要输入 | 视情况 |
| `elicitation_complete` / `auth_success` 等 | 其它事件 | 一般不用 |

**这类提醒只有单向通知能力，不能回传审批。** 原因见第 10 节。

### 关于 `max_message_bytes`

**这是硬限制，不是建议值。** 钉钉服务端限制**整个 HTTP 请求 body** 不超过 **20000 字节**，超出直接返回错误码 `460101`「body 大小不合法」，消息不会送达。

实测数据：

- 固定开销（JSON-RPC 外壳 + 字段名 + robotCode + userId）约 **243 字节**
- 剩余可用空间约 **19757 字节**
- 中文按 UTF-8 每字 **3 字节**，所以 19757 字节 ≈ 6500 个中文字

默认给 6000 是保险值。**不要调到 19000 以上**，否则遇到中文长回复会直接发不出去。脚本还有一层 `clamp_markdown()` 兜底：正文超预算时按行截断并追加 `…（已截断）`。

---

## 7. 移植到非 Claude Code 的 agent

脚本对宿主 agent 的依赖只在入口解析这一层。它读的 stdin JSON 字段：

| 字段 | 用途 | 缺失时 |
|---|---|---|
| `session_id` | 区分会话，算耗时 | 用 `"default"` |
| `cwd` | 显示项目名、读 git 分支 | 用当前目录 |
| `stop_hook_active` | 防重复推送 | 视为 false |
| `transcript_path` | 解析 token / 工具 / 提问 | 降级：不显示这些区块，仍能发 |
| `last_assistant_message` | 结果摘要 | 回退到读 transcript |
| `hook_event_name` | 区分 `SessionStart` / `Stop` | 用命令行参数区分 |

**实测降级行为**：喂一个不存在的 transcript，消息照常发出，只是少了 Token / 工具 / 提问三个区块，不会报错。

所以接别的 agent 只需要写个薄适配器，拼出上面这份 JSON 然后调用脚本即可：

```bash
echo '{"session_id":"x","cwd":"/path","last_assistant_message":"..."}' | \
  python3 -I notify-dingtalk.py
```

---

## 8. 给智能体的执行清单

按顺序做，每步确认后再进下一步：

- [ ] 确认 Python 3.9+，记下绝对路径
- [ ] 问用户要通道凭据（MCP 地址 / AppKey / webhook），**不要自己编**
- [ ] 把 `notify-dingtalk.py` 放到 `~/.claude/`
- [ ] 从 `notify-dingtalk-config.example.json` 复制出 `notify-dingtalk-config.json` 并填写
- [ ] 运行 `check`，必须返回 0
- [ ] 运行 `probe`（仅 MCP 通道），确认机器人可见
- [ ] 运行 `test`，让用户确认收到
- [ ] 合并写入 `~/.claude/settings.json` 的 `hooks` 段（SessionStart / Stop / Notification 三条，**保留已有字段**）
- [ ] 提醒用户敲 `/hooks` 或重启以加载新 hook
- [ ] 让用户跑一次普通对话，确认自然触发
- [ ] 告知用户：钉钉里的授权提醒**只能看不能批**，要批需用 Remote Control（见第 9 节）

---

## 9. 关于「在钉钉里直接批准」

**做不到，原因是钉钉侧的，不是脚本侧的。**

### 为什么不行

1. **钉钉 MCP 只有发送能力，没有接收能力。** 实测它的 12 个工具：

```
add_robot_to_group  batch_recall_robot_users_msg  batch_send_robot_msg_to_users
create_robot  list_group_bots  recall_robot_group_message  remove_robot_in_group
search_bots  search_groups_by_keyword  search_my_robots
send_message_by_custom_robot  send_robot_group_message
```

全部是「发」和「查」，**没有 getMessage / listMessages / webhook 回调**。想读回用户在钉钉里打的字，得自己起一个常驻进程接钉钉的 Stream 事件流。

2. **这需要公网可达。** 本机是 NAT 后面的，钉钉回调进不来。Stream 模式虽然可以主动外连，但要求进程常驻盯事件，而 hook 是「执行完就退出」的一次性进程，形状对不上。

3. **批准动作有本地权限语义。** 「同意这个 Bash 命令」意味着信任本机执行任意命令，把开关放到手机端等于扩大攻击面。这块需要用户明确知情。

### 能用什么替代

Claude Code 内置了三条远程通道，都支持**双向**（能看到请求 + 能批准）：

| 方式 | 怎么开 | 说明 |
|---|---|---|
| **Remote Control** | `claude --remote-control`，或在 settings.json 设 `"remoteControlAtStartup": true` | 从 claude.ai/code 或 Claude 手机 App 控制本机会话，权限弹窗可直接批。这是唯一真正解决「人在外面、卡在授权」的方案 |
| **Claude 手机推送** | `"inputNeededNotifEnabled": true` | 有权限提示/提问待处理时推手机，点进去处理 |
| **Session 同步** | `"autoUploadSessions": true` | 会话镜像到 claude.ai，只读查看 |

其余请用户自行决定；本脚本只负责「**提醒你卡住了**」，钉钉收到 🔐 消息后，人再切到上面任一渠道处理。

配置项 `notify_on` 控制是否发送这类提醒，默认只开 `permission_prompt`。

---

## 10. 安全注意

1. **配置文件含凭据，不要提交进 git**。如果必须进版本库，改用 `${ENV_NAME}` 占位符 + `settings.json` 的 `env` 块。
2. **MCP 的 `key` 是长期有效的**，明文贴在 URL 里。泄露等于别人能往你的钉钉发消息。
3. **群 webhook 会打扰群里所有人**。用它之前必须让用户明确同意。
4. **不要在用户组织里擅自创建应用**。MCP 有个 `create_robot` 工具，调用会在用户组织真实新建一个企业内部应用并自动发布，属于不可逆操作，必须先问。