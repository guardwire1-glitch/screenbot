"""
Tests for AI Vision Extractor (Pipeline Stage 4):
- Strict JSON Line Extraction Schema (temperature 0).
- Automatic Language Detection across all languages.
- Placeholder Token for unreadable characters («UNREADABLE»).
- Flagging line wrapping, code folding (#region), and horizontal scroll.
"""

import json
import pytest
from screen_bot.ai_vision_extractor import (
    ExtractionResult,
    ExtractedLine,
    parse_model_extraction_response,
    build_extraction_prompt,
    UNREADABLE_TOKEN,
)


def test_parse_model_extraction_valid_json():
    raw_json = """
    {
        "file_name": "Calculator.cs",
        "language": "csharp",
        "flags": ["code_folding"],
        "lines": [
            {"line_no": 1, "text": "using System;", "indent_width": 0, "visible": "full", "confidence": 0.98},
            {"line_no": 2, "text": "namespace Demo {", "indent_width": 0, "visible": "full", "confidence": 0.95},
            {"line_no": 3, "text": "    public int Add(int a, int b) {", "indent_width": 4, "visible": "full", "confidence": 0.96},
            {"line_no": 4, "text": "        return a + b;", "indent_width": 8, "visible": "full", "confidence": 0.99},
            {"line_no": 5, "text": "    }", "indent_width": 4, "visible": "full", "confidence": 0.99},
            {"line_no": 6, "text": "}", "indent_width": 0, "visible": "full", "confidence": 0.99}
        ]
    }
    """
    result = parse_model_extraction_response(raw_json)
    assert result.file_name == "Calculator.cs"
    assert result.language == "csharp"
    assert "code_folding" in result.flags
    assert len(result.lines) == 6
    assert result.lines[0].text == "using System;"
    assert result.lines[2].indent_width == 4


def test_parse_model_extraction_handles_markdown_fences():
    raw_markdown = f"""```json
    {{
        "file_name": "App.java",
        "language": "java",
        "flags": [],
        "lines": [
            {{"line_no": 10, "text": "String msg = {UNREADABLE_TOKEN};", "indent_width": 4, "visible": "partial", "confidence": 0.70}}
        ]
    }}
    ```"""
    result = parse_model_extraction_response(raw_markdown)
    assert result.file_name == "App.java"
    assert result.language == "java"
    assert len(result.lines) == 1
    assert UNREADABLE_TOKEN in result.lines[0].text
    assert result.lines[0].visible == "partial"


def test_build_extraction_prompt_contains_all_core_rules():
    prompt = build_extraction_prompt(language_hint="auto")
    assert "TEMPERATURE: 0" in prompt
    assert "JSON ONLY" in prompt
    assert UNREADABLE_TOKEN in prompt
    assert "DO NOT COMPLETE CODE" in prompt
    assert "all languages" in prompt.lower()
