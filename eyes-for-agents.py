#!/usr/bin/env python3
"""eyes-for-agents - give your AI agents eyes on your Frigate camera events.

Embeds a tiny MQTT broker so Frigate can publish events directly to this
script. For each finalized event we pull the clip, sample frames, ask a
local Ollama vision model what's happening, and write per-event Markdown.

Architecture:
    [Frigate] --MQTT--> [embedded amqtt broker] --> [paho subscriber]
                                                          |
                                                          v
                                             /api/events/<id>/clip.mp4
                                                          |
                                                          v
                                                    ffmpeg frames
                                                          |
                                                          v
                                                  Ollama /api/chat
                                                          |
                                                          v
                                              <out-dir>/<event_id>.md

Required Frigate config (config.yml on the device):

    mqtt:
      enabled: true
      host: <ip-of-this-script>   # 127.0.0.1 if same container, else host IP
      port: 1883

Example:
    pip install amqtt paho-mqtt requests
    ./eyes-for-agents.py \\
        --frigate-url http://127.0.0.1:5000 \\
        --ollama-url  http://127.0.0.1:11434 \\
        --model       gemma4:e2b \\
        --out-dir     ./events
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Full, Queue

import paho.mqtt.client as mqtt
import requests
from amqtt.broker import Broker

DEFAULT_FFMPEG = "/usr/lib/ffmpeg/7.0/bin/ffmpeg"
DEFAULT_FFPROBE = "/usr/lib/ffmpeg/7.0/bin/ffprobe"

# Intentionally broad: catch as much as possible by default. Users with a
# specific use case (e.g. only flag people not wearing high-vis vests) can
# narrow this via --prompt or --prompt-file.
DEFAULT_PROMPT = "Describe what you see on these images"


# ---------- Embedded MQTT broker ----------

def start_broker(bind_host: str, bind_port: int) -> threading.Thread:
    """Start an amqtt broker in a background thread on its own asyncio loop.

    Frigate (or any other publisher) connects to bind_host:bind_port.
    Anonymous (no auth) - intended for local-only use.

    The broker MUST be constructed inside a running loop (newer amqtt
    versions call asyncio.get_running_loop() in __init__), so we run a
    small async coroutine that builds it, starts it, then sleeps forever
    to keep the loop alive.
    """
    ready = threading.Event()
    error: dict = {}

    async def _run_broker() -> None:
        # amqtt schema is strict in newer versions. Minimum viable config:
        # a single TCP listener. Anonymous is the default; the new plugins
        # mechanism replaces the old 'auth'/'topic-check' keys.
        config = {
            "listeners": {
                "default": {
                    "type": "tcp",
                    "bind": f"{bind_host}:{bind_port}",
                },
            },
        }
        broker = Broker(config)
        await broker.start()
        ready.set()
        # Keep the loop alive
        while True:
            await asyncio.sleep(3600)

    def runner() -> None:
        try:
            asyncio.run(_run_broker())
        except Exception as e:  # noqa: BLE001
            error["err"] = e
            ready.set()

    t = threading.Thread(target=runner, daemon=True, name="mqtt-broker")
    t.start()
    if not ready.wait(timeout=10):
        raise RuntimeError("broker did not start within 10s")
    if "err" in error:
        raise RuntimeError(f"broker failed: {error['err']}")
    return t


# ---------- MCP server (lets agents query events over the network) ----------

def _parse_md_meta(text: str) -> dict:
    """Pull camera/label/score/duration from the bullet block at the top of an event md."""
    meta: dict = {}
    for line in text.splitlines():
        if line.startswith("- **camera:**"):
            meta["camera"] = line.split("**camera:**", 1)[1].strip()
        elif line.startswith("- **label:**"):
            meta["label"] = line.split("**label:**", 1)[1].strip()
        elif line.startswith("- **score:**"):
            try:
                meta["score"] = float(line.split("**score:**", 1)[1].strip())
            except ValueError:
                pass
        elif line.startswith("- **duration:**"):
            d = line.split("**duration:**", 1)[1].strip().rstrip("s")
            try:
                meta["duration_s"] = float(d)
            except ValueError:
                pass
        elif line.startswith("- **start:**"):
            meta["start"] = line.split("**start:**", 1)[1].strip()
    return meta


def _short_analysis(text: str, max_chars: int = 200) -> str:
    if "## Analysis" in text:
        body = text.split("## Analysis", 1)[1].strip()
    else:
        body = text
    return body[:max_chars] + ("..." if len(body) > max_chars else "")


def _grep_snippet(text: str, q: str, ctx: int = 120) -> str:
    idx = text.lower().find(q.lower())
    if idx < 0:
        return ""
    start = max(0, idx - ctx)
    end = min(len(text), idx + len(q) + ctx)
    snip = text[start:end].strip()
    return ("..." if start > 0 else "") + snip + ("..." if end < len(text) else "")


def _ts_from_filename(p: Path, fallback_mtime: bool = True) -> float:
    """Frigate event ids look like '<unixtime>.<frac>-<id>'. Fall back to mtime."""
    try:
        return float(p.stem.split("-")[0])
    except (ValueError, IndexError):
        return p.stat().st_mtime if fallback_mtime else 0.0


def start_mcp_server(host: str, port: int, events_dir: Path) -> threading.Thread:
    """Run a FastMCP server in a background thread.

    Exposes camera-event tools to any MCP client (e.g. OpenClaw on a sibling
    container reaching us at http://<frigate_uuid>:<port>/mcp).
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("eyes-for-agents", host=host, port=port)

    @mcp.tool()
    def list_events(since_minutes: int = 60, limit: int = 50,
                    camera: str | None = None) -> list[dict]:
        """List recent Frigate camera events, newest first.

        Each entry has id, time, camera, label, duration_s, and a short
        summary (first ~200 chars of the LLM analysis). Use get_event(id)
        to read the full markdown report for any specific event.
        """
        cutoff = time.time() - since_minutes * 60
        rows = []
        files = sorted(events_dir.glob("*.md"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        for p in files:
            ts = _ts_from_filename(p)
            if ts < cutoff:
                continue
            try:
                text = p.read_text()
            except OSError:
                continue
            meta = _parse_md_meta(text)
            if camera and meta.get("camera", "").lower() != camera.lower():
                continue
            rows.append({
                "id": p.stem,
                "time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "camera": meta.get("camera", "?"),
                "label": meta.get("label", "?"),
                "duration_s": meta.get("duration_s"),
                "summary": _short_analysis(text, max_chars=200),
            })
            if len(rows) >= limit:
                break
        return rows

    @mcp.tool()
    def get_event(event_id: str) -> dict:
        """Return the full event report (metadata + LLM analysis markdown)
        for the given event id (as returned by list_events)."""
        p = events_dir / f"{event_id}.md"
        if not p.exists():
            return {"error": f"event not found: {event_id}"}
        text = p.read_text()
        meta = _parse_md_meta(text)
        meta["id"] = event_id
        meta["markdown"] = text
        return meta

    @mcp.tool()
    def search_events(query: str, limit: int = 20) -> list[dict]:
        """Case-insensitive substring search across all event reports.

        Returns matching events newest-first, with a short snippet around
        the first match in each report.
        """
        results = []
        files = sorted(events_dir.glob("*.md"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        for p in files:
            try:
                text = p.read_text()
            except OSError:
                continue
            if query.lower() not in text.lower():
                continue
            ts = _ts_from_filename(p)
            results.append({
                "id": p.stem,
                "time": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                "snippet": _grep_snippet(text, query),
            })
            if len(results) >= limit:
                break
        return results

    def runner():
        try:
            # streamable-http is the modern MCP HTTP transport. Older clients
            # may need "sse" - swap if compatibility matters.
            mcp.run(transport="streamable-http")
        except Exception as e:  # noqa: BLE001
            print(f"[mcp] server error: {e}", file=sys.stderr)

    t = threading.Thread(target=runner, daemon=True, name="mcp-server")
    t.start()
    return t


# ---------- Frigate ----------

def list_events(frigate_url: str, limit: int = 25,
                after: float | None = None) -> list[dict]:
    url = f"{frigate_url.rstrip('/')}/api/events"
    params: dict = {"limit": limit, "include_thumbnails": 0, "has_clip": 1}
    if after is not None:
        params["after"] = after
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def download_clip(frigate_url: str, event_id: str, dest: Path,
                  tries: int = 8, delay: float = 3.0) -> None:
    """Download a finalized clip, retrying while Frigate is still writing it."""
    url = f"{frigate_url.rstrip('/')}/api/events/{event_id}/clip.mp4"
    last = "no attempts"
    for attempt in range(1, tries + 1):
        try:
            r = requests.get(url, timeout=60)
            if r.status_code == 200 and r.content:
                dest.write_bytes(r.content)
                return
            last = f"status={r.status_code} size={len(r.content)}"
        except requests.RequestException as e:
            last = str(e)
        if attempt < tries:
            time.sleep(delay)
    raise RuntimeError(f"clip.mp4 not ready after {tries} tries: {last}")


# ---------- Frame extraction ----------

def clip_duration(clip_path: Path, ffprobe: str) -> float:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
        capture_output=True, text=True, check=True,
    )
    return float(r.stdout.strip() or 0)


def extract_frames(clip_path: Path, count: int, ffmpeg: str,
                   ffprobe: str, max_dim: int = 0) -> list[Path]:
    """Return paths to `count` evenly-spaced PNG frames from the clip."""
    dur = clip_duration(clip_path, ffprobe)
    if dur <= 0:
        raise RuntimeError(f"clip has zero duration: {clip_path}")
    out_dir = Path(tempfile.mkdtemp(prefix="frigate-frames-"))
    paths: list[Path] = []
    scale_arg = (
        f"scale=w='min({max_dim},iw)':h='min({max_dim},ih)':"
        f"force_original_aspect_ratio=decrease:flags=bicubic"
        if max_dim > 0 else None
    )
    # Sample at (i + 0.5)/count so we skip the very first/last frame.
    for i in range(count):
        t = dur * (i + 0.5) / count
        out = out_dir / f"frame_{i:02d}.png"
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
               "-ss", f"{t:.3f}", "-i", str(clip_path),
               "-frames:v", "1", "-q:v", "2"]
        if scale_arg:
            cmd += ["-vf", scale_arg]
        cmd += ["-y", str(out)]
        subprocess.run(cmd, check=True)
        paths.append(out)
    return paths


# ---------- Ollama ----------

def ask_ollama(ollama_url: str, model: str, prompt: str,
               image_paths: list[Path], timeout: float = 180) -> str:
    images_b64 = [base64.b64encode(p.read_bytes()).decode() for p in image_paths]
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "user", "content": prompt, "images": images_b64},
        ],
    }
    r = requests.post(f"{ollama_url.rstrip('/')}/api/chat",
                      json=payload, timeout=timeout)
    if not r.ok:
        raise RuntimeError(
            f"Ollama {r.status_code} on /api/chat for model '{model}': "
            f"{r.text[:500]}"
        )
    return (r.json().get("message") or {}).get("content", "").strip()


