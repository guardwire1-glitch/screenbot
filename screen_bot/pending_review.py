"""Desktop review window for changes held by the safety gates.

The window is intentionally thin: it lists the bridge's pending store, shows a
diff against the file on disk, and hands accept/dismiss decisions back to the
bot's existing resolver, which re-checks the file hash before writing.
"""

from __future__ import annotations

import difflib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

MAX_PREVIEW_LINES = 400
MAX_LINE_CHARS = 500

# Editors whose adapter can prove saved/unsaved buffer state. Anything else is
# reviewed against disk, so the user must save the file in the editor first.
_TRUSTED_SAVED_STATE = frozenset({"vscode", "antigravity", "visualstudio"})


@dataclass(frozen=True)
class ReviewRow:
    id: str
    file_name: str
    file_path: str
    reason: str
    source: str
    created_at: float


def build_rows(items: Sequence[Any]) -> List[ReviewRow]:
    rows: List[ReviewRow] = []
    for item in items:
        path = str(getattr(item, "file_path", "") or "")
        rows.append(
            ReviewRow(
                id=str(getattr(item, "id", "")),
                file_name=Path(path).name or path or "(unbound)",
                file_path=path,
                reason=str(getattr(item, "reason", "") or "held change"),
                source=str(getattr(item, "source", "") or "disk"),
                created_at=float(getattr(item, "created_at", 0.0) or 0.0),
            )
        )
    rows.sort(key=lambda row: row.created_at, reverse=True)
    return rows


def read_file_text(path: str) -> Optional[str]:
    try:
        return Path(path).read_bytes().decode("utf-8-sig")
    except Exception:
        return None


def preview_text(
    item: Any,
    current_text: Optional[str],
    max_lines: int = MAX_PREVIEW_LINES,
) -> str:
    proposed = str(getattr(item, "proposed_text", "") or "")
    if current_text is None:
        lines = [
            "The file is not readable; showing the proposed text only.",
            "",
        ]
        lines.extend("  " + line for line in proposed.splitlines())
    else:
        diff = list(
            difflib.unified_diff(
                current_text.splitlines(),
                proposed.splitlines(),
                fromfile="current file",
                tofile="proposed",
                lineterm="",
                n=3,
            )
        )
        lines = diff or ["No textual differences from the current file."]

    clipped = [
        line[:MAX_LINE_CHARS] + ("..." if len(line) > MAX_LINE_CHARS else "")
        for line in lines[:max_lines]
    ]
    if len(lines) > max_lines:
        clipped.append(
            f"... preview truncated ({len(lines) - max_lines} more lines) ..."
        )
    return "\n".join(clipped)


def preview_for_item(item: Any, max_lines: int = MAX_PREVIEW_LINES) -> str:
    return preview_text(
        item,
        read_file_text(str(getattr(item, "file_path", "") or "")),
        max_lines=max_lines,
    )


def saved_state_warning(source: str) -> str:
    if str(source or "").lower() in _TRUSTED_SAVED_STATE:
        return ""
    return (
        "This editor does not report unsaved state. Save the file in the editor "
        "before applying."
    )


