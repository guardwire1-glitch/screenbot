"""
AI Vision Solver tailored for Aptech ADSE 2509 curriculum.
Supports multi-subject tuning, smart code completion, explanation, and anti-plagiarism phrasing.
"""

import os
import re
import random
from typing import Optional
from google import genai
from google.genai import types

# Default embedded API key for out-of-the-box operation
DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY", "")

SUBJECT_CONTEXTS = {
    "java": "Focus strictly on Java SE, Object-Oriented Programming (encapsulation, inheritance, polymorphism, abstraction), standard collections, and try-catch exception handling according to the Aptech syllabus.",
    "adv_java": "Focus on Advanced Java, JDBC (DriverManager, Connection, PreparedStatement, ResultSet, transactions), JavaFX UI, Lambdas, and Streams according to Aptech Sem 2 standards.",
    "c": "Focus on ANSI C programming, pointers, arrays, memory management (malloc/free), structs, and standard C library idioms.",
    "sql": "Focus on relational database SQL queries, DDL/DML, JOINs, subqueries, aggregate functions (GROUP BY, HAVING), and constraints conforming to Oracle and MySQL standards.",
    "web": "Focus on Web Technologies: semantic HTML5, CSS3 styling, vanilla JavaScript DOM manipulation, and modern ES6 idioms.",
}

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
     * When writing a complete class, use clean section dividers:
       //=============================================================
       // Instance Fields / Variables
       //=============================================================
       //=============================================================
       // Constructors
       //=============================================================
       //=============================================================
       // Methods
       //=============================================================
     * Professional naming: PascalCase for classes, camelCase for methods/variables, UPPER_SNAKE for constants.
     * Avoid obvious/redundant comments (do NOT write "// import scanner" or "// variable declaration").
     * Code must be complete, syntactically correct, and ready to compile immediately.

4. THEORY / DEFINITIONS / SHORT ANSWERS:
   - Concise, academic, and technically precise (1-3 sentences maximum).
   - Use standard Aptech technical terminology.
   - Not too wordy; hit the exact grading key directly without rambling.

{additional_mode_instructions}

Return ONLY what is ready to be pasted immediately.
"""


def generate_variation_directive(mode: str = "concise") -> str:
    """Generates dynamic randomized instructions to guarantee 100% unique phrasing for classmates."""
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
- Several students in this cohort are revising this exact material. You MUST ensure this response is 100% uniquely worded and structured to prevent identical submissions while maintaining complete technical correctness.
- FOR CODE:
  * Vary local variable names sensibly (e.g., if salary calculation, vary between 'empSalary', 'basicSalary', 'salaryAmt', 'grossPay').
  * Formulate fresh, original comments in your own student voice ({style_choice}).
  * Preserve correct logic and Aptech syllabus conventions while varying layout and local identifiers.
- FOR THEORY & DEFINITIONS:
  * Express definitions and concepts in fresh, personalized phrasing.
  * Vary sentence structures, introductory wording, and explanations while preserving required technical keywords (e.g. encapsulation, polymorphism, ACID, JDBC).
"""
    if mode == "paraphrase":
        directive += "\n- EXTRA PARAPHRASING: Maximize uniqueness with alternative synonyms, reordered explanatory points, and personalized reasoning."
    elif mode == "explain":
        directive += "\n- OUTPUT FORMAT: State the direct answer or code first, followed by a brief bulleted breakdown (max 3 bullets) explaining the core concept."
    else:
        directive += "\n- OUTPUT FORMAT: Direct, clean, paste-ready answer or code snippet only."

    return directive


def clean_output(text: str) -> str:
    """Cleans model output to ensure raw, ready-to-paste text without markdown fence artifacts."""
    text = text.strip()

    # If the response is wrapped in markdown code fences, strip them so raw code pastes cleanly into IDEs
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1]).strip()

    # Strip accidental leading "Answer:" or "Solution:" prefixes
    text = re.sub(r"^(Answer|Solution):\s*", "", text, flags=re.IGNORECASE).strip()

    return text


FALLBACK_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
]


