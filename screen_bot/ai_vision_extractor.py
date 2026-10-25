"""
Deterministic Gemini Vision extractor for ScreenBot.

The model is treated as an OCR device, not a programmer:
- it must answer with a single JSON envelope,
- it may not invent code, line numbers, or confidence,
- unreadable characters are marked with «UNREADABLE» and rejected downstream,
- a malformed envelope yields zero lines instead of "best effort" raw text.

Compact output (`code` as one string) is the default because it preserves
indentation. Structured `lines` is still accepted for compatibility and for
models that report per-line visibility/confidence.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

try:
    from google import genai
    from google.genai import types
    HAS_NEW_GENAI = True
except ImportError:
    import google.generativeai as legacy_genai
    HAS_NEW_GENAI = False

UNREADABLE_TOKEN = "«UNREADABLE»"

SUPPORTED_LANGUAGES = [
    "java", "csharp", "c", "cpp", "python", "javascript", "typescript",
    "sql", "html", "css", "go", "rust", "kotlin", "swift", "php", "ruby", "shell",
]

_EXT_LANGUAGE = {
    ".cs": "csharp", ".java": "java", ".py": "python", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".sql": "sql", ".html": "html",
    ".css": "css", ".go": "go", ".rs": "rust", ".kt": "kotlin", ".swift": "swift",
    ".php": "php", ".rb": "ruby", ".sh": "shell", ".ps1": "shell",
}


@dataclass
class ExtractedLine:
    line_no: Optional[int]
    text: str
    indent_width: int = 0
    visible: str = "full"
    # None means "the extractor did not report confidence". It must never be
    # silently upgraded to 1.0, because consensus gates rely on this value.
    confidence: Optional[float] = None


@dataclass
class ExtractionResult:
    language: str
    file_name: Optional[str]
    lines: List[ExtractedLine] = field(default_factory=list)
    has_occlusion: bool = False
    flags: List[str] = field(default_factory=list)
    raw_response: str = ""
    error: Optional[str] = None


EXTRACTION_SYSTEM_PROMPT = """You are a deterministic IDE screen-capture OCR engine.
Return ONLY a single compact JSON object with these exact keys:
{
  "tab": "filename with extension visible in the active editor tab, or null",
  "language": "detected language id (csharp, java, python, cpp, ...), or null",
  "overlay": true if a notification, popup, autocomplete dropdown, or call window covers ANY code, else false,
  "eof_visible": true if blank editor space is visible below the last line of code, else false,
  "code": "exact visible source code from the primary editor pane ONLY, separated by \\n"
}

