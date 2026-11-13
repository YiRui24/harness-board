# -*- coding: utf-8 -*-
"""把各源原始消息收成统一模型，供对话流卡片使用。

kind: text | question | approval | publish | queue | tool | diff | plan | error

字段只从已核对过的协议/适配器里取；对不上的走 generic，verified=False，
并在 card.note 里标明「还没对上真实字段，先按通用卡片显示」。
"""
from __future__ import annotations

import json, re, uuid

KINDS = ("text", "question", "approval", "publish", "queue", "tool", "diff", "plan", "error")

# Codex ThreadItem.type（官方 app-server v2 item.rs）
CODEX_TEXT = {"userMessage", "agentMessage"}
CODEX_TOOL = {
    "commandExecution", "mcpToolCall", "dynamicToolCall", "collabAgentToolCall",
    "functionCallOutput", "webSearch", "imageView", "imageGeneration", "sleep",
    "subAgentActivity", "hookPrompt", "enteredReviewMode", "exitedReviewMode",
    "contextCompaction", "reasoning",
}
HOST_RE = re.compile(r"https?://[^\s\"'<>]+qwenwork\.host[^\s\"'<>]*", re.I)
PUBLISH_TOOLS = ("qwenwork_pages_publish", "qwenwork-pages-publish", "pages_publish")


def _s(v, n=0):
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    return v[:n] if n else v


def _opts(raw):
    """把各种 options 收成 [{id,label,description}]。官方 Codex: label+description；WorkBuddy AskUserQuestion 同形。"""
    out = []
    if not isinstance(raw, list):
        return out
    for i, o in enumerate(raw):
        if isinstance(o, str):
            out.append({"id": str(i), "label": o, "description": ""})
        elif isinstance(o, dict):
            lab = o.get("label") or o.get("title") or o.get("text") or o.get("value") or ""
            out.append({
                "id": str(o.get("id") or i),
                "label": _s(lab),
                "description": _s(o.get("description") or ""),
            })
    return out


def _questions(raw):
    """Codex ToolRequestUserInputQuestion / agentMessage.questions；WorkBuddy AskUserQuestion.questions。"""
    out = []
    if isinstance(raw, dict) and "questions" in raw:
        raw = raw.get("questions")
    if not isinstance(raw, list):
        return out
    for i, q in enumerate(raw):
        if isinstance(q, str):
            out.append({"id": str(i), "header": "", "question": q, "options": [], "multiSelect": False})
            continue
        if not isinstance(q, dict):
            continue
        # 官方：id, header, question, options[{label,description}], isOther, isSecret
        # WorkBuddy：question, header, options, multiSelect
        # 另一种异步题（文档提及）：title + options[]
        qtext = q.get("question") or q.get("title") or q.get("header") or q.get("text") or ""
        out.append({
            "id": str(q.get("id") or i),
            "header": _s(q.get("header") or ""),
            "question": _s(qtext),
            "options": _opts(q.get("options") or []),
            "multiSelect": bool(q.get("multiSelect") or q.get("multi_select")),
            "isSecret": bool(q.get("isSecret") or q.get("is_secret")),
            "isOther": q.get("isOther") is True or q.get("is_other") is True,
            "required": q.get("required") if isinstance(q.get("required"), bool) else None,
            "inputType": q.get("inputType") if q.get("inputType") == "text" else None,
        })
    return out


def _base(raw, kind, verified, role="assistant"):
    mid = raw.get("id") or raw.get("itemId") or raw.get("ordinal")
    if mid is None or mid == "":
        mid = "m-" + uuid.uuid4().hex[:12]
    return {
        "id": str(mid),
        "kind": kind,
        "role": raw.get("role") or role,
        "type": raw.get("type") or raw.get("sourceType") or kind,
        "sourceType": raw.get("type") or raw.get("sourceType") or "",
        "text": raw.get("text") or "",
        "time": raw.get("time") or raw.get("created") or "",
        "status": raw.get("status"),
        "ordinal": raw.get("ordinal"),
        "verified": bool(verified),
        "card": {},
    }


def _guessed(item, why):
    item["verified"] = False
    item["card"]["guessed"] = True
    item["card"]["note"] = why
    return item


