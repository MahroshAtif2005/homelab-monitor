"""Tests for the container-logs SSE stream (app._docker_log_stream) against a
fake Docker daemon on a real unix socket: HTTP/1.0 handshake, 8-byte stream
demux, TTY raw mode, error surfacing, and the heartbeat regression — a followed
container going quiet used to kill the stream with a werkzeug traceback
("OSError: cannot read from timed out object") because http.client's chunked
decoder cannot resume after a socket timeout."""
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app

OK_HEAD = (b"HTTP/1.0 200 OK\r\n"
           b"Content-Type: application/vnd.docker.multiplexed-stream\r\n\r\n")


def _frame(payload, stream=1):
    """Docker's 8-byte multiplex framing: stream id + 3 zero bytes + BE length."""
    return bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "unix sockets required")
class TestDockerLogStream(unittest.TestCase):
    """Each test scripts the fake daemon's reply as a list of bytes-to-send and
    float seconds-to-sleep, then consumes the generator to completion."""

    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        self.sock_path = os.path.join(d, "docker.sock")
        p = mock.patch.object(app, "DOCKER_SOCK", self.sock_path)
        p.start()
        self.addCleanup(p.stop)

    def _serve(self, script):
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.sock_path)
        srv.listen(1)
        self.requests = []

        def run():
            conn, _ = srv.accept()
            req = b""
            while b"\r\n\r\n" not in req:
                d = conn.recv(4096)
                if not d:
                    break
                req += d
            self.requests.append(req)
            for item in script:
                if isinstance(item, (int, float)):
                    time.sleep(item)
                else:
                    conn.sendall(item)
            conn.close()
            srv.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)

    def test_tail_demux_and_end_event(self):
        self._serve([OK_HEAD, _frame(b"hello\n") + _frame(b"world\n", stream=2)])
        out = list(app._docker_log_stream("web", "200", 0))
        self.assertEqual(out, ["data: hello\n\n", "data: world\n\n",
                               "event: end\ndata: done\n\n"])
        # The request must be HTTP/1.0 — that's what keeps the reply un-chunked.
        self.assertIn(b"HTTP/1.0\r\n", self.requests[0])
        self.assertIn(b"/containers/web/logs?", self.requests[0])

    def test_tty_container_streams_raw(self):
        # A TTY container has no framing: first byte is printable, pass through.
        self._serve([OK_HEAD, b"plain line one\nplain line two\n"])
        out = list(app._docker_log_stream("tty", "50", 0))
        self.assertEqual(out[:2], ["data: plain line one\n\n", "data: plain line two\n\n"])

    def test_error_status_becomes_srverror_event(self):
        self._serve([b"HTTP/1.0 404 No such container: web\r\n\r\n"])
        out = list(app._docker_log_stream("web", "200", 0))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("event: srverror\ndata: 404"))

    def test_quiet_follow_heartbeats_then_resumes(self):
        # THE regression: follow a container that goes quiet longer than the
        # heartbeat timeout, then speaks again. The old http.client version raised
        # "cannot read from timed out object" on the read after the first timeout;
        # now the quiet spell must yield keep-alive comments and the stream must
        # deliver the later line and a clean end.
        self._serve([OK_HEAD, _frame(b"before quiet\n"), 0.5, _frame(b"after quiet\n")])
        out = list(app._docker_log_stream("web", "200", 1, timeout=0.1))
        self.assertIn("data: before quiet\n\n", out)
        self.assertIn(": keep-alive\n\n", out)
        self.assertIn("data: after quiet\n\n", out)
        self.assertEqual(out[-1], "event: end\ndata: done\n\n")
        # And no event may be an error.
        self.assertFalse(any(e.startswith("event: srverror") for e in out))

    def test_frame_split_across_reads(self):
        # A frame torn across TCP segments must reassemble (send header+payload in
        # separate writes with a pause so they arrive as separate recv()s).
        payload = b"x" * 6000 + b"\n"
        f = _frame(payload)
        self._serve([OK_HEAD, f[:10], 0.05, f[10:]])
        out = list(app._docker_log_stream("web", "200", 0))
        self.assertEqual(out[0], "data: " + "x" * 6000 + "\n\n")

    def test_unreachable_socket_yields_srverror(self):
        # No daemon listening at DOCKER_SOCK → a single srverror event, no raise.
        out = list(app._docker_log_stream("web", "200", 1))
        self.assertEqual(len(out), 1)
        self.assertTrue(out[0].startswith("event: srverror\ndata: "))


if __name__ == "__main__":
    unittest.main()
