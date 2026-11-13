"""合成设备互访：强制按非回环客户端校验，即使测试连接使用本机地址。"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

BOARD = Path(__file__).resolve().parents[1] / "board"


def worker(root, port):
    sys.path.insert(0, str(BOARD))
    import server as s
    s.ROOT = root
    s.HERE = root
    s.LOG = os.path.join(root, 'send.log')
    s.PORT = int(port)
    s.load_peers()
    s.TOKEN = s.load_or_create_token()
    s.H._loopback = lambda self: False
    s._serve('127.0.0.1', int(port))


def request(port, path, token=None, body=None, headers=None):
    h = dict(headers or {})
    if token is not None:
        h['Authorization'] = 'Bearer ' + token
        h['X-Harness-Node'] = '1'
    if isinstance(body, dict):
        body = json.dumps(body).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request('http://127.0.0.1:%d%s' % (port, path), data=body, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PeerAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ports = [free_port(), free_port()]
        self.procs = []
        for i, name in enumerate(('a', 'b')):
            d = self.root / name
            d.mkdir()
            (d / 'board-token.txt').write_text('synthetic-' + name)
            (d / 'remote.token').write_text('synthetic-' + ('b' if name == 'a' else 'a'))
            cfg = {'self': name, 'handoverRoot': str(d / 'handover'), 'peers': [
                {'id': 'b' if name == 'a' else 'a', 'host': '127.0.0.1',
                 'port': self.ports[1-i], 'tokenFile': 'remote.token'}]}
            (d / 'config.json').write_text(json.dumps(cfg))
        self.start()

    def start(self):
        for i, name in enumerate(('a', 'b')):
            self.procs.append(subprocess.Popen([sys.executable, __file__, 'worker', str(self.root / name), str(self.ports[i])], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        for port in self.ports:
            for _ in range(100):
                try:
                    request(port, '/api/self')
                    break
                except OSError:
                    time.sleep(.03)
            else:
                self.fail('假设备启动失败')

    def stop(self):
        for p in self.procs:
            p.terminate()
        for p in self.procs:
            p.wait(timeout=5)
        self.procs = []

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def test_bidirectional_and_fail_closed(self):
        for i, name in enumerate(('a', 'b')):
            other = 'b' if name == 'a' else 'a'
            port, token = self.ports[i], 'synthetic-' + name
            self.assertEqual(request(port, '/api/self')[0], 401)
            self.assertEqual(request(port, '/api/self', 'wrong')[0], 401)
            self.assertEqual(request(port, '/api/act', 'wrong', {'confirm': True})[0], 403)
            self.assertEqual(request(port, '/api/upload', 'wrong', b'x')[0], 403)
            status, data = request(port, '/api/file?device=' + other + '&rel=probe', token)
            self.assertEqual(status, 200)
            self.assertNotIn('error', data)
            status, data = request(port, '/api/act', token, {'device': other, 'confirm': True})
            self.assertEqual((status, data.get('error')), (200, '缺少对话'))
            status, data = request(port, '/api/upload?device=' + other, token, b'synthetic attachment', {'X-File-Name': 'fake.txt'})
            self.assertEqual(status, 200)
            self.assertTrue(data.get('ok'))
            self.assertEqual((self.root / other / 'handover' / data['rel']).read_bytes(), b'synthetic attachment')
        remote = self.root / 'a' / 'remote.token'
        remote.write_text('wrong')
        self.assertIn('error', request(self.ports[0], '/api/file?device=b&rel=probe', 'synthetic-a')[1])
        self.assertEqual(request(self.ports[0], '/api/upload?device=b', 'synthetic-a', b'x')[0], 502)
        remote.write_text('  ')
        self.assertIn('检查目标电脑口令', request(self.ports[0], '/api/file?device=b&rel=probe', 'synthetic-a')[1]['error'])
        remote.unlink()
        self.assertIn('检查目标电脑口令', request(self.ports[0], '/api/file?device=b&rel=probe', 'synthetic-a')[1]['error'])
        self.assertEqual(request(self.ports[0], '/api/upload?device=b', 'synthetic-a', b'x')[0], 502)

    def test_old_shared_token_config(self):
        self.stop()
        for name in ('a', 'b'):
            d = self.root / name
            (d / 'board-token.txt').write_text('synthetic-shared')
            cfg = json.loads((d / 'config.json').read_text())
            del cfg['peers'][0]['tokenFile']
            (d / 'config.json').write_text(json.dumps(cfg))
        self.start()
        for i, other in enumerate(('b', 'a')):
            self.assertNotIn('error', request(self.ports[i], '/api/file?device=' + other + '&rel=probe', 'synthetic-shared')[1])
            status, data = request(self.ports[i], '/api/upload?device=' + other, 'synthetic-shared', b'old config', {'X-File-Name': 'legacy.txt'})
            self.assertEqual(status, 200)
            self.assertTrue(data.get('ok'))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
