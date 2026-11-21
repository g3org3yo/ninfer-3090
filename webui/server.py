#!/usr/bin/env python3
"""NInfer Studio - self-hosted chat + live metrics UI for a local ninfer server.

Standard library only (no venv, no pip). Proxies /v1/chat/completions with SSE
pass-through and turns ninfer's Prometheus /metrics into JSON for the dashboard.

Env:
  NINFER_URL      upstream engine base URL   (default http://127.0.0.1:8082)
  NINFER_API_KEY  optional bearer token forwarded upstream
  NINFER_UI_PORT  port of this UI            (default 8090)
"""
import json
import math
import os
import posixpath
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(ROOT)
UPSTREAM = os.environ.get("NINFER_URL", "http://127.0.0.1:8082").rstrip("/")
API_KEY = os.environ.get("NINFER_API_KEY", "")
PORT = int(os.environ.get("NINFER_UI_PORT", "8090"))
UPSTREAM_PORT = int(urllib.parse.urlparse(UPSTREAM).port or 8082)

LOGS_DIR = os.path.join(PROJECT, "logs")
MODELS_DIR = os.path.join(PROJECT, "models")
ENGINE_LOG = os.path.join(LOGS_DIR, "engine.log")
UI_LOG = os.path.join(LOGS_DIR, "ui.log")

# The engine appends one JSONL record per request (--request-log-jsonl). It is the only source that
# covers every client of the engine - Studio chat, Hermes, curl - so the Live tab's Recent requests
# table reads this file instead of watching its own browser traffic. Absolute path: the engine runs
# with cwd=PROJECT and argv is a list, so spaces are not a quoting risk.
REQUEST_LOG = os.path.join(LOGS_DIR, "studio-requests.jsonl")
# Written when the Studio itself spawns the engine, so /api/recent-requests can say honestly whether
# the engine now listening is the one that was told to write REQUEST_LOG.
ENGINE_MARKER = os.path.join(LOGS_DIR, "studio-engine.json")
RECENT_MAX = 60                # rows returned to the UI
RECENT_TAIL_BYTES = 1 << 20    # only the tail of the JSONL is ever parsed


# Flags the Studio uses when it starts the engine itself. This list is the only copy: the .bat
# launchers are thin wrappers around webui/launcher.py, which reads it from here.
ENGINE_FLAGS = [
    "--host", "127.0.0.1",
    "--port", str(UPSTREAM_PORT),
    "--spec", "mtp",
    "--draft-tokens", "3",
    "--prefill-cublas",
    "--prefill-chunk", "4096",
    # 65536 is the smallest window Hermes Agent accepts (it hard-refuses a model under 64K), and
    # bf16 KV cannot reach it on 24 GB (the pool would need ~6.7 GiB on top of 16.9 GiB of weights).
    # rk8v4 is what docs/cli.md calls the best all-round format: int8-level quality (+0.082 % ppl)
    # at 2.92 GiB instead of 3.42. 32768 bf16 made every long Hermes turn fail with
    # "prepared prompt exceeds Engine max_context 32768".
    "--max-context", "65536",
    "--kv-capacity", "65536",
    "--kv-dtype", "rk8v4",
    "--fit-context", "65536",       # refuse to start smaller instead of silently serving a window Hermes rejects
    "--vision",
    "--model-suspend",              # lets the UI give the VRAM back without exiting
    "--log-stats-interval-ms", "5000",
    # Engine-wide request history for the Live tab. Without it the table can only ever show what the
    # Studio's own browser saw, which is why Hermes' requests never appeared.
    "--request-log-jsonl", REQUEST_LOG,
]


def _find_engine():
    env = os.environ.get("NINFER_EXE")
    if env and os.path.isfile(env):
        return env
    dist = os.path.join(PROJECT, "dist")
    if os.path.isdir(dist):
        for name in sorted(os.listdir(dist), reverse=True):
            exe = os.path.join(dist, name, "ninfer-serve.exe")
            if os.path.isfile(exe):
                return exe
    return None


