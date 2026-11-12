"""本地假数据：在 127.0.0.1 起几份总控板，方便改界面。不含真实设备名或对话。"""
import json, os, sys, threading
from datetime import datetime, timedelta
sys.argv, args = [sys.argv[0]], sys.argv[1:]
HERE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "board"))
did, label, port, peers = args[0], args[1], int(args[2]), json.loads(args[3])
os.environ["HARNESS_DATA_DIR"] = "/tmp/harness-mock/" + did
sys.path.insert(0, HERE)
import server as S
S.handover_root = lambda: "/tmp/harness-mock-handover/" + did
S.ROOT = "/tmp/harness-mock-root-" + did
os.makedirs(S.ROOT, exist_ok=True)
S.DEVICE_ID, S.DEVICE_LABEL, S.PEERS, S.PORT = did, label, peers, port
S.TOKEN = "mock-token-not-real"
S.LISTEN_HOSTS = ["127.0.0.1"]

def ago(m):
    return (datetime.now() - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")

SESS = {"codex": [{"id": "demo-1", "title": "示例对话", "updated": ago(5), "runState": "idle"}],
        "workbuddy": [], "qwen": []}
ITEMS = {"demo-1": [
    {"role": "user", "type": "message", "kind": "text", "text": "这是一条示例消息", "time": ago(6), "verified": True},
    {"role": "assistant", "type": "message", "kind": "text", "text": "这是示例回复。", "time": ago(5), "status": "completed", "verified": True},
]}
installed = {
    "pc-a": {"codex": (True, True), "workbuddy": (False, False), "qwen": (False, False)},
    "pc-b": {"codex": (True, True), "workbuddy": (True, True), "qwen": (True, False)},
    "pc-c": {"codex": (False, False), "workbuddy": (True, True), "qwen": (True, True)},
}[did]
S.app_status = lambda app: {"app": app, "installed": installed[app][0], "running": installed[app][1]}
S.codex_status = lambda: S.app_status("codex")
S.list_local = lambda app, limit=80: [dict(t, app=app) for t in SESS.get(app, [])][:limit]
S.items_local = lambda app, sid, n=40, title="", extra=None: (ITEMS.get(sid, [])[-n:], "idle", None)
t = threading.Thread(target=S._serve, args=("127.0.0.1", port)); t.start(); t.join()
