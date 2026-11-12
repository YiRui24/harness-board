# -*- coding: utf-8 -*-
"""WorkBuddy 适配器：列对话、读消息、状态（运行 / 界面当前对话 / serve 是否活着分开）、深链打开、官方 HTTP 发送。
密码只从活着的 serve 进程环境读取，只留在内存，绝不打印或写文件。
"""
from __future__ import annotations

import datetime, glob, json, os, re, sqlite3, subprocess, sys, time, urllib.error, urllib.request

IS_WIN = os.name == "nt"
HOME = os.path.expanduser("~")
WB = os.path.join(HOME, ".workbuddy")
WB_DB = os.path.join(WB, "workbuddy.db")
# None = 不限对话（总控板上她本人按「发送」即确认）；设成集合则只允许这些对话
ALLOW_SEND = None


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
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            k.CloseHandle(h)
            return True
        return k.GetLastError() in (5, 299)  # access denied / partial copy → exists
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _tail_bytes(path, max_bytes=524288):
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        data = f.read()
    lines = data.split(b"\n")
    if size > max_bytes:
        lines = lines[1:]
    return [l for l in lines if l.strip()]


def installed():
    return os.path.exists(WB_DB)


def _procs():
    """Return list of (pid, title) for WorkBuddy."""
    out = []
    if IS_WIN:
        try:
            ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                  "Get-Process WorkBuddy -ErrorAction SilentlyContinue | "
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
            raw = subprocess.run(["pgrep", "-l", "-i", "WorkBuddy"],
                                 capture_output=True, timeout=8).stdout.decode()
            for line in raw.splitlines():
                parts = line.split(None, 1)
                if parts:
                    out.append((int(parts[0]), "WorkBuddy"))
        except Exception:
            pass
    return out


def current_conversation():
    p = os.path.join(WB, "logs", "renderer.log")
    if not os.path.exists(p):
        return None
    for line in reversed(_tail_bytes(p, 1_000_000)):
        if b"[heartbeat] appState" not in line:
            continue
        try:
            o = json.loads(line)
            msg = o["message"][0]
            st = json.loads(msg[msg.index("{"):])
            return {
                "conversationId": st.get("currentConversationId"),
                "windowHidden": st.get("docHidden"),
                "logTimeUtc": o.get("timestamp"),
            }
        except Exception:
            continue
    return None


