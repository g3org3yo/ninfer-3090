"""Run NInfer Studio with this console window as its on/off switch.

Double-click "NInfer Studio (close to stop).bat" instead of "NInfer Studio.bat":
the engine and the web UI come up exactly as before (same launcher, same flags,
same browser tab), but this window now owns them. Closing the window -- or
Ctrl+C / Ctrl+Break -- stops both, the same way closing Strata's serve window
stops the model.

Why a wrapper is needed: webui/launcher.py deliberately starts the engine and the
web UI with DETACHED_PROCESS so they survive the launcher exiting; that detach is
what makes the separate "NInfer Studio - Stop.bat" necessary. This script keeps a
live process in the console and runs the launcher's own `stop` on every exit path
Windows gives us: console close (the X), Ctrl+C, Ctrl+Break, logoff, shutdown, and
a normal exit after startup failure.

Untracked on purpose: `git pull` must never touch it.

    py -3 studio_foreground.py [--stop-after=SECONDS] [launcher args...]

--stop-after is a self-test hook (used to verify the cleanup path); it is not
needed in daily use.
"""
from __future__ import annotations

import ctypes
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT / "webui" / "launcher.py"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT = 0, 1, 2

_stopped = False


def say(msg: str = "") -> None:
    print(msg, flush=True)


def stop_studio(why: str) -> None:
    """Stop engine + web UI through the launcher's own logic. Safe to call twice."""
    global _stopped
    if _stopped:
        return
    _stopped = True
    say("")
    say(f"[..] {why}: stopping the engine and the web UI ...")
    try:
        subprocess.run(
            [sys.executable, str(LAUNCHER), "stop"],
            creationflags=CREATE_NO_WINDOW,
            timeout=60,
        )
    except Exception as exc:  # noqa: BLE001 - never block the exit path
        say(f"[x] could not stop cleanly ({exc}); run 'NInfer Studio - Stop.bat'.")
        return
    say("[ok] engine and web UI are down.")


_Handler = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint32)


def _console_event(event: int) -> bool:
    # Runs in a Windows-created thread. Returning False lets the default handler
    # terminate us straight after the cleanup (the X-close gives ~5 s in total,
    # and `launcher.py stop` uses taskkill /T /F, so it finishes well inside that).
    stop_studio(f"console event {event}")
    return False


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.SetConsoleCtrlHandler.argtypes = [_Handler, ctypes.c_bool]
_kernel32.SetConsoleCtrlHandler.restype = ctypes.c_bool
_callback = _Handler(_console_event)


def main(argv: list[str]) -> int:
    stop_after = None
    passthrough: list[str] = []
    for arg in argv:
        if arg.startswith("--stop-after="):
            stop_after = float(arg.split("=", 1)[1])
        else:
            passthrough.append(arg)

    say("[..] starting NInfer Studio (engine :8082, web UI :8090) ...")
    rc = subprocess.call([sys.executable, str(LAUNCHER), "start"] + passthrough)
    if rc != 0:
        say(f"[x] launcher exited with {rc}; nothing to keep alive.")
        return rc

    if not _kernel32.SetConsoleCtrlHandler(_callback, True):
        say(f"[!] could not register the close handler (win error {ctypes.get_last_error()});")
        say("    closing this window will NOT stop the studio - use 'NInfer Studio - Stop.bat'.")

    def _on_break(signum, frame):  # Ctrl+Break
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGBREAK, _on_break)
    except (AttributeError, ValueError):
        pass

    say("")
    say("[ok] NInfer Studio is running. This window is now the on/off switch:")
    say("     CLOSE IT, or press Ctrl+C, to stop the engine and the web UI.")
    say(f"     UI: http://127.0.0.1:8090/   API: http://127.0.0.1:8082/v1")

    started = time.time()
    try:
        while True:
            time.sleep(0.5)
            if stop_after is not None and time.time() - started > stop_after:
                raise KeyboardInterrupt
    except KeyboardInterrupt:
        stop_studio("window closed / interrupted")
    finally:
        _kernel32.SetConsoleCtrlHandler(_callback, False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
