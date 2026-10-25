"""
Editor adapters used by the live follower.

Supported today, in priority order:

1. VS Code / Antigravity / Cursor extension (same VS Code extension API) -
   exact buffer text, dirty flag, diagnostics, formatting provider, hash-checked
   replacement, and undo snapshots. This is the only adapter that can safely
   edit an unsaved buffer.
2. Visual Studio DTE (optional; requires pywin32). Used when a Visual Studio
   instance is reachable through COM.
3. Universal disk adapter - works with IntelliJ/JetBrains, Eclipse, NetBeans,
   SSMS, Notepad++, and any other editor that saves to disk. It writes only
   when it can prove the editor's buffer is saved; otherwise it queues the
   change for review.

The disk adapter is deliberately conservative: most non-VS Code editors do not
expose saved/unsaved state in their window titles, so ScreenBot treats "cannot
tell" as "do not write". The bridge protocol is public, so native plugins for
the other IDEs can be added without changing bot.py.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from editor_bridge import BridgeError, BridgeServer, NoEditorHost, PendingChange

_EXT_LANG = {
    ".cs": "csharp", ".java": "java", ".py": "python", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".js": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".sql": "sql", ".html": "html", ".htm": "html", ".css": "css",
    ".go": "go", ".rs": "rust", ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift", ".php": "php", ".rb": "ruby", ".sh": "shell",
    ".ps1": "shell", ".xml": "xml", ".json": "json",
}

DIRTY_MARKERS = ("●", "•", "*", "[*]")
_TITLE_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")
_TRAILING_DIRTY = re.compile(r"\*(?:\s+[-\u2013\u2014]\s+|$)")

# Windows image names are the only trustworthy IDE signal. A browser tab can
# be titled "Eclipse tutorial" or "JetBrains keynote"; a process cannot.
_EXE_IDES = {
    "antigravity.exe": "antigravity",
    "code.exe": "vscode",
    "code - insiders.exe": "vscode",
    "cursor.exe": "vscode",
    "windsurf.exe": "vscode",
    "devenv.exe": "visualstudio",
    "ssms.exe": "ssms",
    "ssms-splash-screen.exe": "ssms",
    "idea64.exe": "jetbrains", "idea.exe": "jetbrains",
    "pycharm64.exe": "jetbrains", "pycharm.exe": "jetbrains",
    "webstorm64.exe": "jetbrains", "webstorm.exe": "jetbrains",
    "clion64.exe": "jetbrains", "clion.exe": "jetbrains",
    "rider64.exe": "jetbrains", "rider.exe": "jetbrains",
    "goland64.exe": "jetbrains", "goland.exe": "jetbrains",
    "phpstorm64.exe": "jetbrains", "phpstorm.exe": "jetbrains",
    "datagrip64.exe": "jetbrains", "datagrip.exe": "jetbrains",
    "eclipse.exe": "eclipse",
    "netbeans.exe": "netbeans", "netbeans64.exe": "netbeans",
    "notepad++.exe": "editor", "notepad.exe": "editor",
    "sublime_text.exe": "editor",
}

# Processes that display arbitrary document titles but never edit a local file.
# They must never be classified from their title.
_NON_IDE_HOSTS = {
    "chrome.exe", "msedge.exe", "msedgewebview2.exe", "firefox.exe",
    "brave.exe", "opera.exe", "vivaldi.exe", "iexplore.exe",
    "teams.exe", "ms-teams.exe", "zoom.exe", "skype.exe",
    "vlc.exe", "wmplayer.exe", "spotify.exe", "explorer.exe",
    "acrord32.exe", "winword.exe", "excel.exe", "powerpnt.exe",
}

# Title fallback rules are anchored to the last " - " segment, the shape of a
# document window. Bare phrases such as "Eclipse tutorial" never match.
_TITLE_IDES = (
    ("antigravity", "antigravity"),
    ("visual studio code", "vscode"),
    ("microsoft visual studio", "visualstudio"),
    ("sql server management studio", "ssms"),
    ("intellij idea", "jetbrains"),
    ("pycharm", "jetbrains"),
    ("webstorm", "jetbrains"),
    ("clion", "jetbrains"),
    ("goland", "jetbrains"),
    ("rider", "jetbrains"),
    ("phpstorm", "jetbrains"),
    ("datagrip", "jetbrains"),
    ("eclipse", "eclipse"),
    ("netbeans", "netbeans"),
    ("notepad++", "editor"),
    ("sublime text", "editor"),
)

# The only desktop editors whose title bar is known to show unsaved state.
# Everything else is treated as unknown, which means "queue, do not write".
_TITLE_CLEAN_TRUSTED = frozenset({"vscode", "antigravity"})

_DTE_PROGIDS = (
    "VisualStudio.DTE.18.0",
    "VisualStudio.DTE.17.0",
    "VisualStudio.DTE.16.0",
    "VisualStudio.DTE",
)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def language_for_path(path: Optional[str]) -> str:
    return _EXT_LANG.get(Path(path or "").suffix.lower(), "unknown")


def _match_newlines(original: str, proposed: str) -> str:
    """Use exactly one newline style: the file's existing one."""
    text = (proposed or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\r\n" in (original or ""):
        return text.replace("\n", "\r\n")
    return text


def _window_title(hwnd) -> str:
    try:
        user32 = ctypes.windll.user32
        length = int(user32.GetWindowTextLengthW(wintypes.HWND(hwnd)))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
        return buf.value
    except Exception:
        return ""


def _process_name(pid: int) -> str:
    if not pid:
        return ""
    try:
        kernel32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
        )
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            path_buf = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(
                handle, 0, path_buf, ctypes.byref(size)
            ):
                return Path(path_buf.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


def _foreground_window_info() -> Tuple[Optional[int], str, str]:
    """Return (hwnd, window title, lowercase exe name) for the foreground window."""
    try:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None, "", ""
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(
            wintypes.HWND(hwnd), ctypes.byref(pid)
        )
        return hwnd, _window_title(hwnd), _process_name(pid.value)
    except Exception:
        return None, "", ""


def _list_window_info() -> List[Tuple[str, str]]:
    """Visible top-level (title, exe) pairs, including background editors."""
    out: List[Tuple[str, str]] = []
    try:
        user32 = ctypes.windll.user32
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        def _visit(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                title = _window_title(hwnd).strip()
                if not title:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                out.append((title, _process_name(pid.value)))
            except Exception:
                pass
            return True

        user32.EnumWindows(callback_type(_visit), 0)
    except Exception:
        return []
    return out


def _ide_from_title(title: str) -> str:
    parts = [
        part.strip().lower()
        for part in _TITLE_SEPARATOR.split(title or "")
        if part.strip()
    ]
    if len(parts) < 2:
        return ""
    tail = parts[-1]
    for prefix, ide in _TITLE_IDES:
        if tail.startswith(prefix):
            return ide
    return ""


def detect_ide(exe_name: str = "", title: str = "") -> str:
    """Classify the foreground application.

    The image name is authoritative. The title is consulted only when the
    image name is missing or unrecognized, and known non-editor hosts
    (browsers, meetings, media players) never fall through to it.
    """
    exe = (exe_name or "").strip().lower()
    if exe in _EXE_IDES:
        return _EXE_IDES[exe]
    if exe and exe in _NON_IDE_HOSTS:
        return "unknown"
    return _ide_from_title(title) or "unknown"


def title_is_dirty(title: str) -> bool:
    text = (title or "").strip()
    if not text:
        return False
    if text.startswith(DIRTY_MARKERS):
        return True
    if " ● " in text or " • " in text:
        return True
    # Visual Studio-style "Demo.cs* - Microsoft Visual Studio".
    return bool(_TRAILING_DIRTY.search(text))


def assess_disk_dirty(
    requested: str, windows: Iterable[Tuple[str, str]]
) -> Tuple[Optional[bool], str]:
    """Decide whether a disk write is safe, from every visible editor window.

    Returns (True/False/None, evidence). True means a dirty marker was seen;
    False means a trusted editor title proves the buffer is saved; None means
    the saved state cannot be proven, so the caller must queue the change.
    """
    target = Path(requested).name.lower() if requested else ""
    matched: List[Tuple[str, str]] = []
    editor_running = False
    for title, exe in windows:
        ide = detect_ide(exe, title)
        if ide == "unknown":
            continue
        editor_running = True
        if target and target in (title or "").lower():
            matched.append((title, ide))

    for title, ide in matched:
        if title_is_dirty(title):
            return True, f"unsaved changes are visible in the {ide} window title"

    for _title, ide in matched:
        if ide in _TITLE_CLEAN_TRUSTED:
            return False, ""

    if matched:
        ide = matched[0][1]
        return None, (
            f"{ide} does not report unsaved changes reliably in its window "
            "title; save the file and accept the review item, or bind the file "
            "through the editor bridge"
        )
    if editor_running:
        return None, (
            "the file is not visible in any editor window while an editor is "
            "running; it may be open in a background tab with unsaved changes"
        )
    return False, ""



@dataclass
class EditorContext:
    file_path: str
    text: str
    language: str
    dirty: Optional[bool]
    source: str
    hash: str
    cursor_line: int = 0
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    can_apply: bool = True
    detail: str = ""
    hold_reason: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], source: str) -> "EditorContext":
        text = payload.get("text") or ""
        path = payload.get("file_path") or ""
        raw_dirty = payload.get("dirty", False)
        return cls(
            file_path=path,
            text=text,
            language=payload.get("language") or language_for_path(path),
            dirty=None if raw_dirty is None else bool(raw_dirty),
            source=source,
            hash=payload.get("hash") or sha256_text(text),
            cursor_line=int(payload.get("cursor_line") or 0),
            diagnostics=list(payload.get("diagnostics") or []),
            can_apply=bool(payload.get("can_apply", True)),
            detail=str(payload.get("detail") or ""),
            hold_reason=str(payload.get("hold_reason") or ""),
        )