def shorten_path(path: str, max_chars: int = 88) -> str:
    path = str(path or "")
    if len(path) <= max_chars:
        return path
    keep = max_chars - 3
    head = max(1, keep // 3)
    tail = keep - head
    return path[:head] + "..." + path[-tail:]


def result_message(outcome: Optional[Dict[str, Any]], accepted: bool) -> tuple:
    outcome = outcome or {}
    if outcome.get("ok"):
        return True, "Change applied." if accepted else "Change dismissed."
    error = str(outcome.get("error") or "unknown error")
    if outcome.get("conflict"):
        return False, f"Not applied: {error}. Review the file again."
    return False, f"Not applied: {error}"


def _open_path(path: str) -> None:
    os.startfile(path)  # type: ignore[attr-defined]


class PendingReviewWindow:
    """Tk review window. Call run() from a dedicated thread."""

    def __init__(
        self,
        list_items: Callable[[], Sequence[Any]],
        resolve_item: Callable[[str, bool], Dict[str, Any]],
        open_path: Callable[[str], None] = _open_path,
        logger=None,
    ):
        self._list_items = list_items
        self._resolve_item = resolve_item
        self._open_path = open_path
        self._logger = logger or logging.getLogger(__name__)
        self._items: Dict[str, Any] = {}
        self._rows: List[ReviewRow] = []
        self._refresh_job = None
        self._closed = False

    def run(self) -> None:
        import tkinter as tk
        from tkinter import messagebox, ttk

        self._tk = tk
        self._messagebox = messagebox
        self.root = tk.Tk()
        self.root.title("ScreenBot Pending Changes")
        self.root.geometry("1040x620")
        self.root.minsize(820, 480)

        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="Changes held by ScreenBot's safety gates",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="Applying re-checks the file hash first. Dismissing leaves the file untouched.",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 8))

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=1)
        body.add(right, weight=3)

        self.tree = ttk.Treeview(
            left,
            columns=("file", "reason", "source"),
            show="headings",
            selectmode="browse",
        )
        for column, title, width, stretch in (
            ("file", "File", 180, True),
            ("reason", "Reason", 240, True),
            ("source", "Editor", 80, False),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, stretch=stretch)
        tree_scroll = ttk.Scrollbar(
            left, orient="vertical", command=self.tree.yview
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.header_var = tk.StringVar(value="Select a change")
        ttk.Label(
            right, textvariable=self.header_var, font=("Segoe UI", 10, "bold")
        ).pack(anchor="w")
        self.meta_var = tk.StringVar(value="")
        ttk.Label(
            right,
            textvariable=self.meta_var,
            foreground="#555555",
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(2, 4))
        self.warning_var = tk.StringVar(value="")
        ttk.Label(
            right,
            textvariable=self.warning_var,
            foreground="#b36b00",
            wraplength=680,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        text_frame = ttk.Frame(right)
        text_frame.pack(fill="both", expand=True)
        self.preview = tk.Text(
            text_frame,
            wrap="none",
            font=("Consolas", 9),
            state="disabled",
            background="#fbfbfb",
        )
        text_scroll = ttk.Scrollbar(
            text_frame, orient="vertical", command=self.preview.yview
        )
        text_hscroll = ttk.Scrollbar(
            text_frame, orient="horizontal", command=self.preview.xview
        )
        self.preview.configure(
            yscrollcommand=text_scroll.set, xscrollcommand=text_hscroll.set
        )
        self.preview.grid(row=0, column=0, sticky="nsew")
        text_scroll.grid(row=0, column=1, sticky="ns")
        text_hscroll.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)
        self.preview.tag_configure("add", foreground="#137333")
        self.preview.tag_configure("del", foreground="#b3261e")
        self.preview.tag_configure("hunk", foreground="#1a73e8")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(10, 0))
        self.apply_button = ttk.Button(
            buttons, text="Apply Change", command=self._apply_selected
        )
        self.dismiss_button = ttk.Button(
            buttons, text="Dismiss", command=self._dismiss_selected
        )
        self.open_button = ttk.Button(
            buttons, text="Open File", command=self._open_selected
        )
        for button in (
            self.apply_button,
            self.dismiss_button,
            self.open_button,
        ):
            button.pack(side="left", padx=(0, 6))
        close_button = ttk.Button(buttons, text="Close", command=self._close)
        close_button.pack(side="right")
        refresh_button = ttk.Button(
            buttons, text="Refresh", command=lambda: self._refresh(force=True)
        )
        refresh_button.pack(side="right", padx=(0, 6))

        self.status_var = tk.StringVar(value="")
        ttk.Label(
            outer, textvariable=self.status_var, foreground="#555555"
        ).pack(anchor="w", pady=(6, 0))

        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Escape>", lambda _event: self._close())
        self.root.bind("<Control-r>", lambda _event: self._refresh(force=True))

        self._refresh()
        self._schedule_refresh()
        self.root.mainloop()

    def _current_row(self) -> Optional[ReviewRow]:
        selection = self.tree.selection()
        if not selection:
            return None
        selected = str(selection[0])
        for row in self._rows:
            if row.id == selected:
                return row
        return None

    def _current_item(self) -> Optional[Any]:
        row = self._current_row()
        return self._items.get(row.id) if row is not None else None

    def _selected_id(self) -> str:
        selection = self.tree.selection()
        return str(selection[0]) if selection else ""

    def _refresh(self, force: bool = False) -> None:
        if self._closed:
            return
        selected_id = self._selected_id()
        try:
            items = list(self._list_items())
        except Exception as exc:
            self._set_status(f"Could not read pending changes: {exc}")
            return
        rows = build_rows(items)
        if not force and rows == self._rows and selected_id:
            return
        self._items = {str(item.id): item for item in items}
        self._rows = rows
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        for row in self._rows:
            self.tree.insert(
                "",
                "end",
                iid=row.id,
                values=(row.file_name, row.reason, row.source),
            )
        self.root.title(f"ScreenBot Pending Changes ({len(self._rows)})")
        target = selected_id if selected_id in self._items else (
            self._rows[0].id if self._rows else ""
        )
        if target:
            self.tree.selection_set(target)
            self.tree.focus(target)
            self.tree.see(target)
            self._show_selected()
        else:
            self._clear_detail()

    def _clear_detail(self) -> None:
        self.header_var.set("No changes are waiting for review.")
        self.meta_var.set("")
        self.warning_var.set("")
        self._render_preview("")
        for button in (self.apply_button, self.dismiss_button, self.open_button):
            button.state(["disabled"])

    def _on_select(self, _event=None) -> None:
        self._show_selected()

    def _show_selected(self) -> None:
        row = self._current_row()
        item = self._current_item()
        if row is None or item is None:
            self._clear_detail()
            return
        created = time.strftime("%Y-%m-%d %H:%M", time.localtime(row.created_at))
        self.header_var.set(row.file_name)
        self.meta_var.set(
            f"{row.reason}\n{shorten_path(row.file_path)}\n"
            f"{row.source} · {created}"
        )
        self.warning_var.set(saved_state_warning(row.source))
        self._render_preview(preview_for_item(item))
        for button in (self.apply_button, self.dismiss_button, self.open_button):
            button.state(["!disabled"])

    def _render_preview(self, text: str) -> None:
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        for line in text.splitlines():
            if line.startswith(("+++", "---", "@@")):
                tag = "hunk"
            elif line.startswith("+"):
                tag = "add"
            elif line.startswith("-"):
                tag = "del"
            else:
                tag = ""
            self.preview.insert("end", line + "\n", tag)
        self.preview.configure(state="disabled")

    def _apply_selected(self) -> None:
        row = self._current_row()
        item = self._current_item()
        if row is None or item is None:
            return
        if not self._messagebox.askyesno(
            "Apply pending change",
            f"Apply this change to:\n\n{row.file_path}\n\n"
            "ScreenBot re-checks the file before writing.",
            parent=self.root,
        ):
            return
        try:
            outcome = self._resolve_item(row.id, True)
        except Exception as exc:
            self._report_error(f"Apply failed: {exc}")
            return
        ok, message = result_message(outcome, True)
        self._set_status(message)
        if ok:
            self._refresh()
        else:
            self._messagebox.showwarning(
                "Change not applied", message, parent=self.root
            )
            self._refresh(force=True)

    def _dismiss_selected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        try:
            outcome = self._resolve_item(row.id, False)
        except Exception as exc:
            self._report_error(f"Dismiss failed: {exc}")
            return
        ok, message = result_message(outcome, False)
        self._set_status(message)
        if ok:
            self._refresh()
        else:
            self._messagebox.showwarning(
                "Change not dismissed", message, parent=self.root
            )
            self._refresh(force=True)

    def _open_selected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        try:
            if not row.file_path:
                raise RuntimeError("this change has no file path")
            self._open_path(row.file_path)
        except Exception as exc:
            self._report_error(f"Could not open the file: {exc}")

    def _report_error(self, message: str) -> None:
        self._set_status(message)
        self._logger.error(message)
        try:
            self._messagebox.showerror("ScreenBot", message, parent=self.root)
        except Exception:
            pass

    def _set_status(self, message: str) -> None:
        self.status_var.set(message)

    def _schedule_refresh(self) -> None:
        if self._closed:
            return
        self._refresh_job = self.root.after(2000, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self._closed:
            return
        try:
            self._refresh()
        finally:
            self._schedule_refresh()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
        self.root.destroy()
