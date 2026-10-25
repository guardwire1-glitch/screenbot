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
   SSMS, Notepad++, and any other editor that saves to disk. It refuses to
   write when the window title says the buffer has unsaved changes.

The disk adapter is deliberately conservative: it will not guess that an
unsaved JetBrains buffer can be overwritten. ScreenBot queues such a change for
review instead. The bridge protocol is public, so native plugins for the other
IDEs can be added without changing bot.py.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

DIRTY_MARKERS = ("●", "•", "*", "[*]", "• ")


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


def _foreground_window_info() -> Tuple[Optional[int], str, str]:
    """Return (hwnd, window title, lowercase exe name) for the foreground window."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None, "", ""

        length = int(user32.GetWindowTextLengthW(hwnd))
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe_name = ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if handle:
            try:
                size = wintypes.DWORD(32768)
                path_buf = ctypes.create_unicode_buffer(size.value)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path_buf, ctypes.byref(size)):
                    exe_name = Path(path_buf.value).name.lower()
            finally:
                kernel32.CloseHandle(handle)
        return hwnd, title, exe_name
    except Exception:
        return None, "", ""


def detect_ide(exe_name: str = "", title: str = "") -> str:
    exe = (exe_name or "").lower()
    low_title = (title or "").lower()
    if exe in {"antigravity.exe"} or "antigravity" in low_title:
        return "antigravity"
    if exe in {"code.exe", "cursor.exe", "code - insiders.exe", "windsurf.exe"}:
        return "vscode"
    if exe in {"devenv.exe"} or "microsoft visual studio" in low_title:
        return "visualstudio"
    if exe in {"ssms.exe", "ssms-splash-screen.exe"} or "sql server management studio" in low_title:
        return "ssms"
    if exe in {
        "idea64.exe", "idea.exe", "pycharm64.exe", "pycharm.exe",
        "webstorm64.exe", "webstorm.exe", "clion64.exe", "clion.exe",
        "rider64.exe", "rider.exe", "goland64.exe", "goland.exe",
        "phpstorm64.exe", "phpstorm.exe", "datagrip64.exe", "datagrip.exe",
    } or "jetbrains" in low_title:
        return "jetbrains"
    if exe in {"eclipse.exe"} or "eclipse" in low_title:
        return "eclipse"
    if exe in {"netbeans.exe", "netbeans64.exe"} or "netbeans" in low_title:
        return "netbeans"
    if exe in {"notepad++.exe", "notepad.exe", "sublime_text.exe"}:
        return "editor"
    return "unknown"


def title_is_dirty(title: str) -> bool:
    text = (title or "").strip()
    if not text:
        return False
    return text.startswith(DIRTY_MARKERS) or " ● " in text or text.startswith("●")


@dataclass
class EditorContext:
    file_path: str
    text: str
    language: str
    dirty: bool
    source: str
    hash: str
    cursor_line: int = 0
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    can_apply: bool = True
    detail: str = ""

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], source: str) -> "EditorContext":
        text = payload.get("text") or ""
        path = payload.get("file_path") or ""
        return cls(
            file_path=path,
            text=text,
            language=payload.get("language") or language_for_path(path),
            dirty=bool(payload.get("dirty", False)),
            source=source,
            hash=payload.get("hash") or sha256_text(text),
            cursor_line=int(payload.get("cursor_line") or 0),
            diagnostics=list(payload.get("diagnostics") or []),
            can_apply=bool(payload.get("can_apply", True)),
            detail=str(payload.get("detail") or ""),
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


class VisualStudioAdapter:
    """Optional DTE adapter. No pywin32 means the router falls back to disk."""

    name = "visualstudio"

    def __init__(self, logger=None):
        self.logger = logger
        self._dte = None
        self._checked = False

    def _connect(self):
        if self._checked:
            return self._dte
        self._checked = True
        try:
            import win32com.client  # type: ignore
            try:
                self._dte = win32com.client.GetActiveObject("VisualStudio.DTE")
            except Exception:
                self._dte = win32com.client.GetActiveObject("VisualStudio.DTE.17.0")
        except Exception:
            self._dte = None
        return self._dte

    def available(self) -> bool:
        return self._connect() is not None

    def get_context(
        self, timeout: float = 2.0, file_path: Optional[str] = None
    ) -> Optional[EditorContext]:
        dte = self._connect()
        if dte is None:
            return None
        try:
            doc = None
            if file_path:
                wanted = str(Path(file_path).resolve()).lower()
                for candidate in dte.Documents:
                    if str(candidate.FullName).lower() == wanted:
                        doc = candidate
                        break
                if doc is None:
                    return None
            else:
                doc = dte.ActiveDocument
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
        try:
            doc = None
            for candidate in dte.Documents:
                if str(candidate.FullName).lower() == context.file_path.lower():
                    doc = candidate
                    break
            if doc is None:
                doc = dte.ActiveDocument
            if doc is None or str(doc.FullName).lower() != context.file_path.lower():
                return ApplyResult(ok=False, conflict=True, error="target document is not open", source=self.name)
            text_doc = doc.Object("TextDocument")
            current = text_doc.StartPoint.CreateEditPoint().GetText(text_doc.EndPoint)
            if sha256_text(current) != (base_hash or context.hash):
                return ApplyResult(ok=False, conflict=True, error="buffer changed", source=self.name)
            edit = text_doc.StartPoint.CreateEditPoint()
            edit.StartOfDocument()
            edit.Delete(text_doc.EndPoint)
            edit.Insert(new_text)
            doc.Save()
            return ApplyResult(
                ok=True,
                applied=True,
                text=new_text,
                hash=sha256_text(new_text),
                diagnostics=self._diagnostics(dte),
                source=self.name,
            )
        except Exception as exc:
            return ApplyResult(ok=False, error=str(exc), source=self.name)


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
    ) -> ApplyResult:
        if context.dirty:
            return ApplyResult(ok=False, dirty=True, error="editor buffer has unsaved changes", source=self.name)
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
        dirty = title_is_dirty(title)
        if requested:
            # Only trust the foreground title's dirty marker when it is about
            # the file we are editing. Another app's asterisk is meaningless.
            dirty = dirty and Path(requested).name.lower() in (title or "").lower()
        ctx.dirty = dirty
        ctx.can_apply = not dirty
        ctx.detail = f"{ide}:{exe or 'unknown'}"
        return ctx

    def apply(
        self,
        context: EditorContext,
        new_text: str,
        base_hash: Optional[str] = None,
        format_on_apply: Optional[bool] = None,
    ) -> ApplyResult:
        fmt = self.format_on_apply if format_on_apply is None else format_on_apply
        if context.source in {"vscode", "antigravity"}:
            return self.extension.apply(context, new_text, base_hash, fmt)
        if context.source == "visualstudio":
            return self.visual_studio.apply(context, new_text, base_hash, fmt)
        return self.disk.apply(context, new_text, base_hash, fmt)

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
        result = self.apply(context, item.proposed_text)
        if not result.ok:
            return {"ok": False, "error": result.error, "conflict": result.conflict}
        self.bridge.pending.remove(item_id)
        return {"ok": True, "applied": True}
