"""Compile a discovery RunTrace into a draft Capability JSON artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent.trace import RunTrace  # noqa: E402
from src.artifact.compiler import DEFAULT_CAPABILITY_ID, compile_trace  # noqa: E402
from src.artifact.schema import Capability  # noqa: E402


def load_trace(path: Path) -> RunTrace:
    return RunTrace.model_validate(json.loads(path.read_text(encoding="utf-8")))


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="Compile a RunTrace into a draft Capability artifact."
    )
    parser.add_argument("--trace", required=True, type=Path, help="Path to trace.json")
    parser.add_argument("--out", required=True, type=Path, help="Where to write the Capability JSON")
    parser.add_argument(
        "--capability-id",
        default=DEFAULT_CAPABILITY_ID,
        help=f"Stable capability identity slug (default: {DEFAULT_CAPABILITY_ID})",
    )
    args: argparse.Namespace = parser.parse_args()

    trace: RunTrace = load_trace(args.trace)
    capability, review = compile_trace(trace, capability_id=args.capability_id)
    # Constructing Capability already validates; round-trip so a drift from the
    # schema fails here, not at replay time.
    validated: Capability = Capability.model_validate(capability.model_dump(mode="json"))

    print(review.format())

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(validated.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote draft capability to {args.out}")


if __name__ == "__main__":
    main()