def _urls(obj, acc=None, depth=0):
    if acc is None:
        acc = []
    if depth > 6:
        return acc
    if isinstance(obj, str):
        acc.extend(HOST_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _urls(v, acc, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            _urls(v, acc, depth + 1)
    return acc


def _deep_get(obj, keys):
    if not isinstance(obj, dict):
        return None
    for k in keys:
        if obj.get(k):
            return obj[k]
    for v in obj.values():
        if isinstance(v, dict):
            r = _deep_get(v, keys)
            if r:
                return r
    return None


def _tool_name(raw):
    for k in ("tool", "name", "command"):
        if raw.get(k):
            return _s(raw[k])
    return ""


def _already(raw):
    if raw.get("kind") in KINDS:
        it = _base(raw, raw["kind"], raw.get("verified", True), raw.get("role") or "assistant")
        it["text"] = raw.get("text") or it["text"]
        it["card"] = dict(raw.get("card") or {})
        if raw.get("tool"):
            it["tool"] = raw["tool"]
        if raw.get("kind") == "question" and not it["card"].get("questions"):
            it["card"]["questions"] = _questions(raw.get("questions") or raw.get("card") or raw)
        return it
    return None


def from_codex(raw):
    """来源：thread_items.item_json / app-server ThreadItem（官方 v2）。"""
    got = _already(raw)
    if got:
        return [got]
    typ = raw.get("type") or ""
    questions = raw.get("questions")
    if typ in ("userMessage", "agentMessage") or raw.get("role") in ("user", "assistant"):
        if questions:
            it = _base(raw, "question", True, "assistant")
            it["card"] = {
                "questions": _questions(questions),
                "actionable": True,
                "via": "send",  # 官方作答走 item/tool/requestUserInput；板上没有未决 RPC 时改走发送
                "fallback": "send",
            }
            if not it["text"]:
                qs = it["card"]["questions"]
                it["text"] = qs[0]["question"] if qs else ""
            return [it]
        it = _base(raw, "text", True, raw.get("role") or ("user" if typ == "userMessage" else "assistant"))
        return [it]
    if typ == "plan":
        it = _base(raw, "plan", True, "assistant")
        it["card"] = {"text": raw.get("text") or ""}
        it["text"] = raw.get("text") or "计划"
        return [it]
    if typ == "fileChange":
        changes = raw.get("changes") or []
        files = []
        for c in changes if isinstance(changes, list) else []:
            if isinstance(c, dict):
                kind = c.get("kind")
                if isinstance(kind, dict):
                    kind = next(iter(kind.keys()), "update")
                files.append({"path": _s(c.get("path")), "kind": _s(kind or "update"), "diff": _s(c.get("diff"), 4000)})
            elif isinstance(c, str):
                files.append({"path": c, "kind": "update", "diff": ""})
        st = (raw.get("status") or "").lower()
        it = _base(raw, "diff", True, "tool")
        it["card"] = {"files": files, "status": raw.get("status")}
        it["text"] = "改了 %d 个文件" % len(files) if files else (raw.get("text") or "改文件")
        if st == "failed":
            err = _base(raw, "error", True, "system")
            err["card"] = {"message": "改文件失败", "detail": it["text"]}
            err["text"] = "改文件失败"
            return [it, err]
        return [it]
    if typ == "commandExecution":
        st = (raw.get("status") or "").lower()
        it = _base(raw, "tool", True, "tool")
        cmd = raw.get("command") or raw.get("text") or ""
        it["tool"] = "commandExecution"
        it["card"] = {
            "name": "执行命令",
            "command": cmd,
            "cwd": raw.get("cwd") or "",
            "status": raw.get("status"),
            "exitCode": raw.get("exit_code") or raw.get("exitCode"),
            "output": _s(raw.get("aggregated_output") or raw.get("aggregatedOutput") or "", 4000),
        }
        it["text"] = cmd or "执行命令"
        if st == "failed":
            err = _base(raw, "error", True, "system")
            err["id"] = str(it["id"]) + "-err"
            err["card"] = {"message": "命令执行失败", "detail": _s(it["card"]["output"], 500) or cmd}
            err["text"] = "命令执行失败"
            return [it, err]
        if st == "declined":
            it["kind"] = "approval"
            it["card"].update({"scope": "command", "decision": "decline", "actionable": False, "fallback": "open"})
            return [it]
        return [it]
    if typ in CODEX_TOOL:
        if typ == "reasoning" and not (raw.get("text") or "").strip():
            return []
        it = _base(raw, "tool", True, "tool")
        name = typ
        it["tool"] = typ
        it["card"] = {"name": name, "detail": _s(raw.get("text"), 500), "status": raw.get("status")}
        it["text"] = raw.get("text") or typ
        if typ in ("mcpToolCall", "dynamicToolCall"):
            it["card"]["name"] = _tool_name(raw) or typ
            if raw.get("error") or (raw.get("status") or "").lower() == "failed":
                err = _base(raw, "error", True, "system")
                err["id"] = str(it["id"]) + "-err"
                err["card"] = {"message": _s(raw.get("error") or "工具调用失败"), "detail": it["card"]["name"]}
                err["text"] = err["card"]["message"]
                return [it, err]
        return [it]
    if typ or raw.get("text"):
        it = _base(raw, "tool" if raw.get("role") == "tool" else "text", False, raw.get("role") or "assistant")
        return [_guessed(it, "Codex 出现了尚未收录的类型 %s，先按通用卡片显示" % (typ or "unknown"))]
    return []


def _qwen_part_tool(p):
    if not isinstance(p, dict):
        return ""
    for k in ("tool", "name", "toolName", "tool_name"):
        if p.get(k):
            return _s(p[k])
    t = _s(p.get("type") or "")
    return t


def from_qwen(raw):
    """来源：agents.db messages.parts / metadata。发布工具名、公网域名已核实；parts 的精确字段未在本机采到样本。"""
    got = _already(raw)
    if got:
        return [got]
    parts = raw.get("parts")
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except Exception:
            parts = []
    if not isinstance(parts, list):
        parts = []
    blob = {"text": raw.get("text"), "parts": parts, "metadata": raw.get("metadata")}
    urls = _urls(blob)
    items = []
    text = raw.get("text") or ""
    role = raw.get("role") or "assistant"
    typ = raw.get("type") or "message"

    # 发布：官方文档对话里会出现「网页已发布」卡片和 *.qwenwork.host 链接；
    # 技能/工具名 qwenwork_pages_publish 已核实。结果 JSON 里 title/url 的键名未采到，做启发式提取。
    publish_hit = bool(urls) or ("网页已发布" in (text or ""))
    tool_names = []
    questions = []
    tools = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        tn = _qwen_part_tool(p).lower()
        if tn:
            tool_names.append(tn)
        if any(x in tn for x in PUBLISH_TOOLS) or "pages_publish" in tn or "pages-publish" in tn:
            publish_hit = True
        qs = p.get("questions") or ((p.get("input") or {}).get("questions") if isinstance(p.get("input"), dict) else None)
        if qs or any(k in tn for k in ("ask_user", "askuser", "question", "request_user_input")):
            questions.extend(_questions(qs or p))
        if (p.get("type") or "").startswith("tool") or tn:
            tools.append(p)

    if role in ("user", "assistant") and typ in ("message", "text", "") and text and not publish_hit and not questions:
        items.append(_base(raw, "text", True, role))

    if questions:
        it = _base(raw, "question", False, "assistant")
        it["card"] = {"questions": questions, "actionable": True, "via": "send", "fallback": "send"}
        it["text"] = questions[0]["question"] if questions else text
        _guessed(it, "千问办公的反问 parts 字段没有本机样本，先按问题卡片显示，作答走现有发送/半自动")
        items.append(it)

    if publish_hit:
        title = _deep_get(blob, ("name", "title", "pageName", "page_name", "siteName")) or ""
        url = urls[0] if urls else _s(_deep_get(blob, ("url", "publicUrl", "public_url", "host", "domain")))
        # 只采用看起来像链接的
        if url and not str(url).startswith("http"):
            if "qwenwork.host" in str(url):
                url = "https://" + str(url).lstrip("/")
            else:
                url = urls[0] if urls else ""
        st = raw.get("status") or _deep_get(blob, ("status", "state", "deployStatus")) or ("已发布" if url else "发布中")
        it = _base(raw, "publish", bool(urls) or any("pages_publish" in t for t in tool_names), "assistant")
        it["card"] = {
            "title": _s(title) or (text.split("\n")[0][:40] if text else "已发布的网页"),
            "url": _s(url),
            "status": _s(st),
            "preview": _s(_deep_get(blob, ("preview", "previewUrl", "preview_url")) or ""),
        }
        it["text"] = it["card"]["title"]
        if not urls or not title:
            _guessed(it, "发布卡的标题/链接键名还没有对上真实 parts 样本，能识别官方域名或工具名时再尽量抽出")
        items.append(it)
        if text and text not in (it["card"]["title"],) and "网页已发布" not in text:
            t = _base(raw, "text", True, role)
            t["id"] = str(it["id"]) + "-txt"
            t["text"] = text
            items.insert(0, t)

    for p in tools:
        tn = _qwen_part_tool(p)
        if any(x in tn.lower() for x in PUBLISH_TOOLS):
            continue
        if any(k in tn.lower() for k in ("ask_user", "askuser", "question", "request_user_input")):
            continue
        it = _base(raw, "tool", False, "tool")
        it["id"] = str(raw.get("id") or raw.get("sequence") or "q") + "-" + (tn or "tool")
        it["tool"] = tn or (p.get("type") or "tool")
        it["card"] = {"name": tn or p.get("type") or "工具", "status": p.get("status") or raw.get("status")}
        it["text"] = it["card"]["name"]
        _guessed(it, "千问办公工具 parts 的精确字段未采到样本，只显示工具名")
        items.append(it)

    if (raw.get("status") or "").lower() in ("failed", "error") or raw.get("error"):
        err = _base(raw, "error", True, "system")
        err["id"] = str(raw.get("id") or "e") + "-err"
        err["card"] = {"message": _s(raw.get("error") or "出错了"), "detail": ""}
        err["text"] = err["card"]["message"]
        items.append(err)

    if not items and (text or typ):
        it = _base(raw, "text" if role in ("user", "assistant") else "tool", role in ("user", "assistant"), role)
        items.append(it)
    return items


def from_workbuddy(raw):
    """来源：WorkBuddy/CodeBuddy 会话 jsonl。已核实：message / function_call / function_call_result。
    AskUserQuestion 是官方 SDK 工具名（questions/header/options/multiSelect）。
    权限请求走 canUseTool / permission_prompt 钩子，不一定写入 jsonl。"""
    got = _already(raw)
    if got:
        return [got]
    typ = raw.get("type") or ""
    name = _tool_name(raw)
    args = raw.get("arguments") or raw.get("input") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"_raw": args}

    if typ == "function_call" and name == "AskUserQuestion":
        qs = _questions(args if isinstance(args, dict) else raw)
        it = _base(raw, "question", True, "assistant")
        it["card"] = {"questions": qs, "actionable": True, "via": "send", "fallback": "send"}
        it["text"] = qs[0]["question"] if qs else "想跟你确认一件事"
        return [it]

    if typ in ("permission_request", "permission_prompt") or name in ("permission_prompt", "PermissionRequest"):
        # 钩子事件名已核实，但是否出现在 jsonl 未采到样本
        it = _base(raw, "approval", False, "assistant")
        it["card"] = {
            "scope": "permission",
            "title": "等你批准",
            "command": _s((args or {}).get("command") or name),
            "detail": _s((args or {}).get("file_path") or (args or {}).get("path") or ""),
            "actionable": False,
            "fallback": "open",
        }
        it["text"] = "等你批准：" + (it["card"]["command"] or name or "操作")
        _guessed(it, "WorkBuddy 的权限请求主要通过钩子，不一定写进对话记录；能看到时先提示去原窗口处理")
        return [it]

    if typ == "function_call":
        it = _base(raw, "tool", True, "tool")
        it["tool"] = name or "function_call"
        it["card"] = {"name": name or "工具", "status": raw.get("status"), "detail": _s(json.dumps(args, ensure_ascii=False) if args else "", 400)}
        it["text"] = name or "调用工具"
        if name in ("Write", "Edit", "MultiEdit"):
            paths = []
            if isinstance(args, dict):
                if args.get("file_path") or args.get("path"):
                    paths.append(_s(args.get("file_path") or args.get("path")))
                for e in args.get("edits") or []:
                    if isinstance(e, dict) and (e.get("file_path") or e.get("path")):
                        paths.append(_s(e.get("file_path") or e.get("path")))
            if paths:
                it["kind"] = "diff"
                it["card"] = {"files": [{"path": p, "kind": "update", "diff": ""} for p in paths], "status": raw.get("status")}
                it["text"] = "改了 %d 个文件" % len(paths)
        return [it]

    if typ == "function_call_result":
        if raw.get("status") in ("failed", "error") or raw.get("error") or raw.get("isError"):
            it = _base(raw, "error", True, "system")
            it["card"] = {"message": _s(raw.get("error") or raw.get("text") or "工具返回失败"), "detail": name}
            it["text"] = it["card"]["message"]
            return [it]
        return []  # 成功结果不单独占一行，避免刷屏

    if typ == "reasoning":
        if not (raw.get("text") or "").strip():
            return []
        it = _base(raw, "tool", True, "tool")
        it["tool"] = "reasoning"
        it["card"] = {"name": "思考", "detail": _s(raw.get("text"), 400)}
        it["text"] = "思考"
        return [it]

    if typ in ("message", "text", "") or raw.get("role") in ("user", "assistant"):
        it = _base(raw, "text", True, raw.get("role") or "assistant")
        return [it]

    if typ or raw.get("text"):
        it = _base(raw, "tool" if raw.get("role") == "tool" else "text", False, raw.get("role") or "assistant")
        return [_guessed(it, "WorkBuddy 出现了尚未收录的类型 %s，先按通用卡片显示" % (typ or "unknown"))]
    return []


def from_queue(raw, index=1, total=1, cancelable=False, verified=True):
    got = _already(raw)
    if got:
        got["kind"] = "queue"
        got["role"] = "user"
        got["card"].setdefault("position", index)
        got["card"].setdefault("total", total)
        got["card"].setdefault("cancelable", cancelable)
        got["card"].setdefault("queuedId", raw.get("queuedId") or raw.get("id"))
        return got
    it = _base(raw, "queue", verified, "user")
    it["card"] = {
        "position": index,
        "total": total,
        "cancelable": bool(cancelable),
        "queuedId": raw.get("queuedId") or raw.get("id"),
    }
    it["status"] = raw.get("status") or "queued"
    it["text"] = raw.get("text") or ""
    return it


def pending_from_items(items, app, device=None, sid=None, title=""):
    """从已规范化的消息里抽出还没处理的反问/批准，供总览「等你处理」。"""
    out = []
    for it in items or []:
        kind = it.get("kind")
        card = it.get("card") or {}
        if kind == "question" and not card.get("answered"):
            qs = card.get("questions") or []
            preview = (qs[0].get("question") if qs else it.get("text") or "想跟你确认一件事")[:60]
            out.append({
                "kind": "question", "app": app, "sid": sid or it.get("sid"), "id": sid,
                "itemId": it.get("id"), "title": title, "preview": preview,
                "updated": it.get("time"), "dev": device,
            })
        elif kind == "approval" and not card.get("decision") and card.get("fallback") != "done":
            preview = (card.get("command") or card.get("title") or it.get("text") or "等你批准")[:60]
            out.append({
                "kind": "approval", "app": app, "sid": sid or it.get("sid"), "id": sid,
                "itemId": it.get("id"), "title": title, "preview": preview,
                "updated": it.get("time"), "dev": device,
            })
    return out


def _normalize_item(app, raw):
    if not isinstance(raw, dict):
        return []
    if raw.get("kind") == "queue" or raw.get("queued"):
        return [from_queue(raw, index=raw.get("position") or (raw.get("card") or {}).get("position") or 1,
                           total=raw.get("total") or (raw.get("card") or {}).get("total") or 1,
                           cancelable=bool(raw.get("cancelable") if "cancelable" in raw else (raw.get("card") or {}).get("cancelable")),
                           verified=raw.get("verified", True))]
    if app == "codex":
        return from_codex(raw)
    if app == "qwen":
        return from_qwen(raw)
    if app == "workbuddy":
        return from_workbuddy(raw)
    got = _already(raw)
    return [got] if got else from_codex(raw)  # 未知源按通用/Codex 形状尽量收


def normalize_item(app, raw):
    items = _normalize_item(app, raw)
    for it in items:
        if it.get("kind") != "question":
            continue
        card = it.get("card") or {}
        questions = _questions(card.get("questions") or [])
        # 敏感题整组不离开来源设备，也不进入待办预览。
        if any(q.get("isSecret") for q in questions):
            it["text"] = "含敏感问题，请到原窗口处理"
            done = card.get("answered") is True or card.get("fallback") == "done"
            it["card"] = {"sensitive": True, "questions": [], "answered": done,
                          "fallback": "done" if done else "open"}
            continue
        card["questions"] = questions
        # 来源未证明普通消息就是本请求的合法回执，不能降级发送。
        card["responseMode"] = "open"
        card["actionable"] = False
        if card.get("fallback") != "done":
            card["fallback"] = "open"
        it["card"] = card
    return items


def normalize_list(app, items, queue=None):
    out = []
    for raw in items or []:
        out.extend(normalize_item(app, raw))
    qitems = queue or []
    have = {i.get("id") or (i.get("card") or {}).get("queuedId") for i in out if i.get("kind") == "queue"}
    n = len(qitems)
    for i, raw in enumerate(qitems, 1):
        qid = (raw.get("id") if isinstance(raw, dict) else None) or (raw.get("queuedId") if isinstance(raw, dict) else None)
        if qid and qid in have:
            continue
        if isinstance(raw, dict) and raw.get("kind") == "queue":
            out.append(from_queue(raw, index=(raw.get("card") or {}).get("position") or i,
                                  total=(raw.get("card") or {}).get("total") or n,
                                  cancelable=bool((raw.get("card") or {}).get("cancelable")),
                                  verified=raw.get("verified", True)))
        else:
            out.append(from_queue(raw if isinstance(raw, dict) else {"text": str(raw)}, index=i, total=n,
                                  cancelable=bool(isinstance(raw, dict) and raw.get("cancelable")),
                                  verified=True))
    return out
