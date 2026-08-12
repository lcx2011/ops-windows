"""Operating-system primitives used by the local console.

The application deliberately keeps its product logic in :mod:`server`.  This
module owns the small set of things that cannot be made portable by Python's
standard library alone: process ownership, process snapshots, listening
ports, process groups and the per-project instance lock.

Windows uses psutil when it is installed (the recommended runtime dependency)
and falls back to PowerShell/CIM for read-only inspection.  The fallback is
conservative: if Windows cannot prove process ownership, it does not claim
the process belongs to the current user.
"""

import ctypes
import getpass
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from ctypes import wintypes

IS_WINDOWS = os.name == "nt"

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - exercised on minimal installs
    psutil = None


_TOKEN_API = None


def _token_user_sid(pid):
    """Return a process token's raw user SID without invoking WMI."""
    if not IS_WINDOWS:
        return None
    global _TOKEN_API
    try:
        if _TOKEN_API is None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE, wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE)]
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE, wintypes.INT, wintypes.LPVOID,
                wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.GetLengthSid.argtypes = [wintypes.LPVOID]
            advapi32.GetLengthSid.restype = wintypes.DWORD
            _TOKEN_API = (kernel32, advapi32)
        kernel32, advapi32 = _TOKEN_API
        process = kernel32.OpenProcess(0x1000, False, int(pid))
        if not process:
            return None
        token = wintypes.HANDLE()
        try:
            if not advapi32.OpenProcessToken(process, 0x0008,
                                             ctypes.byref(token)):
                return None
            size = wintypes.DWORD(0)
            # TokenUser = 1. The first call only asks Windows for the buffer
            # size and is expected to fail with ERROR_INSUFFICIENT_BUFFER.
            advapi32.GetTokenInformation(token, 1, None, 0,
                                         ctypes.byref(size))
            if not size.value:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                    token, 1, buffer, size, ctypes.byref(size)):
                return None
            sid_pointer = ctypes.cast(
                buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value
            if not sid_pointer:
                return None
            sid_length = int(advapi32.GetLengthSid(sid_pointer))
            return ctypes.string_at(sid_pointer, sid_length)
        finally:
            if token:
                kernel32.CloseHandle(token)
            kernel32.CloseHandle(process)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _user_identity(pid):
    if psutil is not None:
        process = _psutil_process(pid)
        if process is not None:
            try:
                value = _normalise_user(process.username())
                if value:
                    return value
            except (OSError, psutil.Error):
                pass
    return None


_CURRENT_USER_NAME = None
_PROCESS_METRIC_CACHE = {}
_PROCESS_METRIC_LOCK = threading.RLock()


def _cached_current_user_name():
    global _CURRENT_USER_NAME
    if _CURRENT_USER_NAME is None:
        if psutil is not None:
            try:
                _CURRENT_USER_NAME = _normalise_user(
                    psutil.Process(os.getpid()).username())
            except (OSError, psutil.Error):
                _CURRENT_USER_NAME = None
    return _CURRENT_USER_NAME


def _normalise_user(value):
    if value is None:
        return None
    return str(value).strip().casefold() or None


def current_user_id():
    """Return a comparable identity for the current process owner."""
    if not IS_WINDOWS:
        return os.getuid()
    if psutil is not None:
        value = _cached_current_user_name()
        if value:
            return value
    value = _powershell_process_owner(os.getpid())
    if value:
        return value
    return _normalise_user(
        os.environ.get("USERDOMAIN") and
        "%s\\%s" % (os.environ.get("USERDOMAIN"),
                     os.environ.get("USERNAME"))
        or os.environ.get("USERNAME") or getpass.getuser())


def _psutil_process(pid):
    if psutil is None:
        return None
    try:
        return psutil.Process(int(pid))
    except (ValueError, TypeError, OSError, psutil.Error):
        return None


def process_uid(pid):
    """Return the process owner, or ``None`` when it cannot be proven."""
    if IS_WINDOWS:
        value = _user_identity(pid)
        if value:
            return value
    process = _psutil_process(pid)
    if process is not None:
        try:
            value = _normalise_user(process.username())
            if value:
                return value
        except (OSError, psutil.Error):
            pass
    if not IS_WINDOWS:
        return None

    value = _powershell_process_owner(pid)
    if value:
        return value
    sid = _token_user_sid(pid)
    return "sid:" + sid.hex() if sid else None


def _powershell_text(script, timeout=5):
    command = ["powershell.exe", "-NoProfile", "-NonInteractive",
               "-ExecutionPolicy", "Bypass", "-Command", script]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                errors="replace", timeout=timeout,
                                creationflags=getattr(
                                    subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or "").strip()


def _powershell_process_owner(pid):
    """Return a normalized ``DOMAIN\\user`` owner through CIM."""
    try:
        value = _powershell_text(
            "$p = Get-CimInstance Win32_Process -Filter 'ProcessId = %d'; "
            "if ($p) { $o = Invoke-CimMethod -InputObject $p "
            "-MethodName GetOwner; if ($o.ReturnValue -eq 0) { "
            "if ($o.Domain) { $o.Domain + '\\\\' + $o.User } "
            "else { $o.User } } }" % int(pid), timeout=3)
    except (ValueError, TypeError):
        return None
    return _normalise_user(value)


def _powershell_json(script, timeout=5):
    raw = _powershell_text(
        "$ErrorActionPreference = 'SilentlyContinue'; "
        "%s | ConvertTo-Json -Compress -Depth 4" % script,
        timeout=timeout)
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def _format_command_line(parts, fallback=""):
    if not parts:
        return fallback
    if IS_WINDOWS:
        return subprocess.list2cmdline([str(part) for part in parts])
    return " ".join(str(part) for part in parts)


def _psutil_snapshot(pids=None, with_uid=True):
    if psutil is None:
        return {}
    wanted = None if pids is None else {int(pid) for pid in pids}
    snapshot = {}
    ppid_map = None
    try:
        # Fetch the cheap, stable fields in one native pass.  Calling
        # ``cpu_percent`` and ``memory_percent`` for every process makes the
        # first Windows state request unnecessarily slow (and can exceed the
        # browser's request timeout on machines with many processes).
        attrs = ["pid", "name", "cmdline"]
        if IS_WINDOWS:
            try:
                # psutil's per-process ``ppid`` property rebuilds the Windows
                # parent map for every item. Build it once instead.
                ppid_map = psutil._psplatform.ppid_map()
            except (AttributeError, OSError, psutil.Error):
                ppid_map = {}
            if with_uid:
                attrs.extend(["create_time", "username"])
        else:
            attrs.extend(["create_time", "ppid"])
        iterator = psutil.process_iter(attrs=attrs)
    except (OSError, psutil.Error):
        return {}
    for process in iterator:
        try:
            info = getattr(process, "info", {}) or {}
            pid = int(info.get("pid", process.pid))
            if wanted is not None and pid not in wanted:
                continue
            name = info.get("name") or ""
            command = info.get("cmdline") or []
            if not command and pid == os.getpid():
                command = sys.argv
            # A Windows process can be created before its command line is
            # visible through the snapshot API.  Its executable name is still
            # useful for monitoring, but token ownership must be conservative.
            created = info.get("create_time")
            if created is None:
                etime = 0
            else:
                try:
                    etime = max(0, int(time.time() - created))
                except (OSError, ValueError, psutil.Error):
                    etime = 0
            entry = {
                "args": _format_command_line(command, name),
                "comm": name,
                "cpu": 0.0,
                "mem": 0.0,
                "etime": etime,
                "ppid": int(info.get("ppid") or
                              (ppid_map or {}).get(pid, 0)),
            }
            if with_uid:
                entry["uid"] = _normalise_user(info.get("username"))
            snapshot[pid] = entry
        except (OSError, ValueError, psutil.Error):
            continue
    return snapshot


def _powershell_snapshot(pids=None, with_uid=True):
    if not IS_WINDOWS:
        return {}
    filter_script = ""
    if pids is not None:
        values = ",".join(str(int(pid)) for pid in pids)
        if not values:
            return {}
        filter_script = " | Where-Object { @(%s) -contains $_.ProcessId }" % values
    owner = (
        "$owner = Invoke-CimMethod -InputObject $_ -MethodName GetOwner; "
        "$uid = $null; if ($owner.ReturnValue -eq 0) { "
        "if ($owner.Domain) { $uid = $owner.Domain + '\\\\' + $owner.User } "
        "else { $uid = $owner.User } }; "
        if with_uid else "$uid = $null; ")
    script = (
        "Get-CimInstance Win32_Process%s | ForEach-Object { %s"
        "[pscustomobject]@{ pid = [int]$_.ProcessId; "
        "ppid = [int]$_.ParentProcessId; uid = $uid; "
        "comm = [string]$_.Name; args = [string]$_.CommandLine; "
        "cpu = 0; mem = 0; etime = 0 } }" % (filter_script, owner))
    result = {}
    for item in _powershell_json(script, timeout=8):
        try:
            pid = int(item.get("pid"))
        except (TypeError, ValueError):
            continue
        entry = {
            "args": item.get("args") or item.get("comm") or "",
            "comm": item.get("comm") or "",
            "cpu": float(item.get("cpu") or 0),
            "mem": float(item.get("mem") or 0),
            "etime": int(item.get("etime") or 0),
            "ppid": int(item.get("ppid") or 0),
        }
        if with_uid:
            entry["uid"] = _normalise_user(item.get("uid"))
        result[pid] = entry
    return result


def process_snapshot(pids=None, with_uid=True):
    """Return the common process snapshot shape used by the console."""
    if not IS_WINDOWS:
        return {}
    snapshot = _psutil_snapshot(pids, with_uid)
    if psutil is None:
        return _powershell_snapshot(pids, with_uid)
    # psutil can return an empty result for a just-created process while the
    # Windows process table catches up.  Merge the CIM fallback only for PIDs
    # that are missing, preserving psutil's richer CPU/cwd data.
    if pids is not None:
        wanted = {int(pid) for pid in pids}
        missing = wanted.difference(snapshot)
        if missing:
            for pid, info in _powershell_snapshot(missing, with_uid).items():
                snapshot.setdefault(pid, info)
        if with_uid:
            # Do not invoke one PowerShell/CIM owner query per protected PID
            # while building the live state page.  Unknown owners remain
            # hidden conservatively; user-triggered single-PID operations use
            # process_uid() for the slower authoritative fallback.
            _add_process_metrics(snapshot, wanted)
    return snapshot


def _add_process_metrics(snapshot, pids):
    if psutil is None:
        return
    for pid in {int(value) for value in pids}:
        if pid not in snapshot:
            continue
        process = _metric_process(pid)
        if process is None:
            continue
        try:
            snapshot[pid]["cpu"] = float(process.cpu_percent(interval=None))
        except (OSError, psutil.Error):
            pass
        try:
            snapshot[pid]["mem"] = float(process.memory_percent())
        except (OSError, psutil.Error):
            pass


def _metric_process(pid):
    """Return a persistent psutil process object for CPU sampling.

    ``Process.cpu_percent(interval=None)`` needs two observations on the same
    object. Recreating ``psutil.Process(pid)`` on every state poll resets that
    baseline and makes Windows CPU values stay at 0. A creation-time check
    prevents a reused PID from inheriting the previous process's baseline.
    """
    if psutil is None:
        return None
    pid = int(pid)
    process = _psutil_process(pid)
    if process is None:
        with _PROCESS_METRIC_LOCK:
            _PROCESS_METRIC_CACHE.pop(pid, None)
        return None
    try:
        created = process.create_time()
    except (OSError, psutil.Error):
        created = None
    with _PROCESS_METRIC_LOCK:
        cached = _PROCESS_METRIC_CACHE.get(pid)
        if cached is not None and cached[1] == created:
            return cached[0]
        _PROCESS_METRIC_CACHE[pid] = (process, created)
        return process


def process_metrics(pids):
    """Return current CPU/memory values for a small set of PIDs."""
    result = {}
    if not IS_WINDOWS or psutil is None:
        return result
    snapshot = {int(pid): {} for pid in pids}
    _add_process_metrics(snapshot, snapshot)
    return {
        pid: {"cpu": info.get("cpu", 0.0), "mem": info.get("mem", 0.0)}
        for pid, info in snapshot.items()
    }


def process_owner_snapshot(pids):
    """Fetch only ownership for a small set of PIDs."""
    if not IS_WINDOWS or psutil is None:
        return {}
    result = {}
    for pid in {int(value) for value in pids}:
        process = _psutil_process(pid)
        if process is None:
            continue
        try:
            sid = _token_user_sid(pid)
            value = "sid:" + sid.hex() if sid else _normalise_user(
                process.username())
            result[pid] = value
        except (OSError, psutil.Error):
            sid = _token_user_sid(pid)
            value = "sid:" + sid.hex() if sid else None
            result[pid] = value
    return result


def process_cwds(pids):
    result = {}
    for pid in pids:
        process = _psutil_process(pid)
        if process is None:
            continue
        try:
            cwd = process.cwd()
        except (OSError, psutil.Error):
            continue
        if cwd:
            result[int(pid)] = cwd
    return result


def listener_snapshot():
    """Return ``{(pid, port): {bind_host, ...}}`` for TCP listeners."""
    if not IS_WINDOWS:
        return {}
    found = {}
    if psutil is not None:
        try:
            connections = psutil.net_connections(kind="tcp")
        except (OSError, psutil.Error):
            connections = []
        for connection in connections:
            if connection.status != getattr(psutil, "CONN_LISTEN", "LISTEN"):
                continue
            if not connection.pid or not connection.laddr:
                continue
            try:
                host = connection.laddr.ip
                port = int(connection.laddr.port)
                pid = int(connection.pid)
            except (AttributeError, TypeError, ValueError):
                continue
            found.setdefault((pid, port), set()).add(host or "")
        if found:
            return found

    script = (
        "Get-NetTCPConnection -State Listen | "
        "Select-Object OwningProcess,LocalAddress,LocalPort")
    for item in _powershell_json(script, timeout=8):
        try:
            pid = int(item.get("OwningProcess"))
            port = int(item.get("LocalPort"))
        except (TypeError, ValueError):
            continue
        found.setdefault((pid, port), set()).add(
            str(item.get("LocalAddress") or ""))
    return found


def origin_snapshot():
    """Return ``{pid: (ppid, args)}`` for process-origin attribution."""
    snapshot = process_snapshot(None, with_uid=False)
    return {
        pid: (int(info.get("ppid") or 0), info.get("args") or "")
        for pid, info in snapshot.items()
    }


def _tree_snapshot():
    snapshot = process_snapshot(None, with_uid=False)
    children = defaultdict(list)
    for pid, info in snapshot.items():
        try:
            ppid = int(info.get("ppid") or 0)
        except (TypeError, ValueError):
            ppid = 0
        children[ppid].append(pid)
    return snapshot, children


# ---------------------------------------------------------------- Windows Job Objects

_JOB_HANDLES = {}
_JOB_LOCK = threading.RLock()
_WRAPPER_PATHS = {}
_kernel32 = None
_ULONG_PTR = (ctypes.c_uint64 if ctypes.sizeof(ctypes.c_void_p) == 8
              else ctypes.c_uint32)

if IS_WINDOWS:  # pragma: no branch - platform selection is deterministic
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE,
                                                     wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL


def register_process_group(pid, process):
    """Attach a newly started root process to a private Job Object."""
    if not IS_WINDOWS or _kernel32 is None:
        return None
    try:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        process_handle = wintypes.HANDLE(int(process._handle))
        if not _kernel32.AssignProcessToJobObject(handle, process_handle):
            _kernel32.CloseHandle(handle)
            return None
    except (AttributeError, OSError, TypeError, ValueError):
        try:
            if handle:
                _kernel32.CloseHandle(handle)
        except (NameError, OSError):
            pass
        return None
    with _JOB_LOCK:
        _JOB_HANDLES[int(pid)] = handle
    return int(pid)


def release_process_group(group_id):
    if not IS_WINDOWS or _kernel32 is None:
        return
    with _JOB_LOCK:
        handle = _JOB_HANDLES.pop(int(group_id), None)
    if handle:
        _kernel32.CloseHandle(handle)


def register_command_wrapper(pid, path):
    with _JOB_LOCK:
        _WRAPPER_PATHS[int(pid)] = path


def release_command_wrapper(pid):
    with _JOB_LOCK:
        path = _WRAPPER_PATHS.pop(int(pid), None)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _job_members(group_id):
    if not IS_WINDOWS or _kernel32 is None:
        return None
    with _JOB_LOCK:
        handle = _JOB_HANDLES.get(int(group_id))
    if not handle:
        return None
    # A job for a development service is normally tiny.  Keep enough room for
    # a large node process tree without a second native allocation roundtrip.
    count = 4096
    item_size = ctypes.sizeof(_ULONG_PTR)
    size = ctypes.sizeof(wintypes.DWORD) * 2 + item_size * count
    buffer = ctypes.create_string_buffer(size)
    returned = wintypes.DWORD(0)
    ok = _kernel32.QueryInformationJobObject(
        handle, 3, buffer, size, ctypes.byref(returned))
    if not ok:
        return None
    class _ProcessIdList(ctypes.Structure):
        _fields_ = [("assigned", wintypes.DWORD),
                    ("listed", wintypes.DWORD),
                    ("pids", _ULONG_PTR * count)]
    data = ctypes.cast(buffer, ctypes.POINTER(_ProcessIdList)).contents
    return [int(data.pids[index]) for index in range(int(data.listed))
            if int(data.pids[index]) > 0]


def process_group_members(group_id):
    """Return live members of a Windows job/root process group."""
    if not IS_WINDOWS:
        return []
    members = _job_members(group_id)
    if members is not None:
        return members
    if psutil is not None:
        root_process = _psutil_process(group_id)
        if root_process is not None:
            try:
                return sorted({int(root_process.pid)} | {
                    int(child.pid)
                    for child in root_process.children(recursive=True)
                })
            except (OSError, psutil.Error):
                pass
    snapshot, children = _tree_snapshot()
    root = int(group_id)
    if root not in snapshot:
        return []
    result = []
    pending = [root]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        pending.extend(children.get(pid, []))
    return sorted(result)


def process_groups():
    """Return ``{root_pid: [root_pid, descendants...]}`` on Windows."""
    if not IS_WINDOWS:
        return {}
    snapshot, children = _tree_snapshot()
    result = {}
    with _JOB_LOCK:
        job_ids = list(_JOB_HANDLES)
    for group_id in job_ids:
        members = _job_members(group_id)
        if members is None:
            members = []
            pending = [int(group_id)]
            seen = set()
            while pending:
                pid = pending.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                if pid in snapshot:
                    members.append(pid)
                pending.extend(children.get(pid, []))
        if members:
            result[group_id] = sorted(set(members))
    # A restart cannot carry a native Job Object handle across processes.  A
    # live token-bearing controller is still safe to inspect by its PID tree;
    # roots with no marker are intentionally omitted so a reused PID cannot
    # accidentally become an owned application.
    for root, info in snapshot.items():
        if "console-run:" not in (info.get("args") or ""):
            continue
        members = []
        pending = [root]
        seen = set()
        while pending:
            pid = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if pid in snapshot:
                members.append(pid)
            pending.extend(children.get(pid, []))
        if members:
            result[root] = sorted(set(members))
    return result


def pid_alive(pid):
    process = _psutil_process(pid)
    if process is not None:
        try:
            return process.is_running() and process.status() != getattr(
                psutil, "STATUS_ZOMBIE", "zombie")
        except (OSError, psutil.Error):
            return False
    if not IS_WINDOWS:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False


def terminate_pid(pid, force=False):
    """Terminate one validated Windows PID using taskkill."""
    if not IS_WINDOWS:
        return False, "Windows 进程终止函数被错误调用"
    flags = ["taskkill.exe", "/PID", str(int(pid))]
    if force:
        flags.append("/F")
    try:
        result = subprocess.run(flags, capture_output=True, text=True,
                                errors="replace", timeout=5,
                                creationflags=getattr(
                                    subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "结束进程超时"
    except OSError as e:
        return False, "结束进程失败: %s" % e
    if result.returncode == 0 or not pid_alive(pid):
        return True, None
    if not force:
        # Windows does not deliver POSIX-like SIGTERM through taskkill.  The
        # caller already proved current-user ownership, so a failed graceful
        # attempt is safely completed with a bounded forced termination.
        return terminate_pid(pid, force=True)
    detail = (result.stderr or result.stdout or "").strip()
    return False, detail or "结束进程失败（exit %d）" % result.returncode


def terminate_process_group(group_id, force=False):
    """Terminate a validated Windows process tree/job."""
    if not IS_WINDOWS:
        return False, "Windows 进程组终止函数被错误调用"
    with _JOB_LOCK:
        handle = _JOB_HANDLES.get(int(group_id))
    if force and handle and _kernel32.TerminateJobObject(handle, 1):
        return True, None
    flags = ["taskkill.exe", "/PID", str(int(group_id)), "/T"]
    if force:
        flags.append("/F")
    try:
        result = subprocess.run(flags, capture_output=True, text=True,
                                errors="replace", timeout=8,
                                creationflags=getattr(
                                    subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return False, "停止进程组超时"
    except OSError as e:
        return False, "停止进程组失败: %s" % e
    if result.returncode == 0 or not process_group_members(group_id):
        return True, None
    # ``taskkill /T`` cannot politely close a console tree when a descendant
    # owns the console.  The target has already passed token + owner checks in
    # server.py, so a forced tree termination is still bounded to this job/root.
    if not force:
        return terminate_process_group(group_id, force=True)
    detail = (result.stderr or result.stdout or "").strip()
    return False, detail or "停止进程组失败（exit %d）" % result.returncode


def process_group_id(pid):
    if IS_WINDOWS:
        return int(pid) if pid_alive(pid) else None
    try:
        return os.getpgid(int(pid))
    except (ProcessLookupError, PermissionError, OSError, ValueError):
        return None


def shell_command(command, token, wrapper_path=None):
    """Build a token-bearing command line for a managed Windows process.

    A batch wrapper is used on Windows instead of putting the configured
    command directly after ``cmd /c``.  The latter has special first/last
    quote rules and breaks perfectly valid commands such as Python ``-c``
    snippets containing semicolons or nested quotes.
    """
    marker = "console-run:" + str(token)
    comspec = os.environ.get("ComSpec") or os.environ.get("COMSPEC") or "cmd.exe"
    if wrapper_path:
        # Keep the marker in the controller's command line.  The batch file
        # receives it as %1, while ``call`` preserves its exit code and works
        # for paths containing spaces, &, parentheses and non-ASCII text.
        call = "call %s %s" % (
            subprocess.list2cmdline([str(wrapper_path)]), marker)
        return [comspec, "/d", "/q", "/c", call]
    # Fallback for callers that only need a one-shot command.  Product startup
    # always supplies a wrapper_path.
    inner = "%s\r\nrem %s" % (command, marker)
    return [comspec, "/d", "/s", "/c", inner]


def windows_compat_command(command):
    """Adapt common POSIX runtime aliases found in migrated app configs."""
    if not IS_WINDOWS or not isinstance(command, str):
        return command
    command = re.sub(
        r"(?<![A-Za-z0-9_.-])python3(?:\.exe)?(?![A-Za-z0-9_.-])",
        "python", command, flags=re.IGNORECASE)
    # POSIX app configs commonly quote ``python -c`` with single quotes.
    # cmd.exe passes those quote characters literally, so translate only the
    # simple no-nesting form; commands with nested quotes remain untouched and
    # are treated as user-authored Windows shell syntax.
    if re.search(r"\bpython(?:\.exe)?\s+-c\s+'[^'\r\n]*'", command,
                 re.IGNORECASE):
        command = re.sub(
            r"(\bpython(?:\.exe)?\s+-c\s+)'([^'\r\n]*)'",
            lambda match: '%s"%s"' % (match.group(1),
                                       match.group(2).replace('"', '\\"')),
            command, flags=re.IGNORECASE)
    return command


def write_command_wrapper(path, command, token):
    """Write a UTF-8 batch controller for one managed application."""
    marker = "console-run:" + str(token)
    payload = (
        "@echo off\r\n"
        "set \"CONSOLE_RUN_TOKEN=%s\"\r\n"
        "rem %s\r\n"
        "%s\r\n"
        "exit /b %%ERRORLEVEL%%\r\n"
    ) % (str(token), marker, command)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(payload)


def process_creation_flags():
    if not IS_WINDOWS:
        return 0
    return (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) |
            getattr(subprocess, "CREATE_NO_WINDOW", 0) |
            getattr(subprocess, "CREATE_UNICODE_ENVIRONMENT", 0))


def acquire_instance_lock(path, pid):
    """Acquire a one-byte advisory lock using msvcrt on Windows."""
    if not IS_WINDOWS:
        raise RuntimeError("Windows instance lock used on a POSIX host")
    import msvcrt

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    lock_file = open(path, "a+", encoding="ascii")
    try:
        lock_file.seek(0)
        if os.path.getsize(path) == 0:
            lock_file.write("0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock_file.close()
            return None
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write("%d\n" % int(pid))
        lock_file.flush()
        return lock_file
    except Exception:
        lock_file.close()
        raise


def release_instance_lock(lock_file):
    if lock_file is None:
        return
    if IS_WINDOWS:
        import msvcrt
        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    lock_file.close()


def windows_command_quote(path):
    return subprocess.list2cmdline([str(path)])
