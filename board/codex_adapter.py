#!/usr/bin/env python3
"""Harness · Codex 适配器（P1，只读 + 发送演练）

只用标准库。所有 sqlite 一律以 mode=ro 只读打开，不写 ~/.codex。
不读 auth.json / token / creator_user_id。

用法:
  codex_adapter.py list [--all] [--limit N]
  codex_adapter.py tail <thread> [n] [--meta]      # --meta: 只输出类型/时间/长度，不输出正文
  codex_adapter.py watch <thread> [--interval S] [--meta]
  codex_adapter.py send <thread> --message TEXT [--file PATH ...] [--dry-run | --confirm] [--via daemon|cli]
  codex_adapter.py subscribe <thread> [--load] [--seconds N]
  codex_adapter.py daemon-status
<thread> 可以是 id，也可以是对话名(name)或标题(title)。
send 默认只演练（打印将要执行的动作）；只有显式加 --confirm 才会真的投递。
--via daemon（默认）：经 Codex 官方 app-server 守护进程（unix socket 上的 WebSocket JSON-RPC）
  thread/queue/add → 未加载则 thread/resume（自动跑队列）/ 已加载则 thread/queue/start → 流式收回复。
--via cli：旧路径 `codex queue --resume`，只排队，可能需要另行唤醒。
subscribe 只读：默认只挂已在守护进程里加载的对话；--load 才会 resume（队列非空时拒绝，避免顺带触发发送）。
"""
import argparse, glob, json, os, platform, shlex, socket, sqlite3, subprocess, sys, threading, time, uuid, queue as _queue
from datetime import datetime

CODEX_HOME = os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
STATE_DB = os.path.join(CODEX_HOME, "state_5.sqlite")
HIST_DB = os.path.join(CODEX_HOME, "thread_history_1.sqlite")
IS_WIN = os.name == "nt"


