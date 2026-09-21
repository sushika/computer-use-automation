"""Compile a discovery RunTrace into a draft Capability artifact.

The compiler is mechanical: it parameterizes literals, redacts secrets,
and genericizes record-specific click names. A human still reviews the
draft — approval_state is always DRAFT.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from src.artifact.schema import (
    Allowlist,
    ApprovalState,
    Capability,
    Checkpoint,
    CheckpointKind,
    KnownOutcome,
    MatchStrategy,
    ParamSpec,
    ParamType,
    Provenance,
    Step,
    SurfaceType,
    TargetApp,
    TargetDescriptor,
)
from src.surface.types import ActionType, ElementRef, Observation, Role

if TYPE_CHECKING:
    from src.agent.trace import RunTrace, TraceStep

COMPILER_VERSION = "0.1"
RECORDED_ON = "legacy_cu_base"
APP_ID = "legacy_cu"
VENDOR_PRODUCT = "legacy_cu_core"
DEFAULT_CAPABILITY_ID = "lookup_savings_balance"
TELLER_PASSWORD_SECRET = "teller_password"

# Keep in lockstep with src.agent.trace._PASSWORD_HINTS / _looks_like_password.
_PASSWORD_HINTS: tuple[str, ...] = ("password", "passwd", "passcode", "secret")
_SNAKE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")
_MONEY_RE = re.compile(r"^\$?\s*-?\d{1,3}(?:,\d{3})*(?:\.\d{2})?$")
_INT_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?\d+\.\d+$")


@dataclass
class CompileReview:
    """Human-readable guesses the compiler wants confirmed before approval."""

    inputs: list[str] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    genericized_clicks: list[str] = field(default_factory=list)
    known_outcomes: list[str] = field(default_factory=list)

    def format(self) -> str:
        lines: list[str] = [
            "=== Capability compile review ===",
            "approval_state: draft (never auto-approved)",
            "",
            "Proposed inputs:",
        ]
        lines.extend(_bullets(self.inputs))
        lines.extend(["", "Proposed secrets:"])
        lines.extend(_bullets(self.secrets))
        lines.extend(["", "Genericized click targets:"])
        lines.extend(_bullets(self.genericized_clicks))
        lines.extend(["", "Known-outcome stubs:"])
        lines.extend(_bullets(self.known_outcomes))
        lines.append("")
        lines.append("Review these guesses before approving the draft.")
        return "\n".join(lines)


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["  (none)"]
    return [f"  - {item}" for item in items]


def _looks_like_password(name: str, label_text: str) -> bool:
    blob: str = f"{name} {label_text}".casefold()
    return any(hint in blob for hint in _PASSWORD_HINTS)


def snake_case(label: str) -> str:
    """'Member ID' / 'Primary savings balance' → member_id / primary_savings_balance."""
    collapsed: str = _NON_ALNUM_RE.sub("_", label.strip()).strip("_")
    collapsed = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", collapsed)
    out: str = re.sub(r"_+", "_", collapsed).lower()
    if not out or not _SNAKE_RE.match(out):
        out = re.sub(r"^[^a-z_]+", "", out)
        if not out or not _SNAKE_RE.match(out):
            raise ValueError(f"cannot snake_case {label!r} into a legal param name")
    return out


def slugify(text: str) -> str:
    slug: str = snake_case(text) if _NON_ALNUM_RE.sub("", text) else "capability"
    return slug


def name_from_id(capability_id: str) -> str:
    """lookup_savings_balance → 'Lookup savings balance'."""
    words: list[str] = capability_id.replace("-", "_").split("_")
    words = [w for w in words if w]
    if not words:
        return capability_id
    return words[0].capitalize() + (" " + " ".join(words[1:]) if len(words) > 1 else "")


def infer_param_type(value: str) -> ParamType:
    text: str = value.strip()
    if not text:
        return ParamType.STRING
    lowered: str = text.casefold()
    if lowered in ("true", "false", "yes", "no"):
        return ParamType.BOOLEAN
    if text.startswith("$") or ("," in text and _MONEY_RE.match(text)):
        return ParamType.MONEY
    if _INT_RE.match(text):
        return ParamType.INTEGER
    if _DECIMAL_RE.match(text):
        return ParamType.DECIMAL
    return ParamType.STRING


def _surviving_steps(trace: RunTrace) -> list[TraceStep]:
    kept: list[TraceStep] = []
    for step in trace.steps:
        if step.action is None:
            continue
        if step.result is None or not step.result.ok:
            continue
        kept.append(step)
    return kept


def _element_for(step: TraceStep) -> ElementRef:
    action = step.action
    assert action is not None
    if action.element_index is None:
        raise ValueError(f"trace step {step.step}: {action.type.value} has no element_index")
    by_index: dict[int, ElementRef] = {el.index: el for el in step.observation.elements}
    if action.element_index in by_index:
        return by_index[action.element_index]
    try:
        return step.observation.elements[action.element_index]
    except IndexError as exc:
        raise ValueError(
            f"trace step {step.step}: element_index {action.element_index} not in observation"
        ) from exc


def _static_target(element: ElementRef, *, name: Optional[str] = None,
                   robustness_note: str = "static label, exact match expected to hold") -> TargetDescriptor:
    return TargetDescriptor(
        role=element.role if isinstance(element.role, Role) else Role(element.role),
        name=element.name if name is None else name,
        label_text=element.label_text or None,
        container_path=list(element.container_path),
        ordinal_in_container=element.ordinal_in_container,
        match=MatchStrategy.EXACT_THEN_FUZZY,
        robustness_note=robustness_note,
    )


def _extracted_values(extracted: dict[str, str]) -> list[str]:
    return [v.strip() for v in extracted.values() if v and v.strip()]


def _is_record_specific(name: str, extracted: dict[str, str], observation: Observation) -> bool:
    """True when the accessible name encodes instance data, not static chrome."""
    name_cf: str = name.casefold()
    for value in _extracted_values(extracted):
        if len(value) >= 2 and value.casefold() in name_cf and value.casefold() != name_cf:
            return True
    for el in observation.elements:
        if el.name == name:
            continue
        for candidate in (el.name, el.value or "", el.label_text):
            text: str = candidate.strip()
            if len(text) < 4:
                continue
            if text.casefold() != name_cf and text.casefold() in name_cf:
                return True
    return False


def _genericize_click_name(name: str) -> str:
    parts: list[str] = name.split()
    return parts[0] if parts else name


def _snake_key_for_read(trace: RunTrace, read_value: Optional[str], field_name: str) -> str:
    """Prefer a schema-legal key already present in extracted; never the human phrase."""
    matches: list[str] = []
    if read_value is not None:
        for key, value in trace.extracted.items():
            if value == read_value and _SNAKE_RE.match(key):
                matches.append(key)
    if matches:
        # Prefer the key that snake_cases from the human field name when both exist.
        preferred: str | None = None
        try:
            preferred = snake_case(field_name)
        except ValueError:
            preferred = None
        if preferred in matches:
            return preferred
        return matches[0]
    try:
        return snake_case(field_name)
    except ValueError:
        return "value"


def _url_pattern(entry_url: str) -> str:
    if entry_url.endswith("/*"):
        return entry_url
    if entry_url.endswith("/"):
        return f"{entry_url}*"
    return f"{entry_url}/*"


def _input_type(typed: str) -> ParamType:
    """IDs stay strings even when numeric; money/bool/decimal still inferred."""
    inferred: ParamType = infer_param_type(typed)
    if inferred == ParamType.INTEGER:
        return ParamType.STRING
    return inferred


def _generic_rationale(
    action_type: ActionType,
    element: Optional[ElementRef],
    *,
    secret: bool = False,
    genericized: bool = False,
    generic_name: Optional[str] = None,
) -> str:
    """Describe the step's role in the flow — never quote discovery literals."""
    if action_type == ActionType.TYPE:
        if secret:
            return "Enter the teller password"
        label: str = (element.name if element else "value").lower()
        return f"Enter the {label}"
    if action_type == ActionType.CLICK:
        if genericized:
            verb: str = (generic_name or "Select").rstrip(".")
            return f"{verb} the matching record"
        chrome: str = (element.name if element else "").casefold()
        if chrome in ("log on", "log in", "login", "sign in", "sign on"):
            return "Submit the login form"
        if chrome in ("search", "submit"):
            return f"Submit the {chrome}"
        if element is not None:
            return f"Activate {element.name}"
        return "Click the target control"
    if action_type == ActionType.READ:
        label = (element.name if element else "value").lower()
        return f"Read the {label}"
    if action_type == ActionType.NAVIGATE:
        return "Navigate to the entry URL"
    if action_type == ActionType.WAIT:
        return "Wait for the page to settle"
    return action_type.value