def _find_model():
    env = os.environ.get("NINFER_MODEL")
    if env and os.path.isfile(env):
        return env
    for name in ("Swift-1.5-Qwen3.8-27b.ninfer", "qwen3_8_27b.ninfer"):
        path = os.path.join(MODELS_DIR, name)
        if os.path.isfile(path):
            return path
    return None


ENGINE_EXE = _find_engine()
MODEL_PATH = _find_model()

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
    "/icons.svg": ("icons.svg", "image/svg+xml"),
}

_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".html": "text/html; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


# Prometheus lines we surface to the UI (base name -> short key)
SCALARS = {
    "ninfer_engine_ready": "ready",
    "ninfer_decode_tokens_total": "decode_tokens",
    "ninfer_decode_seconds_total": "decode_seconds",
    "ninfer_decode_rounds_total": "rounds",
    "ninfer_generation_tokens_total": "gen_tokens",
    "ninfer_prefill_tokens_total": "prefill_tokens",
    "ninfer_prefill_seconds_total": "prefill_seconds",
    "ninfer_gpu_memory_used_bytes": "vram_used",
    "ninfer_gpu_memory_total_bytes": "vram_total",
    "ninfer_gpu_power_watts": "power",
    "ninfer_gpu_temperature_celsius": "temp",
    "ninfer_gpu_utilization_percent": "gpu_util",
    "ninfer_host_kv_used_bytes": "kv_used",
    "ninfer_host_kv_capacity_bytes": "kv_total",
    "ninfer_device_kv_used_pages": "kv_pages",
    "ninfer_device_kv_capacity_pages": "kv_pages_total",
    "ninfer_host_context_used_bytes": "ctx_used",
    "ninfer_host_context_capacity_bytes": "ctx_total",
    "ninfer_spec_decode_draft_window": "draft_window",
}

LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([-+0-9.eE]+|NaN|\+Inf|-Inf)\s*$")


def _headers(extra=None):
    h = {"Accept": "application/json", "User-Agent": "ninfer-studio/1.0"}
    if API_KEY:
        h["Authorization"] = "Bearer " + API_KEY
    if extra:
        h.update(extra)
    return h


def fetch(path, timeout=4.0):
    """GET an upstream path, return (status, body_bytes)."""
    req = urllib.request.Request(UPSTREAM + path, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception as e:  # connection refused, timeout, ...
        return 0, str(e).encode()


_MODEL_CACHE = {"id": None}


def default_model():
    """ninfer requires an explicit model alias; discover the served one once."""
    if _MODEL_CACHE["id"]:
        return _MODEL_CACHE["id"]
    st, body = fetch("/v1/models", timeout=4.0)
    if st == 200:
        try:
            data = json.loads(body).get("data") or []
            if data and data[0].get("id"):
                _MODEL_CACHE["id"] = data[0]["id"]
                return _MODEL_CACHE["id"]
        except Exception:
            pass
    return "default"


# ---------------------------------------------------------------- GPU thermal
# Source of truth for temperatures is NVML (nvml.dll, ships with the driver):
# no subprocess per poll, no extra software. What a given card exposes differs
# by SKU, so every sensor is reported with an explicit availability flag -
# nothing is estimated or copied from another sensor.
_THERMAL_LOCK = threading.Lock()
_THERMAL_CACHE = {"at": 0.0, "data": None}
_NVML = None
_NVML_HANDLE = None
_NVML_ERROR = None

# NVML sensor ids / thresholds that are documented in nvml.h
NVML_TEMP_GPU = 0
NVML_TEMP_MEMORY = 1
NVML_THRESHOLDS = ((1, "slowdown"), (0, "shutdown"), (3, "max_operating"), (4, "acquire"))
NVML_RC = {0: "ok", 1: "uninitialized", 2: "not supported", 3: "no permission",
           4: "not found", 6: "invalid argument", 999: "unknown"}


def _nvml():
    """Load and initialise NVML once; remember why if it is unavailable."""
    global _NVML, _NVML_HANDLE, _NVML_ERROR
    if _NVML is not None or _NVML_ERROR:
        return _NVML
    try:
        import ctypes
        lib = ctypes.WinDLL("nvml.dll") if os.name == "nt" else ctypes.CDLL("libnvidia-ml.so.1")
        lib.nvmlInit_v2()
        handle = ctypes.c_void_p()
        rc = lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle))
        if rc != 0:
            raise OSError("nvmlDeviceGetHandleByIndex_v2 rc=%d" % rc)
        _NVML, _NVML_HANDLE = lib, handle
    except Exception as e:                                   # no NVIDIA driver / no GPU
        _NVML_ERROR = "%s: %s" % (type(e).__name__, e)
    return _NVML


