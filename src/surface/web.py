"""Playwright-backed Surface. Headed Chromium. Sync API.

Internals (frames, locators, handles) live only for the current
observation tick. They are never copied onto ElementRef / Observation.
WebSurface satisfies types.Surface by structure, not inheritance — the
Protocol file must stay free of Playwright.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Final, TypedDict, cast

from playwright.sync_api import (
    Browser,
    ElementHandle,
    Error as PlaywrightError,
    Frame,
    Page,
    Playwright,
    sync_playwright,
)

from surface.types import (
    Action,
    ActionResult,
    ActionType,
    BBox,
    ElementRef,
    Observation,
    Role,
)

# Fetch-only. Used this tick to find nodes; never stored on ElementRef.
_CANDIDATES: Final[str] = (
    'a[href], button, input:not([type="hidden"]), select, textarea, '
    'h1, h2, h3, h4, h5, h6, caption, dialog[open], [role="dialog"], '
    '[role="alert"], [role="status"], [aria-labelledby]'
)

# Runs in each frame. Name order matches the brief: associated <label>,
# aria-label, aria-labelledby, title, placeholder, nearby text. ASP.NET
# ids (ctl00$...) are treated as noise and never used as names.
# <input type="button"|"submit"> classifies as button — the CU app fires
# those with onclick, not form-submit semantics we can ignore.
_DESCRIBE_JS: Final[str] = r"""
(sel) => {
  const noise = (s) => !s || /ctl\d+\$/i.test(s) || (/\$/.test(s) && /^ctl/i.test(s));
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();

  const labelFor = (el) => {
    const doc = el.ownerDocument;
    if (el.labels && el.labels.length) {
      const t = norm(el.labels[0].innerText);
      if (t && !noise(t)) return t;
    }
    if (el.id) {
      for (const lab of doc.querySelectorAll("label[for]")) {
        if (lab.htmlFor === el.id) {
          const t = norm(lab.innerText);
          if (t && !noise(t)) return t;
        }
      }
    }
    const wrap = el.closest("label");
    if (wrap) {
      const t = norm(wrap.innerText);
      if (t && !noise(t)) return t;
    }
    return "";
  };

  const labelledBy = (el) => {
    const ids = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean);
    const parts = [];
    for (const id of ids) {
      const n = el.ownerDocument.getElementById(id);
      const t = n ? norm(n.innerText) : "";
      if (t && !noise(t)) parts.push(t);
    }
    return parts.join(" ");
  };

  const nearby = (el) => {
    const cell = el.closest("td, th");
    if (cell) {
      let prev = cell.previousElementSibling;
      while (prev) {
        const t = norm(prev.innerText);
        if (t && !noise(t)) return t;
        prev = prev.previousElementSibling;
      }
    }
    let sib = el.previousElementSibling;
    while (sib) {
      const t = norm(sib.innerText);
      if (t && !noise(t)) return t;
      sib = sib.previousElementSibling;
    }
    return "";
  };

  const classify = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (tag === "dialog" || role === "dialog") return "dialog";
    if (["h1", "h2", "h3", "h4", "h5", "h6", "caption"].includes(tag) || role === "heading") return "heading";
    if (tag === "a" || role === "link") return "link";
    if (
      tag === "button" ||
      role === "button" ||
      (tag === "input" && ["button", "submit", "reset", "image"].includes(type))
    ) {
      return "button";
    }
    if (tag === "select" || role === "combobox") return "combobox";
    if ((tag === "input" && type === "checkbox") || role === "checkbox") return "checkbox";
    if (
      tag === "textarea" ||
      role === "textbox" ||
      (tag === "input" &&
        !["hidden", "file", "checkbox", "radio", "button", "submit", "reset", "image"].includes(type))
    ) {
      return "textbox";
    }
    if (role === "alert" || role === "status") return "text";
    // Labeled non-controls (CU "Primary savings balance" is a <strong>).
    if (el.getAttribute("aria-labelledby")) return "text";
    return "unknown";
  };

  const accessibleName = (el, role) => {
    const fromLabel = labelFor(el);
    if (fromLabel) return fromLabel;
    const aria = norm(el.getAttribute("aria-label"));
    if (aria && !noise(aria)) return aria;
    const by = labelledBy(el);
    if (by) return by;
    const title = norm(el.getAttribute("title"));
    if (title && !noise(title)) return title;
    const ph = norm(el.getAttribute("placeholder"));
    if (ph && !noise(ph)) return ph;
    if (role === "button" || role === "link" || role === "heading" || role === "dialog" || role === "text") {
      const val = norm(el.getAttribute("value"));
      if (val && !noise(val)) return val;
      const tx = norm(el.innerText || el.textContent);
      if (tx && !noise(tx)) return tx.split("\n")[0].slice(0, 240);
    }
    const near = nearby(el);
    if (near) return near;
    return "";
  };

  const pageBox = (el) => {
    const r = el.getBoundingClientRect();
    let x = r.x;
    let y = r.y;
    let win = el.ownerDocument.defaultView;
    while (win && win.frameElement) {
      const fr = win.frameElement.getBoundingClientRect();
      x += fr.x;
      y += fr.y;
      win = win.parent;
    }
    return { x, y, width: r.width, height: r.height };
  };

  const isVisible = (el) => {
    const win = el.ownerDocument.defaultView;
    const st = win.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || Number(st.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };

  const controlValue = (el, role) => {
    if (role === "textbox" || role === "combobox") {
      if (el.tagName === "SELECT") {
        const opt = el.options[el.selectedIndex];
        return opt ? opt.text : el.value;
      }
      return el.value || "";
    }
    if (role === "checkbox") return el.checked ? "true" : "false";
    if (role === "text") return norm(el.innerText) || null;
    return null;
  };

  return Array.from(document.querySelectorAll(sel)).map((el) => {
    const role = classify(el);
    const visible = isVisible(el);
    const enabled = !(el.disabled || el.getAttribute("aria-disabled") === "true");
    return {
      role,
      name: accessibleName(el, role),
      value: controlValue(el, role),
      label_text: labelFor(el) || nearby(el) || "",
      enabled,
      visible,
      bbox: pageBox(el),
    };
  });
}
"""

_SHAPE_DIGITS: Final[re.Pattern[str]] = re.compile(r"\d+")
_SHAPE_MONEY: Final[re.Pattern[str]] = re.compile(r"[$£€¥]|,(?=\d)")
_SELECT_PREFIX: Final[re.Pattern[str]] = re.compile(r"^select\s+", re.I)
_NON_SHAPE_ROLES: Final[frozenset[Role]] = frozenset({Role.TEXT, Role.UNKNOWN})

_SETTLE_SECONDS: Final[float] = 5.0
_FRAME_LOAD_MS: Final[int] = 3000
_DEFAULT_WAIT_MS: Final[int] = 1000


class _JsBBox(TypedDict):
    x: float
    y: float
    width: float
    height: float


class _JsElement(TypedDict):
    """Shape of one row returned by _DESCRIBE_JS. Playwright evaluate is Any; we cast into this."""

    role: str
    name: str
    value: str | None
    label_text: str
    enabled: bool
    visible: bool
    bbox: _JsBBox


def _shape_text(name: str) -> str:
    without_money: str = _SHAPE_MONEY.sub("", name)
    without_digits: str = _SHAPE_DIGITS.sub("", without_money)
    return re.sub(r"\s+", " ", without_digits).strip().casefold()


def _shape_control_name(role: Role, name: str) -> str:
    if role is Role.BUTTON and _SELECT_PREFIX.match(name.strip()):
        return "select"
    return _shape_text(name)


def compute_page_signature(elements: list[ElementRef]) -> str:
    # Hash of *chrome*, not *data*.
    #
    # Inputs: heading texts + the set of (role, name) for controls/dialogs.
    # Values are omitted. Digits and currency glyphs are stripped so member
    # ids and balances cannot shift the hash. Role.TEXT is omitted: alerts
    # carry instance copy, and detectors already read text_content.
    #
    # Search-result actions are named "Select <member>". That member is
    # data, so those buttons collapse to the verb "select". Login vs search
    # vs detail still differ — their headings and field names differ. An
    # open <dialog> adds "Host System Message" / OK / Cancel, so an
    # interstitial is a different signature.
    headings: list[str] = []
    pairs: set[tuple[str, str]] = set()
    for el in elements:
        if el.role is Role.HEADING:
            headings.append(_shape_text(el.name))
        if el.role in _NON_SHAPE_ROLES:
            continue
        pairs.add((el.role.value, _shape_control_name(el.role, el.name)))
    headings.sort()
    pair_lines: list[str] = sorted(f"{role}|{name}" for role, name in pairs)
    blob: str = "H:" + ";".join(headings) + "\nP:" + ";".join(pair_lines)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _frame_label(frame: Frame) -> str:
    """Title first, then name. Skip ASP.NET ids — they are not a frame path we want to replay."""
    if frame.parent_frame is None:
        return ""
    try:
        fel: ElementHandle = frame.frame_element()
        title: str = (fel.get_attribute("title") or "").strip()
        name: str = (fel.get_attribute("name") or frame.name or "").strip()
    except PlaywrightError:
        fallback: str = (frame.name or "").strip()
        return fallback or "iframe"
    if title and "$" not in title:
        return title
    if name and "$" not in name:
        return name
    return title or name or "iframe"


def _as_js_elements(raw: object) -> list[_JsElement]:
    if not isinstance(raw, list):
        raise RuntimeError(f"_DESCRIBE_JS must return a list, got {type(raw).__name__}")
    return cast(list[_JsElement], raw)


class WebSurface:
    """Headed Chromium Surface. Implements the Surface protocol structurally."""

    _run_id: str
    _timeout_ms: int
    _evidence_dir: Path
    _obs_n: int
    _last: Observation | None
    _tick_handles: list[ElementHandle]
    _closed: bool
    _pw: Playwright
    _browser: Browser
    _page: Page

    def __init__(
        self,
        run_id: str,
        *,
        evidence_root: str | Path = "evidence",
        timeout_ms: int = 15_000,
    ) -> None:
        self._run_id = run_id
        self._timeout_ms = timeout_ms
        self._evidence_dir = Path(evidence_root) / run_id
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        self._obs_n = 0
        self._last = None
        self._tick_handles = []
        self._closed = False

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=False)
        self._page = self._browser.new_page(viewport={"width": 1280, "height": 800})
        self._page.set_default_timeout(timeout_ms)

    def observe(self) -> Observation:
        self._ensure_open()
        self._settle()
        self._drop_tick()

        elements: list[ElementRef] = []
        handles: list[ElementHandle] = []
        ordinals: dict[tuple[tuple[str, ...], Role], int] = {}

        for frame, path in self._iter_frames():
            try:
                raw_metas: object = frame.evaluate(_DESCRIBE_JS, _CANDIDATES)
                found: list[ElementHandle] = frame.query_selector_all(_CANDIDATES)
            except PlaywrightError:
                continue
            metas: list[_JsElement] = _as_js_elements(raw_metas)
            if len(metas) != len(found):
                raise RuntimeError(
                    f"tick mismatch in frame {path!r}: {len(metas)} metas vs {len(found)} handles"
                )
            for meta, handle in zip(metas, found, strict=True):
                try:
                    role: Role = Role(meta["role"])
                except ValueError:
                    role = Role.UNKNOWN
                if role is Role.UNKNOWN:
                    continue
                if not meta["visible"] and role is not Role.DIALOG:
                    continue
                key: tuple[tuple[str, ...], Role] = (tuple(path), role)
                ordinal: int = ordinals.get(key, 0)
                ordinals[key] = ordinal + 1
                box: _JsBBox = meta["bbox"]
                elements.append(
                    ElementRef(
                        index=len(elements),
                        role=role,
                        name=meta["name"] or "",
                        value=meta["value"],
                        label_text=meta["label_text"] or "",
                        container_path=list(path),
                        ordinal_in_container=ordinal,
                        enabled=bool(meta["enabled"]),
                        visible=bool(meta["visible"]),
                        bbox=BBox(
                            x=float(box["x"]),
                            y=float(box["y"]),
                            width=float(box["width"]),
                            height=float(box["height"]),
                        ),
                    )
                )
                handles.append(handle)

        screenshot_path: Path = self._evidence_dir / f"obs_{self._obs_n}.png"
        self._page.screenshot(path=str(screenshot_path))
        self._obs_n += 1

        observation: Observation = Observation(
            url=self._page.url,
            page_signature=compute_page_signature(elements),
            elements=elements,
            text_content=self._visible_text(),
            screenshot_path=str(screenshot_path),
            dialog_present=self._dialog_present(),
        )
        self._last = observation
        self._tick_handles = handles
        return observation

    def act(self, action: Action) -> ActionResult:
        self._ensure_open()
        try:
            if action.type is ActionType.NAVIGATE:
                return self._act_navigate(action)
            if action.type is ActionType.WAIT:
                return self._act_wait(action)

            resolved: tuple[ElementHandle, ElementRef] | ActionResult = self._resolve_index(
                action.element_index
            )
            if isinstance(resolved, ActionResult):
                return resolved
            handle: ElementHandle
            ref: ElementRef
            handle, ref = resolved
            handle.scroll_into_view_if_needed(timeout=self._timeout_ms)

            if action.type is ActionType.CLICK:
                handle.click(timeout=self._timeout_ms)
                return ActionResult(ok=True)
            if action.type is ActionType.TYPE:
                return self._act_type(handle, ref, action)
            if action.type is ActionType.READ:
                return ActionResult(ok=True, read_value=self._read_handle(handle))
            return ActionResult(ok=False, error=f"unsupported action {action.type}")
        except PlaywrightError as exc:
            return ActionResult(ok=False, error=str(exc))

    def close(self) -> None:
        self._drop_tick()
        if self._closed:
            return
        self._closed = True
        try:
            self._browser.close()
        except PlaywrightError:
            pass
        try:
            self._pw.stop()
        except PlaywrightError:
            pass

    def _act_navigate(self, action: Action) -> ActionResult:
        if not action.url:
            return ActionResult(ok=False, error="navigate requires url")
        self._page.goto(action.url, wait_until="domcontentloaded", timeout=self._timeout_ms)
        self._settle()
        return ActionResult(ok=True)

    def _act_wait(self, action: Action) -> ActionResult:
        ms: int = _DEFAULT_WAIT_MS
        if action.text:
            try:
                ms = int(action.text)
            except ValueError:
                return ActionResult(
                    ok=False,
                    error=f"wait text must be milliseconds, got {action.text!r}",
                )
        self._page.wait_for_timeout(ms)
        return ActionResult(ok=True)

    def _act_type(self, handle: ElementHandle, ref: ElementRef, action: Action) -> ActionResult:
        if action.text is None:
            return ActionResult(ok=False, error="type requires text")
        if ref.role is Role.COMBOBOX:
            try:
                handle.select_option(label=action.text, timeout=self._timeout_ms)
                return ActionResult(ok=True)
            except PlaywrightError:
                pass
        handle.fill(action.text, timeout=self._timeout_ms)
        return ActionResult(ok=True)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("WebSurface is closed")

    def _drop_tick(self) -> None:
        for handle in self._tick_handles:
            try:
                handle.dispose()
            except PlaywrightError:
                pass
        self._tick_handles = []

    def _resolve_index(
        self, index: int | None
    ) -> tuple[ElementHandle, ElementRef] | ActionResult:
        if self._last is None:
            return ActionResult(ok=False, error="no observation; call observe() first")
        if index is None:
            return ActionResult(ok=False, error="element_index is required for this action")
        n: int = len(self._tick_handles)
        if index < 0 or index >= n or index >= len(self._last.elements):
            return ActionResult(
                ok=False,
                error=f"element_index {index} is stale or out of range (n={n})",
            )
        ref: ElementRef = self._last.elements[index]
        if ref.index != index:
            return ActionResult(
                ok=False,
                error="element_index does not match the latest observation",
            )
        return self._tick_handles[index], ref

    def _read_handle(self, handle: ElementHandle) -> str:
        try:
            return handle.input_value(timeout=self._timeout_ms)
        except PlaywrightError:
            pass
        try:
            return (handle.inner_text() or "").strip()
        except PlaywrightError:
            return (handle.text_content() or "").strip() or ""

    def _iter_frames(self) -> Iterator[tuple[Frame, list[str]]]:
        def walk(frame: Frame, path: list[str]) -> Iterator[tuple[Frame, list[str]]]:
            if frame.is_detached():
                return
            yield frame, path
            for child in list(frame.child_frames):
                if child.is_detached():
                    continue
                label: str = _frame_label(child) or "iframe"
                yield from walk(child, path + [label])

        yield from walk(self._page.main_frame, [])

    def _settle(self) -> None:
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        except PlaywrightError:
            pass
        deadline: float = time.monotonic() + _SETTLE_SECONDS
        while time.monotonic() < deadline:
            labels: list[str] = []
            for frame in self._page.frames:
                if frame.is_detached():
                    continue
                labels.append(_frame_label(frame).casefold())
            if "main content" in labels:
                break
            self._page.wait_for_timeout(50)
        for frame in list(self._page.frames):
            if frame.is_detached():
                continue
            try:
                frame.wait_for_load_state("domcontentloaded", timeout=_FRAME_LOAD_MS)
            except PlaywrightError:
                continue

    def _visible_text(self) -> str:
        chunks: list[str] = []
        for frame, _path in self._iter_frames():
            try:
                raw: object = frame.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
            except PlaywrightError:
                continue
            text: str = str(raw or "").strip()
            if text:
                chunks.append(text)
        return "\n".join(chunks)

    def _dialog_present(self) -> bool:
        for frame, _path in self._iter_frames():
            try:
                raw: object = frame.evaluate(
                    "() => document.querySelectorAll('dialog[open]').length"
                )
            except PlaywrightError:
                continue
            if isinstance(raw, int) and raw > 0:
                return True
        return False
