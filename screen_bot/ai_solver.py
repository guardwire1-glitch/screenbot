"""
Gemini client used by ScreenBot.

Three rules matter for the live class path:
1. Quota/rate-limit errors are never retried and never trigger fallback models;
   a 429 storm must stop immediately.
2. Every network call has a wall-clock bound. A hung request cannot freeze the
   live follower loop.
3. Extraction prompts are language-agnostic by default. A subject preset is a
   hint, never an instruction to translate or rewrite the lecturer's code.
"""

import os
import random
import re
import time
from typing import Any, List, Optional

from google import genai
from google.genai import types

DEFAULT_MODEL = "gemini-3.5-flash-lite"
DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY", "")

FALLBACK_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
]

SUBJECT_CONTEXTS = {
    "java": "Focus strictly on Java SE, Object-Oriented Programming (encapsulation, inheritance, polymorphism, abstraction), standard collections, and try-catch exception handling according to the Aptech syllabus.",
    "adv_java": "Focus on Advanced Java, JDBC (DriverManager, Connection, PreparedStatement, ResultSet, transactions), JavaFX UI, Lambdas, and Streams according to Aptech Sem 2 standards.",
    "c": "Focus on ANSI C programming, pointers, arrays, memory management (malloc/free), structs, and standard C library idioms.",
    "sql": "Focus on relational database SQL queries, DDL/DML, JOINs, subqueries, aggregate functions (GROUP BY, HAVING), and constraints conforming to Oracle and MySQL standards.",
    "web": "Focus on Web Technologies: semantic HTML5, CSS3 styling, vanilla JavaScript DOM manipulation, and modern ES6 idioms.",
    "csharp": "Focus on C# language fundamentals, .NET console apps, LINQ, collections, and object-oriented design.",
    "auto": "Detect the programming language and framework from the visible code and answer in that language.",
}

LANGUAGE_LIST = "Java, C++, C#, Python, JavaScript, PHP, Go, Rust, Kotlin, Swift, SQL"

AUTO_LANGUAGE_DIRECTIVE = f"""\
LANGUAGE HANDLING:
- Auto-detect the programming language from the visible code before answering.
- Supported languages include {LANGUAGE_LIST}.
- Transcribe and answer in the SAME language as the screen. NEVER translate code
  or prose into another programming language.
- If the visible code is a different language than the Subject hint, follow the
  visible code; the hint is only a preference for ambiguous cases.
"""

PROMPT_BASE = """\
You are an expert software engineer and academic tutor strictly following the Aptech Advanced Diploma in Software Engineering (ADSE) curriculum (cohort ADSE 2509).

SUBJECT EMPHASIS:
{subject_context}

Analyze the user's screen capture to find the active question, exam problem, quiz item, or coding exercise.
Provide ONLY the direct, accurate, final answer or code solution to be pasted directly into the active editor or application.

CONFORMITY & STYLE SPECIFICATIONS (APTECH ADSE 2509):
1. ZERO AI MARKERS:
   - Absolutely NO conversational openings ("Here is the solution:", "Certainly!", "Based on the screen...").
   - Absolutely NO closing remarks or boilerplate ("Hope this helps!", "Let me know if you need more details.").
   - Do NOT wrap plain text or multiple choice answers in markdown code blocks.
   - Start immediately with the answer or code.

2. MULTIPLE CHOICE QUESTIONS (MCQ):
   - Output ONLY the option letter and concise answer text.
   - Example: B) Polymorphism

3. SMART CODE SCANNING & COMPLETION:
   - CRITICAL: First, carefully inspect any existing code already typed or visible in the user's editor / IDE on screen.
   - CASE A: User has already written partial code that is CORRECT so far:
     * DO NOT duplicate what they already wrote (such as existing class headers, imports, or variable declarations).
     * Output ONLY the missing code snippet / method body needed to complete it seamlessly.
     * Adopt their existing variable names and coding conventions (e.g., if they used 'sc' for Scanner, keep 'sc').
   - CASE B: User's existing code is WRONG, has syntax bugs, flawed logic, or broken architecture:
     * Output the ENTIRE, COMPLETE working code (the full corrected method or full class) so they can replace/paste the whole thing cleanly.
   - CASE C: Fresh or blank template:
     * Provide the complete clean solution.
   - Aptech ADSE 2509 Code Standards:
     * Clean, robust code following syllabus idioms.
     * Meaningful, natural human comments explaining non-trivial logic.
     * Professional naming: PascalCase for classes, camelCase for methods/variables, UPPER_SNAKE for constants.
     * Avoid obvious/redundant comments (do NOT write "// import scanner").
     * Code must be complete, syntactically correct, and ready to compile immediately.

4. THEORY / DEFINITIONS / SHORT ANSWERS:
   - Concise, academic, and technically precise (1-3 sentences maximum).
   - Use standard Aptech technical terminology.

{additional_mode_instructions}

Return ONLY what is ready to be pasted immediately.
"""


class QuotaExceededError(RuntimeError):
    """Raised when Gemini reports 429/RESOURCE_EXHAUSTED. Never retried."""