@dataclass
class ApplyResult:
    ok: bool
    applied: bool = False
    text: str = ""
    hash: str = ""
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    conflict: bool = False
    dirty: bool = False
    error: str = ""
    source: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], source: str) -> "ApplyResult":
        text = payload.get("text")
        return cls(
            ok=bool(payload.get("ok", False)),
            applied=bool(payload.get("applied", False)),
            text=text if isinstance(text, str) else "",
            hash=payload.get("hash") or (sha256_text(text) if isinstance(text, str) else ""),
            diagnostics=list(payload.get("diagnostics") or []),
            conflict=bool(payload.get("conflict", False)),
            dirty=bool(payload.get("dirty", False)),
            error=str(payload.get("error") or ""),
            source=source,
        )


class ExtensionAdapter:
    """Adapter for the VS Code API extension (also Antigravity/Cursor)."""

    name = "extension"

    def __init__(self, bridge: BridgeServer, logger=None):
        self.bridge = bridge
        self.logger = logger

    def available(self) -> bool:
        return bool(self.bridge.live_hosts())

    def get_context(
        self, ide: str, timeout: float = 2.0, file_path: Optional[str] = None
    ) -> Optional[EditorContext]:
        if not self.available():
            return None
        job_payload: Dict[str, Any] = {"ide": ide, "active": file_path is None}
        if file_path:
            job_payload["file_path"] = file_path
        try:
            payload = self.bridge.request_job(
                "context", job_payload, timeout=timeout, ide=ide
            )
        except (NoEditorHost, BridgeError) as exc:
            if self.logger:
                self.logger.debug(f"Extension context failed: {exc}")
            return None
        if not payload.get("ok"):
            return None
        return EditorContext.from_payload(payload, source=ide or "vscode")

    def apply(
        self,
        context: EditorContext,
        new_text: str,
        base_hash: Optional[str] = None,
        format_on_apply: bool = False,
        timeout: float = 10.0,
    ) -> ApplyResult:
        payload = {
            "file_path": context.file_path,
            "base_hash": base_hash or context.hash,
            "text": new_text,
            "save": True,
            "format": bool(format_on_apply),
        }
        try:
            result = self.bridge.request_job("apply", payload, timeout=timeout, ide=context.source)
        except (NoEditorHost, BridgeError) as exc:
            return ApplyResult(ok=False, error=str(exc), source=context.source)
        return ApplyResult.from_payload(result, source=context.source)


