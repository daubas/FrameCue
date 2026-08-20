# Subtitle Workspace 重構規格 Cross-Model Review

Status: complete in degraded two-model mode; all verified findings incorporated with user approval
Review mode: plan
Frozen target: `docs/subtitle-workspace-agenticdub-refactor-plan.md`
Target SHA-256: `44d7840685e8e3c9da46997969ae42995fff628895723cd9fa3f3237983e8ba4`
Incorporated specification SHA-256: `adaf776eda38e492ecf1491da6fac93d1de523b6298726f5487fe1ae77aef2e1`

## Reviewers

| Reviewer | Requested | Reported | Result |
|---|---|---|---|
| Sol | current main-thread Sol | GPT-5.6 Sol | completed first pass and adjudication |
| Claude | `opus`, xhigh | Opus 5 banner | unavailable before inference: 5-hour session limit exhausted |
| Grok | `cliproxy-grok` route | `grok-4.6` | completed, read-only, web disabled |

Claude was stopped without fallback. The final result is therefore a degraded
Sol + Grok review.

## Disposition

The user approved writing every verified finding back to the specification on
2026-08-20. No product code was changed.

| Finding root cause | Incorporated specification sections |
|---|---|
| Partial range acceptance and structural recomposition | 5, 13 |
| Audiovisual wording bypassed the content gate | 2, 4, 9, 17 |
| Portable import trust boundary | 2, 13, 14, 18, 20 |
| Operation-specific allowed fields and evidence | 13, 17 |
| Pending-stage draft mutability | 4, 8, 17 |
| `rewrite_required` transition | 4, 14, 15, 17 |
| Cue/Block/display/speech authority | 7, 12, 13 |
| Dirty-state, multi-Cue, lock, and lead atomicity | 6, 7, 11, 17 |
| Work Order claim, retry, and recovery | 3, 12, 14, 20 |
| Protected-term data at the auto-apply seam | 8, 12, 13 |
| Mechanical auto-apply Undo window | 6, 8 |
| Review render versus publication render | 3, 9, 13, 17 |
| Acceptance ordering and proof matrix | 2, 16, 17, 18 |

## Verified findings

### P0 — Consensus: partial acceptance is not representable safely

Sections 5, 6, 9, and 13 require independently accepted ranges, structural
Cue replacement, and a full-document Candidate, but do not define immutable
range IDs, range-local replacement payloads, dependencies, or recomposition
(plan lines 147-150, 181-185, 258-267, 385-407).

Impact: accepting one range can apply an unaccepted change, lose a split/merge,
or overwrite a newer draft.

Minimum fix: define independently checksum-bound change proposals with stable
range identity, authorized old lineage, allowed fields/operations, replacement
slice, dependencies, and deterministic recomposition into a new full snapshot.

### P0 — Grok-only verified: audiovisual text changes bypass the content gate

Audiovisual issues include translation, and Chinese edits go directly to a
scoped audiovisual correction (lines 245-264). The state flow never returns
them through content agent review and semantic human confirmation (lines
102-123, 213-241).

Impact: formal TTS can run from an unresolved content decision, violating the
core gate.

Minimum fix: route audiovisual `display_text`, `speech_text`, and translation
changes through `content_correction_review`; only approved wording may enter a
TTS-bearing operation.

### P0 — Consensus: portable import has no complete trust boundary

The snapshot includes complete state, open Work Orders, schemas, and included
or referenced assets (lines 409-414), while durable state also contains agent
credentials and lead identity (lines 341-352). Import rules do not exclude live
credentials/sessions, constrain paths, revalidate against host schemas, or mint
new identity.

Impact: import can revive authority, trust snapshot-supplied validation, collide
with an existing Workspace, or reference assets outside the portable package.

Minimum fix: use host schemas only; exclude credentials, browser sessions,
presence, locks, and lead authority; require relative contained assets and
rehash them; mint new Workspace/request identity while preserving source
lineage; revalidate every revision and Work Order before commit.

### P1 — Consensus: Candidate evidence is not operation-specific

The plan defines content correction, voice realization, and audiovisual
correction (lines 385-396), but applies one generic missing-evidence and
allowed-field rejection rule (lines 398-407).

Impact: a text-only Candidate may be required to provide impossible audio
evidence, or a voice Candidate may pass without all alignment/timing evidence.

Minimum fix: publish an allowed-field and required-evidence matrix per operation
and range. Content correction needs document/lineage evidence; voice operations
require checksum-bound audio, alignment, timing audit, and review media.

### P1 — Consensus: completed-round mutability is undefined

Agent-pending stages exist (lines 102-123), Candidates are checksum-bound
(lines 45-46), and agent results wait on Cue locks (line 326), but the plan does
not say whether humans can continue editing while a Work Order is pending or a
Candidate awaits decisions.

Impact: continued edits make Candidates stale or mix two review rounds.

Minimum fix: freeze the completed-round snapshot and its authorized ranges.
Pending stages are read-only except explicit Candidate decisions; later edits
start a new draft. Recheck before-values when applying, not only on submission.

### P1 — Grok-only verified: `rewrite_required` has no legal transition