def gpu_thermal():
    """Temperatures, fans and throttle limits, with per-sensor availability."""
    now = time.time()
    with _THERMAL_LOCK:
        if _THERMAL_CACHE["data"] and now - _THERMAL_CACHE["at"] < 2.0:
            return _THERMAL_CACHE["data"]

    import ctypes

    out = {
        "core_c": None, "memory_c": None, "hotspot_c": None, "board_c": None,
        "fan_pct": None, "fan_count": None, "limits": {},
        "available": {}, "reason": {}, "source": "nvml",
    }
    lib = _nvml()
    if not lib:
        out["source"] = "unavailable"
        for k in ("core_c", "memory_c", "hotspot_c", "board_c", "fan_pct"):
            out["available"][k] = False
        out["reason"]["core_c"] = "NVML not available (%s)" % (_NVML_ERROR or "unknown")
        out["reason"]["memory_c"] = out["reason"]["hotspot_c"] = \
            out["reason"]["board_c"] = out["reason"]["fan_pct"] = "NVML not available"
    else:
        def temp(sensor):
            v = ctypes.c_uint()
            rc = lib.nvmlDeviceGetTemperature(_NVML_HANDLE, sensor, ctypes.byref(v))
            return (v.value, "ok") if rc == 0 else (None, NVML_RC.get(rc, "rc=%d" % rc))

        value, why = temp(NVML_TEMP_GPU)
        out["core_c"], out["available"]["core_c"], out["reason"]["core_c"] = \
            value, value is not None, why

        value, why = temp(NVML_TEMP_MEMORY)
        out["memory_c"], out["available"]["memory_c"], out["reason"]["memory_c"] = \
            value, value is not None, (why if value is None else "ok")

        # NVML has no hot-spot or board sensor: there is no id to ask for it,
        # so it is reported as unsupported rather than borrowed from core.
        for key in ("hotspot_c", "board_c"):
            out["available"][key] = False
            out["reason"][key] = "not exposed by NVML"

        fan = ctypes.c_uint()
        rc = lib.nvmlDeviceGetFanSpeed(_NVML_HANDLE, ctypes.byref(fan))
        out["fan_pct"] = fan.value if rc == 0 else None
        out["available"]["fan_pct"] = rc == 0
        out["reason"]["fan_pct"] = "ok" if rc == 0 else NVML_RC.get(rc, "rc=%d" % rc)
        nfan = ctypes.c_uint()
        if lib.nvmlDeviceGetNumFans(_NVML_HANDLE, ctypes.byref(nfan)) == 0:
            out["fan_count"] = nfan.value

        for threshold, name in NVML_THRESHOLDS:
            v = ctypes.c_uint()
            if lib.nvmlDeviceGetTemperatureThreshold(_NVML_HANDLE, threshold, ctypes.byref(v)) == 0:
                out["limits"][name] = v.value

    with _THERMAL_LOCK:
        _THERMAL_CACHE["at"], _THERMAL_CACHE["data"] = now, out
    return out