@dataclass
class _VisualStudioThreadState:
    dte: Any = None
    com_checked: bool = False
    com_ok: bool = False
    next_attempt: float = 0.0
    delay: float = 5.0


def _connect_active_dte():
    import win32com.client  # type: ignore

    last_error: Optional[Exception] = None
    for progid in _DTE_PROGIDS:
        try:
            return win32com.client.GetActiveObject(progid)
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"no running Visual Studio instance ({last_error})") from last_error


def _initialize_com() -> bool:
    try:
        import pythoncom  # type: ignore
    except Exception:
        return False
    try:
        pythoncom.CoInitialize()
    except Exception:
        # RPC_E_CHANGED_MODE means this thread is already initialized in a
        # different apartment. COM itself is usable, so keep going.
        pass
    return True


def _dte_line_column(text: str, index: int) -> Tuple[int, int]:
    """Map a 0-based text index to a DTE 1-based line and column.

    DTE absolute offsets count line breaks differently from Python and reject
    the position one past a final newline, so positions are expressed as
    line/column instead.
    """
    index = max(0, min(index, len(text)))
    line = text.count("\n", 0, index) + 1
    line_start = text.rfind("\n", 0, index) + 1
    return line, index - line_start + 1


def _dte_edit_point(text_doc, text: str, index: int):
    line, column = _dte_line_column(text, index)
    point = text_doc.StartPoint.CreateEditPoint()
    point.MoveToLineAndOffset(line, column)
    return point