def compile_trace(
    trace: RunTrace,
    capability_id: str = DEFAULT_CAPABILITY_ID,
) -> tuple[Capability, CompileReview]:
    """Turn a successful discovery RunTrace into a DRAFT Capability."""
    surviving: list[Any] = _surviving_steps(trace)
    if not surviving:
        raise ValueError("no successful surface actions to compile")

    review: CompileReview = CompileReview()
    inputs: dict[str, ParamSpec] = {}
    outputs: dict[str, ParamSpec] = {}
    secrets: list[str] = []
    steps: list[Step] = []
    used_actions: list[ActionType] = []

    for new_index, src in enumerate(surviving):
        action = src.action
        assert action is not None
        if action.type not in used_actions:
            used_actions.append(action.type)

        if action.type == ActionType.TYPE:
            element: ElementRef = _element_for(src)
            typed: str = action.text or ""
            if _looks_like_password(element.name, element.label_text):
                name: str = TELLER_PASSWORD_SECRET
                if name not in secrets:
                    secrets.append(name)
                    review.secrets.append(
                        f"{name} - from password-like field {element.name!r}; "
                        f"template {{{{secret.{name}}}}}; literal typed value discarded"
                    )
                steps.append(Step(
                    index=new_index,
                    action=ActionType.TYPE,
                    target=_static_target(element),
                    value_template=f"{{{{secret.{name}}}}}",
                    rationale=_generic_rationale(action.type, element, secret=True),
                ))
                continue
            name = snake_case(element.name)
            if name not in inputs:
                in_goal: bool = bool(typed) and typed in trace.goal
                inputs[name] = ParamSpec(
                    name=name,
                    type=_input_type(typed),
                    description=element.name,
                )
                origin: str = (
                    "discovery literal appeared in the goal"
                    if in_goal
                    else "parameterized even though the discovery literal was not in the goal"
                )
                review.inputs.append(f"{name} ({inputs[name].type.value}) - from {element.name!r}; {origin}")
            steps.append(Step(
                index=new_index,
                action=ActionType.TYPE,
                target=_static_target(element),
                value_template=f"{{{{input.{name}}}}}",
                rationale=_generic_rationale(action.type, element),
            ))

        elif action.type == ActionType.CLICK:
            element = _element_for(src)
            if _is_record_specific(element.name, trace.extracted, src.observation):
                generic: str = _genericize_click_name(element.name)
                review.genericized_clicks.append(
                    f"step {new_index}: {element.name!r} -> {generic!r} "
                    "(accessible name varies per record; fallback is role + container_path + ordinal_in_container)"
                )
                target: TargetDescriptor = _static_target(
                    element,
                    name=generic,
                    robustness_note=(
                        "accessible name varies per record; fallback signal is "
                        "role + container_path + ordinal_in_container"
                    ),
                )
                rationale = _generic_rationale(
                    action.type, element, genericized=True, generic_name=generic
                )
            else:
                target = _static_target(element)
                rationale = _generic_rationale(action.type, element)
            steps.append(Step(
                index=new_index,
                action=ActionType.CLICK,
                target=target,
                rationale=rationale,
            ))

        elif action.type == ActionType.READ:
            element = _element_for(src)
            raw_input: dict[str, Any] = src.raw_tool_call.get("input") or {}
            field_name: str = str(raw_input.get("field_name") or element.name)
            read_value: Optional[str] = src.result.read_value if src.result else None
            out_name: str = _snake_key_for_read(trace, read_value, field_name)
            sample: str = read_value or trace.extracted.get(out_name, "")
            if out_name not in outputs:
                outputs[out_name] = ParamSpec(
                    name=out_name,
                    type=infer_param_type(sample) if sample else ParamType.STRING,
                    description=field_name,
                )
            steps.append(Step(
                index=new_index,
                action=ActionType.READ,
                target=_static_target(element),
                output_key=out_name,
                rationale=_generic_rationale(action.type, element),
            ))

        elif action.type == ActionType.NAVIGATE:
            steps.append(Step(
                index=new_index,
                action=ActionType.NAVIGATE,
                url_template=action.url,
                rationale=_generic_rationale(action.type, None),
            ))

        elif action.type == ActionType.WAIT:
            steps.append(Step(
                index=new_index,
                action=ActionType.WAIT,
                rationale=_generic_rationale(action.type, None),
            ))

        else:
            raise ValueError(f"trace step {src.step}: unsupported action type {action.type}")

    last_obs: Observation = surviving[-1].observation
    success: Checkpoint = Checkpoint(
        kind=CheckpointKind.PAGE_SIGNATURE,
        expected=last_obs.page_signature,
        description="Reached the expected final screen for this capability",
    )
    stub: KnownOutcome = KnownOutcome(
        code="member_not_found",
        detector=Checkpoint(
            kind=CheckpointKind.TEXT_PRESENT,
            expected="TODO: capture actual not-found text",
            description="Placeholder until the not-found screen is captured",
        ),
        description="Member search returned no matching record. Stub detector — replace after capturing that screen.",
    )
    review.known_outcomes.append(
        'member_not_found - detector TEXT_PRESENT expected="TODO: capture actual not-found text"'
    )

    cap_id: str = slugify(capability_id) if not _SNAKE_RE.match(capability_id) else capability_id
    display_name: str = name_from_id(cap_id)
    capability: Capability = Capability(
        capability_id=cap_id,
        capability_version=1,
        name=display_name,
        description=display_name,
        approval_state=ApprovalState.DRAFT,
        target=TargetApp(
            app_id=APP_ID,
            vendor_product=VENDOR_PRODUCT,
            surface_type=SurfaceType.LEGACY_WEB,
            entry_url=trace.entry_url,
            allowlist=Allowlist(
                url_patterns=[_url_pattern(trace.entry_url)],
                action_types=used_actions,
            ),
            required_secrets=list(secrets),
        ),
        inputs=list(inputs.values()),
        outputs=list(outputs.values()),
        steps=steps,
        success_condition=success,
        known_outcomes=[stub],
        provenance=Provenance(
            discovery_run_id=trace.run_id,
            model=trace.model,
            compiled_at=datetime.now(timezone.utc),
            compiler_version=COMPILER_VERSION,
            recorded_on=RECORDED_ON,
        ),
    )
    return capability, review
