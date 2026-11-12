# -*- coding: utf-8 -*-
"""千问办公适配器：列对话、读消息、运行检测、深链打开（Start-Process / open，避免 & 被拆）、半自动发送。
半自动：打开绑定对话 → 核对窗口标题 → 写剪贴板 → 提示她按回车 → 轮询 agents.db。
本轮不往真实工作对话发送。
"""
from __future__ import annotations

import datetime, json, os, re, sqlite3, subprocess, sys, time

IS_WIN = os.name == "nt"
HOME = os.path.expanduser("~")
APPDATA = os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
if IS_WIN:
    QW_ROOT = os.path.join(APPDATA, "QwenWorkCN")
else:
    QW_ROOT = os.path.join(HOME, "Library", "Application Support", "QwenWorkCN")
QW_DB = os.path.join(QW_ROOT, "data", "agents.db")
QW_RELAY = os.path.join(QW_ROOT, "relay-port.json")
# 本轮没有批准的千问测试对话
ALLOW_SEND = set()


def _iso(v):
    if v is None or v == "":
        return None
    try:
        v = float(v)
        if v > 1e12:
            v /= 1000.0
        return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(v)[:19]


def _ro(db):
    return sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True, timeout=2)


def installed():
    return os.path.exists(QW_DB)


def pid_alive(pid):
    if not pid:
        return False
    try:
        pid = int(pid)
    except Exception:
        return False
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)
        if h:
            k.CloseHandle(h)
            return True
        return k.GetLastError() in (5, 299)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _procs():
    out = []
    if IS_WIN:
        try:
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "Get-Process QwenWorkCN -ErrorAction SilentlyContinue | "
                  "Select-Object Id,MainWindowTitle | ConvertTo-Json -Compress")
            raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                 capture_output=True, timeout=20).stdout
            data = json.loads(raw.decode("utf-8", "replace") or "[]")
            if isinstance(data, dict):
                data = [data]
            for o in data:
                out.append((o.get("Id"), o.get("MainWindowTitle") or ""))
        except Exception:
            pass
    else:
        try:
            raw = subprocess.run(["pgrep", "-l", "-i", "QwenWorkCN"],
                                 capture_output=True, timeout=8).stdout.decode()
            for line in raw.splitlines():
                parts = line.split(None, 1)
                if parts:
                    out.append((int(parts[0]), "千问办公"))
        except Exception:
            pass
    return out


def window_titles():
    return [t for _, t in _procs() if t]


def status():
    plist = _procs()
    titles = [t for _, t in plist if t]
    relay = None
    if os.path.exists(QW_RELAY):
        try:
            d = json.load(open(QW_RELAY, encoding="utf-8"))
            relay = {"port": d.get("port"), "pid": d.get("pid"), "pidAlive": pid_alive(d.get("pid"))}
        except Exception:
            pass
    streaming = 0
    if os.path.exists(QW_DB):
        try:
            c = _ro(QW_DB)
            streaming = c.execute("select count(*) from sub_chats where stream_id is not null and stream_id!=''").fetchone()[0]
            c.close()
        except Exception:
            pass
    return {
        "app": "qwen",
        "appLabel": "千问办公",
        "installed": installed(),
        "running": bool(plist),
        "processCount": len(plist),
        "windowTitles": titles,
        "currentTitle": next((t for t in titles if t and t not in ("千问办公", "QwenWorkCN")), titles[0] if titles else None),
        "relay": relay,
        "streamingSubChats": streaming,
        "dbMtime": _iso(os.path.getmtime(QW_DB)) if os.path.exists(QW_DB) else None,
    }


def list_sessions(limit=80):
    if not installed():
        return []
    c = _ro(QW_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """select c.id as chatId, c.name as title, s.id as subChatId, s.session_id as sessionId,
                  s.updated_at, s.stream_id, s.messages
           from chats c join sub_chats s on s.chat_id=c.id
           where c.deleted_at is null
           order by s.updated_at desc limit ?""",
        (limit,),
    ).fetchall()
    titles = window_titles()
    out = []
    for r in rows:
        # last message meta
        last = c.execute(
            """select role, metadata, created_at from messages
               where sub_chat_id=? order by sequence desc limit 1""",
            (r["subChatId"],),
        ).fetchone()
        last_role, last_status, last_at = None, None, None
        if last:
            last_role, last_at = last["role"], _iso(last["created_at"])
            try:
                md = json.loads(last["metadata"] or "{}")
                last_status = md.get("status")
            except Exception:
                pass
        nmsg = c.execute("select count(*) from messages where sub_chat_id=?", (r["subChatId"],)).fetchone()[0]
        title = r["title"] or "(无标题)"
        is_current = any(t == title for t in titles)
        item = {
            "id": r["subChatId"],
            "chatId": r["chatId"],
            "subChatId": r["subChatId"],
            "sessionId": r["sessionId"],
            "title": title,
            "updated": _iso(r["updated_at"]),
            "streaming": bool(r["stream_id"]),
            "isCurrent": is_current,
            "serveLive": False,
            "messages": nmsg,
            "lastRole": last_role,
            "lastStatus": last_status,
            "lastAt": last_at,
            "runState": "running" if r["stream_id"] else "idle",
            "openLink": "qwenwork-cn://notification-click?chatId=%s&subChatId=%s" % (r["chatId"], r["subChatId"]),
        }
        out.append(item)
    return out


def _parts_text(parts):
    if not parts:
        return ""
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except Exception:
            return parts
    txt = ""
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict):
                if isinstance(p.get("text"), str):
                    txt += p["text"]
                elif p.get("type") in ("text", "output_text") and isinstance(p.get("content"), str):
                    txt += p["content"]
    return txt


