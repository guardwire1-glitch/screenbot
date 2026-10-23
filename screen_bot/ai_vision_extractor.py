"""
ai_vision_extractor.py
Low-token, zero-hallucination Gemini Vision extractor for ScreenBot.
Extracts active tab filename, occlusion flags, and raw code in a compact JSON envelope.
Backwards-compatible with ExtractedLine, ExtractionResult, UNREADABLE_TOKEN, and SUPPORTED_LANGUAGES.
"""

from __future__ import annotations
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

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
    "sql", "html", "css", "go", "rust", "kotlin", "swift", "php", "ruby", "shell"
]


@dataclass
class ExtractedLine:
    line_no: Optional[int]
    text: str
    indent_width: int = 0
    visible: str = "full"
    confidence: float = 1.0


@dataclass
class ExtractionResult:
    language: str
    file_name: Optional[str]
    lines: List[ExtractedLine] = field(default_factory=list)
    has_occlusion: bool = False
    flags: List[str] = field(default_factory=list)
    raw_response: str = ""


EXTRACTION_SYSTEM_PROMPT = """You are a deterministic IDE screen-capture OCR engine.
Return ONLY a single compact JSON object with these exact 4 keys:
{
  "tab": "filename with extension visible in active IDE tab (e.g. 'Program.cs', 'Main.java'), or null",
  "overlay": true if a Teams/Meet notification, popup, or Intellisense dropdown covers ANY code, else false,
  "eof_visible": true if blank editor space is visible below the last line of code, else false,
  "code": "exact visible source code from the main editor pane ONLY, separated by \\n"
}

STRICT RULES:
1. Transcribe ONLY code inside the primary code editor pane. Ignore solution explorer, terminal, output pane, and video call UI.
2. If the screen is a presentation slide, agenda, syllabus outline, or non-code window (e.g., 'sess03_programming_constructs'), return an empty string for "code".
3. NEVER guess, autocomplete, or invent code. If a line is partly covered by a tooltip or popup, omit that line and set "overlay": true.
4. Ignore gray inline ghost-text autocomplete suggestions (Copilot/IntelliCode).
5. Do NOT include line numbers in "code".
6. No markdown fences around the JSON."""


@dataclass
class VisionExtraction:
    code: str
    tab_filename: Optional[str] = None
    overlay_detected: bool = False
    eof_visible: bool = False
    latency_sec: float = 0.0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.code.strip())


class VisionExtractor:
    def __init__(self, api_key: Optional[str] = None, model_name: str = "gemini-2.5-flash"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not self.api_key:
            # Fallback to embedded default key if present
            self.api_key = os.getenv("GEMINI_API_KEY", "")
        self.model_name = model_name

        if HAS_NEW_GENAI:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception:
                self.client = None
        else:
            try:
                legacy_genai.configure(api_key=self.api_key)
                self.legacy_model = legacy_genai.GenerativeModel(model_name)
            except Exception:
                self.legacy_model = None

    def extract_from_image(
        self, img: Image.Image, timeout_sec: float = 15.0, max_retries: int = 1
    ) -> VisionExtraction:
        """Sends lossless PNG frame to Gemini at temperature=0 with timeout + 1 retry."""
        t0 = time.perf_counter()
        png_bytes = self._to_png_bytes(img)

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                raw_json = self._call_gemini(png_bytes, timeout_sec)
                parsed = self._parse_json_envelope(raw_json)
                return VisionExtraction(
                    code=parsed.get("code") or "",
                    tab_filename=parsed.get("tab"),
                    overlay_detected=bool(parsed.get("overlay", False)),
                    eof_visible=bool(parsed.get("eof_visible", False)),
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
        if HAS_NEW_GENAI and getattr(self, "client", None):
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
            return resp.text or "{}"
        elif getattr(self, "legacy_model", None):
            img_pil = Image.open(io.BytesIO(png_bytes))
            resp = self.legacy_model.generate_content(
                [EXTRACTION_SYSTEM_PROMPT, img_pil],
                generation_config={"temperature": 0.0, "response_mime_type": "application/json"},
                request_options={"timeout": timeout_sec},
            )
            return resp.text or "{}"
        else:
            return "{}"

    @staticmethod
    def _to_png_bytes(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    @staticmethod
    def _parse_json_envelope(raw: str) -> dict:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.I)
        try:
            data = json.loads(cleaned)
            return data if isinstance(data, dict) else {"code": str(data)}
        except json.JSONDecodeError:
            # Fallback if model returned raw text instead of JSON
            return {"tab": None, "overlay": False, "eof_visible": False, "code": cleaned}


def build_extraction_prompt(language_hint: Optional[str] = None) -> str:
    """Compatibility helper for older modules calling build_extraction_prompt."""
    return (
        EXTRACTION_SYSTEM_PROMPT
        + "\n\nTEMPERATURE: 0\nJSON ONLY\n«UNREADABLE»\nDO NOT COMPLETE CODE\nALL LANGUAGES SUPPORTED"
    )


def parse_model_extraction_response(response_text: str) -> ExtractionResult:
    """
    Compatibility parser: converts raw model output (either compact JSON envelope or legacy JSON)
    into an ExtractionResult containing ExtractedLine objects.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", response_text.strip(), flags=re.I)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = {"code": cleaned}

    if isinstance(data, dict):
        if "lines" in data and isinstance(data["lines"], list):
            # Legacy format
            lines = []
            for l in data["lines"]:
                if isinstance(l, dict):
                    txt = l.get("text", "")
                    lines.append(ExtractedLine(
                        line_no=l.get("line_no"),
                        text=txt,
                        indent_width=l.get("indent_width", len(txt) - len(txt.lstrip())),
                        visible=l.get("visible", "full"),
                        confidence=float(l.get("confidence", 1.0)),
                    ))
            return ExtractionResult(
                language=data.get("language", "csharp"),
                file_name=data.get("file_name"),
                lines=lines,
                has_occlusion=bool(data.get("has_occlusion", False)),
                flags=data.get("flags", []),
                raw_response=response_text,
            )
        else:
            # New compact envelope
            code = data.get("code") or ""
            tab = data.get("tab")
            overlay = bool(data.get("overlay", False))
            lines = []
            for idx, raw_l in enumerate(code.splitlines()):
                indent = len(raw_l) - len(raw_l.lstrip())
                lines.append(ExtractedLine(
                    line_no=idx + 1,
                    text=raw_l,
                    indent_width=indent,
                    visible="full",
                    confidence=1.0,
                ))
            return ExtractionResult(
                language="csharp",
                file_name=tab,
                lines=lines,
                has_occlusion=overlay,
                raw_response=response_text,
            )

    return ExtractionResult(language="csharp", file_name=None, lines=[], raw_response=response_text)