def _minimal_line_edit(
    current: str, proposed: str
) -> Optional[Tuple[int, int, str]]:
    """Return (start, end, replacement) for a line-aligned minimal edit."""
    if current == proposed:
        return None
    limit = min(len(current), len(proposed))
    prefix = 0
    while prefix < limit and current[prefix] == proposed[prefix]:
        prefix += 1
    start = current.rfind("\n", 0, prefix) + 1

    suffix = 0
    max_suffix = limit - prefix
    while (
        suffix < max_suffix
        and current[-1 - suffix] == proposed[-1 - suffix]
    ):
        suffix += 1
    old_end = len(current) - suffix
    if old_end > 0 and current[old_end - 1] != "\n":
        newline = current.find("\n", old_end)
        old_end = len(current) if newline == -1 else newline + 1
    new_end = len(proposed) - (len(current) - old_end)
    return start, old_end, proposed[start:new_end]


class VisualStudioAdapter:
    """Optional DTE adapter with per-thread COM state and reconnect backoff.

    COM objects have apartment affinity, so the DTE pointer and retry state are
    thread-local: the scanner thread and the bridge request thread each
    initialize COM for themselves. A missing or restarted Visual Studio is
    retried with a short exponential backoff instead of disabling the adapter
    for the lifetime of ScreenBot.
    """

    name = "visualstudio"

    def __init__(
        self,
        logger=None,
        connect: Optional[Callable[[], Any]] = None,
        initialize_com: Optional[Callable[[], bool]] = None,
        clock: Optional[Callable[[], float]] = None,
        retry_initial: float = 5.0,
        retry_max: float = 30.0,
    ):
        self.logger = logger
        self._connect_fn = connect or _connect_active_dte
        self._initialize_com = initialize_com or _initialize_com
        self._clock = clock or time.monotonic
        self._retry_initial = max(0.0, float(retry_initial))
        self._retry_max = max(self._retry_initial, float(retry_max))
        self._local = threading.local()

    def _state(self) -> _VisualStudioThreadState:
        state = getattr(self._local, "state", None)
        if state is None:
            state = _VisualStudioThreadState(delay=self._retry_initial)
            self._local.state = state
        return state

    def _connect(self):
        state = self._state()
        if state.dte is not None:
            return state.dte
        now = self._clock()
        if now < state.next_attempt:
            return None
        if not state.com_checked:
            state.com_checked = True
            state.com_ok = bool(self._initialize_com())
        if not state.com_ok:
            state.next_attempt = now + self._retry_max
            return None
        try:
            dte = self._connect_fn()
        except Exception as exc:
            wait = state.delay
            state.next_attempt = now + wait
            state.delay = min(state.delay * 2.0, self._retry_max)
            if self.logger:
                self.logger.debug(
                    f"Visual Studio DTE unavailable ({exc}); "
                    f"retrying in {wait:.0f}s."
                )
            return None
        state.dte = dte
        state.delay = self._retry_initial
        state.next_attempt = 0.0
        if self.logger:
            self.logger.info("Visual Studio DTE connected.")
        return dte

    def _invalidate(self, dte) -> None:
        state = self._state()
        if state.dte is dte:
            state.dte = None
            state.next_attempt = 0.0
            state.delay = self._retry_initial

    def available(self) -> bool:
        return self._connect() is not None

    @staticmethod
    def _normalize(path: Any) -> str:
        try:
            return str(Path(str(path)).resolve()).lower()
        except Exception:
            return str(path or "").replace("/", "\\").lower()

    @classmethod
    def _document_for(
        cls, dte, file_path: Optional[str], allow_active: bool = False
    ):
        if not file_path:
            return dte.ActiveDocument
        wanted = cls._normalize(file_path)
        for candidate in dte.Documents:
            if cls._normalize(candidate.FullName) == wanted:
                return candidate
        if allow_active:
            doc = dte.ActiveDocument
            if doc is not None and cls._normalize(doc.FullName) == wanted:
                return doc
        return None

    def get_context(
        self, timeout: float = 2.0, file_path: Optional[str] = None
    ) -> Optional[EditorContext]:
        dte = self._connect()
        if dte is None:
            return None
        try:
            doc = self._document_for(dte, file_path)
            if doc is None:
                return None
            text_doc = doc.Object("TextDocument")
            edit = text_doc.StartPoint.CreateEditPoint()
            text = edit.GetText(text_doc.EndPoint)
            path = str(doc.FullName)
            diagnostics = self._diagnostics(dte)
            return EditorContext(
                file_path=path,
                text=text,
                language=language_for_path(path),
                dirty=not bool(doc.Saved),
                source=self.name,
                hash=sha256_text(text),
                diagnostics=diagnostics,
            )
        except Exception as exc:
            self._invalidate(dte)
            if self.logger:
                self.logger.debug(f"DTE context failed: {exc}")
            return None

    def _diagnostics(self, dte) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            items = dte.ToolWindows.ErrorList.ErrorItems
            for i in range(1, min(items.Count, 200) + 1):
                item = items.Item(i)
                out.append({
                    "file": str(getattr(item, "FileName", "") or ""),
                    "line": int(getattr(item, "Line", 0) or 0),
                    "severity": "error" if int(getattr(item, "ErrorLevel", 0) or 0) <= 1 else "warning",
                    "message": str(getattr(item, "Description", "") or ""),
                })
        except Exception:
            pass
        return out

    def apply(
        self,
        context: EditorContext,
        new_text: str,
        base_hash: Optional[str] = None,
        format_on_apply: bool = False,
        timeout: float = 10.0,
    ) -> ApplyResult:
        dte = self._connect()
        if dte is None:
            return ApplyResult(ok=False, error="Visual Studio DTE is not available", source=self.name)

        undo = None
        undo_opened = False
        saved = False
        final_text = ""
        try:
            doc = self._document_for(dte, context.file_path, allow_active=True)
            if doc is None:
                return ApplyResult(
                    ok=False, conflict=True,
                    error="target document is not open", source=self.name,
                )
            text_doc = doc.Object("TextDocument")
            current = text_doc.StartPoint.CreateEditPoint().GetText(text_doc.EndPoint)
            if sha256_text(current) != (base_hash or context.hash):
                return ApplyResult(
                    ok=False, conflict=True,
                    error="buffer changed", source=self.name,
                )

            proposed = _match_newlines(current, new_text)
            edit_range = _minimal_line_edit(current, proposed)
            if edit_range is None:
                return ApplyResult(
                    ok=True, applied=False, text=current,
                    hash=sha256_text(current),
                    diagnostics=self._diagnostics(dte), source=self.name,
                )

            undo = dte.UndoContext
            if bool(undo.IsOpen):
                # Joining someone else's transaction would couple our edit to
                # theirs; refuse instead of closing a context we did not open.
                return ApplyResult(
                    ok=False,
                    error="Visual Studio has an undo operation open; change held",
                    source=self.name,
                )
            undo.Open("ScreenBot: apply lecture code", False)
            undo_opened = True

            start_offset, end_offset, replacement = edit_range
            edit_point = _dte_edit_point(text_doc, current, start_offset)
            if end_offset > start_offset:
                end_point = _dte_edit_point(text_doc, current, end_offset)
                edit_point.Delete(end_point)
            if replacement:
                edit_point.Insert(replacement)

            final_text = text_doc.StartPoint.CreateEditPoint().GetText(text_doc.EndPoint)
            if final_text.replace("\r\n", "\n") != proposed.replace("\r\n", "\n"):
                raise RuntimeError("Visual Studio buffer did not match the proposed edit")
            doc.Save()
            saved = True
        except Exception as exc:
            if undo_opened and not saved:
                try:
                    undo.SetAborted()
                except Exception:
                    pass
            if not saved:
                self._invalidate(dte)
            if self.logger:
                self.logger.debug(f"DTE apply failed: {exc}")
            return ApplyResult(ok=False, error=str(exc), source=self.name)
        finally:
            if undo_opened:
                try:
                    undo.Close()
                except Exception as exc:
                    if self.logger:
                        self.logger.warning(f"DTE undo context close failed: {exc}")

        return ApplyResult(
            ok=True,
            applied=True,
            text=final_text,
            hash=sha256_text(final_text),
            diagnostics=self._diagnostics(dte),
            source=self.name,
        )