def tail(sub_or_chat, n=40):
    if not installed():
        return []
    c = _ro(QW_DB)
    c.row_factory = sqlite3.Row
    # accept subChatId or chatId
    sid = sub_or_chat
    row = c.execute("select id from sub_chats where id=? or chat_id=? limit 1", (sid, sid)).fetchone()
    if not row:
        return []
    sub = row["id"]
    rows = c.execute(
        """select role, parts, metadata, created_at, sequence from messages
           where sub_chat_id=? order by sequence desc limit ?""",
        (sub, n),
    ).fetchall()
    out = []
    for r in reversed(list(rows)):
        status = None
        md = {}
        try:
            md = json.loads(r["metadata"] or "{}")
            if not isinstance(md, dict):
                md = {}
            status = md.get("status")
        except Exception:
            md = {}
        typ = "message"
        role = r["role"] or "assistant"
        if role not in ("user", "assistant"):
            typ = "tool"
        parts = r["parts"]
        try:
            if isinstance(parts, str):
                parts = json.loads(parts)
        except Exception:
            pass
        out.append({
            "id": "qw-%s-%s" % (r["sequence"], role),
            "role": role if role in ("user", "assistant") else "tool",
            "type": typ,
            "text": _parts_text(r["parts"]),
            "time": _iso(r["created_at"]),
            "status": status,
            "parts": parts if isinstance(parts, list) else [],
            "metadata": md,
            "sequence": r["sequence"],
        })
    return out


def open_link(uri):
    """Open a deeplink without letting cmd split on &."""
    if IS_WIN:
        # Start-Process with a single-quoted literal
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process '%s'" % uri.replace("'", "''")],
            capture_output=True, timeout=20,
        )
        return r.returncode == 0, uri
    r = subprocess.run(["open", uri], capture_output=True, timeout=20)
    return r.returncode == 0, uri


def open_session(chat_id, sub_chat_id):
    uri = "qwenwork-cn://notification-click?chatId=%s&subChatId=%s" % (chat_id, sub_chat_id)
    return open_link(uri)


def _set_clipboard(text):
    if IS_WIN:
        for i in range(8):
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Set-Clipboard -Value @'\n%s\n'@; (Get-Clipboard -Raw)" % text],
                capture_output=True, timeout=15,
            )
            got = (r.stdout or b"").decode("utf-8", "replace").strip()
            if r.returncode == 0 and got.strip() == text.strip():
                return True, None
            time.sleep(0.3)
        return False, "写不进剪贴板（可能被 SyncClipboard 占用），请手动复制预览里的全文再按回车"
    r = subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=10)
    if r.returncode == 0:
        return True, None
    return False, "写不进剪贴板"


def title_matches(expected_title):
    titles = window_titles()
    if not expected_title:
        return False, titles
    if any(t == expected_title or expected_title in t or t in expected_title for t in titles if t):
        return True, titles
    return False, titles


def send_semiauto(chat_id, sub_chat_id, title, text, confirm=False):
    if not confirm:
        return {"ok": False, "error": "缺少确认"}
    key = "%s/%s" % (chat_id, sub_chat_id)
    if key not in ALLOW_SEND and chat_id not in ALLOW_SEND and sub_chat_id not in ALLOW_SEND:
        return {"ok": False, "error": "本轮不往千问办公的真实工作对话发送。请先在板上登记测试对话，或之后再授权。"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "没有内容"}
    if not _procs():
        return {"ok": False, "error": "千问办公没有在运行"}
    ok, uri = open_session(chat_id, sub_chat_id)
    if not ok:
        return {"ok": False, "error": "没能打开对话"}
    time.sleep(2.5)
    match, titles = title_matches(title)
    if not match:
        return {"ok": False, "error": "窗口对不上要发的对话，已取消写入剪贴板", "titles": titles, "opened": uri}
    clip_ok, clip_err = _set_clipboard(text)
    if not clip_ok:
        return {"ok": False, "phase": "need_paste", "error": clip_err, "opened": True, "titleMatched": True}
    return {
        "ok": True,
        "phase": "waiting_enter",
        "message": "已打开「%s」，全文已放进剪贴板。请在输入框粘贴并按回车。我不会替你按。" % (title or "该对话"),
        "titleMatched": True,
    }


def send_status(chat_id, sub_chat_id, since_iso=None):
    """Poll agent_turn_inputs + last assistant status."""
    if not installed():
        return {"phase": "unknown"}
    c = _ro(QW_DB)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """select id, state, source, created_at, accepted_at, target_turn_completed_at, error_code
           from agent_turn_inputs where chat_id=? and sub_chat_id=?
           order by created_at desc limit 5""",
        (chat_id, sub_chat_id),
    ).fetchall()
    last = c.execute(
        """select role, metadata, created_at from messages where sub_chat_id=?
           order by sequence desc limit 1""",
        (sub_chat_id,),
    ).fetchone()
    stream = c.execute("select stream_id from sub_chats where id=?", (sub_chat_id,)).fetchone()
    phase = "pending"
    info = {}
    if rows:
        r = rows[0]
        info = {
            "state": r["state"],
            "accepted_at": _iso(r["accepted_at"]),
            "completed_at": _iso(r["target_turn_completed_at"]),
            "created_at": _iso(r["created_at"]),
        }
        if r["state"] == "consumed" and r["accepted_at"]:
            phase = "delivered"
        if r["target_turn_completed_at"]:
            phase = "replied"
        if stream and stream[0]:
            phase = "running"
    if last:
        try:
            md = json.loads(last["metadata"] or "{}")
            if last["role"] == "assistant" and md.get("status") == "completed":
                if phase in ("delivered", "running"):
                    phase = "replied"
        except Exception:
            pass
        if stream and stream[0]:
            phase = "running"
    return {"phase": phase, "input": info, "streaming": bool(stream and stream[0])}
