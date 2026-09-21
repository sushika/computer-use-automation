"""
Capability artifact schema.

A Capability is what an AI agent invokes in production. It is compiled from a
discovery RunTrace, reviewed by a human, and replayed with no LLM in the loop.

Design rules:
- Surface-agnostic: targets are described by role / name / label / container,
  never by CSS selector, XPath, or per-observation index.
- Parameterized: literals from the discovery run become typed inputs.
- Secret-free: credentials appear only as {{secret.*}} references, resolved
  from the environment at replay time.
- The three runtime result classes are explicit in the schema:
    known_outcomes -> expected business results (returned, not failures)
    recoveries     -> recoverable conditions (handled, then continue)
    anything else  -> hard failure (stop with debuggable detail)
"""
from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import ClassVar, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.surface.types import ActionType, Role

SCHEMA_VERSION = "1.0"
_TEMPLATE_RE = re.compile(r"\{\{\s*(input|secret)\.([a-z_][a-z0-9_]*)\s*\}\}")


class _Strict(BaseModel):
    """Unknown fields are rejected, so nothing surface-specific can sneak in."""
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")


# ---------- enums ----------

class MatchStrategy(str, Enum):
    EXACT = "exact"
    EXACT_THEN_FUZZY = "exact_then_fuzzy"


class CheckpointKind(str, Enum):
    PAGE_SIGNATURE = "page_signature"
    TEXT_PRESENT = "text_present"
    ELEMENT_PRESENT = "element_present"
    DIALOG_PRESENT = "dialog_present"


class RiskClass(str, Enum):
    SAFE_REVERSIBLE = "safe_reversible"
    RISKY_IRREVERSIBLE = "risky_irreversible"


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    MONEY = "money"
    BOOLEAN = "boolean"


class SurfaceType(str, Enum):
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


class RecoveryAction(str, Enum):
    DISMISS_DIALOG = "dismiss_dialog"
    WAIT_AND_RETRY = "wait_and_retry"


# ---------- building blocks ----------

class TargetDescriptor(_Strict):
    """How to find a control, independent of surface technology.
    Replay scores live elements against all of these signals together."""
    role: Role
    name: str
    label_text: Optional[str] = None
    container_path: list[str] = Field(default_factory=list)
    ordinal_in_container: int = 0
    match: MatchStrategy = MatchStrategy.EXACT_THEN_FUZZY
    robustness_note: str = ""


class Checkpoint(_Strict):
    """A condition asserted to prove we reached the expected state."""
    kind: CheckpointKind
    expected: str
    description: str
    timeout_ms: int = 10_000


class ParamSpec(_Strict):
    """A typed input the caller supplies, or a typed output it gets back."""
    name: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    type: ParamType
    description: str
    required: bool = True
    pattern: Optional[str] = None
    sensitive: bool = False


class Step(_Strict):
    index: int
    action: ActionType
    target: Optional[TargetDescriptor] = None
    value_template: Optional[str] = None
    url_template: Optional[str] = None
    output_key: Optional[str] = None
    wait_for: Optional[Checkpoint] = None
    checkpoint: Optional[Checkpoint] = None
    risk: RiskClass = RiskClass.SAFE_REVERSIBLE
    rationale: str


class KnownOutcome(_Strict):
    """An expected business result. Returned to the caller; NOT a failure."""
    code: str
    detector: Checkpoint
    description: str


class RecoveryRule(_Strict):
    """A recoverable condition: handle it, then continue the flow."""
    code: str
    detector: Checkpoint
    action: RecoveryAction
    max_attempts: int = Field(default=2, ge=1, le=5)
    description: str


class Allowlist(_Strict):
    url_patterns: list[str]
    action_types: list[ActionType]


class TargetApp(_Strict):
    app_id: str
    vendor_product: str
    surface_type: SurfaceType
    entry_url: str
    allowlist: Allowlist
    required_secrets: list[str] = Field(default_factory=list)


class Provenance(_Strict):
    discovery_run_id: str
    model: str
    compiled_at: datetime
    compiler_version: str
    recorded_on: str
    reviewed_by: Optional[str] = None


# ---------- the capability ----------

class Capability(_Strict):
    schema_version: str = SCHEMA_VERSION
    capability_id: str
    capability_version: int = Field(ge=1)
    name: str
    description: str
    approval_state: ApprovalState = ApprovalState.DRAFT

    target: TargetApp
    inputs: list[ParamSpec]
    outputs: list[ParamSpec]
    steps: list[Step]
    success_condition: Checkpoint
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)
    recoveries: list[RecoveryRule] = Field(default_factory=list)
    provenance: Provenance

    @model_validator(mode="after")
    def _check_internal_consistency(self) -> "Capability":
        """An artifact that references things it doesn't declare is rejected
        at load time, not discovered broken halfway through a replay."""
        input_names = {p.name for p in self.inputs}
        output_names = {p.name for p in self.outputs}
        secrets = set(self.target.required_secrets)

        if [s.index for s in self.steps] != list(range(len(self.steps))):
            raise ValueError("step indices must be 0..n-1 with no gaps")

        for s in self.steps:
            for tmpl in (s.value_template, s.url_template):
                if tmpl is None:
                    continue
                for kind, name in _TEMPLATE_RE.findall(tmpl):
                    if kind == "input" and name not in input_names:
                        raise ValueError(f"step {s.index}: undeclared input '{name}'")
                    if kind == "secret" and name not in secrets:
                        raise ValueError(f"step {s.index}: undeclared secret '{name}'")

            if s.action in (ActionType.CLICK, ActionType.TYPE, ActionType.READ) and s.target is None:
                raise ValueError(f"step {s.index}: {s.action.value} needs a target")
            if s.action == ActionType.TYPE and s.value_template is None:
                raise ValueError(f"step {s.index}: type needs a value_template")
            if s.action == ActionType.READ and s.output_key not in output_names:
                raise ValueError(f"step {s.index}: read must write a declared output")

        produced = {s.output_key for s in self.steps if s.output_key}
        missing = output_names - produced
        if missing:
            raise ValueError(f"outputs never produced by any step: {sorted(missing)}")
        return self