# ---------- Output ----------

def write_markdown(out_dir: Path, event: dict, clip_path: Path,
                   frame_paths: list[Path], answer: str, model: str,
                   prompt: str) -> Path:
    event_id = event.get("id", "unknown")
    start = event.get("start_time")
    start_iso = (datetime.fromtimestamp(start, tz=timezone.utc).isoformat()
                 if start else "?")
    md = out_dir / f"{event_id}.md"
    body = (
        f"# Frigate event `{event_id}`\n\n"
        f"- **camera:** {event.get('camera', '?')}\n"
        f"- **label:** {event.get('label', '?')}\n"
        f"- **score:** {event.get('top_score') or event.get('score') or 0:.2f}\n"
        f"- **start:** {start_iso}\n"
        f"- **duration:** {((event.get('end_time') or 0) - (start or 0)):.1f}s\n"
        f"- **zones:** {', '.join(event.get('entered_zones') or []) or '-'}\n"
        f"- **model:** `{model}`\n"
        f"- **clip:** [{clip_path.name}]({clip_path.name})\n"
        f"- **frames:** {len(frame_paths)} "
        f"({', '.join(p.name for p in frame_paths)})\n\n"
        f"## Prompt\n\n"
        f"> {prompt}\n\n"
        f"## Analysis\n\n"
        f"{answer or '(empty response)'}\n"
    )
    md.write_text(body)
    return md


