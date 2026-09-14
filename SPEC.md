# SPEC — Computer-Use Automation System

Internal decisions doc. Source of truth for implementation.
Written before code so the trade-offs are explicit and defensible.

## 0. The through-line
The model discovers once. The run is compiled into a typed, versioned
capability artifact. Production replays that artifact with no LLM in the
decision loop. When replay can't safely proceed, a human takes over the
same live session and hands it back.

## 1. Target application — decision: build our own
A local Flask app, `apps/legacy_cu/`, standing in for a credit-union
back office. Chosen over a public demo site because:
- We must be able to *trigger* runtime errors on demand — record-not-found,
  validation failure, permission denial, surprise confirmation dialog,
  session timeout, slow load. Requirement 3.3 is ungradeable without them.
- No ToS risk, no rate limits, no real PII (ground rule 9).
- We can make the surface deliberately hostile: table-based layout,
  nested iframes, no test IDs, ASP.NET-style generated element IDs.
- A second branded copy gives us a "Tenant B" for cross-tenant reuse
  nearly free.

Failure injection via query flags (`?inject=timeout`) so any error path
is reproducible for evidence.

## 2. Stack
Python 3 / Playwright / Anthropic API / Pydantic / FastAPI.
Playwright over Selenium for auto-waiting and frame handling.
Pydantic because the artifact schema and result contract are the graded
artifacts — they should be typed and self-validating, not loose dicts.
Browser runs **headed** throughout: a human must be able to take over
the live window.

## 3. Perception — decision: accessibility tree + screenshot
We do NOT record CSS selectors. Each observation is a screenshot plus a
numbered list of interactive elements with role, accessible name, nearby
label text, frame path, and bounding box. The model selects by index.

Rationale: an accessibility tree exists on Windows (UI Automation) and
macOS (AX API) as well as in the browser. Choosing it as the perception
layer means the `Surface` abstraction extends to desktop apps without
redesign — the answer to §3.7 falls out of this one decision rather than
being bolted on.

## 4. Element targeting on replay — multi-signal descriptors
Each step stores a descriptor, not a selector:
role, accessible name, label text, frame path, ordinal within container,
and fallback hints (nearby text, last-known position).

On replay we score every candidate against the descriptor.
- High confidence → act.
- Ambiguous or no match → do NOT guess. Escalate.
A wrong click in a banking back office is worse than a halt.

## 5. Error taxonomy — three classes, deliberately separate
- **BusinessOutcome** — a legitimate answer the caller needs
  ("no such member"). Returns cleanly. NOT a failure.
- **Recoverable** — dismiss a known interstitial, wait and retry a
  transient load. Bounded attempts, then escalate.
- **HardFailure** — stop, surface step index, expected vs. observed,
  and a screenshot path.

Detectors run after *every* step, not just at the end. Conflating
business outcomes with failures is the mistake the brief calls out
by name in its glossary.

## 6. Escalation & control transfer
A single `SessionLock` with `owner ∈ {automation, human}`, checked inside
every act. On stuck: flip owner to human, emit an InterventionRequest
carrying capability, step index, screenshot, and reason; expose the
already-open browser window for manual work; record the human's actions
into the evidence log. On resume: flip back, re-observe fresh state,
continue from the next step. Never assume the page is where we left it.

Mocked deliberately: the operator console is a minimal FastAPI page.
Real co-browsing is explicitly out of scope per §3.6.

## 7. Safety
One chokepoint inside `Surface.act()` — no path to the browser bypasses
it. Checks: URL against allowlist, action type against permitted set,
risk class of the step. Risky/irreversible actions (submit, transfer,
delete) are blocked pending confirmation rather than flagged after the
fact.

Redaction runs on all text before it reaches logs, artifacts, or the
model prompt — not only on write-out. The prompt is a leak path too.

## 8. Architecture
Single process, synchronous, files on disk for artifacts and evidence.
No queue, no database, no service split. §7 of the brief explicitly says
building scaling infrastructure is not rewarded; designing abstractions
that *could* scale is.

## 9. Cut deliberately
Desktop surface (seam defined, not implemented).
Multi-tenant plumbing (schema supports overrides; no tenant registry).
Real operator console.
Auth/session management beyond a mock login.