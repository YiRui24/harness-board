#!/usr/bin/env python3
"""Create board-token.txt if missing. Prints fingerprint only, never the token."""
import hashlib, os, secrets, sys
p = sys.argv[1] if len(sys.argv) > 1 else "board-token.txt"
if not os.path.exists(p) or not open(p, encoding="utf-8").read().strip():
    t = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, (t + "\n").encode()); os.close(fd)
    try: os.chmod(p, 0o600)
    except Exception: pass
t = open(p, encoding="utf-8").read().strip()
print("token_fp", hashlib.sha256(t.encode()).hexdigest()[:8], "len", len(t))
