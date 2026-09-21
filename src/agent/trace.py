"""Typed discovery trace. This is not a capability artifact.

The artifact is compiled from a RunTrace in a later step. The brief
requires that artifact be decoupled from the raw model transcript, so
this module only records what happened: observations, tool calls, acts.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from agent.redact import redact_text
from surface.types import Action, ActionResult, Observation

# Requirement 3.4: never persist credentials. Typed values into a
# password-looking textbox are replaced at write time, including any
# copy of that value the model echoed into reason text.
_PASSWORD_HINTS: tuple[str, ...] = ("password", "passwd", "passcode", "secret")
_CREDENTIAL_MASK: str = "[REDACTED]"


class TerminalStatus(str, Enum):
    GOAL_REACHED = "goal_reached"
    MAX_STEPS = "max_steps"
    DEAD_END = "dead_end"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    ESCALATED = "escalated"


class TraceStep(BaseModel):
    """One observe → decide → act cycle."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    step: int
    observation: Observation
    action: Action | None = Field(
        default=None,
        description="Surface action. None for done/stuck — those never hit Surface.act().",
    )
    reason: str
    raw_tool_call: dict[str, Any]
    result: ActionResult | None = None
    elapsed_ms: int


class RunTrace(BaseModel):
    """A full discovery run. Compiler input; not the artifact itself."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    run_id: str
    goal: str
    entry_url: str
    model: str
    started_at: datetime
    ended_at: datetime | None = None
    steps: list[TraceStep] = Field(default_factory=list)
    status: TerminalStatus | None = None
    final_summary: str = ""
    extracted: dict[str, str] = Field(
        default_factory=dict,
        description="Values the model read for the goal (e.g. primary savings balance).",
    )


def _looks_like_password(name: str, label_text: str) -> bool:
    blob: str = f"{name} {label_text}".casefold()
    return any(hint in blob for hint in _PASSWORD_HINTS)


def _password_indices(observation: dict[str, Any]) -> set[int]:
    indices: set[int] = set()
    for el in observation.get("elements") or []:
        if not isinstance(el, dict):
            continue
        if _looks_like_password(str(el.get("name") or ""), str(el.get("label_text") or "")):
            indices.add(int(el["index"]))
    return indices


def _replace_secret(text: str, secret: str) -> str:
    if not secret:
        return text
    return text.replace(secret, _CREDENTIAL_MASK)


def _redact_observation_dump(data: dict[str, Any]) -> dict[str, Any]:
    data["text_content"] = redact_text(str(data.get("text_content") or ""))
    elements: list[dict[str, Any]] = data["elements"]
    for el in elements:
        el["name"] = redact_text(str(el.get("name") or ""))
        el["label_text"] = redact_text(str(el.get("label_text") or ""))
        if el.get("value") is None:
            continue
        if _looks_like_password(str(el.get("name") or ""), str(el.get("label_text") or "")):
            el["value"] = _CREDENTIAL_MASK
        else:
            el["value"] = redact_text(str(el["value"]))
    return data


def _mask_password_typed_values(step: dict[str, Any]) -> None:
    """Drop typed credentials for password-like textboxes. Requirement 3.4."""
    observation: dict[str, Any] = step.get("observation") or {}
    pw_indices: set[int] = _password_indices(observation)
    raw: dict[str, Any] = step.get("raw_tool_call") or {}
    inp: dict[str, Any] = raw["input"] if isinstance(raw.get("input"), dict) else {}
    action: dict[str, Any] | None = step.get("action")

    index: int | None = None
    if action is not None and action.get("element_index") is not None:
        index = int(action["element_index"])
    elif inp.get("element_index") is not None:
        index = int(inp["element_index"])
    if index is None or index not in pw_indices:
        return

    secret: str = ""
    if action is not None and action.get("text"):
        secret = str(action["text"])
        action["text"] = _CREDENTIAL_MASK
    if inp.get("text"):
        if not secret:
            secret = str(inp["text"])
        inp["text"] = _CREDENTIAL_MASK

    if not secret:
        return
    step["reason"] = _replace_secret(str(step.get("reason") or ""), secret)
    if action is not None and action.get("reason"):
        action["reason"] = _replace_secret(str(action["reason"]), secret)
    if inp.get("reason"):
        inp["reason"] = _replace_secret(str(inp["reason"]), secret)


def _typed_password_secrets(steps: list[dict[str, Any]]) -> list[str]:
    secrets: list[str] = []
    for step in steps:
        observation: dict[str, Any] = step.get("observation") or {}
        pw_indices: set[int] = _password_indices(observation)
        raw: dict[str, Any] = step.get("raw_tool_call") or {}
        inp: dict[str, Any] = raw["input"] if isinstance(raw.get("input"), dict) else {}
        action: dict[str, Any] | None = step.get("action")
        index: int | None = None
        if action is not None and action.get("element_index") is not None:
            index = int(action["element_index"])
        elif inp.get("element_index") is not None:
            index = int(inp["element_index"])
        if index is None or index not in pw_indices:
            continue
        for candidate in ((action or {}).get("text"), inp.get("text")):
            text: str = str(candidate or "")
            if text and text != _CREDENTIAL_MASK:
                secrets.append(text)
    return secrets


def _wipe_secrets(obj: Any, secrets: list[str]) -> Any:
    if not secrets:
        return obj
    if isinstance(obj, str):
        out: str = obj
        for secret in secrets:
            out = _replace_secret(out, secret)
        return out
    if isinstance(obj, dict):
        return {key: _wipe_secrets(value, secrets) for key, value in obj.items()}
    if isinstance(obj, list):
        return [_wipe_secrets(item, secrets) for item in obj]
    return obj


def _scrub_step_dump(step: dict[str, Any]) -> dict[str, Any]:
    step["observation"] = _redact_observation_dump(step["observation"])
    _mask_password_typed_values(step)
    step["reason"] = redact_text(str(step.get("reason") or ""))
    return step


def _scrub_payload(payload: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = payload.get("steps") or []
    secrets: list[str] = _typed_password_secrets(steps)
    for step in steps:
        _scrub_step_dump(step)
    return _wipe_secrets(payload, secrets)


def _jsonl_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for step in payload.get("steps") or []:
        result: dict[str, Any] | None = step.get("result")
        compact: dict[str, Any] = {
            "step": step["step"],
            "tool": str((step.get("raw_tool_call") or {}).get("name") or ""),
            "reason": step["reason"],
            "url": step["observation"]["url"],
            "signature": step["observation"]["page_signature"],
            "ok": result.get("ok") if result else None,
            "error": result.get("error") if result else None,
            "ms": step["elapsed_ms"],
            "screenshot": step["observation"]["screenshot_path"],
        }
        lines.append(json.dumps(compact, ensure_ascii=False))
    return lines


def write_run_trace(trace: RunTrace, evidence_dir: Path) -> None:
    """Write trace.json (full) and trace.jsonl (one compact line per step)."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = _scrub_payload(trace.model_dump(mode="json"))
    (evidence_dir / "trace.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    lines: list[str] = _jsonl_lines(payload)
    (evidence_dir / "trace.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def scrub_trace_files(evidence_dir: Path) -> None:
    """Re-apply write-time redaction to an already-written trace.json."""
    path: Path = evidence_dir / "trace.json"
    payload: dict[str, Any] = _scrub_payload(json.loads(path.read_text(encoding="utf-8")))
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines: list[str] = _jsonl_lines(payload)
    (evidence_dir / "trace.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


TraceStep.model_rebuild()
RunTrace.model_rebuild()