def live_serves():
    res = []
    for f in glob.glob(os.path.join(WB, "sessions", "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        sid = d.get("sessionId")
        if not sid or str(sid).startswith("prewarm-"):
            continue
        ep = str(d.get("endpoint") or d.get("url") or "")
        port = ep.rsplit(":", 1)[-1].split("/")[0] if ep.startswith("http") else None
        pid = d.get("pid")
        hb = d.get("lastHeartbeat") or d.get("updatedAt")
        res.append({
            "sessionId": sid,
            "pid": pid,
            "pidAlive": pid_alive(pid),
            "servePort": port,
            "heartbeat": _iso(hb),
        })
    return res


def transcript_path(sid):
    hits = glob.glob(os.path.join(WB, "projects", "*", sid + ".jsonl"))
    return hits[0] if hits else None


def last_state(path):
    last = None
    for line in reversed(_tail_bytes(path, 131072)):
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("type") == "message" and o.get("role") in ("user", "assistant"):
            last = o
            break
    if not last:
        return None
    return {"lastRole": last.get("role"), "lastStatus": last.get("status"), "lastAt": _iso(last.get("timestamp"))}


def status():
    plist = _procs()
    titles = [t for _, t in plist if t]
    cur = current_conversation() if plist else None
    lives = [s for s in live_serves() if s["pidAlive"]] if plist else []
    return {
        "app": "workbuddy",
        "appLabel": "WorkBuddy",
        "installed": installed(),
        "running": bool(plist),
        "processCount": len(plist),
        "windowTitles": titles,
        "currentConversationId": (cur or {}).get("conversationId"),
        "windowHidden": (cur or {}).get("windowHidden"),
        "liveServes": lives,
        "dbMtime": _iso(os.path.getmtime(WB_DB)) if os.path.exists(WB_DB) else None,
    }


def list_sessions(limit=80):
    if not installed():
        return []
    c = _ro(WB_DB)
    c.row_factory = sqlite3.Row
    running = bool(_procs())
    live = {s["sessionId"]: s for s in live_serves() if s["pidAlive"] and running}
    cur = (current_conversation() or {}).get("conversationId")
    rows = c.execute(
        """select id, coalesce(nullif(custom_title,''), title) as title, status, cwd, unread,
           created_at, updated_at, last_activity_at from sessions where deleted_at is null
           order by coalesce(last_activity_at, updated_at) desc limit ?""",
        (limit,),
    ).fetchall()
    out = []
    for r in rows:
        tp = transcript_path(r["id"])
        item = {
            "id": r["id"],
            "title": r["title"] or "(无标题)",
            "cwd": r["cwd"],
            "unread": r["unread"],
            "updated": _iso(r["last_activity_at"] or r["updated_at"]),
            "isCurrent": r["id"] == cur,
            "serveLive": r["id"] in live,
            "servePort": (live.get(r["id"]) or {}).get("servePort"),
            "openLink": "workbuddy://chat/%s" % r["id"],
        }
        if r["id"] in live:
            item["servePid"] = live[r["id"]]["pid"]
        if tp:
            item.update(last_state(tp) or {})
            if item.get("lastRole") == "user":
                item["runState"] = "running"
            elif item.get("lastStatus") == "completed":
                item["runState"] = "idle"
            else:
                item["runState"] = "idle"
        else:
            item["runState"] = "idle"
        out.append(item)
    return out


def tail(sid, n=40):
    tp = transcript_path(sid)
    if not tp:
        return []
    out = []
    for line in _tail_bytes(tp, 2_000_000):
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t != "message" or o.get("role") not in ("user", "assistant"):
            if t in ("function_call", "function_call_result", "reasoning", "permission_request", "permission_prompt"):
                args = o.get("arguments") or o.get("input")
                out.append({
                    "id": o.get("callId") or o.get("id") or o.get("toolUseID"),
                    "role": "tool" if t != "permission_request" else "assistant",
                    "type": t,
                    "tool": o.get("name"),
                    "name": o.get("name"),
                    "arguments": args,
                    "time": _iso(o.get("timestamp")),
                    "status": o.get("status"),
                    "error": o.get("error") or o.get("isError"),
                    "text": o.get("text") or "",
                })
            continue
        txt = ""
        for part in o.get("content") or []:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                # strip bulky system-reminder wrappers for display
                chunk = part["text"]
                if chunk.startswith("<system-reminder"):
                    end = chunk.find("</system-reminder>")
                    if end >= 0:
                        chunk = chunk[end + len("</system-reminder>"):].strip()
                txt += chunk
        out.append({
            "id": o.get("id") or o.get("messageId"),
            "role": o.get("role"),
            "type": "message",
            "text": txt,
            "time": _iso(o.get("timestamp")),
            "status": o.get("status"),
        })
    return out[-n:]


def open_session(sid):
    uri = "workbuddy://chat/%s" % sid
    if IS_WIN:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Start-Process -LiteralPath '%s'" % uri.replace("'", "''")],
            capture_output=True, timeout=20,
        )
        return r.returncode == 0, uri
    r = subprocess.run(["open", uri], capture_output=True, timeout=20)
    return r.returncode == 0, uri


def _read_process_env(pid):
    """Return env dict. Caller must not log values of secret keys."""
    if IS_WIN:
        return _read_env_windows(pid)
    return _read_env_mac(pid)


def _read_env_windows(pid):
    import ctypes
    import ctypes.wintypes as w
    PROCESS_QUERY_INFORMATION, PROCESS_VM_READ = 0x0400, 0x0010
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    OpenProcess = kernel32.OpenProcess
    OpenProcess.restype = w.HANDLE
    OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    ReadProcessMemory = kernel32.ReadProcessMemory
    ReadProcessMemory.restype = w.BOOL
    ReadProcessMemory.argtypes = [w.HANDLE, w.LPCVOID, w.LPVOID, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

    class PBI(ctypes.Structure):
        _fields_ = [("R1", ctypes.c_void_p), ("Peb", ctypes.c_void_p),
                    ("R2", ctypes.c_void_p * 2), ("Pid", ctypes.c_void_p), ("R3", ctypes.c_void_p)]

    h = OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, int(pid))
    if not h:
        raise OSError("OpenProcess failed")
    try:
        pbi = PBI()
        retlen = ctypes.c_ulong()
        st = ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(retlen))
        if st != 0:
            raise OSError("NtQuery failed")

        def read(addr, n):
            buf = (ctypes.c_char * n)()
            got = ctypes.c_size_t()
            if not ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(got)):
                raise OSError("ReadProcessMemory failed")
            return bytes(buf[:got.value])

        def read_ptr(addr):
            return int.from_bytes(read(addr, 8), "little")

        params = read_ptr(pbi.Peb + 0x20)
        env_ptr = read_ptr(params + 0x80)
        text = read(env_ptr, 65536).decode("utf-16le", "replace")
        env = {}
        for item in text.split("\x00"):
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
        return env
    finally:
        kernel32.CloseHandle(h)


