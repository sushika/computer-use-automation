"""LLM-driven discovery: observe → decide → act. Emits a RunTrace.

One tool call per turn. Indices are valid only for the observation sent
with that turn; after Surface.act we always re-observe. This loop does
not compile a capability artifact — that is a later, separate step.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from anthropic import Anthropic
from dotenv import load_dotenv

from agent.redact import redact_text
from agent.trace import RunTrace, TerminalStatus, TraceStep, write_run_trace
from surface.types import Action, ActionResult, ActionType, ElementRef, Observation, Surface
from surface.web import WebSurface

MODEL: Final[str] = "claude-sonnet-5"
MAX_CONSECUTIVE_FAILURES: Final[int] = 3
DEFAULT_MAX_STEPS: Final[int] = 25

SYSTEM_PROMPT: Final[str] = """\
You are discovering a flow on a legacy credit-union back office. A later
compiler will turn this run into a replayable capability. You are not the
production operator.

Rules:
- Call exactly one tool per turn. No free-form actions.
- Act only on listed element indices. Indices are valid for the CURRENT
  observation only. Never reuse an index from a previous turn. Never invent
  an index that is not in the list.
- Prefer controls by accessible name and frame path. ASP.NET ids like
  ctl00$MainContent$txtMemberId are noise; they will not appear as names.
- Some buttons are <input type="button"> with onclick. They still appear
  as role button. Click them.
- If the screen shows an error, permission denial, missing record, an
  unexpected dialog you cannot dismiss from the list, or a required
  control is absent, call stuck. Do not click around hoping it works.
- When the goal asks you to read a value, call read_value on the element
  that displays it, then call done with that value in extracted.
