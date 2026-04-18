#!/usr/bin/env python3
"""mp4_rtsp - publish an MP4 as RTSP, with one-shot file injection over HTTP.

Usage:
    # Start the server (loops main.mp4 forever on rtsp://<host>:18554/live)
    ./mp4_rtsp.py serve main.mp4

    # From any other shell - inject a clip (plays once, then main resumes)
    ./mp4_rtsp.py inject /abs/path/fedex.mp4
    ./mp4_rtsp.py inject /abs/path/trash.mp4

    # Or just hit the HTTP control endpoint directly
    curl -X POST http://127.0.0.1:18555/inject -d 'path=/abs/path/clip.mp4'
    curl http://127.0.0.1:18555/status

Architecture:
    Embeds mediamtx (auto-downloaded on first run to ~/.cache/mp4_rtsp/) as
    the RTSP server. ffmpeg publishes the main MP4 in a loop. A built-in
    HTTP control server on :18555 accepts POST /inject?path=... to swap the
    current ffmpeg phase: inject plays once, then main loop resumes.

    Defaults are 18554/18555 (high range) so they don't collide with
    Frigate's go2rtc on 8554 on a typical Frigate host.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from queue import Empty, Queue

DEFAULT_FFMPEG = "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
MEDIAMTX_VERSION = "1.9.3"
ARCH_MAP = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64v8",
    "arm64": "arm64v8",
    "armv7l": "armv7",
    "armv6l": "armv6",
}


# ---------- mediamtx bootstrap ----------

def download_mediamtx(dest_dir: Path) -> Path:
    sys_name = platform.system().lower()
    if sys_name != "linux":
        sys.exit(f"mediamtx auto-download only supports Linux (got {sys_name}); "
                 "pass --mediamtx with a prebuilt binary")
    arch = ARCH_MAP.get(platform.machine().lower())
    if not arch:
        sys.exit(f"unsupported arch for mediamtx auto-download: {platform.machine()}")

    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = f"mediamtx_v{MEDIAMTX_VERSION}_linux_{arch}.tar.gz"
    url = f"https://github.com/bluenviron/mediamtx/releases/download/v{MEDIAMTX_VERSION}/{fname}"
    archive = dest_dir / fname
    print(f"[mp4_rtsp] downloading mediamtx: {url}")
    urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:gz") as tf:
        tf.extract("mediamtx", dest_dir)
    binary = dest_dir / "mediamtx"
    binary.chmod(0o755)
    try:
        archive.unlink()
    except OSError:
        pass
    return binary


def ensure_mediamtx(explicit: str | None) -> str:
    if explicit:
        if not os.path.isfile(explicit) or not os.access(explicit, os.X_OK):
            sys.exit(f"mediamtx not found or not executable: {explicit}")
        return explicit
    found = shutil.which("mediamtx")
    if found:
        return found
    cache = Path.home() / ".cache" / "mp4_rtsp"
    cached = cache / "mediamtx"
    if cached.is_file() and os.access(cached, os.X_OK):
        return str(cached)
    return str(download_mediamtx(cache))


def write_mediamtx_config() -> str:
    # Disable non-RTSP protocols and allow publishing to any path via the
    # 'all_others' catch-all. Without this, mediamtx rejects publishes with
    # "path '...' is not configured".
    cfg = (
        "logLevel: info\n"
        "rtmp: no\n"
        "hls: no\n"
        "webrtc: no\n"
        "srt: no\n"
        "paths:\n"
        "  all_others:\n"
    )
    fd, path = tempfile.mkstemp(prefix="mp4_rtsp_", suffix=".yml")
    with os.fdopen(fd, "w") as f:
        f.write(cfg)
    return path


def start_mediamtx(binary: str, rtsp_port: int, cfg_path: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["MTX_RTSPADDRESS"] = f":{rtsp_port}"
    print(f"[mp4_rtsp] starting mediamtx on :{rtsp_port}  (config: {cfg_path})")
    return subprocess.Popen([binary, cfg_path], env=env)


def wait_for_port(host: str, port: int, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def port_already_bound(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
    except OSError:
        return True
    finally:
        s.close()
    return False


# ---------- ffmpeg publisher ----------

def build_ffmpeg_cmd(ffmpeg: str, src: str, publish_url: str, loop: bool) -> list[str]:
    # mediamtx expects SPS/PPS via SDP only; avoid -bsf:v dump_extra and
    # extra muxer tweaks (otherwise it returns 400 on ANNOUNCE).
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-re",
        "-stream_loop", "-1" if loop else "0",
        "-fflags", "+genpts+discardcorrupt",
        "-i", src,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-avoid_negative_ts", "make_non_negative",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        publish_url,
    ]


def run_phase(ffmpeg: str, src: str, publish_url: str, loop: bool,
              cmd_q: Queue, stop: threading.Event,
              state: dict) -> str | None:
    state["current"] = src
    state["loop"] = loop
    state["since"] = time.time()
    print(f"[mp4_rtsp] {'loop' if loop else 'once'}: {src}")
    proc = subprocess.Popen(build_ffmpeg_cmd(ffmpeg, src, publish_url, loop))
    inject: str | None = None
    try:
        while proc.poll() is None:
            if stop.is_set():
                break
            try:
                cmd = cmd_q.get(timeout=0.5)
            except Empty:
                continue
            if not os.path.isfile(cmd):
                print(f"[mp4_rtsp] ignoring inject (not a file): {cmd}",
                      file=sys.stderr)
                continue
            inject = cmd
            print(f"[mp4_rtsp] inject requested: {inject}")
            break
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return inject


# ---------- HTTP control server ----------

def make_control_handler(cmd_q: Queue, state: dict, main_input: str,
                         rtsp_url: str):
    """Build a BaseHTTPRequestHandler subclass closing over runtime state."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet default access log
            pass

        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, code: int, msg: str) -> None:
            body = (msg + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                self._json(200, {
                    "rtsp_url": rtsp_url,
                    "main": main_input,
                    "current": state.get("current"),
                    "loop": state.get("loop", False),
                    "since": state.get("since"),
                    "queue_depth": cmd_q.qsize(),
                })
            elif self.path == "/":
                self._text(200,
                           "POST /inject?path=/abs/path/file.mp4\n"
                           "GET  /status\n")
            else:
                self._text(404, "not found")

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/inject":
                self._text(404, "not found")
                return
            # Accept ?path=... or form/JSON body
            path = urllib.parse.parse_qs(parsed.query).get("path", [None])[0]
            if not path:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                ctype = self.headers.get("Content-Type", "")
                if ctype.startswith("application/json"):
                    try:
                        path = json.loads(body or b"{}").get("path")
                    except json.JSONDecodeError:
                        self._text(400, "invalid JSON")
                        return
                else:
                    form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
                    path = form.get("path", [None])[0]
            if not path:
                self._text(400, "missing 'path'")
                return
            if not os.path.isabs(path):
                self._text(400, "path must be absolute")
                return
            if not os.path.isfile(path):
                self._text(404, f"not a file: {path}")
                return
            cmd_q.put(path)
            self._json(202, {"queued": path, "queue_depth": cmd_q.qsize()})

    return Handler


def start_control_server(host: str, port: int, cmd_q: Queue, state: dict,
                         main_input: str, rtsp_url: str) -> HTTPServer:
    Handler = make_control_handler(cmd_q, state, main_input, rtsp_url)
    httpd = HTTPServer((host, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True,
                     name="control-http").start()
    return httpd


# ---------- subcommands ----------

def cmd_serve(args) -> int:
    ffmpeg = args.ffmpeg or (
        DEFAULT_FFMPEG if os.path.exists(DEFAULT_FFMPEG) else shutil.which("ffmpeg")
    )
    if not ffmpeg or not os.path.exists(ffmpeg):
        sys.exit("ffmpeg binary not found")
    if not os.path.isfile(args.input):
        sys.exit(f"input file not found: {args.input}")

    if port_already_bound(args.port):
        sys.exit(
            f"port {args.port} is already in use. "
            f"Pick another with --port."
        )
    if port_already_bound(args.control_port):
        sys.exit(
            f"control port {args.control_port} is already in use; "
            f"pick another with --control-port."
        )

    mediamtx_bin = ensure_mediamtx(args.mediamtx)
    mediamtx_cfg = write_mediamtx_config()
    mproc = start_mediamtx(mediamtx_bin, args.port, mediamtx_cfg)
    if not wait_for_port("127.0.0.1", args.port, timeout=5.0):
        if mproc.poll() is None:
            mproc.terminate()
        sys.exit(f"mediamtx did not bind :{args.port} within 5s "
                 f"(rc={mproc.returncode})")
    if mproc.poll() is not None:
        sys.exit(f"mediamtx exited rc={mproc.returncode}")

    publish_url = f"rtsp://127.0.0.1:{args.port}/{args.path}"
    rtsp_url = f"rtsp://<host>:{args.port}/{args.path}"

    stop = threading.Event()
    cmd_q: Queue = Queue()
    state: dict = {"current": None, "loop": False, "since": None}

    httpd = start_control_server(args.control_bind, args.control_port,
                                 cmd_q, state, args.input, rtsp_url)

    def _stop(_sig, _frm):
        stop.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print(f"[mp4_rtsp] RTSP URL:  {rtsp_url}")
    print(f"[mp4_rtsp] main:      {args.input}")
    print(f"[mp4_rtsp] control:   http://{args.control_bind}:{args.control_port}/inject")
    print(f"[mp4_rtsp] inject:    ./mp4_rtsp.py inject /path/to/file.mp4")

    try:
        pending: str | None = None
        while not stop.is_set():
            if mproc.poll() is not None:
                print(f"[mp4_rtsp] mediamtx died rc={mproc.returncode}",
                      file=sys.stderr)
                break
            if pending:
                pending = run_phase(ffmpeg, pending, publish_url, loop=False,
                                    cmd_q=cmd_q, stop=stop, state=state)
            else:
                inject = run_phase(ffmpeg, args.input, publish_url, loop=True,
                                   cmd_q=cmd_q, stop=stop, state=state)
                if inject:
                    pending = inject
                elif not stop.is_set():
                    time.sleep(1)
    finally:
        print("[mp4_rtsp] shutting down")
        httpd.shutdown()
        if mproc.poll() is None:
            mproc.terminate()
            try:
                mproc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mproc.kill()
        try:
            os.unlink(mediamtx_cfg)
        except OSError:
            pass
    return 0


def cmd_inject(args) -> int:
    abs_path = os.path.abspath(args.path)
    if not os.path.isfile(abs_path):
        sys.exit(f"not a file: {abs_path}")
    url = f"http://{args.control_host}:{args.control_port}/inject"
    data = urllib.parse.urlencode({"path": abs_path}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"[inject] {resp.status} {resp.read().decode().strip()}")
            return 0 if resp.status < 400 else 1
    except urllib.error.HTTPError as e:
        print(f"[inject] HTTP {e.code}: {e.read().decode().strip()}",
              file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"[inject] cannot reach {url}: {e.reason}", file=sys.stderr)
        return 1


def cmd_status(args) -> int:
    url = f"http://{args.control_host}:{args.control_port}/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            print(json.dumps(json.loads(resp.read()), indent=2))
            return 0
    except urllib.error.URLError as e:
        print(f"[status] cannot reach {url}: {e.reason}", file=sys.stderr)
        return 1


# ---------- entry ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Start RTSP server + control HTTP")
    s.add_argument("input", help="Main MP4 file (looped forever)")
    s.add_argument("--port", type=int, default=18554,
                   help="RTSP port (default 18554; chosen high to dodge "
                        "go2rtc's 8554 on Frigate hosts)")
    s.add_argument("--path", default="live", help="RTSP mount path (default 'live')")
    s.add_argument("--control-bind", default="127.0.0.1",
                   help="Control HTTP bind address (default 127.0.0.1)")
    s.add_argument("--control-port", type=int, default=18555,
                   help="Control HTTP port (default 18555; paired with RTSP+1)")
    s.add_argument("--ffmpeg", default=None, help="ffmpeg binary override")
    s.add_argument("--mediamtx", default=None,
                   help="Path to mediamtx binary (auto-download if unset)")
    s.set_defaults(func=cmd_serve)

    i = sub.add_parser("inject", help="Inject an MP4 into a running server")
    i.add_argument("path", help="Path to MP4 (will be made absolute)")
    i.add_argument("--control-host", default="127.0.0.1")
    i.add_argument("--control-port", type=int, default=18555)
    i.set_defaults(func=cmd_inject)

    st = sub.add_parser("status", help="Print server status JSON")
    st.add_argument("--control-host", default="127.0.0.1")
    st.add_argument("--control-port", type=int, default=18555)
    st.set_defaults(func=cmd_status)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
