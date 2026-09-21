from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.artifact.schema import (
    Allowlist, Capability, Checkpoint, CheckpointKind, ParamSpec, ParamType,
    Provenance, Step, SurfaceType, TargetApp, TargetDescriptor,
)
from src.surface.types import ActionType, Role


def make(**overrides):
    data = dict(
        capability_id="lookup_savings_balance",
        capability_version=1,
        name="Look up savings balance",
        description="Returns a member's primary savings balance.",
        target=TargetApp(
            app_id="legacy_cu", vendor_product="legacy_cu_core",
            surface_type=SurfaceType.LEGACY_WEB, entry_url="http://127.0.0.1:5001/",
            allowlist=Allowlist(url_patterns=["http://127.0.0.1:5001/*"],
                                action_types=[ActionType.CLICK, ActionType.TYPE, ActionType.READ]),
        ),
        inputs=[ParamSpec(name="member_id", type=ParamType.STRING,
                          description="Member number", pattern=r"^\d{6}$")],
        outputs=[ParamSpec(name="savings_balance", type=ParamType.MONEY,
                           description="Primary savings balance")],
        steps=[
            Step(index=0, action=ActionType.TYPE,
                 target=TargetDescriptor(role=Role.TEXTBOX, name="Member ID",
                                         container_path=["Main content"]),
                 value_template="{{input.member_id}}", rationale="Enter member"),
            Step(index=1, action=ActionType.READ,
                 target=TargetDescriptor(role=Role.TEXT, name="Primary savings balance",
                                         container_path=["Main content"]),
                 output_key="savings_balance", rationale="Read balance"),
        ],
        success_condition=Checkpoint(kind=CheckpointKind.TEXT_PRESENT,
                                     expected="Primary savings balance",
                                     description="Member detail reached"),
        provenance=Provenance(discovery_run_id="913e8a66", model="claude-sonnet-5",
                              compiled_at=datetime.now(timezone.utc),
                              compiler_version="0.1", recorded_on="legacy_cu_base"),
    )
    data.update(overrides)
    return Capability(**data)


def test_valid_capability_loads():
    assert make().approval_state.value == "draft"

def test_undeclared_input_rejected():
    with pytest.raises(ValidationError, match="undeclared input"):
        make(inputs=[])

def test_undeclared_output_rejected():
    with pytest.raises(ValidationError):
        make(outputs=[])

def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        make(selector="#ctl00_MainContent_txtMemberId")