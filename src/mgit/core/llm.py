"""Headless LLM invocation for skill distillation.

mgit's one LLM touchpoint. Given a prompt and a JSON schema, it runs the local
`claude` binary in headless mode with structured output and returns the parsed
object. Everything else in mgit stays deterministic; this is isolated here so a
change to the CLI's flags or output shape only touches one module.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def invoke_structured(
    prompt: str,
    json_schema: dict,
    *,
    claude_bin: str = "claude",
    model: str = "",
    cwd: Path | None = None,
    timeout: int = 600,
) -> tuple[dict | None, str]:
    """Run `claude -p` with a structured-output schema.

    Returns (parsed_object_or_None, diagnostic). The object is validated to be a
    dict; anything else yields (None, reason) so callers never crash on a
    malformed response.
    """
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(json_schema),
    ]
    if model:
        cmd += ["--model", model]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return None, f"claude binary not found: {claude_bin}"
    except subprocess.TimeoutExpired:
        return None, f"claude -p timed out after {timeout}s"
    if proc.returncode != 0:
        return None, f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}"
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None, f"unparseable claude -p output: {proc.stdout[:300]}"
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured, "ok"
    # Fallback: some CLI versions return the JSON in the result text.
    result_text = envelope.get("result", "")
    if isinstance(result_text, str) and result_text.strip():
        text = result_text.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed, "ok (parsed from result text)"
    return None, "no structured_output in claude -p response"
