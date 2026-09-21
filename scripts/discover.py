"""CLI for a real discovery run. Writes evidence/<run_id>/trace.json."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent.loop import DEFAULT_MAX_STEPS, run  # noqa: E402


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description="LLM-driven discovery against a live surface. Emits a RunTrace."
    )
    parser.add_argument("--goal", required=True, help="Natural-language goal")
    parser.add_argument("--url", required=True, help="Entry URL")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    args: argparse.Namespace = parser.parse_args()
    run(goal=args.goal, entry_url=args.url, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