STRICT RULES:
1. Transcribe ONLY code inside the primary code editor pane. Ignore solution explorer, terminal, output pane, browser chrome, video controls, and meeting UI.
2. If the screen shows a slide, agenda, syllabus, document, or any non-code window, return "" for "code".
3. NEVER guess, autocomplete, translate, repair, or invent code. Copy exactly what is visible.
4. Do NOT add line numbers, gutter numbers, markdown fences, labels, or commentary.
5. Preserve the exact leading indentation of every line. Never re-indent.
6. If a character is unreadable or a line is partly covered, write «UNREADABLE» for the unreadable part and set "overlay": true when a popup is responsible.
7. Ignore grey ghost-text autocomplete suggestions (Copilot/IntelliCode).
8. The code may be in ANY programming language. Never assume a language from the subject name.
9. No markdown fences around the JSON."""


@dataclass
class VisionExtraction:
    code: str
    tab_filename: Optional[str] = None
    overlay_detected: bool = False
    eof_visible: bool = False
    language: Optional[str] = None
    latency_sec: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.code.strip())


def _strip_fences(raw: str) -> str:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _json_object(raw: str) -> Optional[dict]:
    """Returns a dict only for a real JSON object. No raw-text fallback."""
    cleaned = _strip_fences(raw)
    if not cleaned:
        return None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Some models prefix a sentence before the JSON object.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def detect_language_from_code(code: str, filename: Optional[str] = None) -> str:
    """Cheap, deterministic language detection used when the model omits it."""
    if filename:
        ext = os.path.splitext(filename)[1].lower()
        if ext in _EXT_LANGUAGE:
            return _EXT_LANGUAGE[ext]
    text = code or ""
    low = text.lower()
    if re.search(r"\busing System\b|\bnamespace\s+\w+|\bConsole\.Write", text):
        return "csharp"
    if re.search(r"\bSystem\.out\.print|\bpublic\s+static\s+void\s+main\b", text):
        return "java"
    if re.search(r"^\s*(def|class)\s+\w+.*:\s*$", text, re.M):
        return "python"
    if re.search(r"\bSELECT\b[\s\S]+\bFROM\b", text, re.I):
        return "sql"
    if re.search(r"\b(function|const|let|var)\b|\bconsole\.log\b", text):
        return "javascript"
    if re.search(r"^\s*#include\b|\bstd::", text, re.M):
        return "cpp"
    if re.search(r"\bpackage\s+main\b|\bfunc\s+\w+\(", text):
        return "go"
    if re.search(r"^\s*<\w+[^>]*>", text, re.M):
        return "html"
    if re.search(r"^\s*[\w-]+\s*:\s*[^;]+;\s*$", text, re.M) and "{" in text:
        return "css"
    return "unknown"


def build_extraction_prompt(language_hint: Optional[str] = None) -> str:
    """Prompt used by the live Code Follower. Always asks for the same envelope."""
    hint = (language_hint or "").strip()
    hint_line = (
        f"SUBJECT HINT: {hint.upper()}. This is a hint only; if the visible code is "
        "another language, transcribe it as-is without translating."
        if hint and hint.lower() not in {"auto", "general"}
        else "LANGUAGE: auto-detect from the visible code (Java, C++, C#, Python, JavaScript, PHP, Go, Rust, Kotlin, Swift, SQL, HTML, CSS)."
    )
    return (
        EXTRACTION_SYSTEM_PROMPT
        + "\n\nTEMPERATURE: 0\nJSON ONLY\n"
        + UNREADABLE_TOKEN
        + "\nDO NOT COMPLETE CODE\nALL LANGUAGES SUPPORTED\n"
        + hint_line
    )


def _confidence(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, conf))


def _line_no(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = int(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def parse_model_extraction_response(response_text: str) -> ExtractionResult:
    """Convert a model response into typed lines. Invalid output yields no lines."""
    data = _json_object(response_text)
    if data is None:
        return ExtractionResult(
            language="unknown",
            file_name=None,
            lines=[],
            flags=["invalid_json"],
            raw_response=response_text or "",
            error="Model did not return a JSON object",
        )

    raw_lines = data.get("lines")
    file_name = data.get("file_name") or data.get("tab")
    language = (data.get("language") or "").strip().lower()

    lines: List[ExtractedLine] = []
    if isinstance(raw_lines, list):
        for item in raw_lines:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            indent = item.get("indent_width")
            if not isinstance(indent, int):
                indent = len(text) - len(text.lstrip())
            lines.append(ExtractedLine(
                line_no=_line_no(item.get("line_no")),
                text=text,
                indent_width=max(0, indent),
                visible=str(item.get("visible") or "full"),
                confidence=_confidence(item.get("confidence")),
            ))
    else:
        code = data.get("code")
        if isinstance(code, str):
            for raw_line in code.splitlines():
                lines.append(ExtractedLine(
                    line_no=None,
                    text=raw_line,
                    indent_width=len(raw_line) - len(raw_line.lstrip()),
                    visible="full",
                    confidence=None,
                ))

    code_text = "\n".join(l.text for l in lines)
    if language not in SUPPORTED_LANGUAGES:
        language = detect_language_from_code(code_text, file_name)

    overlay = bool(data.get("overlay", False))
    flags = [str(f) for f in (data.get("flags") or []) if isinstance(f, str)]
    if overlay:
        flags.append("overlay")
    if data.get("eof_visible"):
        flags.append("eof_visible")

    return ExtractionResult(
        language=language,
        file_name=str(file_name) if file_name else None,
        lines=lines,
        has_occlusion=overlay,
        flags=flags,
        raw_response=response_text or "",
    )


class VisionExtractor:
    """Standalone extractor retained for the optional smart-sync pipeline."""

    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-2.5-flash"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        self.model_name = model_name
        self.client = None
        self.legacy_model = None
        if HAS_NEW_GENAI:
            if self.api_key:
                try:
                    self.client = genai.Client(api_key=self.api_key)
                except Exception:
                    self.client = None
        else:
            try:
                if self.api_key:
                    legacy_genai.configure(api_key=self.api_key)
                    self.legacy_model = legacy_genai.GenerativeModel(model_name)
            except Exception:
                self.legacy_model = None

    def extract_from_image(
        self, img: Image.Image, timeout_sec: float = 15.0, max_retries: int = 1
    ) -> VisionExtraction:
        t0 = time.perf_counter()
        if not self.client and not self.legacy_model:
            return VisionExtraction(code="", error="GEMINI_API_KEY is not configured")
        png_bytes = self._to_png_bytes(img)
        last_err = ""
        for attempt in range(max(0, max_retries) + 1):
            try:
                raw = self._call_gemini(png_bytes, timeout_sec)
                data = _json_object(raw)
                if data is None:
                    raise ValueError("model returned non-JSON output")
                return VisionExtraction(
                    code=data.get("code") or "",
                    tab_filename=data.get("tab"),
                    overlay_detected=bool(data.get("overlay", False)),
                    eof_visible=bool(data.get("eof_visible", False)),
                    language=data.get("language"),
                    latency_sec=round(time.perf_counter() - t0, 3),
                )
            except Exception as exc:
                last_err = str(exc)
                if attempt < max_retries:
                    time.sleep(1.2 * (attempt + 1))
        return VisionExtraction(
            code="",
            latency_sec=round(time.perf_counter() - t0, 3),
            error=f"Gemini extraction failed: {last_err}",
        )

    def _call_gemini(self, png_bytes: bytes, timeout_sec: float) -> str:
        if HAS_NEW_GENAI and self.client:
            resp = self.client.models.generate_content(
                model=self.model_name,
                contents=[
                    types.Part.from_bytes(data=png_bytes, mime_type="image/png"),
                    EXTRACTION_SYSTEM_PROMPT,
                ],
                config=types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    http_options=types.HttpOptions(timeout=int(timeout_sec * 1000)),
                ),
            )
            return resp.text or ""
        if self.legacy_model:
            img_pil = Image.open(io.BytesIO(png_bytes))
            resp = self.legacy_model.generate_content(
                [EXTRACTION_SYSTEM_PROMPT, img_pil],
                generation_config={"temperature": 0.0, "response_mime_type": "application/json"},
                request_options={"timeout": timeout_sec},
            )
            return resp.text or ""
        raise RuntimeError("Gemini client is not configured")

    @staticmethod
    def _to_png_bytes(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    @staticmethod
    def _parse_json_envelope(raw: str) -> dict:
        data = _json_object(raw)
        if data is None:
            raise ValueError("model returned non-JSON output")
        return data
