#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code -> 钉钉 完成通知

由 hook 自动调用，一般不需要手动执行：
    python -I notify-dingtalk.py            # Stop：任务结束，发通知
    python -I notify-dingtalk.py start      # SessionStart：记录开始时间
    python -I notify-dingtalk.py dsh        # DSH 插件：stdin 收结构化 payload 后发送
    python -I notify-dingtalk.py dsh-render # 同上但只打印，不发（联调用）
    python -I notify-dingtalk.py test       # 手动发一条测试消息
    python -I notify-dingtalk.py probe      # 只读自检：查 MCP 工具与可见机器人
    python -I notify-dingtalk.py check      # 校验配置文件是否合法

配置不在本文件里，读同目录的 notify-dingtalk-config.json
（可用环境变量 NOTIFY_DINGTALK_CONFIG 指定其它路径）。
三种通道按优先级从上到下，填全哪个走哪个：
    1. MCP 服务      mcp_url + mcp_robot_code + (mcp_user_ids 或 mcp_open_conversation_id)
    2. 企业机器人API app_key / app_secret / robot_code / user_id
    3. 群机器人 webhook  webhook_url（可选 webhook_secret）
配置值可写 ${ENV_NAME} 从环境变量取，密钥不必落盘。
任何情况下本脚本都 exit 0，不会影响 Claude Code 主流程。
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HOME = Path.home() / ".claude"
STATE_FILE = HOME / "notify-state.json"
TOKEN_FILE = HOME / "notify-dingtalk-token.json"
LOG_FILE = HOME / "notify-dingtalk.log"
HTTP_TIMEOUT = 6

CONFIG_NAME = "notify-dingtalk-config.json"

# 缺省值：配置文件里没写的项用这些，保证老配置 / 残缺配置也能跑
DEFAULTS = {
    "mcp_url": "",
    "mcp_robot_code": "",
    "mcp_open_conversation_id": "",
    "mcp_user_ids": [],
    "app_key": "",
    "app_secret": "",
    "robot_code": "",
    "user_id": "",
    "webhook_url": "",
    "webhook_secret": "",
    "min_duration_seconds": 0,
    "min_interval_seconds": 10,
    "include_last_reply": True,
    "include_question": True,
    "max_question_chars": 120,
    "max_reply_chars": 900,
    "max_reply_lines": 20,
    "include_git_branch": True,
    "include_timestamp": True,
    "max_message_bytes": 6000,

    # Notification hook：哪些情况要提醒。可选值：
    #   permission_prompt  有工具调用在等你授权（默认开）
    #   idle_prompt        空闲等你输入（默认关，容易和 Stop 重复）
    #   agent_needs_input  子 agent 需要输入
    "notify_on": ["permission_prompt"],
}


UNRESOLVED = []   # 占位符指向的环境变量没设置，记下来给 check 用


def _env(v):
    """把 "${NAME}" 替换成同名环境变量，支持列表内元素。"""
    if isinstance(v, list):
        return [_env(x) for x in v]
    if isinstance(v, str) and v.startswith("${") and v.endswith("}"):
        name = v[2:-1]
        if name not in os.environ:
            UNRESOLVED.append(name)
        return os.environ.get(name, "")
    return v


def _config_candidates():
    """配置文件查找顺序，先精确后兜底。"""
    paths = []
    env_path = os.environ.get("NOTIFY_DINGTALK_CONFIG", "").strip()
    if env_path:
        paths.append(Path(env_path).expanduser())
    here = Path(__file__).resolve().parent
    paths.append(here / CONFIG_NAME)
    paths.append(HOME / CONFIG_NAME)
    return paths


def load_config():
    """读 JSON 配置并合并缺省值。找不到文件时返回全缺省（等于禁用）。"""
    cfg = dict(DEFAULTS)
    for path in _config_candidates():
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception as e:
            print(f"配置文件解析失败: {path}: {e}", file=sys.stderr)
            continue
        if not isinstance(data, dict):
            print(f"配置文件根节点必须是对象: {path}", file=sys.stderr)
            continue
        for k, v in data.items():
            if k in DEFAULTS:
                cfg[k] = _env(v)
        return cfg, path
    return cfg, None