# ---------- Event processing ----------

def process_event(event: dict, args) -> None:
    event_id = event["id"]
    cam = event.get("camera", "?")
    label = event.get("label", "?")
    score = event.get("top_score") or event.get("score") or 0
    start = event.get("start_time") or 0
    end = event.get("end_time") or 0
    duration = end - start if end and start else 0
    print(f"\n[event] {event_id}  {cam}/{label}  score={score:.2f}  "
          f"dur={duration:.0f}s")

    if args.max_event_duration and duration > args.max_event_duration:
        print(f"[event] SKIP: duration {duration:.0f}s exceeds "
              f"--max-event-duration {args.max_event_duration:.0f}s")
        return

    out_dir = Path(args.out_dir)
    clip_path = out_dir / f"{event_id}.mp4"
    frames_dir: Path | None = None
    try:
        download_clip(args.frigate_url, event_id, clip_path)
        print(f"[event] clip: {clip_path.stat().st_size // 1024} KB  ({clip_path})")

        frames = extract_frames(clip_path, args.frames, args.ffmpeg,
                                args.ffprobe, max_dim=args.frame_max_dim)
        frames_dir = frames[0].parent if frames else None
        total_kb = sum(p.stat().st_size for p in frames) // 1024
        print(f"[event] frames: {len(frames)}  ({total_kb} KB total)")

        t0 = time.time()
        answer = ask_ollama(args.ollama_url, args.model, args.prompt, frames)
        print(f"[event] model ({time.time() - t0:.1f}s) says:\n{answer}\n")

        md = write_markdown(out_dir, event, clip_path,
                            frames, answer, args.model, args.prompt)
        print(f"[event] wrote {md}")
    except Exception as e:
        print(f"[event] FAILED: {e}", file=sys.stderr)
    finally:
        if frames_dir and not args.keep_frames:
            shutil.rmtree(frames_dir, ignore_errors=True)
        if not args.keep_clips and clip_path.exists():
            clip_path.unlink(missing_ok=True)


