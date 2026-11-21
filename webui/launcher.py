#!/usr/bin/env python3
"""NInfer Studio launcher - one entry point for the engine + the web UI.

Reads every path, flag and port from webui/server.py (the single source of
truth for this install), so nothing is duplicated here:

  engine  : dist/<build>/ninfer-serve.exe <model> [ENGINE_FLAGS]   -> 127.0.0.1:8082
  web UI  : python webui/server.py                                 -> 127.0.0.1:8090

Usage:
  python launcher.py start [--no-browser] [--timeout SECONDS]
  python launcher.py stop
  python launcher.py status

Exit codes: 0 ok / already running, 1 failure (message on stderr).
"""
import os
import subprocess
import sys
import time
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server as S  # noqa: E402  (reuse ENGINE_EXE, MODEL_PATH, ENGINE_FLAGS, ports, pids_on_port)

ENGINE_URL = S.UPSTREAM
UI_URL = "http://127.0.0.1:%d" % S.PORT
SERVER_PY = os.path.join(HERE, "server.py")
DETACHED = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def say(msg):
    print(msg, flush=True)


def ui_pids():
    return S.pids_on_port(S.PORT)


def http_ok(url, path, timeout=2.0):
    """GET url+path. S.fetch() only knows the engine URL, so the UI is checked here."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url + path, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for(predicate, seconds, label, every=0.5):
    """Poll `predicate` and print a progress dot every ~5 s. True when ready."""
    deadline = time.time() + seconds
    started = time.time()
    last_note = 0.0
    while time.time() < deadline:
        if predicate():
            if time.time() - started > 2:
                say("")  # close the dot line
            return True
        if time.time() - last_note >= 5:
            sys.stdout.write(".")
            sys.stdout.flush()
            last_note = time.time()
        time.sleep(every)
    say("")
    return False


def show_log_tail(path, title, n=25):
    lines = S.tail(path, n)
    say("")
    say("--- %s (%s) ---" % (title, path))
    if not lines:
        say("(empty or missing)")
    for ln in lines:
        say("  " + ln)
    say("---")


def start_engine():
    """Start the engine unless something already listens on its port."""
    pids = S.pids_on_port(S.UPSTREAM_PORT)
    if pids:
        if http_ok(ENGINE_URL, "/health"):
            say("[i] engine already running on %s (pid %s)" % (ENGINE_URL, ", ".join(pids)))
            return True
        say("[x] port %d is taken by pid %s but %s/health does not answer 200."
            % (S.UPSTREAM_PORT, ", ".join(pids), ENGINE_URL))
        say("    Not starting a second engine. Close that process, then run this launcher again.")
        return False

    if not S.ENGINE_EXE:
        say("[x] ninfer-serve.exe not found under dist/")
        return False
    if not S.MODEL_PATH:
        say("[x] no .ninfer model found under models/")
        return False

    say("[>] starting engine: %s" % os.path.basename(S.ENGINE_EXE))
    say("    model: %s" % S.MODEL_PATH)
    result = S.start_engine()          # detached, logs to logs/engine.log
    if not result.get("ok"):
        say("[x] engine did not start: %s" % result.get("error"))
        return False
    say("    pid %s, log %s" % (result.get("pid"), result.get("log")))

    say("[.] waiting for the engine to load the model (up to %ds)" % START_TIMEOUT)
    if wait_for(lambda: http_ok(ENGINE_URL, "/health"), START_TIMEOUT, "engine"):
        return True
    say("[x] engine did not become healthy in %ds." % START_TIMEOUT)
    show_log_tail(S.ENGINE_LOG, "engine.log")
    return False


def start_ui():
    pids = ui_pids()
    if pids:
        if http_ok(UI_URL, "/api/ping"):
            say("[i] web UI already running on %s (pid %s)" % (UI_URL, ", ".join(pids)))
            return True
        say("[x] port %d is taken by pid %s but /api/ping does not answer." % (S.PORT, ", ".join(pids)))
        say("    Stop it with 'NInfer Studio - Stop.bat', then start again.")
        return False

    os.makedirs(S.LOGS_DIR, exist_ok=True)
    log = open(S.UI_LOG, "ab", buffering=0)
    log.write(("\n==== launcher start %s ====\n" % time.strftime("%Y-%m-%d %H:%M:%S")).encode())
    say("[>] starting web UI on %s" % UI_URL)
    subprocess.Popen(
        [sys.executable, SERVER_PY],
        cwd=S.PROJECT, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=DETACHED,
    )
    log.close()

    say("[.] waiting for the UI to answer")
    if wait_for(lambda: http_ok(UI_URL, "/api/ping"), 40, "UI"):
        return True
    say("[x] web UI did not answer on %s." % UI_URL)
    show_log_tail(S.UI_LOG, "ui.log")
    return False


def cmd_start(argv):
    global START_TIMEOUT
    START_TIMEOUT = 300
    if "--timeout" in argv:
        try:
            START_TIMEOUT = int(argv[argv.index("--timeout") + 1])
        except (IndexError, ValueError):
            say("[x] --timeout needs a number of seconds")
            return 1

    say("NInfer Studio launcher")
    say("  engine : %s" % ENGINE_URL)
    say("  web UI : %s" % UI_URL)
    say("")

    if not start_engine():
        say("")
        say("[x] start aborted: the engine is not ready, so the UI was not started.")
        return 1
    if not start_ui():
        say("")
        say("[x] start aborted: the engine is running, the UI is not.")
        show_log_tail(S.ENGINE_LOG, "engine.log", 10)
        return 1

    say("")
    say("[ok] ready")
    say("     Studio : %s/" % UI_URL)
    say("     API    : %s/v1" % ENGINE_URL)
    say("     logs   : %s" % S.LOGS_DIR)
    if "--no-browser" not in argv:
        webbrowser.open(UI_URL + "/")
    return 0


def _kill(pid, note):
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        say("    stopped %s (pid %s)" % (note, pid))
        return True
    except Exception as e:  # noqa: BLE001
        say("    could not stop pid %s: %s" % (pid, e))
        return False


def cmd_stop(argv):
    say("NInfer Studio launcher - stopping")
    killed = 0
    # UI first: it only polls the engine, so stopping it first avoids noise.
    for pid in ui_pids():
        killed += 1 if _kill(pid, "web UI") else 0
    for pid in S.pids_on_port(S.UPSTREAM_PORT):
        killed += 1 if _kill(pid, "engine") else 0

    if not killed:
        say("[i] nothing was running on :%d or :%d" % (S.PORT, S.UPSTREAM_PORT))
        return 0

    # confirm the ports really went free
    for _ in range(20):
        if not ui_pids() and not S.pids_on_port(S.UPSTREAM_PORT):
            break
        time.sleep(0.5)
    left = ui_pids() + S.pids_on_port(S.UPSTREAM_PORT)
    if left:
        say("[x] ports still held by pid %s" % ", ".join(left))
        return 1
    say("[ok] stopped. ports :%d and :%d are free." % (S.PORT, S.UPSTREAM_PORT))
    return 0


def cmd_status(argv):
    e = S.pids_on_port(S.UPSTREAM_PORT)
    u = ui_pids()
    say("engine : %s  %s" % ("pid " + ", ".join(e) if e else "down",
                             "health ok" if http_ok(ENGINE_URL, "/health") else "no /health"))
    say("web UI : %s  %s" % ("pid " + ", ".join(u) if u else "down",
                             "ping ok" if http_ok(UI_URL, "/api/ping") else "no /api/ping"))
    say("logs   : %s" % S.LOGS_DIR)
    return 0


def main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        say(__doc__)
        return 0
    cmd = argv[0].lower()
    if cmd == "start":
        return cmd_start(argv[1:])
    if cmd == "stop":
        return cmd_stop(argv[1:])
    if cmd == "status":
        return cmd_status(argv[1:])
    say("unknown command: %s (use start | stop | status)" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