class ScreenSolver:
    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-3.5-flash-lite"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or DEFAULT_API_KEY
        self.model_name = model or "gemini-3.5-flash-lite"
        self._client = None
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)

    def update_key(self, api_key: str):
        self.api_key = api_key
        if self.api_key:
            self._client = genai.Client(api_key=self.api_key)

    def solve(self, image_bytes: bytes, mode: str = "concise", subject: str = "java") -> str:
        """Sends the image bytes to Gemini Vision with subject, mode tuning, and automatic fallback."""
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Please add your free Gemini API key to screen_bot/config.json or .env"
            )

        subject_ctx = SUBJECT_CONTEXTS.get(subject, SUBJECT_CONTEXTS["java"])
        mode_inst = generate_variation_directive(mode=mode)

        prompt = PROMPT_BASE.format(
            subject_context=subject_ctx,
            additional_mode_instructions=mode_inst,
        )

        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg",
        )

        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]
        last_err = ""

        for m in models_to_try:
            try:
                response = self._client.models.generate_content(
                    model=m,
                    contents=[image_part, prompt],
                )
                raw_text = response.text or ""
                return clean_output(raw_text)
            except Exception as e:
                last_err = str(e)
                continue

        return f"[ScreenBot] Error answering: {last_err}"



    def solve_text(self, text_prompt: str, mode: str = "concise", subject: str = "java") -> str:
        """Solves a text-based question, exercise, or scenario using Gemini with Aptech tuning and fallback."""
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Please add your free Gemini API key to screen_bot/config.json or .env"
            )

        subject_ctx = SUBJECT_CONTEXTS.get(subject, SUBJECT_CONTEXTS["java"])
        mode_inst = generate_variation_directive(mode=mode)

        prompt = f"""{PROMPT_BASE.format(subject_context=subject_ctx, additional_mode_instructions=mode_inst)}

USER QUESTION / ASSIGNMENT PROMPT:
\"\"\"
{text_prompt}
\"\"\"

CRITICAL INSTRUCTION:
Provide ONLY the direct, fully working, complete code or answer solution to the above prompt.
Start immediately with the code. No conversational text, no markdown code fence blocks.
"""

        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]
        last_err = ""

        for m in models_to_try:
            try:
                response = self._client.models.generate_content(
                    model=m,
                    contents=[prompt],
                )
                raw_text = response.text or ""
                return clean_output(raw_text)
            except Exception as e:
                last_err = str(e)
                continue

        return f"[ScreenBot] Error answering: {last_err}"

    def extract_lecture_code(self, image_bytes: bytes, subject: str = "java") -> str:
        """Extracts the exact code shown in a live lecture or video tutorial.
        Works across browsers (YouTube, Teams Web, Google Meet, Udemy) and desktop apps.
        Ignores browser tabs, URL bars, YouTube controls, comments, and attendee webcams."""
        if not self._client:
            return "[ScreenBot] Error: GEMINI_API_KEY is not set."

        prompt = f"""\
You are an expert code synchronizer and OCR transcription engine.
The attached image shows a live lecture, coding tutorial, or presentation. It may be running inside a web browser (e.g., YouTube, Teams Web, Google Meet, Panopto) or a desktop app.

YOUR TASK:
1. Locate the presenter's code editor or code display viewport.
2. Ignore all surrounding web/app UI elements: browser tabs, URL address bars, YouTube player scrubber controls, recommendations, comments, Teams toolbars, and video bubbles.
3. Transcribe the EXACT source code visible in the editor.
4. Maintain exact indentation, variable names, classes, methods, and syntax.

LANGUAGE / SUBJECT FOCUS: {subject.upper()}

RULES:
- Absolutely NO conversational openings or explanations.
- Do NOT wrap in markdown backtick code blocks (e.g. ```java ... ```).
- Start immediately with the first character of code.
- Transcribe the code ONCE. Never repeat or duplicate code blocks, classes, or lines.
"""
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]
        last_err = ""

        for m in models_to_try:
            try:
                response = self._client.models.generate_content(
                    model=m,
                    contents=[image_part, prompt],
                )
                raw_text = response.text or ""
                return clean_output(raw_text)
            except Exception as e:
                last_err = str(e)
                continue

        return f"[ScreenBot] Error extracting code: {last_err}"

    def extract_lecture_code_structured(self, image_bytes: bytes, prompt: str) -> str:
        """Invokes Gemini Vision with structured JSON line extraction at temperature 0."""
        if not self._client:
            return ""

        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]

        for m in models_to_try:
            try:
                response = self._client.models.generate_content(
                    model=m,
                    contents=[image_part, prompt],
                    config=types.GenerateContentConfig(temperature=0.0),
                )
                return response.text or ""
            except Exception:
                continue

        return ""

    def generate_lecture_notes(self, image_bytes: bytes, subject: str = "general") -> str:
        """Analyzes lecture slides, presentations, or video tutorials (YouTube, Teams Web/Desktop, Zoom, Meet)
        and converts them into clean, structured Cornell study notes ready to paste into Word, OneNote, or Notion."""
        if not self._client:
            return "[ScreenBot] Error: GEMINI_API_KEY is not set."

        prompt = f"""\
You are an academic high-yield note taker and study assistant.
The attached image is a lecture slide, presentation, video (YouTube, Teams Web/App, Google Meet, Zoom), or whiteboard.

YOUR TASK:
Analyze the visible content (bullet points, diagrams, formulas, speaker notes, or live captions) and convert it into clean, structured study notes formatted for Microsoft Word, OneNote, or Notion.
Ignore browser URL bars, YouTube recommendations, comments, and player controls.

FORMAT STRUCTURE:
📌 TOPIC: [Title or main theme of the slide/lecture segment]

💡 CORE SUMMARY:
[1-2 sentences clearly explaining the concept in plain academic language]

📝 KEY TAKEAWAYS & DEFINITIONS:
• [Key term / concept]: [Clear explanation]
• [Important rule, formula, step, or principle]
• [Notable practical application or nuance]

🎯 EXAM & REVISION FOCUS:
• [Likely test question or critical pitfall to remember]

RULES:
- Be concise, dense with information, and highly organized.
- Do NOT use markdown code blocks or conversational prefixes.
- Return ONLY the clean study notes text.
"""
        image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
        models_to_try = [self.model_name] + [m for m in FALLBACK_MODELS if m != self.model_name]
        last_err = ""

        for m in models_to_try:
            try:
                response = self._client.models.generate_content(
                    model=m,
                    contents=[image_part, prompt],
                )
                raw_text = response.text or ""
                return clean_output(raw_text)
            except Exception as e:
                last_err = str(e)
                continue

        return f"[ScreenBot] Error generating notes: {last_err}"



    def solve_text(self, text_prompt: str, mode: str = "concise", subject: str = "java") -> str:
        """Solves a text question/comment directly using Gemini with subject and mode tuning."""
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Please add your free Gemini API key to screen_bot/config.json or .env"
            )

        subject_ctx = SUBJECT_CONTEXTS.get(subject, SUBJECT_CONTEXTS["java"])
        mode_inst = generate_variation_directive(mode=mode)

        prompt = f"""{PROMPT_BASE.format(subject_context=subject_ctx, additional_mode_instructions=mode_inst)}

HIGHLIGHTED / COPIED QUESTION OR INSTRUCTIONS:
\"\"\"
{text_prompt}
\"\"\"

Provide ONLY the direct, complete solution/code ready to paste or type directly under the instructions. No conversational openings, no markdown fences.
"""

        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[prompt],
            )
            raw_text = response.text or ""
            return clean_output(raw_text)

        except Exception as e:
            err_msg = str(e)
            if "API_KEY_INVALID" in err_msg or "400" in err_msg:
                return f"[ScreenBot] API Key Error: Please verify your Gemini API key in config.json ({err_msg})"
            elif "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
                return f"[ScreenBot] Rate limit reached. Please wait a moment and try again."
            else:
                return f"[ScreenBot] Error answering: {err_msg}"

    def solve_text(self, text_prompt: str, mode: str = "concise", subject: str = "java") -> str:
        """Solves a text-based question, exercise, or scenario using Gemini with Aptech tuning."""
        if not self._client:
            return (
                "[ScreenBot] Error: GEMINI_API_KEY is not set.\n"
                "Please add your free Gemini API key to screen_bot/config.json or .env"
            )

        subject_ctx = SUBJECT_CONTEXTS.get(subject, SUBJECT_CONTEXTS["java"])
        mode_inst = generate_variation_directive(mode=mode)

        prompt = f"""{PROMPT_BASE.format(subject_context=subject_ctx, additional_mode_instructions=mode_inst)}

USER QUESTION / ASSIGNMENT PROMPT:
\"\"\"
{text_prompt}
\"\"\"

CRITICAL INSTRUCTION:
Provide ONLY the direct, fully working, complete code or answer solution to the above prompt.
Start immediately with the code. No conversational text, no markdown code fence blocks.
"""

        try:
            response = self._client.models.generate_content(
                model=self.model_name,
                contents=[prompt],
            )
            raw_text = response.text or ""
            return clean_output(raw_text)

        except Exception as e:
            err_msg = str(e)
            if "API_KEY_INVALID" in err_msg or "400" in err_msg:
                return f"[ScreenBot] API Key Error: Please verify your Gemini API key ({err_msg})"
            elif "RESOURCE_EXHAUSTED" in err_msg or "429" in err_msg:
                return f"[ScreenBot] Rate limit reached. Please wait a moment and try again."
            else:
                return f"[ScreenBot] Error answering: {err_msg}"