def parse_metrics(text):
    out, hist = {}, {}
    backend = ""
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        name, labels, val = m.group(1), m.group(2) or "", m.group(3)
        key = name
        if name == "ninfer_model_info":
            m2 = re.search(r'speculative_backend="([^"]*)"', m.group(2) or "")
            if m2:
                backend = m2.group(1)
            continue
        if key.endswith("_bucket") or key.endswith("_sum") or key.endswith("_count"):
            hist.setdefault(key, 0.0)
            continue
        try:
            v = float(val)
        except ValueError:
            continue
        if name in out:
            out[name] += v
        else:
            out[name] = v
    flat = {}
    for name, k in SCALARS.items():
        if name in out:
            flat[k] = out[name]
    flat["_raw_count"] = len(out)
    flat["_histograms"] = len(hist)
    if backend:
        flat["spec_backend"] = backend
    return flat


def _post_upstream(path, payload=None, timeout=60.0):
    """POST JSON to the engine, return (status, body_bytes)."""
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        UPSTREAM + path,
        data=data,
        headers=_headers({"Content-Type": "application/json"}),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except Exception as e:
        return 0, str(e).encode()


def pids_on_port(port):
    """PIDs listening on a TCP port (Windows netstat)."""
    found = []
    try:
        res = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].endswith(":%d" % port):
                    pid = parts[4]
                    if pid.isdigit() and int(pid) > 0 and pid not in found:
                        found.append(pid)
    except Exception:
        pass
    return found


def engine_pid():
    return pids_on_port(UPSTREAM_PORT)


# ── engine-wide request history (Live tab) ────────────────────────────────────────────────────
# Source of truth is the engine's own JSONL log, not the browser: that is what makes requests from
# Hermes or any other OpenAI client appear next to the Studio's own.
_FINISH_STATUS = {
    "stop_token": "done", "stop_string": "done", "none": "done", "unknown": "done",
    "output_limit": "max", "context_capacity": "context", "cancelled": "stopped",
}
_recent_cache = {"key": None, "payload": None}
_recent_lock = threading.Lock()
_live_cache = {"at": 0.0, "live": False}