def _is_quota_error(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in (
        "429",
        "resource_exhausted",
        "resource exhausted",
        "quota exceeded",
        "quota_exceeded",
        "rate limit",
        "rate_limit",
        "too many requests",
    ))


def generate_variation_directive(mode: str = "concise") -> str:
    seed = random.randint(10000, 99999)
    styles = [
        "Use clean, professional camelCase identifiers and concise single-line comments explaining intent.",
        "Use authentic student variable names with natural inline comments focused on step-by-step logic.",
        "Use distinct idiomatic variable naming and place comments directly above key calculations or conditions.",
        "Use expressive local variable synonyms and brief, natural explanatory phrasing in comments.",
    ]
    style_choice = random.choice(styles)
    directive = f"""\
ANTI-PLAGIARISM & CLASSMATE VARIATION ENGINE (Seed #{seed}):
- Several students in this cohort are revising this exact material. Ensure the response is uniquely worded while remaining completely technically correct.
- FOR CODE: vary local identifiers sensibly, write fresh comments in your own student voice ({style_choice}), and preserve correct logic.
- FOR THEORY: vary sentence structure and explanations while preserving required technical keywords (encapsulation, polymorphism, ACID, JDBC).
"""
    if mode == "paraphrase":
        directive += "\n- EXTRA PARAPHRASING: maximize uniqueness with alternative synonyms and reordered points."
    elif mode == "explain":
        directive += "\n- OUTPUT FORMAT: direct answer or code first, followed by at most 3 concise bullets."
    else:
        directive += "\n- OUTPUT FORMAT: direct, clean, paste-ready answer or code snippet only."
    return directive


def clean_output(text: str) -> str:
    text = (text or "").strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()
    text = re.sub(r"^(Answer|Solution):\s*", "", text, flags=re.IGNORECASE).strip()
    return text


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _subject_hint(subject: str) -> str:
    subj = (subject or "auto").strip().lower()
    if subj in {"auto", "", "general"}:
        return AUTO_LANGUAGE_DIRECTIVE
    return (
        f"Subject hint: {subj.upper()}. Use the hint only for ambiguous cases; "
        "If the visible code is a different language, follow the visible code "
        "and do not translate it.\n"
    )