CONFIG, CONFIG_PATH = load_config()


# ---------------- 基础设施 ----------------

def log(msg):
    """写日志文件，hook 的 stderr 用户看不到，只能落盘。"""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            LOG_FILE.write_text("", encoding="utf-8")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"save_json failed: {e}")


def http_post(url, payload, headers=None):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_verbose(url, payload, headers=None):
    """出错时把钉钉返回的 errorCode/message 带出来，否则只剩 400 Bad Request。"""
    try:
        return http_post(url, payload, headers)
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {}
        raise RuntimeError(
            f"HTTP {e.code} {detail.get('code') or ''} {detail.get('message') or ''}".strip()
        ) from None


# ---------------- 钉钉 模式一：单聊 ----------------

def get_access_token():
    """client credentials 换 token，缓存到过期前 5 分钟。"""
    cache = load_json(TOKEN_FILE, {})
    if cache.get("token") and cache.get("expire_at", 0) > time.time() + 300:
        return cache["token"]

    data = _post_verbose(
        "https://api.dingtalk.com/v1.0/oauth2/accessToken",
        {"appKey": CONFIG["app_key"], "appSecret": CONFIG["app_secret"]},
    )
    token = data.get("accessToken")
    if not token:
        raise RuntimeError(f"换 token 失败: {data}")
    expire = data.get("expireIn", 7200)
    save_json(TOKEN_FILE, {"token": token, "expire_at": time.time() + expire})
    return token


def send_robot(title, text):
    token = get_access_token()
    headers = {"x-acs-dingtalk-access-token": token}
    base = {
        "robotCode": CONFIG["robot_code"],
        "userIds": [CONFIG["user_id"]],
    }
    # 先试 markdown，失败退回纯文本
    attempts = [
        ("sampleMarkdown", {"title": title, "text": text}),
        ("sampleText", {"content": f"{title}\n{text}"}),
    ]
    last_err = None
    for msg_key, param in attempts:
        try:
            body = dict(base, msgKey=msg_key, msgParam=json.dumps(param, ensure_ascii=False))
            resp = _post_verbose(
                "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
                body, headers,
            )
            if resp.get("invalidStaffIdList"):
                raise RuntimeError(f"userId 无效: {resp['invalidStaffIdList']}")
            log(f"robot send ok ({msg_key})")
            return True
        except Exception as e:
            last_err = e
            log(f"robot send failed ({msg_key}): {e}")
    raise RuntimeError(last_err)


# ---------------- 钉钉 模式二：群机器人 webhook ----------------

def send_webhook(title, text):
    url = CONFIG["webhook_url"]
    secret = CONFIG.get("webhook_secret") or ""
    if secret:
        ts = str(round(time.time() * 1000))
        sign_raw = f"{ts}\n{secret}".encode("utf-8")
        sign = base64.b64encode(
            hmac.new(secret.encode("utf-8"), sign_raw, hashlib.sha256).digest()
        ).decode("utf-8")
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"

    resp = _post_verbose(url, {"msgtype": "markdown",
                               "markdown": {"title": title, "text": text}})
    if resp.get("errcode") != 0:
        raise RuntimeError(f"webhook 返回错误: {resp}")
    log("webhook send ok")
    return True


# ---------------- 钉钉 模式三：MCP 服务 ----------------