def _number(value):
    """float if value is a real, finite, non-negative number, else None (never a stand-in)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out < 0.0:
        return None
    return out


def _rate(tokens, seconds):
    """tokens / seconds, only when both are measured and positive."""
    if tokens is None or seconds is None or tokens <= 0.0 or seconds <= 0.0:
        return None
    return tokens / seconds


def _request_row(record):
    """One table row from one JSONL record, or None when the record is not a request outcome."""
    event = record.get("event")
    stamp = _number(record.get("timestamp_unix_ms"))
    if stamp is None or event not in ("request_done", "request_error", "request_rejected"):
        return None
    row = {"time": round(stamp / 1000.0, 3), "status": "error", "prompt": None, "reused": None,
           "output": None, "prefill": None, "decode": None, "draft": None, "duration": None}
    if event == "request_rejected":
        row["status"] = "rejected"
        return row
    if event == "request_error":
        return row
    result = record.get("result") or {}
    timing = record.get("timings_seconds") or {}
    spec = record.get("speculative") or {}
    row["status"] = _FINISH_STATUS.get(result.get("finish_reason"), "done")
    row["prompt"] = _number(result.get("prompt_tokens"))
    row["reused"] = _number(result.get("prefix_cache_hit_tokens"))
    row["output"] = _number(result.get("completion_tokens"))
    row["prefill"] = _rate(_number(result.get("computed_prefill_tokens")),
                           _number(timing.get("prefill")))
    # Same numerator as the streaming API (predicted_n - 1 completed decode intervals) over the
    # engine's own decode seconds. The API divides those intervals by first->last token wall time
    # instead, so this column can read a little higher - the tooltip says exactly that.
    if row["output"] is not None and row["output"] > 1.0:
        row["decode"] = _rate(row["output"] - 1.0, _number(timing.get("decode")))
    drafted = _number(spec.get("drafted_tokens"))
    accepted = _number(spec.get("accepted_tokens"))
    if drafted and accepted is not None and accepted <= drafted:
        row["draft"] = accepted / drafted
    row["duration"] = _number(timing.get("total"))
    return row


def _tail_lines(path, limit):
    """Complete lines from the last `limit` bytes; a half-written line is never returned."""
    size = os.path.getsize(path)
    with open(path, "rb") as handle:
        if size > limit:
            handle.seek(size - limit)
            handle.readline()          # drop the partial line we landed in
        data = handle.read()
    end = data.rfind(b"\n")            # the writer may be mid-line: cut at the last newline
    if end <= 0:
        return []
    return data[:end].split(b"\n")


def _engine_launched_by_studio():
    """True only when the engine now listening is the one the Studio started with the request log."""
    now = time.time()
    if now - _live_cache["at"] < 10.0:
        return _live_cache["live"]
    live = False
    try:
        with open(ENGINE_MARKER, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError):
        marker = None
    if isinstance(marker, dict) and marker.get("request_log") == REQUEST_LOG:
        live = str(marker.get("pid")) in engine_pid()
    _live_cache.update(at=now, live=live)
    return live


def recent_requests():
    """Rows for the Live tab table. Never raises: a missing log is a clean unavailable state."""
    payload = {"ok": True, "available": False, "live": False, "reason": None,
               "source": os.path.basename(REQUEST_LOG), "max": RECENT_MAX, "count": 0, "rows": []}
    payload["live"] = _engine_launched_by_studio()
    try:
        stat = os.stat(REQUEST_LOG)
    except OSError:
        payload["reason"] = "no-request-log"
        return payload
    key = (stat.st_size, stat.st_mtime_ns)
    with _recent_lock:
        if _recent_cache["key"] == key:
            cached = dict(_recent_cache["payload"])
            cached["live"] = payload["live"]   # never serve a stale liveness answer
            return cached
    rows = []
    try:
        raw_lines = _tail_lines(REQUEST_LOG, RECENT_TAIL_BYTES)
    except OSError:
        payload["reason"] = "unreadable-request-log"
        return payload
    for raw in raw_lines:
        try:
            record = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue                   # malformed line: skip it, never fail the endpoint
        if isinstance(record, dict):
            row = _request_row(record)
            if row is not None:
                rows.append(row)
    rows.sort(key=lambda r: r["time"], reverse=True)
    payload.update(available=True, count=min(len(rows), RECENT_MAX), rows=rows[:RECENT_MAX])
    with _recent_lock:
        _recent_cache["key"] = key
        _recent_cache["payload"] = dict(payload)
    return payload


def tail(path, n=60):
    """Last n lines of a log file. Missing file is not an error: the engine
    may be running in its own console, started outside the Studio."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = [ln.rstrip() for ln in data.splitlines() if ln.strip()]
    return lines[-n:]