def _default_codex_bin():
    if not IS_WIN:
        return "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
    # Windows：优先 Microsoft Store 版 Codex 自带的 CLI（新版才有 app-server daemon），取版本号最大的
    try:  # WindowsApps 目录本身不可列举，向 Appx 查安装位置
        loc = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-AppxPackage OpenAI.Codex | Sort-Object Version | Select-Object -Last 1).InstallLocation"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        exe = os.path.join(loc, "app", "resources", "codex.exe")
        if loc and os.path.exists(exe):
            return exe
    except Exception:
        pass
    c = sorted(glob.glob(os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe")), key=os.path.getmtime)
    return c[-1] if c else "codex"


CODEX_BIN = os.environ.get("CODEX_BIN") or _default_codex_bin()
DEVICE = os.environ.get("HARNESS_DEVICE", platform.node())
DAEMON_SOCK = os.path.join(CODEX_HOME, "app-server-control", "app-server-control.sock")
# Windows 上商店版 Codex 的 CLI 不支持 `app-server daemon start`（报 no complete local package），
# 改为自启 `codex app-server --listen ws://127.0.0.1:PORT`（只回环），用这个地址连。
APPSERVER_URL = os.environ.get("HARNESS_APPSERVER_URL") or ("ws://127.0.0.1:8766" if IS_WIN else "")


def ro(path):
    if not os.path.exists(path):
        sys.exit(f"找不到数据库: {path}")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def ts(ms):
    if not ms:
        return None
    if ms < 10**11:  # 秒
        ms *= 1000
    return datetime.fromtimestamp(ms / 1000).astimezone().isoformat(timespec="seconds")


def resolve(state, key):
    q = "select id,name,title from threads where id=? or name=? or title=? " \
        "order by archived asc, coalesce(recency_at_ms,updated_at_ms) desc"
    rows = state.execute(q, (key, key, key)).fetchall()
    if not rows:
        sys.exit(f"找不到对话: {key}")
    if len({r["id"] for r in rows}) > 1:
        print(f"注意: '{key}' 匹配到 {len(rows)} 个对话，取最近的一个", file=sys.stderr)
    return rows[0]["id"]


def guess_state(hist, tid):
    t = hist.execute("select status from thread_turns where thread_id=? "
                     "order by rollout_ordinal desc limit 1", (tid,)).fetchone()
    if not t:
        return "idle"
    st = t["status"]
    if st == "inProgress":
        return "running"
    last = hist.execute("select item_json from thread_items where thread_id=? and "
                        "item_type='agentMessage' order by rollout_ordinal desc limit 1",
                        (tid,)).fetchone()
    if last:
        try:
            if json.loads(last["item_json"]).get("questions"):
                return "waiting_human"
        except Exception:
            pass
    return "idle"  # completed / interrupted / failed 都视为空闲，last_turn 字段另给原始状态


def text_of(d):
    """从 item_json 里尽量取出可读文本。"""
    t = d.get("type")
    if isinstance(d.get("text"), str):
        return d["text"]
    if t == "commandExecution":
        return d.get("command") or ""
    if t == "mcpToolCall":
        return f"{d.get('server')}.{d.get('tool')}"
    if t == "fileChange":
        return ", ".join(c.get("path", "") for c in d.get("changes") or [])
    if t == "reasoning":
        return " ".join(x if isinstance(x, str) else x.get("text", "")
                        for x in d.get("summary") or [])
    if t == "imageView":
        return d.get("path") or ""
    for k in ("content", "items"):
        v = d.get(k)
        if isinstance(v, list):
            parts = []
            for x in v:
                if isinstance(x, str):
                    parts.append(x)
                elif isinstance(x, dict):
                    parts.append(x.get("text") or (f"[{x.get('type')}]" if x.get("type") else ""))
            return " ".join(p for p in parts if p)
    return ""


ROLE = {"userMessage": "user", "agentMessage": "assistant"}


def items(hist, tid, n, after=None):
    if after is None:
        rows = hist.execute("select * from thread_items where thread_id=? "
                            "order by rollout_ordinal desc limit ?", (tid, n)).fetchall()[::-1]
    else:
        rows = hist.execute("select * from thread_items where thread_id=? and rollout_ordinal>? "
                            "order by rollout_ordinal asc", (tid, after)).fetchall()
    out = []
    for r in rows:
        try:
            d = json.loads(r["item_json"])
        except Exception:
            d = {}
        out.append({
            "id": d.get("id") or ("ord-%s" % r["rollout_ordinal"]),
            "ordinal": r["rollout_ordinal"],
            "turn_id": r["turn_id"],
            "type": r["item_type"] or d.get("type"),
            "role": ROLE.get(r["item_type"] or d.get("type"), "tool"),
            "time": ts(r["created_at_ms"]),
            "done": ts(r["completed_at_ms"]),
            "status": d.get("status"),
            "text": text_of(d),
            "questions": d.get("questions"),
            "command": d.get("command"),
            "cwd": d.get("cwd") if not isinstance(d.get("cwd"), dict) else (d.get("cwd") or {}).get("path") or str(d.get("cwd")),
            "changes": d.get("changes"),
            "exit_code": d.get("exit_code") or d.get("exitCode"),
            "aggregated_output": d.get("aggregated_output") or d.get("aggregatedOutput"),
            "server": d.get("server"),
            "tool": d.get("tool") or d.get("name"),
            "error": d.get("error") if not isinstance(d.get("error"), dict) else (d.get("error") or {}).get("message") or d.get("error"),
        })
    return out


def meta_only(it):
    it = dict(it)
    it["text_len"] = len(it.pop("text") or "")
    return it


def cmd_list(a):
    s, h = ro(STATE_DB), ro(HIST_DB)
    where = "archived=0" + ("" if a.all else " and name is not null and name!=''")
    rows = s.execute(f"select id,name,title,originator,cwd,is_pinned,updated_at_ms,recency_at_ms "
                     f"from threads where {where} order by coalesce(recency_at_ms,updated_at_ms) desc "
                     f"limit ?", (a.limit,)).fetchall()
    out = []
    for r in rows:
        lt = h.execute("select status from thread_turns where thread_id=? order by rollout_ordinal "
                       "desc limit 1", (r["id"],)).fetchone()
        out.append({"device": DEVICE, "app": "codex", "session_id": r["id"],
                    "title": r["name"] or r["title"], "originator": r["originator"],
                    "cwd": r["cwd"], "pinned": bool(r["is_pinned"]),
                    "updated": ts(r["updated_at_ms"]),
                    "state": guess_state(h, r["id"]),
                    "last_turn": lt["status"] if lt else None})
    print(json.dumps(out, ensure_ascii=False, indent=1))


def cmd_tail(a):
    s, h = ro(STATE_DB), ro(HIST_DB)
    tid = resolve(s, a.thread)
    its = items(h, tid, a.n)
    if a.meta:
        its = [meta_only(i) for i in its]
    print(json.dumps({"session_id": tid, "state": guess_state(h, tid), "items": its},
                     ensure_ascii=False, indent=1))


def cmd_watch(a):
    s = ro(STATE_DB)
    tid = resolve(s, a.thread)
    h = ro(HIST_DB)
    r = h.execute("select max(rollout_ordinal) m from thread_items where thread_id=?", (tid,)).fetchone()
    last, state = r["m"] or -1, None
    print(f"# watching {tid} from ordinal {last}, Ctrl-C 退出", file=sys.stderr)
    try:
        while True:
            h = ro(HIST_DB)  # 每轮重开，确保看到 WAL 新写入
            st = guess_state(h, tid)
            if st != state:
                print(json.dumps({"event": "state", "state": st, "time": ts(time.time() * 1000)},
                                 ensure_ascii=False), flush=True)
                state = st
            for it in items(h, tid, 0, after=last):
                last = it["ordinal"]
                print(json.dumps(meta_only(it) if a.meta else it, ensure_ascii=False), flush=True)
            h.close()
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass


# ---------------- app-server 守护进程（WebSocket over unix socket） ----------------
class AppServer:
    """最小 JSON-RPC 客户端。notifications 进 self.events 队列；request() 同步等结果。"""

    def __init__(self, sock_path=DAEMON_SOCK, name="harness-adapter"):
        import websocket  # websocket-client
        if APPSERVER_URL:
            self.ws = websocket.WebSocket()
            self.ws.connect(APPSERVER_URL, suppress_origin=True, timeout=10)
            self.ws.settimeout(None)
            self._init(name)
            return
        if not os.path.exists(sock_path):
            raise RuntimeError(f"守护进程 socket 不存在: {sock_path}（先运行 codex app-server daemon start）")
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("此 Python 不支持 AF_UNIX（Windows 需 Python 3.13+ 的 AF_UNIX 支持）")
        us = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        us.connect(sock_path if IS_WIN else os.path.realpath(sock_path))
        self.ws = websocket.WebSocket()
        self.ws.connect("ws://localhost/", socket=us, suppress_origin=True)
        self._init(name)

    def _init(self, name):
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self._server_reqs = {}
        self.events = _queue.Queue()
        self.closed = threading.Event()
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": name, "version": "0.2.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.notify("initialized", {})

    def _reader(self):
        try:
            while True:
                raw = self.ws.recv()
                if not raw:
                    break
                m = json.loads(raw)
                if "id" in m and ("result" in m or "error" in m):
                    q = self._pending.pop(m["id"], None)
                    if q:
                        q.put(m)
                elif "method" in m and "id" in m:
                    # 服务端请求（审批 / requestUserInput）。不要自动回错误，以免抢走桌面端的处理权。
                    # 先记下，板上若能作答再回；否则留给 Codex 窗口。
                    self._server_reqs[m["id"]] = m
                    self.events.put({"method": "server/request", "id": m["id"], "params": m})
                elif "method" in m:
                    self.events.put(m)
        except Exception as e:
            self.events.put({"method": "harness/disconnected", "params": {"error": str(e)}})
        finally:
            self.closed.set()

    def _send(self, obj):
        with self._lock:
            self.ws.send(json.dumps(obj, ensure_ascii=False))

    def request(self, method, params, timeout=60):
        with self._lock:
            self._id += 1
            rid = self._id
        q = _queue.Queue()
        self._pending[rid] = q
        self._send({"id": rid, "method": method, "params": params})
        try:
            m = q.get(timeout=timeout)
        except _queue.Empty:
            raise RuntimeError(f"{method} 超时")
        if "error" in m:
            raise RuntimeError(f"{method} 出错: {m['error']}")
        return m["result"]

    def notify(self, method, params):
        self._send({"method": method, "params": params})

    def loaded(self):
        return set(self.request("thread/loaded/list", {}).get("data") or [])

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def daemon_alive():
    try:
        s = AppServer(name="harness-probe")
        n = len(s.loaded())
        s.close()
        return True, n
    except Exception as e:
        return False, str(e)


def build_input(message, files=()):
    """文本 + 附件：图片走 localImage，其它文件只把路径附在文本末尾（大文件走交接盘）。"""
    IMG = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    text, imgs, others = message, [], []
    for f in files or []:
        f = os.path.expanduser(f)
        (imgs if f.lower().endswith(IMG) else others).append(f)
    if others:
        text += "\n\n附件路径：\n" + "\n".join(f"- {x}" for x in others)
    inp = [{"type": "text", "text": text, "text_elements": []}]
    inp += [{"type": "localImage", "path": x} for x in imgs]
    return inp


def ev_brief(m):
    """把通知压成一行可读事件（板子与命令行共用）。"""
    meth, p = m.get("method"), m.get("params") or {}
    if meth == "item/agentMessage/delta":
        return {"kind": "delta", "item_id": p.get("itemId"), "delta": p.get("delta", "")}
    if meth in ("item/started", "item/completed"):
        it = p.get("item") or {}
        return {"kind": meth.split("/")[1], "type": it.get("type"), "id": it.get("id"),
                "role": ROLE.get(it.get("type"), "tool"),
                "text": text_of(it) if it.get("type") in ROLE else ""}
    if meth == "thread/status/changed":
        return {"kind": "status", "status": (p.get("status") or {}).get("type")}
    if meth in ("turn/started", "turn/completed"):
        return {"kind": meth.split("/")[1], "turn_status": (p.get("turn") or {}).get("status")}
    if meth == "thread/queue/changed":
        return {"kind": "queue"}
    if meth == "server/request":
        inner = (p.get("params") or p) if isinstance(p, dict) else {}
        req = inner if "method" in inner else p
        method = (req or {}).get("method") if isinstance(req, dict) else None
        return {"kind": "server_request", "method": method, "id": m.get("id"), "params": (req or {}).get("params") if isinstance(req, dict) else p}
    return {"kind": "other", "method": meth}


def stream_until_done(srv, tid, timeout, quiet=False):
    t0, got_turn = time.time(), False
    while time.time() - t0 < timeout:
        try:
            m = srv.events.get(timeout=1)
        except _queue.Empty:
            continue
        p = m.get("params") or {}
        if p.get("threadId") not in (None, tid):
            continue
        b = ev_brief(m)
        if not quiet:
            if b["kind"] == "delta":
                sys.stdout.write(b["delta"]); sys.stdout.flush()
            else:
                print(("\n" if b["kind"] == "completed" else "") + "# " + json.dumps(b, ensure_ascii=False), flush=True)
        if b["kind"] == "started" and b.get("type") == "userMessage":
            got_turn = True
        if b["kind"] == "turn_started" or b["kind"] == "started":
            got_turn = True
        if b["kind"] == "completed" and m.get("method") == "turn/completed":
            return "completed"
        if m.get("method") == "turn/completed":
            return "completed"
        if m.get("method") == "harness/disconnected":
            return "disconnected"
    return "timeout" if got_turn else "no_turn"


def daemon_send(tid, message, files=(), wait=600, on_event=None):
    """真实发送：queue/add → resume 或 queue/start。on_event(brief) 用于板子转发。返回结果 dict。"""
    srv = AppServer(name="harness-send")
    try:
        was_loaded = tid in srv.loaded()
        r = srv.request("thread/queue/add", {"threadId": tid, "clientUserMessageId": str(uuid.uuid4()),
                                             "input": build_input(message, files)})
        sub_id = ((r or {}).get("queuedSubmission") or {}).get("id")
        if was_loaded:
            srv.request("thread/queue/start", {"threadId": tid, "queuedSubmissionId": sub_id})
            how = "queue/start"
        else:
            srv.request("thread/resume", {"threadId": tid, "excludeTurns": True}, timeout=120)
            how = "resume"
        result = {"thread": tid, "queued_id": sub_id, "wake": how, "outcome": None}
        if on_event is None:
            result["outcome"] = stream_until_done(srv, tid, wait)
        else:
            t0 = time.time()
            while time.time() - t0 < wait:
                try:
                    m = srv.events.get(timeout=1)
                except _queue.Empty:
                    continue
                if (m.get("params") or {}).get("threadId") not in (None, tid):
                    continue
                b = ev_brief(m)
                if b["kind"] != "other":
                    on_event(b)
                if m.get("method") in ("turn/completed", "harness/disconnected"):
                    result["outcome"] = m["method"]
                    break
            result["outcome"] = result["outcome"] or "timeout"
        return result
    finally:
        srv.close()


def cmd_send(a):
    s = ro(STATE_DB)
    tid = resolve(s, a.thread)
    print("目标对话:", tid, f"({a.thread})")
    print("消息长度:", len(a.message), "附件:", a.file or [])
    if a.via == "cli":
        cmd = [CODEX_BIN, "queue", "--resume", "--thread", tid, "--message", a.message]
        print("将执行:", shlex.join(cmd))
    else:
        print("将执行: daemon thread/queue/add → thread/resume（未加载）或 thread/queue/start（已加载）→ 流式收回复")
        print("input 预览:", json.dumps([{k: (v if k != "text" else f"<{len(v)} 字>") for k, v in x.items()}
                                          for x in build_input(a.message, a.file)], ensure_ascii=False))
    if not a.confirm:
        print("[演练] 未发送。确认无误后加 --confirm 才会真的投递。")
        return
    print("[真实发送] 执行中 ...", flush=True)
    if a.via == "cli":
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        print("exit:", p.returncode)
        sys.stdout.write(p.stdout); sys.stderr.write(p.stderr)
        sys.exit(p.returncode)
    res = daemon_send(tid, a.message, a.file, wait=a.wait)
    print("\n# 结果:", json.dumps(res, ensure_ascii=False))


def cmd_subscribe(a):
    s = ro(STATE_DB)
    tid = resolve(s, a.thread)
    srv = AppServer(name="harness-subscribe")
    if tid not in srv.loaded():
        if not a.load:
            sys.exit("该对话没有加载在守护进程里；只读模式下不主动加载。用 watch（读库）或加 --load。")
        q = srv.request("thread/queue/list", {"threadId": tid}).get("data") or []
        if q:
            sys.exit(f"队列里还有 {len(q)} 条待发，--load 会顺带把它们跑起来，已拒绝。")
        srv.request("thread/resume", {"threadId": tid, "excludeTurns": True}, timeout=120)
    print(f"# subscribed {tid}", file=sys.stderr, flush=True)
    t0 = time.time()
    try:
        while not a.seconds or time.time() - t0 < a.seconds:
            try:
                m = srv.events.get(timeout=1)
            except _queue.Empty:
                continue
            if (m.get("params") or {}).get("threadId") not in (None, tid):
                continue
            b = ev_brief(m)
            if b["kind"] == "other" and not a.verbose:
                continue
            if a.meta:
                b.pop("text", None); b["delta"] = len(b.get("delta", "")) if "delta" in b else None
            if a.meta and b.get("delta") is None:
                b.pop("delta", None)
            print(json.dumps(b, ensure_ascii=False), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            srv.request("thread/unsubscribe", {"threadId": tid}, timeout=10)
        except Exception:
            pass
        srv.close()


def cmd_daemon_status(a):
    ok, info = daemon_alive()
    print(json.dumps({"daemon": ok, "loaded_threads" if ok else "error": info}, ensure_ascii=False))

QUEUE_DB = os.path.join(CODEX_HOME, "queue_1.sqlite")


def _payload_text(payload):
    """从 queue_1.sqlite 的 payload_json 或 QueuedSubmission.input 抽出正文。"""
    if payload is None:
        return ""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return payload
    texts = []
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("text"), str) and o["text"].strip():
                texts.append(o["text"])
            elif isinstance(o.get("input"), (list, dict)):
                walk(o.get("input"))
            else:
                for v in o.values():
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(payload)
    return "\n".join(texts).strip()


def list_queued(tid):
    """只读 queue_1.sqlite。表字段来自官方 SqliteQueueStore：id, thread_id, payload_json, queue_order, created_at_ms, updated_at_ms。
    若有 status 列（pending/claimed/failed，PR 描述过）一并带上。守护进程不可用时也能看队列。"""
    if not tid or not os.path.exists(QUEUE_DB):
        return []
    try:
        c = ro(QUEUE_DB)
    except Exception:
        return []
    try:
        cols = [r[1] for r in c.execute("pragma table_info(queued_items)").fetchall()]
        if "thread_id" not in cols or "payload_json" not in cols:
            return []
        order = "queue_order" if "queue_order" in cols else "created_at_ms"
        sel = ["id", "payload_json"]
        for col in ("queue_order", "created_at_ms", "updated_at_ms", "status"):
            if col in cols:
                sel.append(col)
        q = "select %s from queued_items where thread_id=? order by %s" % (",".join(sel), order)
        rows = c.execute(q, (tid,)).fetchall()
        out = []
        for i, r in enumerate(rows, 1):
            d = dict(r)
            out.append({
                "id": d.get("id"),
                "queuedId": d.get("id"),
                "text": _payload_text(d.get("payload_json")),
                "time": ts(d.get("created_at_ms") or d.get("updated_at_ms")),
                "status": d.get("status") or "pending",
                "position": i,
                "cancelable": True,  # 官方 thread/queue/delete；实际取消还要守护进程
                "kind": "queue",
                "role": "user",
            })
        for rec in out:
            rec["total"] = len(out)
        return out
    except Exception:
        return []
    finally:
        try:
            c.close()
        except Exception:
            pass


def queue_delete(tid, queued_id):
    """官方：thread/queue/delete {threadId, queuedSubmissionId}。需要守护进程。不写 sqlite。"""
    if not tid or not queued_id:
        return {"ok": False, "error": "缺少队列编号"}
    try:
        srv = AppServer(name="harness-queue")
    except Exception as e:
        return {"ok": False, "error": "连不上 Codex 守护进程，请在原窗口取消", "fallback": "open", "detail": str(e)}
    try:
        srv.request("thread/queue/delete", {"threadId": tid, "queuedSubmissionId": queued_id})
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": "取消排队失败：%s" % e, "fallback": "open"}
    finally:
        try:
            srv.close()
        except Exception:
            pass


def try_answer_server_request(method, params, decision=None, answers=None):
    """若当前没有挂着的服务端请求，无法代答，返回 fallback=open。
    审批决策官方枚举：Accept / Decline / Cancel / AcceptForSession。
    反问官方响应：{answers: {questionId: {answers: [...]}}}。"""
    return {"ok": False, "fallback": "open", "error": "这块审批/反问要在已连接的守护进程会话里作答，总控板现在没有拿到未决请求，请到原窗口处理"}



def main():
    ap = argparse.ArgumentParser(description="Harness Codex adapter")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("list"); p.add_argument("--all", action="store_true")
    p.add_argument("--limit", type=int, default=200); p.set_defaults(f=cmd_list)
    p = sp.add_parser("tail"); p.add_argument("thread"); p.add_argument("n", nargs="?", type=int, default=20)
    p.add_argument("--meta", action="store_true"); p.set_defaults(f=cmd_tail)
    p = sp.add_parser("watch"); p.add_argument("thread"); p.add_argument("--interval", type=float, default=2)
    p.add_argument("--meta", action="store_true"); p.set_defaults(f=cmd_watch)
    p = sp.add_parser("send"); p.add_argument("thread"); p.add_argument("--message", required=True)
    p.add_argument("--file", action="append"); p.add_argument("--via", choices=["daemon", "cli"], default="daemon")
    p.add_argument("--wait", type=int, default=600)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true", default=True)
    g.add_argument("--confirm", action="store_true")
    p.set_defaults(f=cmd_send)
    p = sp.add_parser("subscribe"); p.add_argument("thread"); p.add_argument("--load", action="store_true")
    p.add_argument("--seconds", type=float, default=0); p.add_argument("--meta", action="store_true")
    p.add_argument("--verbose", action="store_true"); p.set_defaults(f=cmd_subscribe)
    p = sp.add_parser("daemon-status"); p.set_defaults(f=cmd_daemon_status)
    a = ap.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