# ---------- MQTT subscriber ----------

def setup_subscriber(broker_host: str, broker_port: int, event_q: Queue,
                     seen: set[str]) -> mqtt.Client:
    """Connect a paho client to the embedded broker and queue end-events."""
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                         client_id="eyes-for-agents")

    def on_connect(cl, _userdata, _flags, reason_code, _props):
        print(f"[mqtt] connected to {broker_host}:{broker_port} "
              f"(rc={reason_code})")
        cl.subscribe("frigate/events", qos=1)

    def on_disconnect(_cl, _userdata, _flags, reason_code, _props):
        print(f"[mqtt] disconnected (rc={reason_code}), will auto-reconnect")

    def on_message(_cl, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            print(f"[mqtt] bad payload on {msg.topic}", file=sys.stderr)
            return
        if payload.get("type") != "end":
            return
        event = payload.get("after") or payload.get("before") or {}
        eid = event.get("id")
        if not eid or eid in seen:
            return
        try:
            event_q.put_nowait(event)
        except Full:
            print(f"[mqtt] queue full, dropping event {eid}", file=sys.stderr)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    client.connect(broker_host, broker_port, keepalive=60)
    client.loop_start()
    return client


# ---------- Main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--frigate-url", default="http://127.0.0.1:5000")
    ap.add_argument("--ollama-url", default="http://ollama:11434",
                    help="Ollama API URL. Default 'http://ollama:11434' "
                         "matches the typical container-network setup; "
                         "use 'http://127.0.0.1:11434' for ollama on the host.")
    ap.add_argument("--model", default="gemma4:e2b",
                    help="Ollama model tag (must be a vision-capable model "
                         "pulled on the target host)")
    ap.add_argument("--out-dir", default="./events",
                    help="Directory for per-event markdown files")
    ap.add_argument("--mqtt-bind", default="0.0.0.0",
                    help="MQTT broker bind address (Frigate connects here)")
    ap.add_argument("--mqtt-port", type=int, default=1883,
                    help="MQTT broker port")
    ap.add_argument("--mcp-bind", default="0.0.0.0",
                    help="MCP server bind address (other agents like OpenClaw "
                         "connect here over the container network)")
    ap.add_argument("--mcp-port", type=int, default=1884,
                    help="MCP server port. 0 disables the MCP server.")
    ap.add_argument("--frames", type=int, default=10,
                    help="Number of frames to sample evenly across the clip")
    ap.add_argument("--frame-max-dim", type=int, default=640,
                    help="Downscale frames so the longer side is at most this "
                         "many pixels (0 disables). Default 640.")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT,
                    help="Prompt sent to the model with the frames")
    ap.add_argument("--prompt-file", default=None,
                    help="Read prompt from a file (overrides --prompt)")
    ap.add_argument("--backfill-minutes", type=float, default=10.0,
                    help="On startup, process existing events whose end_time "
                         "is within the last N minutes (HTTP one-shot; MQTT "
                         "only delivers live events). 0 = skip backfill.")
    ap.add_argument("--max-event-duration", type=float, default=120.0,
                    help="Skip events longer than this many seconds. "
                         "Default 120s. 0 = no limit.")
    ap.add_argument("--queue-size", type=int, default=100,
                    help="Max events buffered while LLM is busy")
    ap.add_argument("--no-keep-clips", dest="keep_clips",
                    action="store_false", default=True,
                    help="Delete each clip after analysis "
                         "(default: keep <event_id>.mp4 next to the .md)")
    ap.add_argument("--keep-frames", action="store_true",
                    help="Keep extracted PNG frames")
    ap.add_argument("--ffmpeg", default=(
        DEFAULT_FFMPEG if os.path.exists(DEFAULT_FFMPEG) else "ffmpeg"))
    ap.add_argument("--ffprobe", default=(
        DEFAULT_FFPROBE if os.path.exists(DEFAULT_FFPROBE) else "ffprobe"))
    ap.add_argument("--debug-amqtt", action="store_true",
                    help="Show amqtt's debug logs (very chatty)")
    args = ap.parse_args()

    if args.prompt_file:
        args.prompt = Path(args.prompt_file).read_text().strip()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.debug_amqtt:
        for name in ("amqtt", "amqtt.broker", "amqtt.mqtt.protocol",
                     "transitions.core"):
            logging.getLogger(name).setLevel(logging.WARNING)

    print(f"[broker]  starting MQTT broker on {args.mqtt_bind}:{args.mqtt_port}")
    start_broker(args.mqtt_bind, args.mqtt_port)
    print(f"[frigate] expects MQTT at: host=<this-host> port={args.mqtt_port}")
    print(f"[ollama]  {args.ollama_url}  model={args.model}")
    print(f"[out]     {out_dir.resolve()}")

    if args.mcp_port:
        print(f"[mcp]     starting MCP server on {args.mcp_bind}:{args.mcp_port} "
              f"(http://<host>:{args.mcp_port}/mcp)")
        try:
            start_mcp_server(args.mcp_bind, args.mcp_port, out_dir)
        except Exception as e:  # noqa: BLE001
            print(f"[mcp]     failed to start: {e}", file=sys.stderr)

    seen: set[str] = set()
    event_q: Queue = Queue(maxsize=args.queue_size)

    # Subscribe to our own broker on loopback (broker is in-process)
    setup_subscriber("127.0.0.1", args.mqtt_port, event_q, seen)

    stopping = threading.Event()

    def _stop(_sig, _frm):
        stopping.set()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # Backfill: HTTP one-shot for events that ended while we were down
    if args.backfill_minutes > 0:
        cutoff = time.time() - args.backfill_minutes * 60
        try:
            existing = list_events(args.frigate_url, limit=100, after=cutoff)
            existing.sort(key=lambda e: e.get("start_time") or 0)
            print(f"[backfill] {len(existing)} event(s) from last "
                  f"{args.backfill_minutes:.0f} min")
            for e in existing:
                if e.get("end_time") is None:
                    continue
                process_event(e, args)
                seen.add(e["id"])
        except Exception as e:
            print(f"[backfill] failed: {e}", file=sys.stderr)

    print("[main]    waiting for live events via MQTT...")
    while not stopping.is_set():
        try:
            event = event_q.get(timeout=0.5)
        except Empty:
            continue
        eid = event.get("id")
        if not eid or eid in seen:
            continue
        process_event(event, args)
        seen.add(eid)
        if len(seen) > 10000:
            seen.clear()

    print("\n[main] shutting down")


if __name__ == "__main__":
    main()