class ScreenSolver:
    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or DEFAULT_API_KEY
        self.model_name = model or DEFAULT_MODEL
        self._client = None
        # Last extraction failure kind ("quota", "error", ""). The structured
        # wrapper returns "" on failure, so the live path reads this to decide
        # whether to enter a quota cooldown.
        self.last_error_kind = ""
        self.last_error = ""
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)

    def update_key(self, api_key: str):
        self.api_key = api_key
        self._client = genai.Client(api_key=api_key) if api_key else None

    # ------------------------------------------------------------------
    def _call_with_retry_and_timeout(
        self,
        model: str,
        contents: List[Any],
        config: Optional[types.GenerateContentConfig] = None,
        timeout: float = 15.0,
    ) -> str:
        """One logical request with a bounded timeout and at most one retry.

        Quota errors are fatal for the cycle: retrying a 429 only burns more
        quota and delays the next lecture frame.
        """
        if not self._client:
            raise RuntimeError("GEMINI_API_KEY is not configured")

        base_config = config or types.GenerateContentConfig(temperature=0.0)
        try:
            http_options = types.HttpOptions(timeout=int(max(1.0, timeout) * 1000))
        except Exception:
            http_options = None
        if http_options is not None:
            try:
                base_config.http_options = http_options
            except Exception:
                pass

        attempts = 2
        last_err = ""
        for attempt in range(attempts):
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=contents,
                    config=base_config,
                )
                return response.text or ""
            except Exception as exc:
                last_err = str(exc)
                if _is_quota_error(last_err):
                    raise QuotaExceededError(last_err) from exc
                if attempt + 1 < attempts:
                    time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(last_err)

    def _models_to_try(self) -> List[str]:
        models = [self.model_name]
        for model in FALLBACK_MODELS:
            if model not in models:
                models.append(model)
        return models

    def _generate(
        self,
        contents: List[Any],
        config: Optional[types.GenerateContentConfig] = None,
        timeout: float = 20.0,
    ) -> str:
        """Try the configured model, then declared fallbacks. Quota stops the loop."""
        if not self._client:
            raise RuntimeError("GEMINI_API_KEY is not configured")
        last_err = ""
        for model in self._models_to_try():
            try:
                return self._call_with_retry_and_timeout(
                    model=model, contents=contents, config=config, timeout=timeout
                )
            except QuotaExceededError:
                raise
            except Exception as exc:
                last_err = str(exc)
                continue
        raise RuntimeError(last_err)

    @staticmethod
    def _image_part(image_bytes: bytes):
        return types.Part.from_bytes(data=image_bytes, mime_type=_image_mime(image_bytes))

    @staticmethod
    def _quota_marker(action: str) -> str:
        return f"[ScreenBot Quota Error] Gemini rate limit reached while {action}. Wait and retry."

    # ------------------------------------------------------------------
    def solve(self, image_bytes: bytes, mode: str = "concise", subject: str = "auto") -> str:
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Add a free Gemini API key to screen_bot/config.json or .env"
            )

        subject_ctx = SUBJECT_CONTEXTS.get((subject or "auto").lower(), SUBJECT_CONTEXTS["auto"])
        prompt = PROMPT_BASE.format(
            subject_context=subject_ctx,
            additional_mode_instructions=_subject_hint(subject) + generate_variation_directive(mode=mode),
        )
        try:
            raw = self._generate([self._image_part(image_bytes), prompt])
            return clean_output(raw)
        except QuotaExceededError:
            return self._quota_marker("solving the screen")
        except Exception as exc:
            return f"[ScreenBot] Error answering: {exc}"

    def solve_text(self, text_prompt: str, mode: str = "concise", subject: str = "auto") -> str:
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Add a free Gemini API key to screen_bot/config.json or .env"
            )
        subject_ctx = SUBJECT_CONTEXTS.get((subject or "auto").lower(), SUBJECT_CONTEXTS["auto"])
        prompt = (
            PROMPT_BASE.format(
                subject_context=subject_ctx,
                additional_mode_instructions=_subject_hint(subject) + generate_variation_directive(mode=mode),
            )
            + "\n\nUSER QUESTION / ASSIGNMENT PROMPT:\n\"\"\"\n"
            + (text_prompt or "")
            + "\n\"\"\"\n\nProvide ONLY the direct, complete, paste-ready solution."
        )
        try:
            raw = self._generate([prompt])
            return clean_output(raw)
        except QuotaExceededError:
            return self._quota_marker("answering the question")
        except Exception as exc:
            return f"[ScreenBot] Error answering: {exc}"

    def extract_lecture_code(
        self,
        image_bytes: bytes,
        subject: str = "auto",
        prompt: Optional[str] = None,
        json_mode: bool = False,
    ) -> str:
        """Transcribe visible lecture code. `prompt` may add the strict JSON envelope."""
        self.last_error_kind = ""
        self.last_error = ""
        if not self._client:
            self.last_error_kind = "error"
            self.last_error = "GEMINI_API_KEY is not configured"
            return ""

        base_prompt = f"""\
You are a deterministic code transcription engine. The image is a live lecture,
coding tutorial, or presentation inside a browser or desktop application.

YOUR TASK:
1. Locate the presenter's code editor or code display viewport.
2. Ignore browser tabs, address bars, video controls, recommendations, comments, meeting toolbars, and webcam bubbles.
3. Transcribe the EXACT visible source code, preserving indentation, identifiers, and syntax.
4. Auto-detect the language from the visible code. Never translate code into another language.
5. Transcribe the code ONCE. Never duplicate blocks or invent missing lines.

{_subject_hint(subject)}

Supported languages: {LANGUAGE_LIST}.
NEVER translate or rewrite the code. Copy it exactly as shown.
"""

        if prompt:
            full_prompt = base_prompt + "\n\nOUTPUT CONTRACT (follow exactly):\n" + prompt
        else:
            full_prompt = base_prompt + "\nStart immediately with the first character of code. No markdown fences."

        config = None
        if json_mode or prompt:
            config = types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
            )
        try:
            raw = self._generate([self._image_part(image_bytes), full_prompt], config=config)
            return raw if (json_mode or prompt) else clean_output(raw)
        except QuotaExceededError as exc:
            self.last_error_kind = "quota"
            self.last_error = str(exc)
            return self._quota_marker("transcribing lecture code")
        except Exception as exc:
            self.last_error_kind = "error"
            self.last_error = str(exc)
            return f"[ScreenBot] Error extracting code: {exc}"

    def extract_lecture_code_structured(self, image_bytes: bytes, prompt: str) -> str:
        """Strict-JSON wrapper. A failed extraction is an empty result, never an
        error string that could be mistaken for code."""
        raw = self.extract_lecture_code(image_bytes, subject="auto", prompt=prompt, json_mode=True)
        if not raw or raw.startswith("[ScreenBot"):
            return ""
        return raw

    def generate_lecture_notes(self, image_bytes: bytes, subject: str = "general") -> str:
        if not self._client:
            return "[ScreenBot] Error: GEMINI_API_KEY is not set."

        prompt = """\
You are an academic high-yield note taker. The image is a lecture slide, presentation,
video, or whiteboard. Ignore browser chrome, recommendations, comments, and player controls.

Convert the visible content into clean, structured Cornell-style study notes in plain text:
TOPIC: [title]
CORE SUMMARY: [1-2 sentences]
KEY TAKEAWAYS & DEFINITIONS:
- [term]: [explanation]
- [rule / formula / step]
EXAM & REVISION FOCUS:
- [likely test question or pitfall]

Be concise and information-dense. No markdown code blocks, no conversational prefixes.
Return ONLY the study notes text.
"""
        try:
            raw = self._generate([self._image_part(image_bytes), prompt])
            return clean_output(raw)
        except QuotaExceededError:
            return self._quota_marker("generating study notes")
        except Exception as exc:
            return f"[ScreenBot] Error generating notes: {exc}"