def start_engine():
    live = engine_pid()
    if live:
        return {"ok": True, "already": True, "pids": live}
    if not ENGINE_EXE:
        return {"ok": False, "error": "ninfer-serve.exe not found under dist/"}
    if not MODEL_PATH:
        return {"ok": False, "error": "no .ninfer artifact found under models/"}
    os.makedirs(LOGS_DIR, exist_ok=True)
    log = open(ENGINE_LOG, "ab", buffering=0)
    log.write(("\n==== studio start %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    proc = subprocess.Popen(
        [ENGINE_EXE, MODEL_PATH] + ENGINE_FLAGS,
        cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    log.close()
    # remember who spawned this engine: /api/recent-requests must not claim live history when the
    # engine now listening was started some other way and never got --request-log-jsonl
    try:
        with open(ENGINE_MARKER, "w", encoding="utf-8") as handle:
            json.dump({"pid": proc.pid, "request_log": REQUEST_LOG,
                       "started_unix": time.time()}, handle, ensure_ascii=False)
    except OSError:
        pass
    return {"ok": True, "pid": proc.pid, "exe": ENGINE_EXE, "model": MODEL_PATH,
            "log": ENGINE_LOG, "flags": ENGINE_FLAGS}


def stop_engine():
    live = engine_pid()
    if not live:
        return {"ok": True, "killed": []}
    for pid in live:
        subprocess.run(
            ["taskkill", "/PID", pid, "/T", "/F"],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    return {"ok": True, "killed": live}


def model_action(action):
    """suspend / resume the served model (engine must run with --model-suspend)."""
    mid = default_model()
    return _post_upstream("/v1/models/%s/%s" % (urllib.parse.quote(mid), action), {})


def artifacts():
    rows, total = [], 0
    if os.path.isdir(MODELS_DIR):
        for name in os.listdir(MODELS_DIR):
            path = os.path.join(MODELS_DIR, name)
            if os.path.isfile(path) and name.endswith(".ninfer"):
                st = os.stat(path)
                rows.append({"name": name, "bytes": st.st_size, "mtime": int(st.st_mtime)})
                total += st.st_size
    rows.sort(key=lambda r: -r["bytes"])
    try:
        du = shutil.disk_usage(PROJECT)
    except OSError:
        du = None
    return {
        "files": rows,
        "total": total,
        "disk_free": du.free if du else None,
        "disk_total": du.total if du else None,
        "loaded": MODEL_PATH,
        "engine_exe": ENGINE_EXE,
        "flags": ENGINE_FLAGS,
    }


def snapshot():
    """Collect engine state for the dashboard. Each upstream call is independent."""
    result = {"ts": time.time(), "upstream": UPSTREAM, "up": False, "errors": {}}
    jobs = {
        "health": "/health",
        "models": "/v1/models",
        "load": "/v1/load",
        "metrics": "/metrics",
        "props": "/props",
    }
    got = {}

    def run(k, p):
        got[k] = fetch(p)

    threads = [threading.Thread(target=run, args=(k, p), daemon=True) for k, p in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    st, body = got.get("health", (0, b""))
    if st == 200:
        result["up"] = True
    else:
        result["errors"]["health"] = {"status": st, "body": body[:200].decode("utf-8", "replace")}

    st, body = got.get("models", (0, b""))
    if st == 200:
        try:
            data = json.loads(body).get("data", [])
            result["model"] = data[0] if data else None
        except Exception as e:
            result["errors"]["models"] = str(e)
    else:
        result["errors"]["models"] = st

    st, body = got.get("load", (0, b""))
    if st == 200:
        try:
            result["load"] = json.loads(body)
        except Exception as e:
            result["errors"]["load"] = str(e)
    else:
        result["errors"]["load"] = st

    st, body = got.get("props", (0, b""))
    if st == 200:
        try:
            p = json.loads(body)
            result["props"] = {k: p[k] for k in ("model_path", "n_ctx", "build_info", "is_sleeping") if k in p}
        except Exception:
            pass

    st, body = got.get("metrics", (0, b""))
    if st == 200:
        result["metrics"] = parse_metrics(body.decode("utf-8", "replace"))
    else:
        result["errors"]["metrics"] = {"status": st, "body": body[:120].decode("utf-8", "replace")}

    mid = (result.get("model") or {}).get("id")
    if result["up"] and mid:
        st, body = fetch("/v1/models/%s/residency" % urllib.parse.quote(mid), timeout=3.0)
        if st == 200:
            try:
                result["residency"] = json.loads(body)
            except Exception:
                pass
    try:
        result["gpu"] = gpu_thermal()
    except Exception as e:                                  # never break /api/state
        result["gpu"] = {"source": "error", "error": "%s: %s" % (type(e).__name__, e)}
    result["pids"] = engine_pid() if result["up"] else []
    result["engine_exe"] = ENGINE_EXE
    result["model_path"] = MODEL_PATH
    return result


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NinferStudio/1.0"

    def log_message(self, fmt, *args):
        # Keep the console readable: only non-200 requests are worth a line.
        line = fmt % args
        if " 200 " not in line:
            sys.stderr.write("%s - %s\n" % (self.address_string(), line))

    # ---------- helpers ----------
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _safe_static(self, name):
        path = posixpath.normpath(name).lstrip("/")
        if path.startswith("..") or not os.path.isfile(os.path.join(ROOT, path)):
            return None
        return os.path.join(ROOT, path)

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in _STATIC:
            fname, ctype = _STATIC[path]
            full = os.path.join(ROOT, fname)
            try:
                with open(full, "rb") as f:
                    self._send(200, f.read(), ctype)
            except OSError as e:
                self._send(500, "missing %s: %s" % (fname, e), "text/plain; charset=utf-8")
            return
        if path == "/api/state":
            self._json(snapshot())
            return
        if path == "/api/ping":
            self._json({"ok": True, "ts": time.time()})
            return
        if path == "/api/recent-requests":
            self._json(recent_requests())
            return
        if path == "/api/files":
            self._json(artifacts())
            return
        if path == "/api/log":
            want = "ui" if "ui=1" in self.path else "engine"
            path_log = UI_LOG if want == "ui" else ENGINE_LOG
            self._json({"path": path_log, "lines": tail(path_log, 60)})
            return
        full = self._safe_static(path)
        if full:
            ctype = _MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/engine/start":
            self._json(start_engine())
            return
        if path == "/api/engine/stop":
            self._json(stop_engine())
            return
        if path in ("/api/model/suspend", "/api/model/resume"):
            action = path.rsplit("/", 1)[-1]
            st, body = model_action(action)
            text = body.decode("utf-8", "replace")
            try:
                obj = json.loads(text) if text.strip() else {}
            except Exception:
                obj = {"raw": text[:400]}
            if st in (200, 202, 409):
                self._json({"ok": st in (200, 202), "status": st, "engine": obj})
            else:
                self._json({"ok": False, "status": st or 0, "error": obj}, 502 if st == 0 else st)
            return

        if path != "/api/chat":
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            self._json({"error": {"message": "bad request: %s" % e}}, 400)
            return

        payload["stream"] = bool(payload.get("stream", True))
        if not payload.get("model"):
            payload["model"] = default_model()
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            UPSTREAM + "/v1/chat/completions",
            data=data,
            headers=_headers({"Content-Type": "application/json"}),
            method="POST",
        )
        try:
            up = urllib.request.urlopen(req, timeout=900)
        except urllib.error.HTTPError as e:
            body = e.read() or b""
            self._send(e.code, body, "application/json; charset=utf-8")
            return
        except Exception as e:
            self._json({"error": {"message": "engine unreachable: %s" % e}}, 502)
            return

        ctype = up.headers.get("Content-Type", "text/event-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(b):
            self.wfile.write(b"%X\r\n" % len(b) + b + b"\r\n")
            self.wfile.flush()

        try:
            while True:
                buf = up.read(2048)
                if not buf:
                    break
                chunk(buf)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()


def wait_upstream(seconds=300):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if fetch("/health", timeout=2.0)[0] == 200:
            return True
        time.sleep(1.0)
    return False


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # Windows: refuse a second bind instead of silently sharing :8090

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return  # the browser closed a metric poll / stream: nothing to report
        super().handle_error(request, client_address)


def main():
    if "--wait" in sys.argv:
        ok = wait_upstream()
        sys.stderr.write("[studio] engine %s at %s\n" % ("ready" if ok else "NOT ready", UPSTREAM))
        if not ok:
            return 1
        return 0
    try:
        srv = StudioServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        sys.stderr.write("[studio] cannot bind 127.0.0.1:%d (%s) - already running?\n" % (PORT, e))
        return 1
    sys.stderr.write("[studio] NInfer Studio on http://127.0.0.1:%d/ -> %s\n" % (PORT, UPSTREAM))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