- reason is mandatory on every tool. It is the evidence log.
"""

TOOLS: Final[list[dict[str, Any]]] = [
    {
        "name": "click",
        "description": "Click an element from the current observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "reason": {"type": "string"},
            },
            "required": ["element_index", "reason"],
        },
    },
    {
        "name": "type_text",
        "description": "Type into a textbox or combobox from the current observation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "text": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_index", "text", "reason"],
        },
    },
    {
        "name": "navigate",
        "description": "Navigate the browser to a url.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["url", "reason"],
        },
    },
    {
        "name": "read_value",
        "description": "Read the text or value of an element the goal asked for.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_index": {"type": "integer"},
                "field_name": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["element_index", "field_name", "reason"],
        },
    },
    {
        "name": "done",
        "description": "The goal is complete. Stop.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "extracted": {"type": "object"},
                "reason": {"type": "string"},
            },
            "required": ["summary", "extracted", "reason"],
        },
    },
    {
        "name": "stuck",
        "description": "Cannot proceed safely. Escalation seam — terminates this run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "what_i_tried": {"type": "string"},
            },
            "required": ["reason", "what_i_tried"],
        },
    },
]


def _frame_label(el: ElementRef) -> str:
    return "/".join(el.container_path) if el.container_path else "top"


def render_element_list(obs: Observation) -> str:
    lines: list[str] = []
    for el in obs.elements:
        name: str = redact_text(el.name)
        lines.append(f'[{el.index}] {el.role.value} "{name}" (frame: {_frame_label(el)})')
    return "\n".join(lines) if lines else "(no elements)"


def render_history(steps: list[TraceStep]) -> str:
    if not steps:
        return "(none)"
    lines: list[str] = []
    for step in steps:
        tool_name: str = str(step.raw_tool_call.get("name") or "")
        outcome: str = "ok"
        if step.result is not None and not step.result.ok:
            outcome = step.result.error or "failed"
        elif step.result is not None and step.result.read_value is not None:
            outcome = f"ok read={redact_text(step.result.read_value)!r}"
        lines.append(f"{step.step}. {tool_name} — {redact_text(step.reason)} → {outcome}")
    return "\n".join(lines)


def build_user_turn(goal: str, obs: Observation, steps: list[TraceStep]) -> str:
    """Current observation only. Prior observations are not replayed into context."""
    visible: str = redact_text(obs.text_content)
    if len(visible) > 2000:
        visible = visible[:2000] + "\n…"
    dialog_line: str = (
        "dialog_present: true — dismiss it from the list before other work."
        if obs.dialog_present
        else "dialog_present: false"
    )
    return (
        f"Goal:\n{goal}\n\n"
        f"Current url: {obs.url}\n"
        f"page_signature: {obs.page_signature}\n"
        f"{dialog_line}\n\n"
        f"Prior actions (reasons only; indices from those turns are dead):\n"
        f"{render_history(steps)}\n\n"
        f"Elements (current observation only):\n"
        f"{render_element_list(obs)}\n\n"
        f"Visible text (redacted; balances kept):\n{visible}"
    )


def _image_block(screenshot_path: str) -> dict[str, Any] | None:
    path: Path = Path(screenshot_path)
    if not path.is_file():
        return None
    data: str = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": data,
        },
    }


def _parse_tool(response: Any) -> dict[str, Any] | None:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use":
            return {
                "id": block.id,
                "name": block.name,
                "input": dict(block.input),
                "type": "tool_use",
            }
    return None


def _require_reason(args: dict[str, Any]) -> str:
    reason: str = str(args.get("reason") or "").strip()
    if not reason:
        raise ValueError("reason is required on every tool")
    return reason


def _action_from_tool(name: str, args: dict[str, Any]) -> Action:
    reason: str = _require_reason(args)
    if name == "click":
        return Action(
            type=ActionType.CLICK,
            element_index=int(args["element_index"]),
            reason=reason,
        )
    if name == "type_text":
        return Action(
            type=ActionType.TYPE,
            element_index=int(args["element_index"]),
            text=str(args["text"]),
            reason=reason,
        )
    if name == "navigate":
        return Action(
            type=ActionType.NAVIGATE,
            url=str(args["url"]),
            reason=reason,
        )
    if name == "read_value":
        return Action(
            type=ActionType.READ,
            element_index=int(args["element_index"]),
            reason=reason,
        )
    raise ValueError(f"not a surface tool: {name}")


def _stringify_extracted(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        out[str(key)] = "" if value is None else str(value)
    return out


def decide(client: Anthropic, goal: str, obs: Observation, steps: list[TraceStep]) -> dict[str, Any]:
    content: list[dict[str, Any]] = [{"type": "text", "text": build_user_turn(goal, obs, steps)}]
    image: dict[str, Any] | None = _image_block(obs.screenshot_path)
    if image is not None:
        content.append(image)
    response: Any = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=TOOLS,
        tool_choice={"type": "any"},
        messages=[{"role": "user", "content": content}],
    )
    parsed: dict[str, Any] | None = _parse_tool(response)
    if parsed is None:
        raise ValueError("model returned no tool call")
    return parsed


def _print_step_start(step: int, obs: Observation) -> None:
    print(f"step {step}  url={obs.url}  sig={obs.page_signature}  n={len(obs.elements)}")
    print(render_element_list(obs))


def _print_decision(name: str, args: dict[str, Any]) -> None:
    reason: str = str(args.get("reason") or "")
    extra: str = ""
    if "element_index" in args:
        extra = f" [{args['element_index']}]"
    if "text" in args:
        extra += f" text={args['text']!r}"
    if "field_name" in args:
        extra += f" field={args['field_name']!r}"
    print(f"        -> {name}{extra}  ({reason})")


def _print_result(result: ActionResult | None, elapsed_ms: int) -> None:
    if result is None:
        print(f"        <- (no surface act)  {elapsed_ms}ms")
        return
    if result.ok:
        suffix: str = f" read={result.read_value!r}" if result.read_value is not None else ""
        print(f"        <- ok{suffix}  {elapsed_ms}ms")
    else:
        print(f"        <- FAIL {result.error}  {elapsed_ms}ms")


def run(
    goal: str,
    entry_url: str,
    max_steps: int = DEFAULT_MAX_STEPS,
    *,
    evidence_root: str | Path = "evidence",
) -> RunTrace:
    load_dotenv()
    api_key: str | None = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY missing from environment / .env")

    run_id: str = uuid.uuid4().hex[:8]
    evidence_dir: Path = Path(evidence_root) / run_id
    evidence_dir.mkdir(parents=True, exist_ok=True)
    client: Anthropic = Anthropic(api_key=api_key)
    surface: Surface = WebSurface(run_id=run_id, evidence_root=evidence_root)

    trace: RunTrace = RunTrace(
        run_id=run_id,
        goal=goal,
        entry_url=entry_url,
        model=MODEL,
        started_at=datetime.now(timezone.utc),
    )
    print(f"run_id={run_id}")
    print(f"evidence={evidence_dir}")
    print(f"goal={goal}")

    consecutive_failures: int = 0
    extracted: dict[str, str] = {}

    try:
        nav: ActionResult = surface.act(
            Action(type=ActionType.NAVIGATE, url=entry_url, reason="open entry url")
        )
        if not nav.ok:
            trace.status = TerminalStatus.DEAD_END
            trace.final_summary = f"entry navigate failed: {nav.error}"
            return trace

        for step_index in range(max_steps):
            obs: Observation = surface.observe()
            _print_step_start(step_index, obs)
            t0: float = time.perf_counter()
            try:
                tool_call: dict[str, Any] = decide(client, goal, obs, trace.steps)
            except (ValueError, OSError) as exc:
                elapsed_ms: int = int((time.perf_counter() - t0) * 1000)
                trace.steps.append(
                    TraceStep(
                        step=step_index,
                        observation=obs,
                        action=None,
                        reason=str(exc),
                        raw_tool_call={"name": "stuck", "input": {"reason": str(exc)}},
                        result=ActionResult(ok=False, error=str(exc)),
                        elapsed_ms=elapsed_ms,
                    )
                )
                consecutive_failures += 1
                _print_result(ActionResult(ok=False, error=str(exc)), elapsed_ms)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    trace.status = TerminalStatus.DEAD_END
                    trace.final_summary = "three consecutive failed actions"
                    break
                continue

            name: str = str(tool_call.get("name") or "")
            args: dict[str, Any] = dict(tool_call.get("input") or {})
            _print_decision(name, args)

            if name == "done":
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                reason: str = str(args.get("reason") or "done")
                extracted.update(_stringify_extracted(args.get("extracted")))
                trace.steps.append(
                    TraceStep(
                        step=step_index,
                        observation=obs,
                        action=None,
                        reason=reason,
                        raw_tool_call=tool_call,
                        result=ActionResult(ok=True),
                        elapsed_ms=elapsed_ms,
                    )
                )
                _print_result(ActionResult(ok=True), elapsed_ms)
                trace.status = TerminalStatus.GOAL_REACHED
                trace.final_summary = str(args.get("summary") or "")
                trace.extracted = extracted
                break

            if name == "stuck":
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                reason = str(args.get("reason") or "stuck")
                tried: str = str(args.get("what_i_tried") or "")
                trace.steps.append(
                    TraceStep(
                        step=step_index,
                        observation=obs,
                        action=None,
                        reason=reason,
                        raw_tool_call=tool_call,
                        result=ActionResult(ok=True),
                        elapsed_ms=elapsed_ms,
                    )
                )
                _print_result(None, elapsed_ms)
                trace.status = TerminalStatus.ESCALATED
                trace.final_summary = f"{reason} | tried: {tried}"
                trace.extracted = extracted
                break

            try:
                action: Action = _action_from_tool(name, args)
            except (KeyError, TypeError, ValueError) as exc:
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                fail: ActionResult = ActionResult(ok=False, error=str(exc))
                trace.steps.append(
                    TraceStep(
                        step=step_index,
                        observation=obs,
                        action=None,
                        reason=str(args.get("reason") or str(exc)),
                        raw_tool_call=tool_call,
                        result=fail,
                        elapsed_ms=elapsed_ms,
                    )
                )
                consecutive_failures += 1
                _print_result(fail, elapsed_ms)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    trace.status = TerminalStatus.DEAD_END
                    trace.final_summary = "three consecutive failed actions"
                    break
                continue

            result: ActionResult = surface.act(action)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            if name == "read_value" and result.ok and result.read_value is not None:
                field_name: str = str(args.get("field_name") or "value")
                extracted[field_name] = result.read_value
            if result.ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            trace.steps.append(
                TraceStep(
                    step=step_index,
                    observation=obs,
                    action=action,
                    reason=action.reason,
                    raw_tool_call=tool_call,
                    result=result,
                    elapsed_ms=elapsed_ms,
                )
            )
            _print_result(result, elapsed_ms)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                trace.status = TerminalStatus.DEAD_END
                trace.final_summary = "three consecutive failed actions"
                break
        else:
            trace.status = TerminalStatus.MAX_STEPS
            trace.final_summary = f"hit max_steps={max_steps}"
            trace.extracted = extracted
    finally:
        surface.close()
        trace.ended_at = datetime.now(timezone.utc)
        if trace.status is None:
            trace.status = TerminalStatus.DEAD_END
        if not trace.extracted:
            trace.extracted = extracted
        write_run_trace(trace, evidence_dir)
        print(f"status={trace.status.value}")
        print(f"wrote {evidence_dir / 'trace.json'}")
    return trace
