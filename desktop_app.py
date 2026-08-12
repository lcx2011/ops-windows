"""Windows desktop host for the local console.

The existing Python HTTP server remains the application core.  This module
only owns the native window and the backend lifecycle: it starts the server in
the same process, loads the local UI in WebView2, and restarts or stops the
backend without leaving a browser tab or a second console process behind.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import tempfile
import threading
import time


if os.name == "nt":
    os.environ.setdefault("CONSOLE_DESKTOP_BACKEND", "1")

ACTION_FILE = os.path.join(
    tempfile.gettempdir(), "local-ops-desktop-%d.json" % os.getpid())
os.environ.setdefault("CONSOLE_DESKTOP_ACTION_FILE", ACTION_FILE)

import server  # noqa: E402  (environment is configured before import)


WINDOW_TITLE = "总控台"
BACKEND_READY_TIMEOUT = 15.0
WINDOW_WIDTH = 1440
WINDOW_HEIGHT = 920
WINDOW_MIN_WIDTH = 1000
WINDOW_MIN_HEIGHT = 650


def _remove_action_file():
    try:
        os.remove(ACTION_FILE)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _read_action_file():
    try:
        with open(ACTION_FILE, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        _remove_action_file()
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None


def _show_error(message):
    """Show an error even when the desktop entry was started with pythonw."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                0, str(message), WINDOW_TITLE, 0x10)
            return
        except Exception:
            pass
    print("%s: %s" % (WINDOW_TITLE, message), file=sys.stderr)


class DesktopHost:
    """Own one native window and one restartable backend worker."""

    def __init__(self):
        self.window = None
        self.port = None
        self._server = None
        self._backend_thread = None
        self._backend_done = threading.Event()
        self._backend_ready = threading.Event()
        self._lock = threading.RLock()
        self._closing = False
        self._backend_error = None
        self._monitor_thread = None
        self._actions = queue.Queue()

    def start_backend(self, preferred_port=None):
        with self._lock:
            if self._closing:
                return
            self.port = None
            self._server = None
            self._backend_error = None
            self._backend_ready.clear()
            self._backend_done.clear()
            thread = threading.Thread(
                target=self._run_backend,
                args=(preferred_port,),
                name="console-backend",
                daemon=True,
            )
            self._backend_thread = thread
        thread.start()

    def _run_backend(self, preferred_port):
        try:
            started = server.main(
                preferred_port=preferred_port,
                open_browser=False,
                log_to_file=True,
                on_ready=self._backend_ready_callback,
            )
            if not started:
                self._backend_error = "总控台已经在运行，或数据目录被其他实例占用。"
        except Exception as exc:  # pragma: no cover - displayed by the host
            self._backend_error = "后端启动失败：%s" % exc
        finally:
            self._backend_done.set()

    def _backend_ready_callback(self, http_server, port):
        with self._lock:
            self._server = http_server
            self.port = port
            window = self.window
        self._backend_ready.set()
        if window is not None:
            self._navigate(port)

    def _navigate(self, port):
        with self._lock:
            window = self.window
            closing = self._closing
        if window is None or closing:
            return
        try:
            window.load_url("http://%s:%d/" % (server.HOST, port))
        except Exception:
            # The window can be between native close events while a restart
            # finishes. The next successful state poll will recover normally.
            pass

    def wait_initial_ready(self, timeout=BACKEND_READY_TIMEOUT):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._backend_ready.wait(0.1):
                return True
            if self._backend_done.is_set():
                return False
        return self._backend_ready.is_set()

    def start_monitor(self):
        self._monitor_thread = threading.Thread(
            target=self._monitor_backend,
            name="console-backend-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_backend(self):
        while True:
            self._backend_done.wait()
            with self._lock:
                if self._closing:
                    return
            action, preferred = self._next_action()
            if action == "restart":
                if not isinstance(preferred, int):
                    preferred = None
                self.start_backend(preferred)
                continue
            if action == "stop":
                self.close_window()
                return
            self._backend_error = self._backend_error or "总控台后端意外停止。"
            self.close_window()
            return

    def request_backend_action(self, action, preferred_port=None):
        """Receive a restart/stop request from the in-process HTTP handler."""
        self._actions.put((action, preferred_port))

    def _next_action(self):
        try:
            action, preferred = self._actions.get_nowait()
            return action, preferred
        except queue.Empty:
            pass
        value = _read_action_file() or {}
        return value.get("action"), value.get("preferredPort")

    def stop_backend(self):
        with self._lock:
            http_server = self._server
        if http_server is None:
            return
        try:
            http_server.shutdown()
        except Exception:
            pass

    def close_window(self):
        with self._lock:
            self._closing = True
            window = self.window
        self.stop_backend()
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def on_window_closed(self):
        with self._lock:
            self._closing = True
        _remove_action_file()
        self.stop_backend()


def run():
    if os.name != "nt":
        raise RuntimeError("桌面应用入口只支持 Windows")

    try:
        import webview
    except ImportError as exc:
        _show_error(
            "缺少桌面运行依赖 pywebview。\n\n"
            "请在 PowerShell 执行：\n"
            "python -m pip install -r requirements-desktop-windows.txt")
        raise SystemExit(2) from exc

    _remove_action_file()
    host = DesktopHost()
    server.set_desktop_action_handler(host.request_backend_action)
    host.start_backend()
    if not host.wait_initial_ready():
        host._closing = True
        host.stop_backend()
        _show_error(host._backend_error or "总控台后端未能在规定时间内启动。")
        raise SystemExit(1)

    icon_path = os.path.join(server.STATIC_DIR, "assets", "favicon.ico")
    storage_path = os.path.join(server.DATA_DIR, "webview")
    os.makedirs(storage_path, exist_ok=True)
    window = webview.create_window(
        WINDOW_TITLE,
        "http://%s:%d/" % (server.HOST, host.port),
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        resizable=True,
        confirm_close=False,
        text_select=True,
        zoomable=True,
        background_color="#0c1320",
    )
    host.window = window
    window.events.closed += host.on_window_closed
    host.start_monitor()
    try:
        webview.start(
            debug=os.environ.get("CONSOLE_DESKTOP_DEBUG") == "1",
            private_mode=False,
            storage_path=storage_path,
            icon=icon_path if os.path.isfile(icon_path) else None,
        )
    finally:
        host.on_window_closed()
        _remove_action_file()


if __name__ == "__main__":
    try:
        run()
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - desktop-only fallback
        _show_error("桌面应用启动失败：%s" % exc)
        raise SystemExit(1) from exc
