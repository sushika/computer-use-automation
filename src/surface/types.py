"""Surface-agnostic perception and action contract.

This is the seam between "how we perceive and act on a surface" and
"the recorded flow." Layers above — discovery, the capability artifact,
replay, detectors, escalation — may only see these types.

Nothing above this layer may reference Playwright, CSS selectors, or the
DOM. A DesktopSurface backed by Windows UI Automation must satisfy this
same interface with no change to the artifact schema or replay engine.

ElementRef.index is a per-observation handle, not a stable identity.
Replay stores a multi-signal descriptor (role, accessible name, label
text, container_path, ordinal_in_container, bbox as a hint) and re-scores
candidates on a fresh Observation. High confidence → act; ambiguous or
no match → do not guess, escalate.
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class Role(str, Enum):
    """Accessibility role. `str` Enum so logs and JSON show 'button', not 'Role.BUTTON'."""

    BUTTON = "button"
    TEXTBOX = "textbox"
    LINK = "link"
    COMBOBOX = "combobox"
    CHECKBOX = "checkbox"
    TEXT = "text"
    DIALOG = "dialog"
    HEADING = "heading"
    UNKNOWN = "unknown"


class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    NAVIGATE = "navigate"
    READ = "read"
    WAIT = "wait"


class BBox(BaseModel):
    """Axis-aligned box in top-level surface coordinates (page / window)."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    x: float
    y: float
    width: float
    height: float


class ElementRef(BaseModel):
    """One node from an Observation's accessibility tree.

    `index` is valid only against the Observation that produced it.
    Do not persist it in a capability artifact. Do not treat it as an id.
    extra='forbid' is the hard constraint: a selector cannot be smuggled in.
    """

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(
        description="Per-observation handle only. Explicitly NOT a stable identity."
    )
    role: Role
    name: str = Field(description="Accessible name. Never a generated DOM id.")
    value: str | None = None
    label_text: str = Field(
        default="",
        description="Associated or nearby label text, even when name came from elsewhere.",
    )
    container_path: list[str] = Field(
        default_factory=list,
        description="Iframe names/titles on web; window/pane hierarchy on desktop.",
    )
    ordinal_in_container: int = Field(
        description="0-based index among same-role siblings in this container.",
    )
    enabled: bool = True
    visible: bool = True
    bbox: BBox


class Observation(BaseModel):
    """A screenshot plus a numbered accessibility tree. No selectors."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    url: str
    page_signature: str = Field(
        description="Short hash of screen chrome (headings + control roles/names), not instance data.",
    )
    elements: list[ElementRef]
    text_content: str = Field(description="All visible text, for detectors.")
    screenshot_path: str
    dialog_present: bool


class Action(BaseModel):
    """An act on the current Observation. element_index is that Observation's handle."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="forbid")

    type: ActionType
    element_index: int | None = None
    text: str | None = None
    url: str | None = None
    reason: str = Field(description="Why the action was taken — goes into the evidence log.")


class ActionResult(BaseModel):
    """Outcome of Surface.act. Ordinary interaction failure is ok=False, not an exception."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    error: str | None = None
    read_value: str | None = None


@runtime_checkable
class Surface(Protocol):
    def observe(self) -> Observation:
        """Screenshot + indexed a11y elements. Indices die on the next observe()."""
        ...

    def act(self, action: Action) -> ActionResult:
        """Sole chokepoint for acting on the live surface."""
        ...

    def close(self) -> None:
        """Release the underlying session."""
        ...
