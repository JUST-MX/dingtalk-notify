# dsh-plugin · 钉钉通知（DSH 版）

把「每轮结束 / 等授权时推一条钉钉」接到 **DeepSeek Harness**。
发送逻辑不在这个包里：它复用 `~/.claude/notify-dingtalk.py`，只负责从 DSH 的事件流里取数。

## 它怎么工作

DSH 的会话本身就是事件溯源日志，所以这个插件**不轮询、不读文件、不解析 transcript**，
只订阅 `session/event`：

| 事件 | 作用 |
|---|---|
| `turn/start` | 开一本新账（起点时间、提问、工具计数清零） |
| `user/message` | 只收 `source.kind === "user"` 的，记住这一轮的提问 |
| `assistant/message` | 累加 `usage.inputTokens / outputTokens / cacheReadTokens`，记最后一次答复 |
| `tool/call` | 工具计数，顺便记住 callId → 参数摘要 |
| `approval/asked` | 立刻推「🔐 等待授权」，带上工具名与该调用的参数 |
| `turn/end` | 汇总耗时/Token/工具/提问/结果，交给 Python 发送 |

`session.header.origin === "subagent"` 的会话直接忽略，否则子 agent 会把同一条回复重复推一遍。

## 安装

```bash
dsh plugin --profile desktop add "<本仓库>/dsh-plugin"
```

装完**需要重启 DSH**（插件在启动时加载）。

## 配置

默认值写在 `index.js` 的 `DEFAULTS` 里，覆盖方式是在 `~/.dsh/profiles/desktop/cordis.patch.yml`
里对准这个 id 写 config：

```yaml
- id: dingtalk-notify
  name: "@local/dsh-plugin-dingtalk-notify"
  config:
    notifyApproval: true
    notifyTurnEnd: true
    minDurationSeconds: 30
    python: "C:/Python313/python.exe"
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关 |
| `python` | `""` | 留空则依次尝试 `python` / `python3` / `py` |
| `script` | `""` | 留空用 `~/.claude/notify-dingtalk.py` |
| `notifyTurnEnd` | `true` | 每轮结束推一条 |
| `notifyApproval` | `true` | 等授权时推一条 |
| `minDurationSeconds` | `0` | 短于这个秒数不推 |
| `timeoutMs` | `30000` | 单次 Python 进程的硬超时 |

凭据仍然只有一份，在 `~/.claude/notify-dingtalk-config.json`（见 [INSTALL.md](../INSTALL.md) 第 2 节）。

## 联调

不联网、不发送，只渲染出来看排版：

```bash
echo '{"session_id":"t","cwd":"/path/to/project","duration":83,"stats":{"ok":true,"prompt":"你好","tools":{"Bash":2},"input_tokens":2600,"output_tokens":1500,"requests":5}}' \
  | python -I -X utf8 ~/.claude/notify-dingtalk.py dsh-render
```

真发一条：把 `dsh-render` 换成 `dsh`。

## 边界

- **只覆盖 DSH 自己跑的会话。** 在 DSH 里新建的 Claude Code 工作项是由
  `@local/dsh-plugin-claude-sessions` 起的独立 `claude -p` 进程，它不产生 DSH 会话事件，
  仍然由 Claude Code 自己的 `Stop` hook 推送，两边不会重复。
- **只能提醒，不能批准。** 理由与 Claude Code 版相同（钉钉侧没有接收能力），
  详见 [INSTALL.md](../INSTALL.md) 第 9 节。
