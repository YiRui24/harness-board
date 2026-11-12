#!/usr/bin/env python3
"""Harness 总控板 · 本机 127.0.0.1 + Tailscale IP，同伴之间用本地口令互调。
口令只存文件，不打印。本轮发送白名单仅 WorkBuddy 测试对话。
"""
from __future__ import annotations

import argparse, hashlib, json, os, queue, re, secrets, shutil, subprocess, sys, threading, time, uuid, urllib.error, urllib.request
import normalize as N
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote, unquote

if os.name == "nt":
    # 服务用 pythonw 跑时没有控制台，子进程（powershell/tailscale/ipconfig）默认会各弹一个黑窗口。
    # 这里统一给所有子进程加 CREATE_NO_WINDOW，适配器里的调用也一并覆盖。
    _Popen_init = subprocess.Popen.__init__

    def _popen_no_window(self, *a, **kw):
        kw["creationflags"] = (kw.get("creationflags") or 0) | 0x08000000
        return _Popen_init(self, *a, **kw)

    subprocess.Popen.__init__ = _popen_no_window

HERE = os.path.dirname(os.path.abspath(__file__))
# pythonw 下没有 stdout/stderr（是 None），写日志会让每个请求中途断开；改写到本地日志文件
for _n, _f in (("stdout", "server.out.log"), ("stderr", "server.err.log")):
    if getattr(sys, _n) is None:
        try:
            setattr(sys, _n, open(os.path.join(HERE, _f), "a", encoding="utf-8", buffering=1))
        except Exception:
            setattr(sys, _n, open(os.devnull, "w"))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    import codex_adapter as A
    HAS_CODEX = True
except Exception:
    A = None
    HAS_CODEX = False

try:
    import workbuddy_adapter as WB
except Exception:
    WB = None
try:
    import qwen_adapter as QW
except Exception:
    QW = None

LOG = os.path.join(HERE, "send.log")
STATE_ZH = {
    "idle": "空闲", "running": "对方在跑", "waiting_human": "等你",
    "pending": "待发", "delivered": "已投递", "replied": "已回复",
    "failed": "失败", "blocked": "已拦住", "waiting_enter": "等你按回车",
    "sending": "发送中", "waiting_paste": "已备好，等你粘贴回车",
}
V14_MARK = ("V1.4", "v1.4")
MAX_UPLOAD = 200 * 1024 * 1024
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_bus, _bus_lock = {}, threading.Lock()
_sending = {}
TOKEN = ""
DEVICE_ID = os.environ.get("HARNESS_DEVICE_ID", "")
DEVICE_LABEL = os.environ.get("HARNESS_DEVICE_LABEL", "")
PEERS = []
LISTEN_HOSTS = ["127.0.0.1"]
PORT = 8765


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def token_path():
    return os.environ.get("HARNESS_BOARD_TOKEN_FILE") or os.path.join(ROOT, "board-token.txt")