def _read_env_mac(pid):
    """KERN_PROCARGS2. Never print the result."""
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library("c") or "/usr/lib/libc.dylib", use_errno=True)
    CTL_KERN, KERN_PROCARGS2 = 1, 49
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
    size = ctypes.c_size_t(0)
    sysctl = libc.sysctl
    if sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
        raise OSError("sysctl size failed")
    buf = ctypes.create_string_buffer(size.value)
    if sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        raise OSError("sysctl read failed")
    data = buf.raw[:size.value]
    # skip argc (4 bytes) then exe path and args until env
    # format: int argc; exe\0; extra \0s; arg0\0...argN\0; env0=val\0...
    if len(data) < 8:
        return {}
    # find first env-like KEY=VAL after the executable
    text = data[4:]
    # split on NUL
    parts = text.split(b"\x00")
    env = {}
    seen_args = True
    for p in parts:
        if not p:
            continue
        if b"=" in p and seen_args:
            # once we start seeing KEY=VAL, treat as env
            try:
                s = p.decode("utf-8", "replace")
            except Exception:
                continue
            k, v = s.split("=", 1)
            if k:
                env[k] = v
    return env


def _api(method, url, password, body=None, timeout=15):
    data = None
    headers = {"Authorization": "Bearer " + password, "X-CodeBuddy-Request": "1"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if e.fp else b""
    except Exception as e:
        return None, {"error": type(e).__name__}
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return status, {"nonjson": True}
    return status, obj


def _live_id(body):
    if not isinstance(body, dict):
        return None, None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    return data.get("sessionId"), data.get("writerOccupied")


def send(sid, text, confirm=False):
    """Official HTTP send. Returns a dict with ok/phase/error; never includes secrets."""
    if not confirm:
        return {"ok": False, "error": "缺少确认"}
    if ALLOW_SEND is not None and sid not in ALLOW_SEND:
        return {"ok": False, "error": "这个对话不在允许自动发送的名单里"}
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "没有内容"}
    lives = [s for s in live_serves() if s["pidAlive"] and s["sessionId"] == sid]
    if not lives:
        return {"ok": False, "error": "这个对话的后台服务还没起来，没法自动发送。先在 WorkBuddy 里打开它并发过一条，或等服务起来后再试。"}
    port, pid = lives[0]["servePort"], lives[0]["pid"]
    if not port:
        return {"ok": False, "error": "找不到这个对话的本地端口"}
    try:
        env = _read_process_env(pid)
    except Exception:
        return {"ok": False, "error": "读不到本地访问密码（进程环境）"}
    password = env.get("CODEBUDDY_GATEWAY_PASSWORD")
    env = None
    if not password:
        return {"ok": False, "error": "进程环境里没有本地访问密码"}
    base = "http://127.0.0.1:%s" % port
    deadline_idle = time.time() + 30
    live_sid, occupied, st = None, True, None
    while time.time() < deadline_idle:
        st, body = _api("GET", base + "/api/v1/sessions/live", password)
        live_sid, occupied = _live_id(body)
        if st == 200 and live_sid == sid and not occupied:
            break
        if st == 200 and live_sid and live_sid != sid:
            password = None
            return {"ok": False, "error": "当前活跃对话对不上，没有发送"}
        time.sleep(1.2)
    if st != 200 or live_sid != sid:
        password = None
        return {"ok": False, "error": "当前活跃对话对不上，没有发送"}
    if occupied:
        password = None
        return {"ok": False, "error": "对方还在跑，先等这一轮结束"}
    t0 = time.time()
    st, body = _api("POST", base + "/api/v1/sessions/%s/reply" % sid, password, body={"text": text})
    password = None
    delivered = False
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        delivered = bool(data.get("delivered"))
    if st != 200 or not delivered:
        return {"ok": False, "error": "接口没有确认投递", "http": st, "elapsed": round(time.time() - t0, 2)}
    # poll jsonl
    phase = "delivered"
    tp = transcript_path(sid)
    deadline = time.time() + 180
    while time.time() < deadline:
        items = tail(sid, 8)
        users = [i for i in items if i.get("role") == "user" and text[:20] in (i.get("text") or "")]
        if users:
            after = [i for i in items if i.get("role") == "assistant" and (i.get("time") or "") >= (users[-1].get("time") or "")]
            if after and after[-1].get("status") == "completed":
                return {"ok": True, "phase": "replied", "elapsed": round(time.time() - t0, 1), "http": st}
            phase = "running"
        time.sleep(1.5)
    return {"ok": True, "phase": phase, "elapsed": round(time.time() - t0, 1), "http": st, "note": "已投递，但还没等到完整回复"}