def mcp_call(method, params=None, timeout=HTTP_TIMEOUT):
    """JSON-RPC 调 MCP 服务端点。钉钉这个网关不返回 Mcp-Session-Id，无状态直连。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        CONFIG["mcp_url"],
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    # 兼容 SSE 风格响应：取最后一行 data:
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        raw = data_lines[-1] if data_lines else raw
    return json.loads(raw)


def send_mcp(title, text):
    robot = CONFIG.get("mcp_robot_code") or ""
    users = CONFIG.get("mcp_user_ids") or []
    if not robot or not users:
        raise RuntimeError("MCP 通道缺少 mcp_robot_code 或 mcp_user_ids")

    resp = mcp_call("tools/call", {
        "name": "batch_send_robot_msg_to_users",
        "arguments": {
            "robotCode": robot,
            "userIds": list(users),
            "msgType": "sampleMarkdownDX",
            "title": title,
            "markdown": text,
        },
    })
    if resp.get("error"):
        raise RuntimeError(f"MCP error: {resp['error']}")
    result = resp.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"MCP tool error: {result.get('content')}")
    log("mcp send ok")
    return True


def send_mcp_group(title, text):
    robot = CONFIG.get("mcp_robot_code") or ""
    cid = CONFIG.get("mcp_open_conversation_id") or ""
    if not robot or not cid:
        raise RuntimeError("MCP 群通道缺少 mcp_robot_code 或 mcp_open_conversation_id")

    resp = mcp_call("tools/call", {
        "name": "send_robot_group_message",
        "arguments": {
            "robotCode": robot,
            "openConversationId": cid,
            "msgKey": "sampleMarkdown",
            "markdown": {"title": title, "text": text},
        },
    })
    if resp.get("error"):
        raise RuntimeError(f"MCP error: {resp['error']}")
    result = resp.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"MCP tool error: {result.get('content')}")
    log("mcp group send ok")
    return True


def send(title, text):
    # 占位符没被环境变量替换掉，等于没配，不能拿去发
    if CONFIG.get("mcp_url") and CONFIG.get("mcp_robot_code"):
        if CONFIG.get("mcp_user_ids"):
            return send_mcp(title, text)
        if CONFIG.get("mcp_open_conversation_id"):
            return send_mcp_group(title, text)
    if all(CONFIG.get(k) for k in ("app_key", "app_secret", "robot_code", "user_id")):
        return send_robot(title, text)
    if CONFIG["webhook_url"]:
        return send_webhook(title, text)
    log("未配置任何通道，跳过推送")
    return False


# ---------------- 会话状态 ----------------

def state_path_key(session_id):
    return session_id or "default"


def read_state():
    return load_json(STATE_FILE, {})


def write_state(state):
    # 顺手清理 7 天前的记录
    now = time.time()
    state = {k: v for k, v in state.items()
             if isinstance(v, dict) and now - v.get("start", now) < 7 * 86400}
    save_json(STATE_FILE, state)


# ---------------- transcript 解析 ----------------

def tail_lines(path, max_bytes=512 * 1024):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # 丢掉可能被截断的首行
    return lines


def iter_records(transcript_path):
    """顺序读 transcript（Stop 时文件可能几 MB，只在需要统计时才走这条路）。"""
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def last_prompt_index(recs):
    """找本轮起点：最后一条 last-prompt 的 leafUuid 指向的记录位置。"""
    leaf = None
    for rec in reversed(recs):
        if rec.get("type") == "last-prompt":
            leaf = rec.get("leafUuid")
            break
    if leaf:
        for i, rec in enumerate(recs):
            if rec.get("uuid") == leaf:
                return i
    return None


def last_user_prompt(recs):
    """取本轮的用户提问原文。只认真正的输入，跳过 tool_result 和系统注入。"""
    for rec in reversed(recs):
        if rec.get("type") != "user":
            continue
        if rec.get("isSidechain") or rec.get("isMeta"):
            continue
        msg = rec.get("message") or {}
        content = msg.get("content")
        texts = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            # 全是 tool_result 的记录不是用户提问
            if blocks and all(b.get("type") == "tool_result" for b in blocks):
                continue
            for b in blocks:
                if b.get("type") == "text":
                    texts.append(b.get("text", ""))
        text = "\n".join(t for t in texts if t).strip()
        if text:
            return text
    return ""


def turn_stats(transcript_path):
    """统计本轮（最后一次用户提问之后）的状态、耗时、token。

    transcript 里同一条 API 响应会被拆成多条 assistant 记录，
    因此按 message.id 分组，每组取 output_tokens 最大的那条，避免重复累加。
    """
    stats = {"ok": False}
    if not transcript_path or not Path(transcript_path).exists():
        return stats
    try:
        recs = list(iter_records(transcript_path))
        if not recs:
            return stats
        start = last_prompt_index(recs)
        turn = recs[start:] if start is not None else recs

        groups, timestamps = {}, []
        interrupted = False
        tools = {}
        for rec in turn:
            ts = rec.get("timestamp")
            if ts:
                timestamps.append(ts)
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            mid = msg.get("id")
            if mid and usage:
                prev = groups.get(mid)
                if prev is None or (usage.get("output_tokens", 0) or 0) > \
                        (prev.get("output_tokens", 0) or 0):
                    groups[mid] = usage
            for block in msg.get("content") or []:
                if isinstance(block, dict):
                    if block.get("type") == "tool_use":
                        name = block.get("name", "?")
                        tools[name] = tools.get(name, 0) + 1
                    elif block.get("type") == "text" and "Request interrupted" in \
                            (block.get("text") or ""):
                        interrupted = True

        stats.update(
            requests=len(groups),
            input_tokens=sum(u.get("input_tokens", 0) or 0 for u in groups.values()),
            output_tokens=sum(u.get("output_tokens", 0) or 0 for u in groups.values()),
            cache_read=sum(u.get("cache_read_input_tokens", 0) or 0 for u in groups.values()),
            cache_write=sum(u.get("cache_creation_input_tokens", 0) or 0 for u in groups.values()),
            tools=tools,
            interrupted=interrupted,
            prompt=last_user_prompt(recs),
            model=next((((r.get("message") or {}).get("model")) for r in reversed(turn)
                        if (r.get("message") or {}).get("model")), ""),
            ok=True,
        )
        if timestamps:
            stats["started"] = min(timestamps)
            stats["ended"] = max(timestamps)
        return stats
    except Exception as e:
        log(f"turn_stats failed: {e}")
        return stats


def extract_last_reply(transcript_path):
    """从 jsonl 里倒着找最后一条 assistant 的纯文本。"""
    if not transcript_path or not Path(transcript_path).exists():
        return ""
    try:
        for line in reversed(tail_lines(transcript_path)):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            content = msg.get("content")
            texts = []
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
            elif isinstance(content, str):
                texts.append(content)
            text = "\n".join(t for t in texts if t).strip()
            if text:
                return text
    except Exception as e:
        log(f"parse transcript failed: {e}")
    return ""


# ---------------- 主流程 ----------------

def format_duration(seconds):
    seconds = int(seconds or 0)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m{s}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def summarize(text, limit, max_lines=0):
    """保留换行（钉钉 markdown 认列表/代码块），只清空行与多余空白，再按字符截断。"""
    out, used = [], 0
    for raw in text.splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        if line.startswith("```"):
            continue  # 消息里不放代码块，围栏去掉，内容按普通行收
        if max_lines and len(out) >= max_lines:
            break
        out.append(line)
        used += len(line)
        if used >= limit:
            break
    text = "\n".join(out)
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def git_branch(cwd):
    """读 .git/HEAD，只用于展示，失败就静默返回空串。"""
    d = Path(cwd)
    for parent in [d, *d.parents]:
        head = parent / ".git" / "HEAD"
        if head.is_file():
            try:
                ref = head.read_text(encoding="utf-8").strip()
            except Exception:
                return ""
            if ref.startswith("ref: refs/heads/"):
                return ref[len("ref: refs/heads/"):]
            return ref[:7]  # 游离 HEAD，显示短 SHA
    return ""


def handle_start(payload):
    session_id = state_path_key(payload.get("session_id"))
    state = read_state()
    entry = state.get(session_id) or {}
    now = time.time()
    # 新建/清空会话重新计时；续接会话保持原起点
    if payload.get("source") in ("startup", "clear") or now - entry.get("start", 0) > 12 * 3600:
        entry["start"] = now
    entry.setdefault("start", now)
    state[session_id] = entry
    write_state(state)


def format_tokens(n):
    """手机上 1,234,567 太难扫，压成 1.2M / 34.5K。"""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _turn_span(stats, fallback):
    """本轮真实跨度，失败时用 fallback。"""
    if stats.get("started") and stats.get("ended"):
        try:
            t0 = datetime.fromisoformat(stats["started"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(stats["ended"].replace("Z", "+00:00"))
            real = (t1 - t0).total_seconds()
            if real > 0:
                return real
        except Exception:
            pass
    return fallback


def clamp_markdown(text, title=""):
    """钉钉限制整个请求 body 不超过 20000 字节，超出直接拒收。
    按调用方在 payload 里的固定开销留足余量，超了就按字节截断。"""
    budget = CONFIG["max_message_bytes"]
    if len(text.encode("utf-8")) <= budget:
        return text, False
    # 逐行砍，保留结构，砍掉的部分标出来
    out, used = [], 0
    for line in text.splitlines():
        b = len(line.encode("utf-8")) + 1
        if used + b > budget - len("…（已截断）".encode("utf-8")):
            break
        out.append(line)
        used += b
    return "\n".join(out) + "\n\n…（已截断）", True


def build_message(payload, session_id, duration):
    """拼装通知正文：状态 / 耗时 / Token / 工具 / 提问 / 结果摘要。

    `payload["stats"]` 优先：DSH 插件已经把结构化统计算好传进来，
    这时不需要也不该去读 Claude Code 的 transcript。
    """
    cwd = payload.get("cwd") or os.getcwd()
    project = payload.get("project") or Path(cwd).name or cwd
    stats = payload.get("stats") or turn_stats(payload.get("transcript_path"))
    # 宿主名字：Claude Code 的 hook 不传，DSH 插件传 "DSH"
    host = payload.get("host") or "Claude Code"

    status, mark = "已完成", "✅"
    if stats.get("interrupted"):
        status, mark = "已中断", "⚠️"
    elif stats.get("error"):
        status, mark = "出错", "❌"

    title = f"{mark} {host} {status} · {project}"

    # 耗时：优先用本轮 transcript 的真实跨度；SessionStart 记的时间跨天会失真
    span = duration
    real = _turn_span(stats, 0)
    if real and (real < span or span <= 0):
        span = real

    meta = [f"{mark} {status}", f"⏱ {format_duration(span)}"]
    if CONFIG["include_timestamp"]:
        meta.append(time.strftime("%H:%M"))
    if CONFIG["include_git_branch"]:
        br = git_branch(cwd)
        if br:
            meta.append(f"⎇ {br}")

    lines = [f"**{project}**", "　·　".join(meta)]

    # Token 区
    if stats.get("ok") and (stats["input_tokens"] or stats["output_tokens"]):
        tok = [f"输入 {format_tokens(stats['input_tokens'])}",
               f"输出 {format_tokens(stats['output_tokens'])}"]
        if stats.get("cache_read"):
            tok.append(f"缓存 {format_tokens(stats['cache_read'])}")
        line = "**Token**　" + "　·　".join(tok)
        if stats.get("requests"):
            line += f"　·　{stats['requests']} 次请求"
        lines.append("---")
        lines.append(line)

    # 工具调用
    tools = stats.get("tools") or {}
    if tools:
        top = sorted(tools.items(), key=lambda kv: -kv[1])[:5]
        lines.append("**工具**　" + "　·　".join(f"{k}×{v}" for k, v in top))

    # 提问
    if CONFIG["include_question"]:
        prompt = stats.get("prompt") or ""
        if prompt:
            lines.append("---")
            lines.append("**提问**")
            lines.append(summarize(prompt, CONFIG["max_question_chars"], 2))

    # 结果摘要
    reply = ""
    if CONFIG["include_last_reply"]:
        reply = payload.get("last_assistant_message") or extract_last_reply(
            payload.get("transcript_path"))
        if reply:
            lines.append("---")
            lines.append("**结果**")
            lines.append(summarize(reply, CONFIG["max_reply_chars"],
                                   CONFIG["max_reply_lines"]))

    text, truncated = clamp_markdown(guard_markdown("\n\n".join(lines)), title)
    if truncated:
        log("消息超出字节上限，已截断")
    return title, text


def handle_stop(payload):
    # 若 Claude 已经因为别的 Stop hook 在续跑，避免重复推送
    if payload.get("stop_hook_active"):
        log("stop_hook_active=True，跳过")
        return

    session_id = state_path_key(payload.get("session_id"))
    state = read_state()
    entry = state.get(session_id) or {}
    now = time.time()

    start = entry.get("start")
    duration = now - start if start else 0

    if duration < CONFIG["min_duration_seconds"]:
        log(f"耗时 {duration:.0f}s 低于阈值，跳过")
        return
    if now - entry.get("last_sent", 0) < CONFIG["min_interval_seconds"]:
        log("距上次推送过近，跳过")
        return

    title, text = build_message(payload, session_id, duration)

    try:
        if send(title, text):
            entry["last_sent"] = now
            state[session_id] = entry
            write_state(state)
    except Exception as e:
        log(f"send failed: {e}")


def handle_dsh(payload, dry_run=False):
    """DSH 插件入口：payload 由 @local/dsh-plugin-dingtalk-notify 拼好。

    与 hook 的区别只有一点——统计量是插件从 session 事件流里精确算出来的，
    放在 payload["stats"] 里，所以这里不碰 transcript。
    payload 也可带 `kind: "permission_prompt"` 走提醒分支。
    """
    session_id = state_path_key(payload.get("session_id"))
    state = read_state()
    entry = state.get(session_id) or {}
    now = time.time()

    if payload.get("kind") in ("permission_prompt", "idle_prompt"):
        ntype = payload["kind"]
        wanted = CONFIG.get("notify_on") or []
        if ntype not in wanted:
            log(f"kind={ntype!r} 不在 notify_on 里，跳过")
            return 0
        key = f"last_notify_{ntype}"
        if now - entry.get(key, 0) < CONFIG["min_interval_seconds"]:
            log(f"{ntype} 距上次提醒过近，跳过")
            return 0
        title, text = build_notification(payload)
    else:
        duration = float(payload.get("duration") or 0)
        if duration < CONFIG["min_duration_seconds"]:
            log(f"耗时 {duration:.0f}s 低于阈值，跳过")
            return 0
        if now - entry.get("last_sent", 0) < CONFIG["min_interval_seconds"]:
            log("距上次推送过近，跳过")
            return 0
        title, text = build_message(payload, session_id, duration)
        key = "last_sent"

    if dry_run:
        print(f"===== {title} =====")
        print(text)
        return 0

    try:
        if send(title, text):
            entry[key] = now
            state[session_id] = entry
            write_state(state)
    except Exception as e:
        log(f"dsh send failed: {e}")
        return 1
    return 0


def pending_tool_call(transcript_path):
    """找最近一次工具调用里、本轮是否卡在权限确认上。
    返回 (工具名, 参数摘要)；没卡住就返回 ("", "")。"""
    if not transcript_path or not Path(transcript_path).exists():
        return "", ""
    try:
        recs = list(iter_records(transcript_path))
        start = last_prompt_index(recs)
        turn = recs[start:] if start is not None else recs

        # 从尾部往前找最后的 tool_use，看它有没有对应的 tool_result
        last_use = None
        answered = set()
        for rec in turn:
            msg = rec.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    last_use = block
                elif block.get("type") == "tool_result":
                    answered.add(block.get("tool_use_id"))
        if last_use and last_use.get("id") not in answered:
            args = last_use.get("input") or {}
            brief = args.get("command") or args.get("file_path") or \
                args.get("path") or args.get("url") or \
                json.dumps(args, ensure_ascii=False)
            return last_use.get("name", "?"), " ".join(str(brief).split())
    except Exception as e:
        log(f"pending_tool_call failed: {e}")
    return "", ""


def build_notification(payload):
    """等你授权 / 空闲待输入时的提醒。

    DSH 插件会直接给 `tool` / `brief`，这时不读 transcript；
    Claude Code 的 hook 只给 transcript_path，按老路子从里面找卡住的工具调用。
    """
    cwd = payload.get("cwd") or os.getcwd()
    project = Path(cwd).name or cwd
    ntype = payload.get("notification_type") or payload.get("notificationType") or ""
    raw_msg = payload.get("message") or ""
    host = payload.get("host") or "Claude Code"
    ntype = (payload.get("notification_type") or payload.get("notificationType")
             or payload.get("kind") or "")

    if ntype == "permission_prompt":
        if payload.get("tool") is not None or payload.get("brief") is not None:
            tool = payload.get("tool") or ""
            brief = payload.get("brief") or ""
        else:
            tool, brief = pending_tool_call(payload.get("transcript_path"))
        title = f"🔐 {host} 等待授权 · {project}"
        lines = [f"**{project}**", "🔐 **等待授权**"]
        if tool:
            lines.append(f"**工具**　{tool}")
        if brief:
            lines.append("---")
            lines.append("**请求内容**")
            lines.append(guard_markdown(summarize(brief, 400, 6)))
        else:
            lines.append("---")
            lines.append(raw_msg or "有工具调用需要你确认")
        lines.append("---")
        lines.append("请回到终端确认，或在 claude.ai/code / 手机 App 里处理。")
    else:
        title = f"💬 {host} 等待输入 · {project}"
        lines = [f"**{project}**", "💬 **等待输入**"]
        if raw_msg:
            lines.append("---")
            lines.append(guard_markdown(summarize(raw_msg, 300, 4)))

    text, _ = clamp_markdown(guard_markdown("\n\n".join(lines)), title)
    return title, text


def handle_notification(payload):
    """Notification hook：只在真的需要人介入时推，避免刷屏。"""
    ntype = payload.get("notification_type") or payload.get("notificationType") or ""
    wanted = CONFIG.get("notify_on") or []
    if ntype not in wanted:
        log(f"notification_type={ntype!r} 不在 notify_on 里，跳过")
        return

    session_id = state_path_key(payload.get("session_id"))
    state = read_state()
    entry = state.get(session_id) or {}
    now = time.time()

    # 同一个会话里连续的同类提醒，压到最小间隔
    key = f"last_notify_{ntype}"
    if now - entry.get(key, 0) < CONFIG["min_interval_seconds"]:
        log(f"{ntype} 距上次提醒过近，跳过")
        return

    title, text = build_notification(payload)
    try:
        if send(title, text):
            entry[key] = now
            state[session_id] = entry
            write_state(state)
    except Exception as e:
        log(f"notification send failed: {e}")


def probe():
    """自检：直接问 MCP 服务要工具清单，再查一遍可见机器人。只读，不发送消息。"""
    if not CONFIG.get("mcp_url"):
        print("mcp_url 未填写")
        return 1
    try:
        init = mcp_call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "notify-dingtalk", "version": "1.0"},
        }, timeout=10)
        info = ((init.get("result") or {}).get("serverInfo") or {})
        print(f"MCP 连通 ✓  server={info.get('name')}  v={info.get('version')}")

        tools = [t["name"] for t in ((mcp_call("tools/list", {}, timeout=10).get("result") or {}).get("tools") or [])]
        print(f"可用工具 {len(tools)} 个，含发送接口: "
              f"{'batch_send_robot_msg_to_users' in tools}")

        robots = mcp_call("tools/call", {
            "name": "search_my_robots",
            "arguments": {"currentPage": 1, "pageSize": 20},
        }, timeout=15)
        raw = "".join(c.get("text", "") for c in
                      ((robots.get("result") or {}).get("content") or []))
        print(f"可见机器人: {raw[:500]}")
    except Exception as e:
        print(f"探测失败: {e}")
        log(f"probe failed: {e}")
        return 1
    return 0


def handle_test():
    ok = send("✅ 测试消息", "**Claude Code 通知测试**\n\n如果你看到这条，说明配置正确。")
    print("发送成功" if ok else "未配置通道，检查配置文件里的凭据")
    return 0 if ok else 1


def check_config():
    """校验配置文件：位置、语法、必填项、通道是否配全。只读，不联网。"""
    print(f"配置文件: {CONFIG_PATH or '(未找到，使用全部缺省值 = 不会推送)'}")
    if CONFIG_PATH is None:
        print("  候选位置:")
        for p in _config_candidates():
            print(f"    {p}  {'存在' if p.is_file() else '不存在'}")
        return 1

    problems, notes = [], []

    if UNRESOLVED:
        # 只有真正要用到的通道才把缺环境变量当错误，否则只是提示
        notes.append("以下环境变量未设置（相关通道会被跳过）: "
                     + ", ".join(dict.fromkeys(UNRESOLVED)))

    mcp = bool(CONFIG["mcp_url"] and CONFIG["mcp_robot_code"])
    mcp_target = bool(CONFIG["mcp_user_ids"] or CONFIG["mcp_open_conversation_id"])
    if mcp and not mcp_target:
        problems.append("MCP 通道已配机器人，但既没有 mcp_user_ids 也没有 "
                        "mcp_open_conversation_id，无处可发")

    mode1 = bool(CONFIG["app_key"] and CONFIG["app_secret"]
                 and CONFIG["robot_code"] and CONFIG["user_id"])
    mode2 = bool(CONFIG["webhook_url"])

    if mcp and mcp_target:
        notes.append("生效通道: MCP（单聊）" if CONFIG["mcp_user_ids"]
                     else "生效通道: MCP（群聊）")
    elif mode1:
        notes.append("生效通道: 企业机器人直连 API")
    elif mode2:
        notes.append("生效通道: 群机器人 webhook")
    else:
        problems.append("没有任何一条通道配置完整，推送会被跳过")

    uid = CONFIG["mcp_user_ids"]
    if uid and not isinstance(uid, list):
        problems.append(f'mcp_user_ids 必须是数组，当前是 {uid!r}（应写成 ["你的userId"]）')
    elif isinstance(uid, list) and not all(isinstance(x, str) and x for x in uid):
        problems.append(f"mcp_user_ids 里有非字符串或空值: {uid!r}")

    budget = CONFIG.get("max_message_bytes", 0)
    if not isinstance(budget, int) or budget <= 0 or budget > 19750:
        problems.append(f"max_message_bytes 建议 1000~19750，当前 {budget!r}")
    elif budget > 12000:
        notes.append(f"max_message_bytes={budget} 偏高，中文内容接近上限时容易触发钉钉 460101")

    for n in notes:
        print(f"  · {n}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1
    print("  ✓ 配置完整，可以发送")
    return 0


def guard_markdown(text):
    """钉钉 markdown 模式下，正文里的裸反斜杠/尖括号会顶掉消息，做最小转义。
    代码块和行内反引号里的内容不能转义，否则 Windows 路径会变成双反斜杠。"""
    out, in_code = [], False
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code or stripped.startswith("|"):
            out.append(line)
            continue
        # 把 `...` 包住的部分先挖出来，只对剩下的文本做转义
        parts, buf = [], ""
        i = 0
        while i < len(line):
            if line[i] == "`":
                j = line.find("`", i + 1)
                if j > i:
                    parts.append(("t", buf))
                    parts.append(("c", line[i:j + 1]))
                    buf = ""
                    i = j + 1
                    continue
            buf += line[i]
            i += 1
        parts.append(("t", buf))
        out.append("".join(p if k == "c" else
                           p.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;")
                           for k, p in parts))
    return "\n".join(out)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "test":
        return handle_test()
    if mode == "probe":
        return probe()
    if mode == "check":
        return check_config()

    raw = ""
    try:
        if not sys.stdin.isatty():
            buf = sys.stdin.buffer.read()
            # Claude Code 发的是 UTF-8；Windows 下 sys.stdin 会用 GBK 解码把中文弄坏，
            # 所以必须走 buffer 自己解码。
            raw = buf.decode("utf-8-sig", errors="replace")
    except Exception:
        pass
    payload = {}
    if raw.strip():
        try:
            payload = json.loads(raw)
        except Exception as e:
            log(f"stdin 不是合法 JSON: {e} | 前200字符={raw[:200]!r}")

    try:
        if mode == "start" or payload.get("hook_event_name") == "SessionStart":
            handle_start(payload)
        elif mode == "stop" or payload.get("hook_event_name") == "Stop":
            handle_stop(payload)
        elif mode == "notify" or payload.get("hook_event_name") == "Notification":
            handle_notification(payload)
        elif mode in ("dsh", "dsh-render"):
            if not payload:
                print("stdin 没有 payload，DSH 插件应通过 stdin 传 JSON", file=sys.stderr)
                return 1
            return handle_dsh(payload, dry_run=(mode == "dsh-render"))
    except Exception as e:
        log(f"main error: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