def load_or_create_token():
    p = token_path()
    if os.path.exists(p):
        t = open(p, encoding="utf-8").read().strip()
        if t:
            return t
    t = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(p, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(t + "\n")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return t


def token_fp(t):
    return hashlib.sha256(t.encode()).hexdigest()[:8]


OWNER_NAME = ""


def _config_paths():
    # 根目录的 config.json（推荐）或旧的 peers.json
    return [
        os.path.join(ROOT, "config.json"),
        os.path.join(os.path.dirname(ROOT), "config.json") if os.path.basename(ROOT) == "board" else "",
        os.path.join(ROOT, "peers.json"),
    ]


def load_config():
    for p in _config_paths():
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return json.load(f) or {}
            except Exception:
                return {}
    return {}


def ensure_config():
    """第一次运行：若还没有 config.json，从旁边的 config.example.json 拷一份并填本机名。"""
    paths = [p for p in _config_paths() if p.endswith("config.json")]
    if any(os.path.exists(p) for p in paths):
        return
    root = ROOT if os.path.basename(ROOT) != "board" else os.path.dirname(ROOT)
    dest = os.path.join(root, "config.json")
    example = os.path.join(root, "config.example.json")
    data = {}
    if os.path.exists(example):
        try:
            data = json.load(open(example, encoding="utf-8")) or {}
        except Exception:
            data = {}
    host = (os.environ.get("HARNESS_DEVICE_ID") or os.environ.get("COMPUTERNAME") or os.uname().nodename).strip()
    data["self"] = data.get("self") or re.sub(r"[^A-Za-z0-9._-]+", "-", host).strip("-") or "local"
    data["label"] = os.environ.get("HARNESS_DEVICE_LABEL") or data.get("label") or data["self"]
    data["port"] = data.get("port") or 8765
    data["peers"] = data.get("peers") or []
    # 示例里的占位同伴不要写进真实配置
    data["peers"] = [p for p in data["peers"] if isinstance(p, dict) and not str(p.get("host") or "").startswith("<")]
    try:
        with open(dest, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        try:
            os.chmod(dest, 0o600)
        except Exception:
            pass
    except Exception:
        pass


def load_peers():
    global DEVICE_ID, DEVICE_LABEL, PEERS, OWNER_NAME
    ensure_config()
    cfg = load_config()
    DEVICE_ID = os.environ.get("HARNESS_DEVICE_ID") or cfg.get("self") or "local"
    DEVICE_LABEL = os.environ.get("HARNESS_DEVICE_LABEL") or cfg.get("label") or DEVICE_ID
    OWNER_NAME = cfg.get("ownerName") or cfg.get("owner") or ""
    PEERS = list(cfg.get("peers") or [])


def _is_cgnat(ip):
    try:
        a, b, *_ = ip.split(".")
        return int(a) == 100 and 64 <= int(b) <= 127
    except Exception:
        return False


def ts_ip():
    env = os.environ.get("HARNESS_TS_IP")
    if env and _is_cgnat(env.strip()):
        return env.strip()
    for cmd in (
        ["tailscale", "ip", "-4"],
        [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"],
        ["/usr/local/bin/tailscale", "ip", "-4"],
        ["/Applications/Tailscale.app/Contents/MacOS/tailscale", "ip", "-4"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=8)
            lines = (r.stdout or b"").decode().strip().splitlines()
            ip = lines[0].strip() if r.returncode == 0 and lines else ""
            if _is_cgnat(ip):
                return ip
        except Exception:
            continue
    # LaunchAgent 里 tailscale 可能不在 PATH，从网卡兜底找 CGNAT 地址
    for cmd in (
        ["ifconfig"],
        ["ipconfig"],
        ["/sbin/ifconfig"],
        ["ip", "-4", "addr"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=8)
            text = (r.stdout or b"").decode("utf-8", "replace")
        except Exception:
            continue
        import re
        for m in re.finditer(r"\b(100\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", text):
            if _is_cgnat(m.group(1)):
                return m.group(1)
    return None


def handover_root():
    """交接盘根目录：优先配置里的 handoverRoot，否则常见默认路径，再否则用本目录下的「交接盘」。"""
    cfg_root = (load_config() or {}).get("handoverRoot") or os.environ.get("HARNESS_HANDOVER")
    if cfg_root:
        p = os.path.expanduser(cfg_root)
        os.makedirs(p, exist_ok=True)
        return p
    for c in (os.path.expanduser("~/交接盘"), os.path.expanduser("~/Handover"),
              r"D:\交接盘", r"C:\交接盘"):
        if os.path.isdir(c):
            return c
    p = os.path.join(ROOT, "交接盘")
    os.makedirs(p, exist_ok=True)
    return p


def handover_dir():
    p = os.path.join(handover_root(), "harness-board")
    os.makedirs(p, exist_ok=True)
    return p


def state_path():
    return os.path.join(handover_dir(), "state.json")


def _empty_state():
    return {
        "bindings": {
            "codexDev": {"device": "", "app": "codex", "id": "", "title": ""},
            "qwenPublish": {"device": "", "app": "qwen", "chatId": "", "subChatId": "", "id": "", "title": ""},
        },
        "handoff": {
            "prompt": "",
            "steps": {
                "prompt_ready": {"status": "empty"},
                "sent_codex": {"status": "pending"},
                "authorized": {"status": "pending"},
                "published_qwen": {"status": "pending", "once": False},
                "codex_review": {"status": "pending"},
            },
        },
    }


_state_lock = threading.Lock()


def read_state():
    p = state_path()
    if not os.path.exists(p):
        return _empty_state()
    try:
        d = json.load(open(p, encoding="utf-8"))
        base = _empty_state()
        base.update({k: d.get(k, base[k]) for k in base})
        if isinstance(d.get("bindings"), dict):
            base["bindings"].update(d["bindings"])
        if isinstance(d.get("handoff"), dict):
            h = base["handoff"]
            h["prompt"] = d["handoff"].get("prompt", "")
            h["steps"].update(d["handoff"].get("steps") or {})
            for k in ("runId", "templateId", "templateName", "startedAt", "files"):
                if d["handoff"].get(k) is not None:
                    h[k] = d["handoff"][k]
        return base
    except Exception:
        return _empty_state()


def write_state(st):
    p = state_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def publish(key, ev):
    with _bus_lock:
        subs = list(_bus.get(key, []))
    for q in subs:
        q.put(ev)


def log_send(rec):
    rec = dict(rec)
    rec["at"] = now_iso()
    # never persist secrets if they slipped in
    for k in list(rec):
        lk = str(k).lower()
        if any(s in lk for s in ("password", "token", "secret", "bearer", "authorization")):
            rec[k] = "[redacted]"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def is_v14(title, tid=None):
    t = title or ""
    return any(m in t for m in V14_MARK)


# ---------------- local adapters ----------------
def codex_status():
    if not HAS_CODEX:
        return {"app": "codex", "appLabel": "Codex", "installed": False, "running": False, "daemon": False}
    try:
        ok, info = A.daemon_alive()
    except Exception:
        ok, info = False, "读不到守护进程"
    return {
        "app": "codex", "appLabel": "Codex", "installed": True,
        "running": bool(ok), "daemon": bool(ok),
        "info": ("已加载 %s 个对话" % info) if ok else str(info),
    }


def list_codex(limit=200):
    if not HAS_CODEX or not os.path.exists(getattr(A, "STATE_DB", "")) or not os.path.exists(getattr(A, "HIST_DB", "")):
        return []
    s, h = A.ro(A.STATE_DB), A.ro(A.HIST_DB)
    try:
        rows = s.execute(
            "select id,name,title,is_pinned,updated_at_ms,recency_at_ms from threads "
            "where archived=0 and name is not null and name!='' "
            "order by coalesce(recency_at_ms,updated_at_ms) desc limit ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            title = r["name"] or r["title"]
            st = A.guess_state(h, r["id"])
            if r["id"] in _sending:
                st = "running"
            out.append({
                "id": r["id"], "title": title, "pinned": bool(r["is_pinned"]),
                "updated": A.ts(r["recency_at_ms"] or r["updated_at_ms"]),
                "state": st, "state_zh": STATE_ZH.get(st, st),
                "runState": st, "isCurrent": False, "serveLive": bool(st == "running"),
                "protected": is_v14(title, r["id"]),
                "app": "codex",
                "openLink": "codex://threads/%s" % r["id"],
            })
        return out
    finally:
        s.close(); h.close()


def slim_codex(it):
    keep = ("id", "ordinal", "role", "type", "time", "status", "text", "questions",
            "command", "cwd", "changes", "exit_code", "aggregated_output",
            "server", "tool", "error")
    return {k: it[k] for k in keep if k in it and it[k] not in (None, "", [], {})}


def items_codex(tid, n=40, after=None, title=""):
    if is_v14(title, tid):
        return [], "idle", "这个对话受保护，不展示内容"
    if not HAS_CODEX or not os.path.exists(getattr(A, "HIST_DB", "")):
        return [], "idle", "本机没有 Codex"
    h = A.ro(A.HIST_DB)
    try:
        its = [slim_codex(i) for i in A.items(h, tid, n, after=after)]
        return its, A.guess_state(h, tid), None
    finally:
        h.close()


def app_status(app):
    if app == "codex":
        return codex_status()
    if app == "workbuddy":
        return WB.status() if WB else {"app": "workbuddy", "appLabel": "WorkBuddy", "installed": False, "running": False}
    if app == "qwen":
        return QW.status() if QW else {"app": "qwen", "appLabel": "千问办公", "installed": False, "running": False}
    return {"error": "unknown app"}


def list_local(app, limit=80):
    if app == "codex":
        return list_codex(limit)
    if app == "workbuddy":
        return WB.list_sessions(limit) if WB else []
    if app == "qwen":
        return QW.list_sessions(limit) if QW else []
    return []


def items_local(app, sid, n=40, title="", extra=None):
    extra = extra or {}
    if app == "codex":
        return items_codex(sid, n, title=title)
    if app == "workbuddy":
        return (WB.tail(sid, n) if WB else []), None, None
    if app == "qwen":
        return (QW.tail(sid, n) if QW else []), None, None
    return [], None, "unknown app"


def list_queue_local(app, sid):
    if app == "codex" and A and hasattr(A, "list_queued"):
        try:
            return A.list_queued(sid) or []
        except Exception:
            return []
    return []


def pack_items(app, sid, n=40, title="", extra=None, device=None):
    its, st, err = items_local(app, sid, n, title=title, extra=extra)
    queue = list_queue_local(app, sid)
    items = N.normalize_list(app, its, queue=queue)
    pending = N.pending_from_items(items, app, device=device, sid=sid, title=title)
    return items, st, err, pending, queue



def handle_act(body):
    """卡片动作：作答 / 批准 / 取消排队。Mock 可覆盖本函数。"""
    app = body.get("app") or "codex"
    sid = body.get("id") or body.get("thread") or body.get("subChatId")
    action = body.get("action") or ""
    if not sid:
        return {"ok": False, "error": "缺少对话"}
    if action == "answer":
        text = (body.get("text") or body.get("option") or "").strip()
        if not text:
            return {"ok": False, "error": "没有作答内容"}
        rec = start_send(dict(body, message=text, files=[], confirm=True, id=sid, app=app))
        return {"ok": True, "via": "send", "send": public_send(rec)}
    if action in ("approve", "deny", "cancel"):
        # 官方 Codex 审批走 item/commandExecution/requestApproval 等未决 RPC。
        # 总控板若没挂上这条请求，不能代签，请去原窗口。
        if app == "codex" and A and hasattr(A, "try_answer_server_request"):
            decision = {"approve": "Accept", "deny": "Decline", "cancel": "Cancel"}[action]
            r = A.try_answer_server_request(body.get("method") or "", body.get("params") or {}, decision=decision)
            if isinstance(r, dict):
                return r
        return {"ok": False, "fallback": "open", "error": "请到原窗口批准或拒绝，总控板现在不能代签"}
    if action == "queue_cancel":
        qid = body.get("queuedId") or body.get("itemId")
        if app == "codex" and A and hasattr(A, "queue_delete"):
            return A.queue_delete(sid, qid)
        return {"ok": False, "fallback": "open", "error": "这个来源还不支持在总控板取消排队，请到原窗口处理"}
    return {"ok": False, "error": "未知动作"}


def local_overview():
    apps, lists = {}, {}
    for app in ("codex", "workbuddy", "qwen"):
        try:
            st = app_status(app)
            try:
                lists[app] = list_local(app, 400) or []
            except Exception:
                lists[app] = []
            st["sessionCount"] = len(lists[app])
            apps[app] = st
        except Exception as e:
            apps[app] = {"app": app, "installed": False, "running": False, "error": type(e).__name__}
    try:
        act = local_activity(lists)
    except Exception:
        act = {"recent": [], "pending": []}
    return {
        "id": DEVICE_ID, "label": DEVICE_LABEL, "ownerName": OWNER_NAME, "self": True, "online": True,
        "listen": ["http://%s:%s/" % (h, PORT) for h in LISTEN_HOSTS],
        "apps": apps, "version": 5, "now": now_iso(), "fetchedAt": now_iso(), "stale": False,
        "recent": act["recent"], "pending": act["pending"],
    }


# ---------------- peers ----------------
def find_peer(device):
    if device == DEVICE_ID or device in ("self", "local", ""):
        return None
    for p in PEERS:
        if p.get("id") == device:
            return p
    return None


def peer_request(peer, method, path, body=None, timeout=25):
    url = "http://%s:%s%s" % (peer["host"], peer["port"], path)
    headers = {
        "Authorization": "Bearer " + TOKEN,
        "X-Harness-Node": "1",
        "X-Harness": "1",
        "Content-Type": "application/json",
    }
    data = None
    if body is not None and method != "GET":
        data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read() if e.fp else b"{}"
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            obj = {"error": "同伴返回 %s" % e.code}
        return e.code, obj
    except Exception as e:
        return 0, {"error": "同伴暂时连不上（%s）" % type(e).__name__, "peer": peer.get("id")}


_peer_snap, _peer_lists, _peer_items = {}, {}, {}
_peer_cache_lock = threading.Lock()


def _cache_put(store, key, body):
    with _peer_cache_lock:
        store[key] = {"at": now_iso(), "t": time.time(), "body": body}


def _cache_get(store, key, max_age=86400):
    with _peer_cache_lock:
        rec = store.get(key)
    if not rec or (time.time() - rec["t"] > max_age):
        return None
    return rec


def _copy(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


def _stale_wrap(rec, err, extra=None):
    body = _copy(rec["body"])
    body.update(stale=True, error=err, lastOkAt=rec["at"], fetchedAt=rec["at"])
    if extra:
        body.update(extra)
    return body


def peer_overview(peer):
    st, body = peer_request(peer, "GET", "/api/local/overview", timeout=8)
    pid = peer.get("id")
    if st == 200 and isinstance(body, dict):
        body["self"] = False
        body["online"] = True
        body["stale"] = False
        body["id"] = pid
        body["label"] = body.get("label") or peer.get("label")
        body["fetchedAt"] = now_iso()
        body["lastOkAt"] = body["fetchedAt"]
        _cache_put(_peer_snap, pid, body)
        return body
    err = (body or {}).get("error") or "离线"
    rec = _cache_get(_peer_snap, pid)
    if rec:
        cached = _stale_wrap(rec, err, {"online": False, "self": False})
        return cached
    return {
        "id": pid, "label": peer.get("label") or peer.get("id"),
        "self": False, "online": False, "stale": False,
        "error": err, "apps": {}, "recent": [], "pending": [],
    }


# ---------------- send / open ----------------
def focus_codex():
    if not HAS_CODEX:
        return None
    if A.IS_WIN:
        cmds = (["explorer.exe", r"shell:AppsFolder\OpenAI.Codex_2p2nqsd0c76g0!App"], [A.CODEX_BIN, "app"])
    else:
        cmds = (["open", "-a", "ChatGPT"], ["open", "-a", "Codex"], [A.CODEX_BIN, "app"])
    for cmd in cmds:
        try:
            rc = subprocess.run(cmd, capture_output=True, timeout=15).returncode
            if rc == 0 or (A.IS_WIN and cmd[0] == "explorer.exe"):
                return " ".join(cmd[:3])
        except Exception:
            continue
    return None


def do_open(app, sid, body):
    if app == "workbuddy":
        if not WB:
            return {"ok": False, "error": "本机没有 WorkBuddy 适配器"}
        ok, uri = WB.open_session(sid)
        return {"ok": ok, "uri": uri}
    if app == "qwen":
        if not QW:
            return {"ok": False, "error": "本机没有千问办公适配器"}
        chat = body.get("chatId") or sid
        sub = body.get("subChatId") or sid
        ok, uri = QW.open_session(chat, sub)
        return {"ok": ok, "uri": uri}
    if app == "codex":
        if is_v14(body.get("title") or "", sid):
            return {"ok": False, "error": "这个对话受保护，不从总控板打开"}
        if sid:
            uri = "codex://threads/%s" % sid
            if os.name == "nt":
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", "Start-Process '%s'" % uri.replace("'", "''")],
                    capture_output=True, timeout=20, creationflags=NO_WINDOW,
                )
                return {"ok": r.returncode == 0, "uri": uri}
            r = subprocess.run(["open", uri], capture_output=True, timeout=20)
            return {"ok": r.returncode == 0, "uri": uri}
        how = focus_codex()
        return {"ok": bool(how), "how": how, "note": "没有指定对话时，只能把 Codex 唤到前台"}
    return {"ok": False, "error": "未知工具"}


# ---------------- 聊天式发送：能自动就自动，不能就半自动（复制好 + 打开对话） ----------------
APP_ZH = {"codex": "Codex", "workbuddy": "WorkBuddy", "qwen": "千问办公"}
PHASE_RANK = {"sending": 0, "waiting_paste": 1, "delivered": 2, "replied": 3}
SENDS = {}
_sends_lock = threading.Lock()


def codex_title(sid):
    if not HAS_CODEX or not os.path.exists(getattr(A, "STATE_DB", "")):
        return ""
    try:
        s = A.ro(A.STATE_DB)
        try:
            r = s.execute("select name,title from threads where id=?", (sid,)).fetchone()
            return (r["name"] or r["title"] or "") if r else ""
        finally:
            s.close()
    except Exception:
        return ""


def send_mode(app, sid, title=""):
    """返回 (mode, 一句话说明)。mode: auto / semi / blocked"""
    if app == "codex":
        if is_v14(title, sid) or is_v14(codex_title(sid), sid):
            return "blocked", "这个对话受保护，不从总控板发"
        if not HAS_CODEX:
            return "blocked", "这台电脑没有 Codex"
        try:
            ok, _ = A.daemon_alive()
        except Exception:
            ok = False
        if ok:
            return "auto", "会直接自动发送"
        return "semi", "Codex 后台服务不在线：会帮你复制好并打开对话，你粘贴回车"
    if app == "workbuddy":
        if not WB:
            return "blocked", "这台电脑没有 WorkBuddy"
        try:
            running = bool(WB._procs())
            live = [x for x in WB.live_serves() if x["pidAlive"] and x["sessionId"] == sid]
        except Exception:
            running, live = False, []
        if running and live:
            return "auto", "会直接自动发送"
        return "semi", "这个对话的自动接口没开：会帮你复制好并打开对话，你粘贴回车"
    if app == "qwen":
        if not QW:
            return "blocked", "这台电脑没有千问办公"
        return "semi", "千问办公要你自己按回车：会帮你复制好并打开对话"
    return "blocked", "未知工具"


def with_files(text, files):
    text = (text or "").strip()
    if files:
        text += ("\n\n" if text else "") + "附件（已放在这台电脑的交接盘）：\n" + "\n".join("- " + f for f in files)
    return text


def set_clipboard(text):
    if os.name == "nt":
        ps = ("[Console]::InputEncoding=[Text.Encoding]::UTF8; $t=[Console]::In.ReadToEnd(); "
              "Set-Clipboard -Value $t; Start-Sleep -Milliseconds 150; $c=Get-Clipboard -Raw; "
              "if((($c -replace '\\r','').TrimEnd()) -eq (($t -replace '\\r','').TrimEnd())){'OK'}")
        for _ in range(6):
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], input=text.encode("utf-8"),
                                   capture_output=True, timeout=20, creationflags=NO_WINDOW)
                if b"OK" in (r.stdout or b""):
                    return True
            except Exception:
                pass
            time.sleep(0.4)
        return False
    env = dict(os.environ, LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8")
    try:
        r = subprocess.run(["pbcopy"], input=text.encode("utf-8"), capture_output=True, timeout=10, env=env)
        return r.returncode == 0
    except Exception:
        return False


def _norm(t):
    return re.sub(r"\s+", "", t or "")


def _send_key(text):
    return _norm(text)[:36]


def _match_users(items, key):
    return sum(1 for it in items if it.get("role") == "user" and key and key in _norm(it.get("text")))


def _items_for(rec, n=60):
    try:
        its, st, _ = items_local(rec["app"], rec["sid"], n, title=rec.get("title") or "")
        return its or [], st
    except Exception:
        return [], None


def public_send(r):
    return {k: r.get(k) for k in ("id", "device", "app", "sid", "title", "text", "files", "mode", "phase",
                                  "note", "error", "at", "updated", "step")}


def upd_send(rec, **kw):
    ph = kw.get("phase")
    if ph in PHASE_RANK and rec.get("phase") in PHASE_RANK and PHASE_RANK[ph] < PHASE_RANK[rec["phase"]]:
        kw.pop("phase")  # 状态只往前走
    if ph in ("failed", "blocked") and rec.get("phase") in ("delivered", "replied"):
        kw.pop("phase", None)
    rec.update(kw)
    rec["updated"] = now_iso()
    publish("%s:%s:%s" % (DEVICE_ID, rec["app"], rec["sid"]), {"kind": "sendrec", "send": public_send(rec)})


def watch_delivery(rec, key, base, limit_s=1800):
    """通过本地库检测真正发出（新出现一条匹配的“我”的消息）和回复完成。"""
    t0 = time.time()
    seen_at = None
    while time.time() - t0 < limit_s:
        if rec.get("phase") in ("replied", "failed", "blocked"):
            return
        time.sleep(2)
        its, st = _items_for(rec)
        users = [i for i, it in enumerate(its) if it.get("role") == "user" and key and key in _norm(it.get("text"))]
        if len(users) > base or (seen_at is not None and users):
            if seen_at is None:
                seen_at = time.time()
                upd_send(rec, phase="delivered", note="")
            last_u = users[-1]
            after = [it for it in its[last_u + 1:] if it.get("role") == "assistant" and (it.get("text") or "").strip()]
            if after:
                done = False
                if rec["app"] == "codex":
                    done = st not in ("running",)
                else:
                    done = after[-1].get("status") in (None, "", "completed") and not (rec["app"] == "workbuddy" and its and its[-1].get("role") == "tool")
                if done:
                    upd_send(rec, phase="replied")
                    return
    if rec.get("phase") == "waiting_paste":
        upd_send(rec, note="半小时内没检测到发出；如果已经发了，刷新一下即可")


def go_semi(rec, why=""):
    full = with_files(rec["text"], rec.get("files"))
    clip = set_clipboard(full)
    opened = do_open(rec["app"], rec["sid"], {"title": rec.get("title"), "chatId": rec.get("chatId"),
                                              "subChatId": rec.get("subChatId") or rec["sid"]})
    app = APP_ZH.get(rec["app"], rec["app"])
    if clip:
        note = "内容已复制好" + ("，对话也打开了" if opened.get("ok") else "，请自己切到这个对话") + "（Ctrl+V / ⌘V 粘贴）"
    else:
        note = "剪贴板没写进去，请手动复制这条内容，到 %s 里粘贴回车" % app
    if why:
        note = why + "；" + note
    upd_send(rec, mode="semi", phase="waiting_paste", note=note)


def codex_auto(rec):
    """经 Codex app-server 自动发送。连不上/入队失败 → 返回 False（交给半自动）；入队后出错 → 标失败，不重复发。"""
    try:
        srv = A.AppServer(name="harness-board")
    except Exception:
        return False
    try:
        try:
            was_loaded = rec["sid"] in srv.loaded()
            r = srv.request("thread/queue/add", {"threadId": rec["sid"], "clientUserMessageId": str(uuid.uuid4()),
                                                 "input": A.build_input(rec["text"], rec.get("files") or [])})
        except Exception:
            return False
        sub_id = ((r or {}).get("queuedSubmission") or {}).get("id")
        try:
            if was_loaded:
                srv.request("thread/queue/start", {"threadId": rec["sid"], "queuedSubmissionId": sub_id})
            else:
                srv.request("thread/resume", {"threadId": rec["sid"], "excludeTurns": True}, timeout=120)
        except Exception as e:
            upd_send(rec, phase="failed", error="已排进 Codex 队列，但没能唤醒（%s），请到 Codex 里看一下" % type(e).__name__)
            return True
        key = "%s:codex:%s" % (DEVICE_ID, rec["sid"])
        t0 = time.time()
        while time.time() - t0 < 1800:
            try:
                m = srv.events.get(timeout=1)
            except queue.Empty:
                if rec.get("phase") == "replied":
                    break
                continue
            if (m.get("params") or {}).get("threadId") not in (None, rec["sid"]):
                continue
            b = A.ev_brief(m)
            if b["kind"] == "delta":
                publish(key, b)
            elif b["kind"] == "started" and b.get("type") == "userMessage":
                upd_send(rec, phase="delivered")
            if m.get("method") == "turn/completed":
                upd_send(rec, phase="replied")
                break
            if m.get("method") == "harness/disconnected":
                break
        return True
    finally:
        srv.close()


def run_send(rec):
    key = _send_key(rec["text"])
    its, _ = _items_for(rec)
    base = _match_users(its, key)
    mode, why = send_mode(rec["app"], rec["sid"], rec.get("title") or "")
    if mode == "blocked":
        upd_send(rec, mode="blocked", phase="blocked", error=why)
        log_send({"event": "chat_blocked", "app": rec["app"], "thread": rec["sid"], "reason": why})
        return
    threading.Thread(target=watch_delivery, args=(rec, key, base), daemon=True).start()
    log_send({"event": "chat_send", "app": rec["app"], "thread": rec["sid"], "mode": mode, "len": len(rec["text"]),
              "files": len(rec.get("files") or [])})
    if mode == "auto":
        upd_send(rec, mode="auto")
        if rec["app"] == "codex":
            if codex_auto(rec):
                return
            return go_semi(rec, "自动发送没接上")
        if rec["app"] == "workbuddy":
            res = WB.send(rec["sid"], with_files(rec["text"], rec.get("files")), confirm=True)
            if res.get("ok"):
                ph = res.get("phase")
                upd_send(rec, phase="replied" if ph == "replied" else "delivered")
                return
            if res.get("http"):  # 接口已经收过请求，不再重复投递
                upd_send(rec, phase="failed", error=res.get("error") or "接口没有确认投递")
                return
            return go_semi(rec, "自动发送没成功（%s）" % (res.get("error") or "接口不可用"))
    go_semi(rec)


def start_send(body):
    rec = {
        "id": uuid.uuid4().hex[:12], "device": DEVICE_ID, "app": body.get("app") or "codex",
        "sid": body.get("id") or body.get("thread"), "title": body.get("title") or "",
        "chatId": body.get("chatId"), "subChatId": body.get("subChatId"),
        "text": (body.get("message") or body.get("text") or "").strip(),
        "files": [f.strip() for f in body.get("files") or [] if isinstance(f, str) and f.strip()],
        "mode": None, "phase": "sending", "note": "", "error": None, "at": now_iso(), "updated": now_iso(),
        "step": body.get("step"),
    }
    with _sends_lock:
        SENDS[rec["id"]] = rec
        if len(SENDS) > 300:
            for k in list(SENDS)[:100]:
                SENDS.pop(k, None)

    def _run():
        try:
            run_send(rec)
        except Exception as e:
            upd_send(rec, phase="failed", error="出错了（%s）" % type(e).__name__)

    threading.Thread(target=_run, daemon=True).start()
    return rec


def list_sends(app, sid):
    with _sends_lock:
        return [public_send(r) for r in SENDS.values() if r["app"] == app and r["sid"] == sid][-30:]


# ---------------- 交接盘文件 ----------------
def safe_name(n):
    n = os.path.basename((n or "").replace("\\", "/")).strip().lstrip(".")
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", n)
    if len(n) > 150:
        b, e = os.path.splitext(n)
        n = b[:150 - len(e)] + e
    return n or "file"


def unique_path(d, name):
    p = os.path.join(d, name)
    b, e = os.path.splitext(name)
    i = 1
    while os.path.exists(p):
        p = os.path.join(d, "%s (%d)%s" % (b, i, e))
        i += 1
    return p


def resolve_rel(rel):
    root = os.path.normpath(handover_root())
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    p = os.path.normpath(os.path.join(root, *rel.split("/")))
    if not p.startswith(root + os.sep):
        return None
    return p


def file_info(rel):
    p = resolve_rel(rel)
    if not p:
        return {"exists": False, "error": "路径不对"}
    ok = os.path.isfile(p)
    return {"exists": ok, "size": os.path.getsize(p) if ok else 0, "path": p, "device": DEVICE_ID, "label": DEVICE_LABEL}


def save_upload(rfile, length, name):
    day = datetime.now().strftime("%Y-%m-%d")
    d = os.path.join(handover_root(), day)
    os.makedirs(d, exist_ok=True)
    tmpd = os.path.join(ROOT, "upload-tmp")  # 写完再挪进交接盘，避免 Syncthing 同步半截文件
    os.makedirs(tmpd, exist_ok=True)
    tmp = os.path.join(tmpd, uuid.uuid4().hex + ".part")
    got = 0
    try:
        with open(tmp, "wb") as f:
            while got < length:
                chunk = rfile.read(min(1 << 20, length - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got != length:
            raise IOError("上传中断")
        dst = unique_path(d, safe_name(name))
        try:
            os.replace(tmp, dst)
        except OSError:
            shutil.move(tmp, dst)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
    fn = os.path.basename(dst)
    return {"ok": True, "device": DEVICE_ID, "label": DEVICE_LABEL, "name": fn, "size": got,
            "rel": day + "/" + fn, "path": dst}


def peer_upload(peer, rfile, length, name):
    import http.client
    conn = http.client.HTTPConnection(peer["host"], peer["port"], timeout=120)
    try:
        conn.putrequest("POST", "/api/upload?device=%s" % quote(peer["id"]))
        conn.putheader("Authorization", "Bearer " + TOKEN)
        conn.putheader("X-Harness-Node", "1")
        conn.putheader("X-Harness", "1")
        conn.putheader("X-File-Name", quote(name))
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(length))
        conn.endheaders()
        left = length
        while left > 0:
            chunk = rfile.read(min(1 << 20, left))
            if not chunk:
                break
            conn.send(chunk)
            left -= len(chunk)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            return resp.status, json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return resp.status, {"error": "目标电脑返回 %s" % resp.status}
    except Exception as e:
        return 0, {"error": "传不到那台电脑（%s）" % type(e).__name__}
    finally:
        conn.close()


# ---------------- v5：总览动态（最近活跃 / 等你操作） ----------------
_act_cache = {"t": 0.0, "v": None}
_act_lock = threading.Lock()


def _ms(t):
    """各种时间字符串 → 毫秒（失败返回 0）。"""
    if not t:
        return 0
    if isinstance(t, (int, float)):
        return int(t if t > 1e11 else t * 1000)
    s = str(t).strip().replace("Z", "+00:00")
    for cand in (s, s.replace(" ", "T")):
        try:
            d = datetime.fromisoformat(cand)
            if d.tzinfo is None:
                d = d.astimezone()
            return int(d.timestamp() * 1000)
        except Exception:
            continue
    return 0


def local_activity(lists, top=8):
    """最近活跃的对话（带最后一条是谁说的）+ 正在等你处理的发送。结果缓存 8 秒。"""
    with _act_lock:
        if _act_cache["v"] is not None and time.time() - _act_cache["t"] < 8:
            return _act_cache["v"]
    rows = []
    for app, sess in (lists or {}).items():
        for t in sess or []:
            rows.append((app, t))
    rows.sort(key=lambda x: _ms(x[1].get("updated") or x[1].get("lastAt")), reverse=True)
    recent = []
    wait_pending = []
    for app, t in rows[:top]:
        r = {"app": app, "id": t.get("id"), "title": t.get("title") or "", "updated": t.get("updated") or t.get("lastAt"),
             "state": t.get("runState") or t.get("state") or "idle", "protected": bool(t.get("protected")),
             "chatId": t.get("chatId"), "subChatId": t.get("subChatId"), "lastRole": None, "lastAt": None, "preview": ""}
        if not r["protected"]:
            try:
                its, st, _, pend, _ = pack_items(app, r["id"], 8, title=r["title"], device=DEVICE_ID)
                msgs = [i for i in (its or []) if i.get("kind") in ("text", "question", "publish", "plan") and (i.get("text") or "").strip()]
                if msgs:
                    last = msgs[-1]
                    r["lastRole"], r["lastAt"] = last.get("role"), last.get("time")
                    txt = re.sub(r"[`*#>|_\-\[\]()]+", " ", last.get("text") or "")
                    r["preview"] = re.sub(r"\s+", " ", txt).strip()[:60]
                if st:
                    r["state"] = st
                if any(i.get("kind") == "question" and not (i.get("card") or {}).get("answered") for i in its or []):
                    r["state"] = "waiting_human"
                wait_pending.extend(pend)
            except Exception:
                pass
        recent.append(r)
    # 不在最近几条里、但状态是等你的对话，也进待办
    seen_wait = {(p.get("app"), p.get("sid") or p.get("id")) for p in wait_pending}
    for app, t in rows:
        stt = t.get("runState") or t.get("state")
        wk = t.get("waitKind")
        if stt == "waiting_human" or wk in ("question", "approval"):
            key = (app, t.get("id"))
            if key in seen_wait:
                continue
            wait_pending.append({
                "kind": wk or "question", "app": app, "sid": t.get("id"), "id": t.get("id"),
                "title": t.get("title") or "", "preview": t.get("waitPreview") or "",
                "updated": t.get("updated") or t.get("lastAt"),
            })
            seen_wait.add(key)
    pending = list(wait_pending)
    now = time.time()
    with _sends_lock:
        recs = list(SENDS.values())
    for s in recs:
        age = now - (_ms(s.get("updated")) / 1000.0 or now)
        if s.get("phase") == "waiting_paste" and age < 7200:
            pending.append(dict(public_send(s), kind="waiting_paste"))
        elif s.get("phase") in ("replied", "failed") and age < 1800:
            pending.append(dict(public_send(s), kind=s.get("phase")))
    for p in pending:
        p.pop("text", None)  # 总览只要标题和状态，不带正文
    v = {"recent": recent, "pending": pending[-20:]}
    with _act_lock:
        _act_cache.update(t=time.time(), v=v)
    return v


# ---------------- v5：本地数据（常用交接 / 每轮历史），存本机 board/data ----------------
_data_lock = threading.Lock()


def data_dir():
    p = os.environ.get("HARNESS_DATA_DIR") or os.path.join(HERE, "data")
    os.makedirs(p, exist_ok=True)
    return p


def _jload(name, default):
    p = os.path.join(data_dir(), name)
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _jsave(name, obj):
    p = os.path.join(data_dir(), name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _clip(s, n):
    return str(s or "")[:n]


def _bind_clean(b, kind):
    b = b if isinstance(b, dict) else {}
    keys = ("device", "app", "id", "title") + (("chatId", "subChatId") if kind == "qwen" else ())
    out = {k: _clip(b.get(k), 300) for k in keys}
    out["app"] = "qwen" if kind == "qwen" else "codex"
    return out


def list_templates():
    t = _jload("templates.json", [])
    return t if isinstance(t, list) else []


def save_template(body):
    name = _clip(body.get("name"), 60).strip()
    if not name:
        return {"ok": False, "error": "给这个常用交接起个名字"}
    cx = _bind_clean(body.get("codexDev"), "codex")
    qw = _bind_clean(body.get("qwenPublish"), "qwen")
    if not cx.get("id"):
        return {"ok": False, "error": "还没选开发用的 Codex 对话"}
    if is_v14(cx.get("title")):
        return {"ok": False, "error": "受保护的对话不能放进常用交接"}
    if not (qw.get("subChatId") or qw.get("id")):
        return {"ok": False, "error": "还没选千问办公的发布对话"}
    files = [_clip(f, 400) for f in (body.get("files") or []) if isinstance(f, str) and resolve_rel(f)][:20]
    prel = _clip(body.get("promptRel"), 400)
    if prel and not resolve_rel(prel):
        prel = ""
    rec = {"id": _clip(body.get("id"), 40) or uuid.uuid4().hex[:10], "name": name,
           "codexDev": cx, "qwenPublish": qw, "prompt": _clip(body.get("prompt"), 100000),
           "promptRel": prel, "files": files, "note": _clip(body.get("note"), 300), "updated": now_iso()}
    with _data_lock:
        ts = list_templates()
        k = next((i for i, t in enumerate(ts) if t.get("id") == rec["id"]), None)
        if k is None:
            rec["created"] = now_iso()
            ts.append(rec)
        else:
            rec["created"] = ts[k].get("created") or now_iso()
            rec["lastRun"] = ts[k].get("lastRun")
            ts[k] = rec
        _jsave("templates.json", ts[-50:])
    return {"ok": True, "template": rec, "templates": list_templates()}


def delete_template(tid):
    with _data_lock:
        ts = [t for t in list_templates() if t.get("id") != tid]
        _jsave("templates.json", ts)
    return {"ok": True, "templates": ts}


def list_runs(limit=30):
    r = _jload("runs.json", [])
    r = r if isinstance(r, list) else []
    return list(reversed(r[-limit:]))


STEP_KEYS = ("prompt_ready", "sent_codex", "authorized", "published_qwen", "codex_review")


def _run_steps(h):
    out = {}
    for k in STEP_KEYS:
        s = (h.get("steps") or {}).get(k) or {}
        out[k] = {"status": s.get("status"), "at": s.get("at")}
    return out


def _run_result(h):
    s = h.get("steps") or {}
    if (s.get("codex_review") or {}).get("status") in ("delivered", "replied"):
        return "done"
    if any((s.get(k) or {}).get("status") in ("failed", "blocked") for k in STEP_KEYS):
        return "problem"
    return "running"


def touch_run(st, create=True, end=None):
    """把当前这一轮的各步状态写进历史；还没有这一轮且有进展时自动开一轮。调用方已持有 _state_lock。"""
    h = st["handoff"]
    rid = h.get("runId")
    with _data_lock:
        runs = _jload("runs.json", [])
        runs = runs if isinstance(runs, list) else []
        k = next((i for i, r in enumerate(runs) if r.get("id") == rid), None) if rid else None
        if k is None:
            if not create:
                return
            rid = uuid.uuid4().hex[:10]
            h["runId"] = rid
            h.setdefault("startedAt", now_iso())
            runs.append({"id": rid, "templateId": h.get("templateId"), "name": h.get("templateName") or "手动交接",
                         "device": DEVICE_ID, "startedAt": h.get("startedAt") or now_iso(),
                         "codexTitle": (st["bindings"].get("codexDev") or {}).get("title", ""),
                         "qwenTitle": (st["bindings"].get("qwenPublish") or {}).get("title", "")})
            k = len(runs) - 1
        r = runs[k]
        r["steps"] = _run_steps(h)
        r["updated"] = now_iso()
        r["result"] = end or _run_result(h)
        if r["result"] in ("done", "reset") and not r.get("endedAt"):
            r["endedAt"] = now_iso()
        _jsave("runs.json", runs[-300:])


# ---------------- v5：交接盘浏览（选文件 / 提示词模板） ----------------
TEXT_EXT = (".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".log", ".prompt", ".markdown")
TPL_DIR = "提示词模板"
SKIP_DIRS = {"harness-board", ".stfolder", ".stversions", ".git", "node_modules", "__pycache__"}


def handover_files(q="", limit=300):
    root = handover_root()
    try:
        os.makedirs(os.path.join(root, TPL_DIR), exist_ok=True)
    except Exception:
        pass
    out = []
    q = (q or "").strip().lower()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")] if depth < 3 else []
        for fn in filenames:
            if fn.startswith(".") or fn.endswith((".tmp", ".part")) or fn.startswith("~$"):
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace(os.sep, "/")
            if q and q not in rel.lower():
                continue
            try:
                stt = os.stat(p)
            except OSError:
                continue
            out.append({"rel": rel, "name": fn, "folder": "" if rel_dir == "." else rel_dir.replace(os.sep, "/"),
                        "size": stt.st_size, "mtime": datetime.fromtimestamp(stt.st_mtime).astimezone().isoformat(timespec="seconds"),
                        "text": fn.lower().endswith(TEXT_EXT), "tpl": rel.startswith(TPL_DIR + "/"), "path": p})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"device": DEVICE_ID, "label": DEVICE_LABEL, "root": root, "tplDir": TPL_DIR, "files": out[:limit]}


def handover_text(rel):
    p = resolve_rel(rel)
    if not p or not os.path.isfile(p):
        return {"ok": False, "error": "交接盘里没有这个文件"}
    if not p.lower().endswith(TEXT_EXT):
        return {"ok": False, "error": "这个文件不是文字，只能当附件"}
    if os.path.getsize(p) > 256 * 1024:
        return {"ok": False, "error": "文件太大（超过 256KB），请当附件发"}
    with open(p, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return {"ok": True, "rel": rel, "text": raw.decode(enc), "path": p}
        except UnicodeDecodeError:
            continue
    return {"ok": True, "rel": rel, "text": raw.decode("utf-8", "replace"), "path": p}


# ---------------- HTTP ----------------
def _item_sig(it):
    t = it.get("text") or ""
    return "%s|%s|%s|%s|%s|%s|%d|%s" % (it.get("id"), it.get("kind"), it.get("role"), it.get("type"), it.get("time"), it.get("ordinal"), len(t), t[:60])


class H(BaseHTTPRequestHandler):
    server_version = "harness-board/0.5"

    def log_message(self, fmt, *args):
        msg = fmt % args
        for secret in (TOKEN,):
            if secret and secret in msg:
                msg = msg.replace(secret, "[redacted]")
        sys.stderr.write("%s %s\n" % (datetime.now().strftime("%H:%M:%S"), msg))

    def _json(self, obj, code=200, headers=None):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def _loopback(self):
        return self.client_address[0] in ("127.0.0.1", "::1")

    def _token_ok(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[7:].strip() == TOKEN:
            return True
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.startswith("harness_board=") and part.split("=", 1)[1].strip() == TOKEN:
                return True
        return False

    def _same_origin(self):
        o = (self.headers.get("Origin") or "").replace("http://", "")
        host = self.headers.get("Host") or ""
        ok = {"127.0.0.1:%s" % PORT, "localhost:%s" % PORT}
        for h in LISTEN_HOSTS:
            ok.add("%s:%s" % (h, PORT))
        return host in ok and (o == "" or o in ok) and self.headers.get("X-Harness") == "1"

    def _can_read(self):
        return self._loopback() or self._token_ok()

    def _can_write(self):
        if self.headers.get("X-Harness-Node") == "1" and self._token_ok():
            return True
        if self._loopback() and self._same_origin():
            return True
        if self._token_ok() and self._same_origin():
            return True
        return False

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._page()
            if p.startswith("/icons/"):
                return self._icon(p)
            if p == "/login":
                return self._login_page()
            if not self._can_read():
                if p.startswith("/api/"):
                    return self._json({"error": "需要口令"}, 401)
                self.send_response(302)
                self.send_header("Location", "/login")
                self.end_headers()
                return
            if p == "/api/overview":
                return self._json(self._overview())
            if p == "/api/local/overview":
                return self._json(local_overview())
            if p == "/api/sessions":
                return self._json(self._sessions(q))
            if p == "/api/items":
                return self._json(self._items(q))
            if p == "/api/stream":
                return self._sse(q)
            if p == "/api/sends":
                return self._json(self._sends(q))
            if p == "/api/cansend":
                return self._json(self._cansend(q))
            if p == "/api/file":
                return self._json(self._file(q))
            if p == "/api/daemon":
                return self._json(codex_status())
            if p == "/api/bindings" or p == "/api/state":
                return self._json(read_state())
            if p == "/api/templates":
                return self._json({"templates": list_templates()})
            if p == "/api/runs":
                return self._json({"runs": list_runs(int((q.get("limit") or ["30"])[0]))})
            if p == "/api/handover/files":
                return self._json(self._hfiles(q))
            if p == "/api/handover/text":
                return self._json(self._htext(q))
            if p == "/api/self":
                return self._json({"id": DEVICE_ID, "label": DEVICE_LABEL, "ownerName": OWNER_NAME, "listen": LISTEN_HOSTS, "port": PORT, "tokenFp": token_fp(TOKEN)})
            # legacy aliases
            if p == "/api/threads":
                q = {"device": [DEVICE_ID], "app": ["codex"]}
                return self._json({"device": DEVICE_LABEL, "app": "codex", "threads": list_local("codex")})
            if p.startswith("/api/threads/") and p.endswith("/items"):
                tid = p.split("/")[3]
                its, st, err = items_local("codex", tid, int((parse_qs(urlparse(self.path).query).get("n") or ["40"])[0]))
                return self._json({"id": tid, "state": st, "sending": tid in _sending, "items": its, "error": err})
            self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except SystemExit:
            self._json({"error": "本机没有可用的会话库", "sessions": []}, 200)
        except Exception as e:
            self._json({"error": type(e).__name__}, 500)

    def _page(self):
        b = open(os.path.join(HERE, "index.html"), "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _icon(self, p):
        """Serve local app/device icons. Public, no secrets, names only."""
        name = os.path.basename(p.split("?", 1)[0])
        if not re.match(r"^[a-z0-9-]+\.(svg|png)$", name):
            return self._json({"error": "not found"}, 404)
        path = os.path.join(HERE, "icons", name)
        if not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        b = open(path, "rb").read()
        self.send_response(200)
        ctype = "image/png" if name.endswith(".png") else "image/svg+xml; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "public, max-age=86400")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _login_page(self):
        html = (
            "<!doctype html><meta charset=utf-8><title>打开总控板</title>"
            "<body style='font-family:sans-serif;padding:48px;max-width:420px'>"
            "<h2>打开总控板</h2><p>从 Tailscale 打开时需要本机口令（口令在各台 Harness 目录的本地文件里，不要发给别人）。</p>"
            "<form method=post action=/login><input name=token type=password style='width:100%;padding:8px'>"
            "<p><button type=submit>进入</button></p></form></body>"
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _overview(self):
        devices = [local_overview()]
        if PEERS:
            slots = [None] * len(PEERS)
            def one(i, peer):
                slots[i] = peer_overview(peer)
            th = []
            for i, peer in enumerate(PEERS):
                t = threading.Thread(target=one, args=(i, peer), daemon=True)
                t.start()
                th.append(t)
            for t in th:
                t.join(timeout=10)
            for i, peer in enumerate(PEERS):
                devices.append(slots[i] or {
                    "id": peer.get("id"), "label": peer.get("label") or peer.get("id"),
                    "self": False, "online": False, "stale": False, "error": "超时", "apps": {},
                })
        return {"self": DEVICE_ID, "label": DEVICE_LABEL, "ownerName": OWNER_NAME, "devices": devices, "state": read_state(), "syncedAt": now_iso()}

    def _sessions(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        app = (q.get("app") or ["codex"])[0]
        limit = int((q.get("limit") or ["80"])[0])
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/sessions?device=%s&app=%s&limit=%s" % (device, app, limit), timeout=8)
            if st == 200 and isinstance(body, dict):
                body["fetchedAt"] = now_iso()
                body["stale"] = False
                _cache_put(_peer_lists, (device, app), body)
                return body
            err = (body or {}).get("error") or "同伴暂时连不上"
            rec = _cache_get(_peer_lists, (device, app))
            if rec:
                return _stale_wrap(rec, err)
            return {"device": device, "app": app, "sessions": [], "error": err, "offline": True}
        try:
            sess = list_local(app, limit)
        except Exception as e:
            return {"device": DEVICE_ID, "app": app, "sessions": [], "error": type(e).__name__}
        return {"device": DEVICE_ID, "label": DEVICE_LABEL, "app": app, "sessions": sess, "fetchedAt": now_iso(), "stale": False}

    def _items(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        app = (q.get("app") or ["codex"])[0]
        sid = (q.get("id") or [""])[0]
        title = (q.get("title") or [""])[0]
        n = int((q.get("n") or ["40"])[0])
        peer = find_peer(device)
        if peer:
            path = "/api/items?device=%s&app=%s&id=%s&n=%s&title=%s" % (
                device, app, urllib.request.quote(sid), n, urllib.request.quote(title),
            )
            st, body = peer_request(peer, "GET", path, timeout=8)
            if st == 200 and isinstance(body, dict):
                # 同伴若还是旧板，这里再规范化一遍（已有 kind 会原样通过）
                if isinstance(body.get("items"), list) and body["items"] and "kind" not in (body["items"][0] or {}):
                    body["items"] = N.normalize_list(app, body["items"], queue=body.get("queue"))
                    body["pending"] = N.pending_from_items(body["items"], app, device=device, sid=sid, title=title)
                body["fetchedAt"] = now_iso()
                body["stale"] = False
                _cache_put(_peer_items, (device, app, sid), body)
                return body
            err = (body or {}).get("error") or "同伴暂时连不上"
            rec = _cache_get(_peer_items, (device, app, sid))
            if rec:
                return _stale_wrap(rec, err)
            return {"id": sid, "items": [], "error": err, "offline": True}
        its, stt, err, pend, queue = pack_items(app, sid, n, title=title, device=DEVICE_ID)
        run = stt
        if not run:
            # derive from last item
            if any(i.get("kind") == "question" and not (i.get("card") or {}).get("answered") for i in its):
                run = "waiting_human"
            elif its and its[-1].get("role") == "user" and its[-1].get("kind") != "queue":
                run = "running"
            elif its and its[-1].get("status") == "completed":
                run = "idle"
            else:
                run = "idle"
        return {"id": sid, "state": run, "sending": "%s:%s" % (app, sid) in _sending,
                "items": its, "pending": pend, "queue": [{"id": q.get("id"), "text": q.get("text"), "position": q.get("position")} for q in (queue or [])],
                "error": err, "fetchedAt": now_iso(), "stale": False}

    def _sse(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        app = (q.get("app") or ["codex"])[0]
        sid = (q.get("id") or q.get("thread") or [""])[0]
        title = (q.get("title") or [""])[0]
        if not sid:
            return self._json({"error": "id required"}, 400)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        key = "%s:%s:%s" % (device, app, sid)
        mq = queue.Queue()
        with _bus_lock:
            _bus.setdefault(key, []).append(mq)

        def emit(ev, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (ev, json.dumps(data, ensure_ascii=False))).encode())
            self.wfile.flush()

        try:
            seen, first = set(), True
            state = None
            beat = 0
            emit("hello", {"device": device, "app": app, "id": sid})
            while True:
                t_end = time.time() + 1.6
                while time.time() < t_end:
                    try:
                        emit("live", mq.get(timeout=max(0.05, t_end - time.time())))
                    except queue.Empty:
                        pass
                body = self._items({"device": [device], "app": [app], "id": [sid], "n": ["40"], "title": [title]})
                st = body.get("state")
                if key in _sending:
                    st = "running"
                if st != state:
                    state = st
                    emit("state", {"state": st, "state_zh": STATE_ZH.get(st, st)})
                emit("sync", {
                    "ok": not body.get("offline") and not (body.get("error") and not body.get("stale")),
                    "stale": bool(body.get("stale") or body.get("offline")),
                    "at": body.get("fetchedAt") or body.get("lastOkAt"),
                    "error": body.get("error"),
                })
                items = body.get("items") or []
                sigs = [_item_sig(it) for it in items]
                if first:
                    seen.update(sigs)  # 页面刚用 /api/items 拿过这些，不重复推
                    first = False
                else:
                    for it, sg in zip(items, sigs):
                        if sg not in seen:
                            seen.add(sg)
                            emit("item", it)
                beat += 1
                if beat % 10 == 0:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _bus_lock:
                if mq in _bus.get(key, []):
                    _bus[key].remove(mq)

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/upload":
            return self._upload()
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2 * 1024 * 1024:
            self.close_connection = True
            return self._json({"error": "请求太大"}, 413)
        raw = self.rfile.read(n) if n else b""
        if p == "/login":
            return self._do_login(raw)
        if not self._can_write():
            return self._json({"error": "只接受本机页面或带口令的同伴请求"}, 403)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if p == "/api/focus":
            return self._json(self._route_focus(body))
        if p == "/api/open":
            return self._json(self._route_open(body))
        if p == "/api/send":
            return self._json(self._route_send(body))
        if p == "/api/act":
            return self._json(self._route_act(body))
        if p == "/api/bindings":
            return self._json(self._save_bindings(body))
        if p == "/api/handoffs" or p == "/api/handoff":
            return self._json(self._handoff(body))
        if p == "/api/templates":
            act = body.get("action") or "save"
            if act == "delete":
                return self._json(delete_template(_clip(body.get("id"), 40)))
            return self._json(save_template(body))
        self._json({"error": "not found"}, 404)

    def _do_login(self, raw):
        # application/x-www-form-urlencoded
        from urllib.parse import parse_qs as _pq
        token = ""
        try:
            if raw.startswith(b"{") or (self.headers.get("Content-Type") or "").startswith("application/json"):
                token = (json.loads(raw) or {}).get("token") or ""
            else:
                token = (_pq(raw.decode("utf-8", "replace")).get("token") or [""])[0]
        except Exception:
            token = ""
        if token != TOKEN:
            return self._json({"error": "口令不对"}, 403)
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", "harness_board=%s; Path=/; HttpOnly; SameSite=Strict" % TOKEN)
        self.end_headers()

    def _route_focus(self, body):
        device = body.get("device") or DEVICE_ID
        peer = find_peer(device)
        if peer:
            st, res = peer_request(peer, "POST", "/api/focus", {"device": device, "app": body.get("app") or "codex"})
            return res
        app = body.get("app") or "codex"
        if app == "codex":
            how = focus_codex()
            return {"ok": bool(how), "how": how}
        if app in ("workbuddy", "qwen"):
            return do_open(app, body.get("id") or body.get("thread"), body)
        return {"ok": False, "error": "未知工具"}

    def _route_open(self, body):
        device = body.get("device") or DEVICE_ID
        peer = find_peer(device)
        if peer:
            st, res = peer_request(peer, "POST", "/api/open", body)
            return res
        return do_open(body.get("app") or "codex", body.get("id") or body.get("thread") or body.get("subChatId"), body)

    def _route_send(self, body):
        device = body.get("device") or DEVICE_ID
        app = body.get("app") or "codex"
        sid = body.get("id") or body.get("thread")
        msg = (body.get("message") or body.get("text") or "").strip()
        files = [f for f in body.get("files") or [] if isinstance(f, str) and f.strip()]
        if body.get("confirm") is not True:
            return {"error": "缺少确认", "ok": False}
        if not sid or not (msg or files):
            return {"error": "缺少对话或内容", "ok": False}
        step = body.get("step")
        is_origin = self.headers.get("X-Harness-Node") != "1"
        if step and is_origin:
            err = self._check_step(step, device, app, sid)
            if err:
                return {"ok": False, "error": err}
        peer = find_peer(device)
        if peer:
            st, res = peer_request(peer, "POST", "/api/send", body, timeout=30)
            return res
        rec = start_send(dict(body, message=msg, files=files))
        return {"ok": True, "send": public_send(rec)}

    def _route_act(self, body):
        device = body.get("device") or DEVICE_ID
        if body.get("confirm") is not True:
            return {"ok": False, "error": "缺少确认"}
        peer = find_peer(device)
        if peer:
            st, res = peer_request(peer, "POST", "/api/act", body, timeout=20)
            return res if isinstance(res, dict) else {"ok": False, "error": "同伴没有回应"}
        return handle_act(body)

    def _check_step(self, step, device, app, sid):
        st = read_state()
        b, h = st["bindings"], st["handoff"]
        if step in ("send_codex", "review_codex"):
            bind = b.get("codexDev") or {}
            if not bind.get("id"):
                return "还没选定开发用的 Codex 对话"
            if app != "codex" or bind.get("id") != sid or (bind.get("device") or DEVICE_ID) != device:
                return "当前对话和登记的开发对话对不上，没有发送"
            return None
        if step == "publish_qwen":
            bind = b.get("qwenPublish") or {}
            if not (bind.get("subChatId") or bind.get("id")):
                return "还没选定千问办公的发布对话"
            if app != "qwen" or sid not in (bind.get("subChatId"), bind.get("id")) or (bind.get("device") or DEVICE_ID) != device:
                return "当前对话和登记的发布对话对不上，没有发送"
            if (h["steps"].get("authorized") or {}).get("status") != "yes":
                return "还没有你的授权，先点「我授权发布」"
            pq = h["steps"].get("published_qwen") or {}
            if pq.get("once") or pq.get("status") in ("delivered", "replied"):
                return "这条发布已经发过一次，不会再发"
            return None
        return "未知的交接步骤"

    def _sends(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        app = (q.get("app") or ["codex"])[0]
        sid = (q.get("id") or [""])[0]
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/sends?device=%s&app=%s&id=%s" % (quote(device), quote(app), quote(sid)), timeout=10)
            return body if st == 200 else {"sends": [], "old": st == 404, "error": None if st == 404 else (body or {}).get("error")}
        return {"sends": list_sends(app, sid)}

    def _cansend(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        app = (q.get("app") or ["codex"])[0]
        sid = (q.get("id") or [""])[0]
        title = (q.get("title") or [""])[0]
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/cansend?device=%s&app=%s&id=%s&title=%s" % (
                quote(device), quote(app), quote(sid), quote(title)), timeout=15)
            if st == 404:
                return {"mode": "blocked", "why": "那台电脑的总控板还是旧版，等它更新后才能从这里发", "old": True}
            return body if st == 200 else {"mode": "blocked", "why": (body or {}).get("error") or "那台电脑连不上"}
        mode, why = send_mode(app, sid, title)
        return {"mode": mode, "why": why}

    def _file(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        rel = (q.get("rel") or [""])[0]
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/file?device=%s&rel=%s" % (quote(device), quote(rel)), timeout=8)
            if st == 404:
                return {"exists": False, "old": True, "error": "那台电脑的总控板还是旧版，只能等共享盘自动同步"}
            return body if st == 200 else {"exists": False, "error": (body or {}).get("error")}
        return file_info(rel)

    def _hfiles(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        qq = (q.get("q") or [""])[0]
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/handover/files?device=%s&q=%s" % (quote(device), quote(qq)), timeout=10)
            if st == 404:
                return {"files": [], "old": True, "error": "那台电脑的总控板还是旧版，暂时看不了它的交接盘"}
            return body if st == 200 else {"files": [], "error": (body or {}).get("error") or "那台电脑连不上"}
        return handover_files(qq)

    def _htext(self, q):
        device = (q.get("device") or [DEVICE_ID])[0]
        rel = (q.get("rel") or [""])[0]
        peer = find_peer(device)
        if peer:
            st, body = peer_request(peer, "GET", "/api/handover/text?device=%s&rel=%s" % (quote(device), quote(rel)), timeout=10)
            if st == 404:
                return {"ok": False, "old": True, "error": "那台电脑的总控板还是旧版"}
            return body if st == 200 else {"ok": False, "error": (body or {}).get("error") or "那台电脑连不上"}
        return handover_text(rel)

    def _upload(self):
        q = parse_qs(urlparse(self.path).query)
        device = (q.get("device") or [DEVICE_ID])[0]
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._json({"ok": False, "error": "缺少文件大小"}, 411)
        if n > MAX_UPLOAD:
            self.close_connection = True
            return self._json({"ok": False, "error": "单个文件先限制在 200MB 以内"}, 413)
        if not self._can_write():
            self.close_connection = True
            return self._json({"error": "只接受本机页面或带口令的同伴请求"}, 403)
        name = unquote(self.headers.get("X-File-Name") or "") or "file"
        peer = find_peer(device)
        try:
            if peer:
                pst, _ = peer_request(peer, "GET", "/api/file?device=%s&rel=probe" % quote(peer["id"]), timeout=6)
                if pst == 200:
                    st, res = peer_upload(peer, self.rfile, n, name)
                    return self._json(res, 200 if st == 200 else 502)
                # 目标机是旧版或连不上：先存本机交接盘，靠 Syncthing 同步过去
                res = save_upload(self.rfile, n, name)
                res["fallback"] = True
                res["note"] = ("那台电脑的总控板还是旧版" if pst == 404 else "那台电脑暂时连不上") + "，先存在本机交接盘，会自动同步过去"
                log_send({"event": "upload_fallback", "name": res["name"], "size": res["size"], "target": peer.get("id")})
                return self._json(res)
            res = save_upload(self.rfile, n, name)
            log_send({"event": "upload", "name": res["name"], "size": res["size"]})
            return self._json(res)
        except Exception as e:
            self.close_connection = True
            return self._json({"ok": False, "error": "没存进交接盘（%s）" % type(e).__name__}, 500)

    def _save_bindings(self, body):
        with _state_lock:
            st = read_state()
            b = st["bindings"]
            for key in ("codexDev", "qwenPublish"):
                if key in body and isinstance(body[key], dict):
                    cur = b.get(key) or {}
                    cur.update({k: body[key].get(k, cur.get(k)) for k in set(cur) | set(body[key])})
                    b[key] = cur
            write_state(st)
            return {"ok": True, "bindings": st["bindings"]}

    def _handoff(self, body):
        """交接五步。真正的发送都走聊天框（/api/send 带 step），这里只记状态。"""
        action = body.get("action")
        with _state_lock:
            st = read_state()
            h = st["handoff"]
            if action == "save_prompt":
                h["prompt"] = body.get("prompt") or ""
                h["steps"]["prompt_ready"] = {"status": "ready" if h["prompt"].strip() else "empty", "at": now_iso()}
                if isinstance(body.get("files"), list):
                    h["files"] = [_clip(f, 400) for f in body["files"] if isinstance(f, str) and resolve_rel(f)][:20]
                if h["prompt"].strip():
                    touch_run(st)
                write_state(st)
                return {"ok": True, "handoff": h}
            if action == "authorize":
                h["steps"]["authorized"] = {"status": "yes", "at": now_iso()}
                touch_run(st)
                write_state(st)
                return {"ok": True, "handoff": h}
            if action == "start_template":
                tpl = next((t for t in list_templates() if t.get("id") == body.get("templateId")), None)
                if not tpl:
                    return {"ok": False, "error": "没找到这个常用交接"}
                if h.get("runId"):
                    touch_run(st, create=False, end=None if _run_result(h) == "done" else "reset")
                prompt, note = tpl.get("prompt") or "", ""
                if tpl.get("promptRel"):
                    r = handover_text(tpl["promptRel"])
                    if r.get("ok"):
                        prompt = r["text"]
                    else:
                        note = "提示词文件没读到（%s），先用了保存时的内容" % r.get("error")
                st["bindings"]["codexDev"] = dict(tpl.get("codexDev") or {})
                st["bindings"]["qwenPublish"] = dict(tpl.get("qwenPublish") or {})
                nh = _empty_state()["handoff"]
                nh.update(prompt=prompt, templateId=tpl["id"], templateName=tpl.get("name"), startedAt=now_iso(),
                          files=list(tpl.get("files") or []))
                nh["steps"]["prompt_ready"] = {"status": "ready" if prompt.strip() else "empty", "at": now_iso()}
                st["handoff"] = nh
                touch_run(st)
                write_state(st)
                with _data_lock:
                    ts = list_templates()
                    for t in ts:
                        if t.get("id") == tpl["id"]:
                            t["lastRun"] = now_iso()
                    _jsave("templates.json", ts)
                return {"ok": True, "handoff": nh, "bindings": st["bindings"], "note": note}
            if action == "step_status":
                step = body.get("step")
                status = body.get("status")
                key = {"send_codex": "sent_codex", "publish_qwen": "published_qwen", "review_codex": "codex_review"}.get(step)
                if not key or status not in ("sending", "waiting_paste", "delivered", "replied", "failed", "blocked"):
                    return {"ok": False, "error": "参数不对"}
                cur = h["steps"].get(key) or {}
                if cur.get("status") in ("delivered", "replied") and status not in ("replied",):
                    return {"ok": True, "handoff": h}
                rec = {"status": status, "at": now_iso(), "sendId": body.get("sendId")}
                if key == "published_qwen":
                    rec["once"] = bool(cur.get("once")) or status in ("delivered", "replied")
                h["steps"][key] = rec
                touch_run(st)
                write_state(st)
                return {"ok": True, "handoff": h}
            if action == "start_manual":
                if h.get("runId"):
                    touch_run(st, create=False, end=None if _run_result(h) == "done" else "reset")
                nh = _empty_state()["handoff"]
                nh.update(templateName="手动交接", startedAt=now_iso())
                st["handoff"] = nh
                touch_run(st)
                write_state(st)
                return {"ok": True, "handoff": nh, "bindings": st["bindings"]}
            if action == "reset":
                if h.get("runId"):
                    touch_run(st, create=False, end=None if _run_result(h) == "done" else "reset")
                st["handoff"] = _empty_state()["handoff"]
                write_state(st)
                return {"ok": True, "handoff": st["handoff"]}
            if action in ("send_codex", "publish_qwen", "review_codex"):
                return {"ok": False, "error": "请在中间的聊天框里发送"}
            return {"ok": False, "error": "未知动作"}


class DualHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _serve(host, port):
    try:
        srv = DualHTTPServer((host, port), H)
    except OSError as e:
        print("绑定失败 %s:%s %s" % (host, port, type(e).__name__), flush=True)
        return
    print("Harness 总控板: http://%s:%s/" % (host, port), flush=True)
    srv.serve_forever()


def wait_bind_ts(port):
    for _ in range(60):
        time.sleep(5)
        ip = ts_ip()
        if not ip or ip in LISTEN_HOSTS:
            if ip and ip in LISTEN_HOSTS:
                return
            continue
        try:
            LISTEN_HOSTS.append(ip)
            _serve(ip, port)
            return
        except OSError:
            if ip in LISTEN_HOSTS:
                LISTEN_HOSTS.remove(ip)
            continue


def main():
    global TOKEN, PORT, LISTEN_HOSTS
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    PORT = a.port
    load_peers()
    TOKEN = load_or_create_token()
    LISTEN_HOSTS = ["127.0.0.1"]
    ip = ts_ip()
    threads = []
    t0 = threading.Thread(target=_serve, args=("127.0.0.1", PORT), daemon=False)
    t0.start()
    threads.append(t0)
    if ip:
        LISTEN_HOSTS.append(ip)
        try:
            t1 = threading.Thread(target=_serve, args=(ip, PORT), daemon=False)
            t1.start()
            threads.append(t1)
        except OSError as e:
            print("Tailscale 地址绑定失败: %s" % type(e).__name__, flush=True)
            threading.Thread(target=wait_bind_ts, args=(PORT,), daemon=True).start()
    else:
        threading.Thread(target=wait_bind_ts, args=(PORT,), daemon=True).start()
    print("设备 %s · 口令指纹 %s" % (DEVICE_ID, token_fp(TOKEN)), flush=True)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
