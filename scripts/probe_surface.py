"""Manual perception harness against the local legacy CU app.

Start the app first:  python apps/legacy_cu/app.py
Then:                 python scripts/probe_surface.py
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

ROOT: Path = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from surface.types import (  # noqa: E402
    Action,
    ActionResult,
    ActionType,
    ElementRef,
    Observation,
    Role,
)
from surface.web import WebSurface  # noqa: E402

APP_URL: str = "http://127.0.0.1:5001/"


def print_observation(obs: Observation, heading: str) -> None:
    print(heading)
    print(f"  url={obs.url}")
    print(f"  signature={obs.page_signature}")
    print(f"  dialog_present={obs.dialog_present}")
    print(f"  screenshot={obs.screenshot_path}")
    print(f"  text_chars={len(obs.text_content)}")
    for el in obs.elements:
        frames: str = "/".join(el.container_path) if el.container_path else "(top)"
        print(
            f"  [{el.index}] {el.role.value} name={el.name!r} "
            f"frames={frames} ord={el.ordinal_in_container}"
        )
    print()


def require(obs: Observation, role: Role, name: str) -> ElementRef:
    hits: list[ElementRef] = [el for el in obs.elements if el.role is role and el.name == name]
    if not hits:
        names: str = ", ".join(f"{el.role.value}:{el.name!r}" for el in obs.elements)
        raise SystemExit(f"expected {role.value} name={name!r}; have: {names}")
    return hits[0]


def main() -> None:
    run_id: str = uuid.uuid4().hex[:8]
    surface: WebSurface = WebSurface(run_id=run_id)
    try:
        nav: ActionResult = surface.act(
            Action(type=ActionType.NAVIGATE, url=APP_URL, reason="open legacy CU shell")
        )
        if not nav.ok:
            raise SystemExit(f"navigate failed: {nav.error}")

        login: Observation = surface.observe()
        print_observation(login, "=== login (after navigate) ===")

        teller: ElementRef = require(login, Role.TEXTBOX, "Teller ID")
        password: ElementRef = require(login, Role.TEXTBOX, "Password")
        logon: ElementRef = require(login, Role.BUTTON, "Log On")

        typed: ActionResult = surface.act(
            Action(
                type=ActionType.TYPE,
                element_index=teller.index,
                text="jdoe",
                reason="enter teller id",
            )
        )
        if not typed.ok:
            raise SystemExit(f"type teller id failed: {typed.error}")
        typed = surface.act(
            Action(
                type=ActionType.TYPE,
                element_index=password.index,
                text="demo123",
                reason="enter password",
            )
        )
        if not typed.ok:
            raise SystemExit(f"type password failed: {typed.error}")
        clicked: ActionResult = surface.act(
            Action(
                type=ActionType.CLICK,
                element_index=logon.index,
                reason="submit logon",
            )
        )
        if not clicked.ok:
            raise SystemExit(f"click Log On failed: {clicked.error}")

        search: Observation = surface.observe()
        print_observation(search, "=== search (after login) ===")
        require(search, Role.TEXTBOX, "Member ID")
        require(search, Role.TEXTBOX, "Last name")
        require(search, Role.BUTTON, "Search")
    finally:
        surface.close()


if __name__ == "__main__":
    main()