class DiskAdapter:
    """Universal adapter. Works with any IDE that persists documents to disk."""

    name = "disk"

    def __init__(self, resolver: Callable[[], Optional[Path]], logger=None):
        self.resolver = resolver
        self.logger = logger

    def get_context(self, file_path: Optional[str] = None) -> Optional[EditorContext]:
        path = file_path or self.resolver()
        if not path:
            return None
        try:
            path = Path(path).resolve()
            if not path.is_file():
                return None
            text = _read_text(path)
            return EditorContext(
                file_path=str(path),
                text=text,
                language=language_for_path(str(path)),
                dirty=False,
                source=self.name,
                hash=sha256_text(text),
                can_apply=True,
                detail="disk",
            )
        except Exception as exc:
            if self.logger:
                self.logger.debug(f"Disk context failed: {exc}")
            return None

    def apply(
        self,
        context: EditorContext,
        new_text: str,
        base_hash: Optional[str] = None,
        format_on_apply: bool = False,
        timeout: float = 10.0,
        explicit: bool = False,
    ) -> ApplyResult:
        if context.dirty is True:
            return ApplyResult(ok=False, dirty=True, error="editor buffer has unsaved changes", source=self.name)
        if (context.dirty is None or not context.can_apply) and not explicit:
            return ApplyResult(
                ok=False,
                dirty=True,
                error=context.hold_reason or "cannot verify the editor buffer is saved",
                source=self.name,
            )
        path = Path(context.file_path)
        try:
            current = _read_text(path)
        except Exception as exc:
            return ApplyResult(ok=False, error=f"cannot re-read file: {exc}", source=self.name)
        if sha256_text(current) != (base_hash or context.hash):
            return ApplyResult(ok=False, conflict=True, error="file changed on disk", source=self.name)
        text_to_write = _match_newlines(current, new_text)
        ok, err = _atomic_write(path, text_to_write)
        if not ok:
            return ApplyResult(ok=False, error=err, source=self.name)
        return ApplyResult(
            ok=True,
            applied=True,
            text=text_to_write,
            hash=sha256_text(text_to_write),
            source=self.name,
        )


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _atomic_write(path: Path, text: str) -> Tuple[bool, str]:
    last_err = ""
    for attempt in range(3):
        tmp = path.with_name(path.name + f".sb_{os.getpid()}_{attempt}.tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return True, ""
        except Exception as exc:
            last_err = str(exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            time.sleep(0.12)
    return False, last_err


class EditorRouter:
    """Chooses the best available adapter and enforces dirty/conflict rules."""

    def __init__(
        self,
        bridge: BridgeServer,
        resolver: Callable[[], Optional[Path]],
        logger=None,
        format_on_apply: bool = False,
    ):
        self.bridge = bridge
        self.logger = logger
        self.format_on_apply = format_on_apply
        self.extension = ExtensionAdapter(bridge, logger)
        self.visual_studio = VisualStudioAdapter(logger)
        self.disk = DiskAdapter(resolver, logger)

    def active_window(self) -> Tuple[Optional[int], str, str, str]:
        hwnd, title, exe = _foreground_window_info()
        return hwnd, title, exe, detect_ide(exe, title)

    def get_context(
        self, file_path: Optional[str] = None, prefer_extension: bool = True
    ) -> Optional[EditorContext]:
        """Return ground truth for `file_path` (or the active editor).

        An explicit path is never silently swapped for a different active file:
        the extension is asked for that exact path and the disk adapter reads
        exactly that file. This is what stops a bound C# file from being
        replaced by whatever tab happens to be focused.
        """
        hwnd, title, exe, ide = self.active_window()
        requested = str(file_path) if file_path else None

        wants_extension = prefer_extension and (
            ide in {"vscode", "antigravity"} or (requested and self.extension.available())
        )
        if wants_extension:
            ctx = self.extension.get_context(ide, file_path=requested)
            if ctx is None and requested and self.extension.available():
                # The file may be open in another VS Code-compatible host that
                # is not foreground. Exact-path matching keeps this safe.
                for host in self.bridge.live_hosts():
                    if host == (ide or "").lower():
                        continue
                    ctx = self.extension.get_context(host, file_path=requested)
                    if ctx is not None:
                        break
            if ctx is not None:
                return ctx

        if (ide == "visualstudio" or requested) and self.visual_studio.available():
            ctx = self.visual_studio.get_context(file_path=requested)
            if ctx is not None:
                return ctx

        ctx = self.disk.get_context(requested)
        if ctx is None:
            return None
        # The foreground window is usually the lecture video, so inspect every
        # visible editor window for the target file rather than only the title
        # of whatever happens to be focused.
        windows = _list_window_info()
        if not windows:
            windows = [(title, exe)] if (title or exe) else []
        dirty, evidence = assess_disk_dirty(ctx.file_path, windows)
        ctx.dirty = dirty
        ctx.can_apply = dirty is False
        ctx.hold_reason = evidence
        ctx.detail = f"{ide}:{exe or 'unknown'}"
        return ctx

    def apply(
        self,
        context: EditorContext,
        new_text: str,
        base_hash: Optional[str] = None,
        format_on_apply: Optional[bool] = None,
        explicit: bool = False,
    ) -> ApplyResult:
        fmt = self.format_on_apply if format_on_apply is None else format_on_apply
        if context.source in {"vscode", "antigravity"}:
            return self.extension.apply(context, new_text, base_hash, fmt)
        if context.source == "visualstudio":
            return self.visual_studio.apply(context, new_text, base_hash, fmt)
        return self.disk.apply(
            context, new_text, base_hash, fmt, explicit=explicit
        )

    def restore(
        self,
        context: EditorContext,
        original_text: str,
        applied_hash: str = "",
    ) -> ApplyResult:
        """Put `original_text` back after a rejected/diagnostic-worsening apply.
        `applied_hash` is the hash the successful apply reported, because the
        buffer/file no longer matches `context.hash`."""
        base = applied_hash or sha256_text(context.text)
        return self.apply(context, original_text, base_hash=base)

    def queue_pending(
        self,
        context: EditorContext,
        proposed_text: str,
        reason: str,
        diagnostics: Optional[List[Dict[str, Any]]] = None,
    ) -> PendingChange:
        change = PendingChange(
            id=f"pc_{int(time.time() * 1000)}_{os.getpid()}",
            created_at=time.time(),
            file_path=context.file_path,
            base_hash=context.hash,
            proposed_text=proposed_text,
            reason=reason,
            source=context.source,
            diagnostics=diagnostics or [],
        )
        return self.bridge.pending.add(change)

    def resolve_pending(self, item_id: str, accept: bool) -> Dict[str, Any]:
        item = self.bridge.pending.get(item_id)
        if item is None:
            return {"ok": False, "error": "pending change not found"}
        if not accept:
            self.bridge.pending.remove(item_id)
            return {"ok": True, "accepted": False}
        context = self.get_context(file_path=item.file_path)
        if context is None:
            return {"ok": False, "error": "no editor context"}
        if Path(context.file_path).resolve() != Path(item.file_path).resolve():
            return {"ok": False, "error": "a different file is active"}
        if context.hash != item.base_hash and sha256_text(context.text) != item.base_hash:
            return {"ok": False, "error": "file changed since this change was queued"}
        # Accepting a review item is an explicit decision, so it may bypass the
        # "saved state unknown" hold. A known-unsaved buffer is still refused.
        result = self.apply(context, item.proposed_text, explicit=True)
        if not result.ok:
            return {"ok": False, "error": result.error, "conflict": result.conflict}
        self.bridge.pending.remove(item_id)
        return {
            "ok": True,
            "applied": True,
            "text": result.text,
            "hash": result.hash,
        }