Timing fit can return `rewrite_required` (lines 433-442), but after Content
Revision the state machine has only voice realization and audiovisual review
(lines 102-128).

Impact: retrying cannot fix unfittable wording, while treating it as a generic
failure can stall forever or tempt hard trimming.

Minimum fix: make `rewrite_required` a non-retryable Work Order outcome that
reopens a content-decision range and blocks Voice-Aligned Revision until a new
approved wording revision exists.

### P1 — Consensus: Cue, Block, display, and speech ownership is incomplete

The plan edits Cue `display_text` and `speech_text` and permits split/merge
(lines 152-203), but Document v2 does not define Block fields, Block membership
updates, TTS source text, the action that deliberately separates speech text,
or the invariant tying approved meaning to display and speech (lines 373-383).

Impact: Block meaning/audio can become stale and AgenticDub cannot know which
text is authoritative for TTS.

Minimum fix: freeze Document v2 Block fields and derivation rules, add an
explicit speech-separation action, define the normalized content invariant, and
state the exact TTS source.

### P1 — Consensus: synchronization and ownership changes are not atomic enough

Completion depends on client-unsynchronized state (lines 135-145, 333-336),
while edits debounce locally, locks/presence are ephemeral, search/replace spans
many Cues, and lead takeover is allowed (lines 187-211, 312-339).

Impact: the server cannot safely complete from an unadvertised dirty buffer;
search/replace may partially cross locks; two clients may race to become lead.

Minimum fix: define server-visible dirty acknowledgements for connected clients,
all-or-nothing multi-Cue operations, compare-and-swap lead transfer, lock expiry,
and the loss semantics for never-acknowledged disconnected input.

### P1 — Grok-only verified: Work Order claim, retry, and recovery are absent

The Workspace interface only obtains/submits orders (lines 92-100, 416-420),
while the skill owns failed/interrupted recovery and rollback relies on export
(lines 78-83, 584-590).

Impact: two workers may process one pending order, repeated correction rounds
may collide, and a failed post-cutover task has no defined in-place recovery.

Minimum fix: define atomic claim, fail, retry, cancel, and lease expiry; retries
retain the base checksum but receive new request identity. Separate validated
in-place restore from import-as-copy.

### P1 — Grok-only verified: protected-term data is missing at the auto-apply seam

AgenticDub owns protected-term policy (lines 68-74), but FrameCue must prevent
protected-term auto-apply without receiving a protected-term snapshot (lines
213-233, 373-390).

Impact: FrameCue cannot deterministically distinguish a mechanical replacement
from a protected terminology change.

Minimum fix: bind a protected-term/glossary snapshot and checksum into the
document and content-review Work Order; auto-apply classification uses only that
snapshot.

### P1 — Grok-only verified: automatic revision removes the promised undo window

Mechanical changes remain undoable until revision creation, but FrameCue
immediately creates a Content Revision when no semantic decision remains
(lines 235-241).

Impact: the normal mechanical-only result has effectively no human-visible Undo
window.

Minimum fix: keep an explicit lead confirmation state after mechanical
auto-apply, or remove the undo promise. The approved product decision favors the
confirmation state.

### P1 — Sol-only verified: review render and publication render are conflated

AgenticDub owns full rendered-review media and a final render after approval
(lines 68-76, 256-259, 563-564), but the relationship between them is not bound.

Impact: the reviewer may approve a preview while publication uses materially
different pixels/audio.

Minimum fix: distinguish the Candidate review render from publication output
and require publication to derive from the approved revision and asset hashes;
material differences re-enter audiovisual review.

### P1 — Consensus: the acceptance gate is ordered impossibly

The plan says every criterion passes before the user receives the workflow
(lines 530-538), while one real active HITL task and human browser use are the
final gate (lines 540-569). It also asks one task to demonstrate browser matrix,
legacy workflows, snapshot copy, and downstream production.

Impact: the workflow cannot be both withheld and human-accepted, and one run
cannot prove unrelated compatibility checks.

Minimum fix: separate automated contract/security tests, browser matrix, legacy
regression, and one real HITL UAT. Deliver one implementation-complete acceptance
build to the reviewer; release/cutover only after UAT passes.

## Discarded or merged claims

- Grok suggested removing search/replace, phone read-only, recent Workspace,
  lead takeover, and the separate shell as optional scope. These are explicit
  approved product decisions, so they are not review defects.
- Generic claims that ownership was absent were discarded; section 3 already
  defines it. Specific ownership gaps are retained above.
- Repeated state, range, evidence, import, and acceptance claims were merged by
  root cause.

## Open questions and residual risks

- Exact mechanical auto-apply normalization must be frozen as deterministic
  rules before implementation.
- The protected-term snapshot format should reuse the existing AgenticDub
  glossary artifact rather than introduce a second vocabulary schema.
- The smallest real HITL acceptance task must still exercise at least one
  structural edit, one semantic Candidate decision, and one audiovisual
  correction; synthetic tests cover the remaining failure matrix.
- Claude remains unreviewed until its session allowance resets. Rerunning Claude
  later would be an additional review, not part of this frozen two-model result.
