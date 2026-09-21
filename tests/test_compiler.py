from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.trace import RunTrace  # noqa: E402
from src.artifact.compiler import (  # noqa: E402
    compile_trace,
    infer_param_type,
    snake_case,
)
from src.artifact.schema import (  # noqa: E402
    ApprovalState,
    Capability,
    CheckpointKind,
    ParamType,
)
from src.surface.types import ActionType  # noqa: E402

TRACE_PATH = ROOT / "evidence" / "913e8a66" / "trace.json"


def _load_example_trace() -> RunTrace:
    if not TRACE_PATH.is_file():
        pytest.skip(f"example trace not present: {TRACE_PATH}")
    return RunTrace.model_validate(json.loads(TRACE_PATH.read_text(encoding="utf-8")))


def test_snake_case_element_names():
    assert snake_case("Member ID") == "member_id"
    assert snake_case("Teller ID") == "teller_id"
    assert snake_case("Password") == "password"
    assert snake_case("Primary savings balance") == "primary_savings_balance"


def test_infer_money_and_not_bare_integers():
    assert infer_param_type("$12,847.55") == ParamType.MONEY
    assert infer_param_type("100241") == ParamType.INTEGER
    assert infer_param_type("jdoe") == ParamType.STRING


@pytest.fixture(scope="module")
def compiled():
    capability, review = compile_trace(_load_example_trace())
    return capability, review


def test_example_trace_compiles_to_expected_shape(compiled):
    capability, _review = compiled
    assert [s.action for s in capability.steps] == [
        ActionType.TYPE, ActionType.TYPE, ActionType.CLICK,
        ActionType.TYPE, ActionType.CLICK, ActionType.CLICK,
        ActionType.READ,
    ]
    assert [s.index for s in capability.steps] == list(range(7))
    assert [p.name for p in capability.inputs] == ["teller_id", "member_id"]
    assert [p.name for p in capability.outputs] == ["primary_savings_balance"]
    assert capability.outputs[0].type == ParamType.MONEY
    assert capability.outputs[0].description == "Primary savings balance"
    assert capability.capability_id == "lookup_savings_balance"
    assert capability.name == "Lookup savings balance"
    assert capability.target.required_secrets == ["teller_password"]
    assert capability.steps[0].value_template == "{{input.teller_id}}"
    assert capability.steps[1].value_template == "{{secret.teller_password}}"
    assert capability.steps[3].value_template == "{{input.member_id}}"
    assert capability.steps[6].output_key == "primary_savings_balance"


def test_done_step_dropped_and_indices_renumbered(compiled):
    capability, _review = compiled
    assert len(capability.steps) == 7
    reasons = [s.rationale for s in capability.steps]
    assert not any("already read in a prior step" in r for r in reasons)


def test_password_literal_never_enters_artifact(compiled):
    capability, _review = compiled
    dumped = capability.model_dump(mode="json")
    blob = json.dumps(dumped)
    assert "[REDACTED]" not in blob
    assert capability.steps[1].value_template == "{{secret.teller_password}}"


def test_rationales_are_generic(compiled):
    capability, _review = compiled
    blob = " ".join(s.rationale for s in capability.steps)
    assert "jdoe" not in blob
    assert "100241" not in blob
    assert "Eleanor" not in blob
    assert "[REDACTED]" not in blob
    assert capability.steps[0].rationale == "Enter the teller id"
    assert capability.steps[1].rationale == "Enter the teller password"
    assert capability.steps[2].rationale == "Submit the login form"
    assert capability.steps[3].rationale == "Enter the member id"
    assert capability.steps[4].rationale == "Submit the search"
    assert capability.steps[5].rationale == "Select the matching record"
    assert capability.steps[6].rationale == "Read the primary savings balance"


def test_record_specific_click_is_genericized(compiled):
    capability, review = compiled
    select = capability.steps[5]
    assert select.action == ActionType.CLICK
    assert select.target is not None
    assert select.target.name == "Select"
    assert "varies per record" in select.target.robustness_note
    assert any("Select Eleanor Vasquez" in line for line in review.genericized_clicks)


def test_static_clicks_keep_observed_names(compiled):
    capability, _review = compiled
    assert capability.steps[2].target is not None
    assert capability.steps[2].target.name == "Log On"
    assert capability.steps[2].target.robustness_note == "static label, exact match expected to hold"
    assert capability.steps[4].target is not None
    assert capability.steps[4].target.name == "Search"


def test_success_condition_and_known_outcome_stub(compiled):
    capability, review = compiled
    assert capability.success_condition.kind == CheckpointKind.PAGE_SIGNATURE
    assert capability.success_condition.expected == "cf5c000c6165205d"
    assert capability.known_outcomes[0].code == "member_not_found"
    assert capability.known_outcomes[0].detector.expected == "TODO: capture actual not-found text"
    assert review.known_outcomes


def test_never_auto_approves(compiled):
    capability, _review = compiled
    assert capability.approval_state is ApprovalState.DRAFT
    Capability.model_validate(capability.model_dump(mode="json"))
