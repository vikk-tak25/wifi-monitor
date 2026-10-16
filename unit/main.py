# Boot loader for Wi-Fi fleet units (MicroPython).
#
# Starts app.py. This file is installed when the unit is provisioned and is never replaced
# by a remote update, so it can always undo a bad one:
# - app.py writes update_pending.json before rebooting into a new version, and deletes it
#   after the new version completes a handshake with the server.
# - If the pending update has not been confirmed after MAX_TRIES starts, or app.py crashes
#   while an update is pending, the previous version (app_prev.py) is restored.
#
# Press Ctrl-C within a second of boot to get the REPL without starting the agent.

import json
import os
import sys
import time

PENDING_FILE = "update_pending.json"
FAILED_FILE = "update_failed.json"
ERROR_LOG = "last_error.txt"
MAX_TRIES = 2


def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _read(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def rollback(reason):
    pending = _read(PENDING_FILE) or {}
    if _exists("app_prev.py"):
        _remove("app.py")
        os.rename("app_prev.py", "app.py")
    _write(FAILED_FILE, {"version": pending.get("version"), "error": reason[:200], "reported": False})
    _remove(PENDING_FILE)


def _describe(exc):
    try:
        import io

        buf = io.StringIO()
        sys.print_exception(exc, buf)
        return buf.getvalue()[-300:]
    except Exception:
        return repr(exc)


def start(import_app, reset, sleep_s):
    pending = _read(PENDING_FILE)
    if pending is not None:
        tries = pending.get("tries", 0) + 1
        if tries > MAX_TRIES:
            rollback("new agent was not confirmed after %d starts" % MAX_TRIES)
            reset()
            return
        pending["tries"] = tries
        _write(PENDING_FILE, pending)
    try:
        app = import_app()
        app.run()
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        message = _describe(exc)
        try:
            with open(ERROR_LOG, "w") as f:
                f.write(message)
        except OSError:
            pass
        if _read(PENDING_FILE) is not None:
            rollback(message)
        sleep_s(10)
        reset()


def _main():
    import machine

    time.sleep(1)  # a chance to press Ctrl-C before the watchdog starts

    def import_app():
        import app

        return app

    start(import_app, machine.reset, time.sleep)


if __name__ == "__main__":
    _main()
