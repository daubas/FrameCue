# FrameCue v2 Refactor - Implementation Note

Status: v2 static runtime complete at `v2.6.0`; Inline Cue Subtitle Workspace milestone implemented and locally validated; release acceptance pending
Last updated: 2026-08-21
Owner: FrameCue
Related handoff: `AgenticDub/docs/architecture/framecue-v2-agenticdub-handoff.md`

## Purpose

This is the canonical implementation note for the FrameCue v2 refactor. It records the agreed product boundary, contracts, rollout order, validation gates, and implementation feedback. Update this document as implementation or user feedback changes the plan.

The released FrameCue v2 runtime has one job: provide a portable, static
human-review gate for media-aligned content. The Subtitle Workspace extension
adds local revision, collaboration, and agent handoff state without moving
subtitle generation, translation, speech synthesis, or rendering into
FrameCue.

## 2026-08-20 — Subtitle Workspace extension

The v2 static package/result contract remains immutable and portable. The new
subtitle Workspace adds a same-machine SQLite/CLI path around that contract so
the reviewer no longer has to download a result before the agent continues.
FrameCue still does not run TTS: it owns revisions and Work Orders; AgenticDub
returns a checksum-bound Candidate with audio/alignment/timing evidence.

The implemented milestone now provides:

- a SQLite-backed complete Subtitle Document v2 draft with compare-and-swap
  edit, Cue split/merge, Block split/merge, and category-plus-note Agent marking;
- reverse approval that creates an immutable Content Revision directly for a
  clean round, or one frozen content-correction Work Order for edited and
  flagged ranges;
- fail-closed Content Candidate v2 validation, proposal-level accept/reject,
  stale-checksum protection, and recomposition from the frozen base document;
- a separate Subtitle Workspace UI with sessions, presence, lead-only round
  completion, Cue locks, dirty-state gates, autosave, and server-authoritative
  reload notifications;
- scoped agent bearer tokens and leased Work Order
  list/read/claim/submit/fail/retry operations. Only token hashes are stored.

AgenticDub currently validates Work Order v2 and produces a dry-run realization
plan. Real TTS/alignment/render execution, Voice Candidate v2, audiovisual
exception review, portable Workspace export/import and recovery, and final UAT
remain pending. No production workflow has cut over to this Workspace. The
current contract and rollout order live in
[`subtitle-workspace-agenticdub-refactor-plan.md`](subtitle-workspace-agenticdub-refactor-plan.md).

## 2026-08-21 — Inline Cue Subtitle Workspace milestone contract (approved)

This is the implemented contract for the current Subtitle Workspace UI pass.
It applies to subtitle `content_review`; static v2,
redraw, boundary, Markdown, and HyperFrames workflows keep their existing
workflow-specific media surfaces.

### Primary surface

- Every Cue is one visible row/card and the row is the only primary editing
  surface. The reviewer edits `display_text` inline in that row; there is no
  persistent separate editor column for Subtitle Workspace content review.
- The row owns the direct interactions: plain `Enter` at a valid interior caret
  splits the Cue and focuses the new right Cue; `Ctrl/Cmd+Enter` inserts a
  newline inside the same Cue; `Backspace`/`Delete` merges at a Cue boundary;
  and `M` toggles the quick Agent mark. IME-composition Enter is ignored, and a
  split may not create an empty edge. The row's `更多操作` menu is the shared
  fallback for Cue and Block structural operations; disabled actions explain
  the stage, lock, adjacency, or invariant reason.
- Semantic Blocks remain low-interference grouping: a subtle rail/container and
  header around Cue rows showing Block ID, Cue count, timing state, and review
  state. Block is not a second review mode or a competing editor. Cue merge may
  cross one adjacent Block boundary; the separate Block merge keeps every Cue
  row, while Block split creates a boundary before the selected non-first Cue
  without splitting or deleting Cues. The left rail is the primary Block
  interaction: `−` at an internal Cue boundary splits there, while `+` on an
  existing Block boundary merges the adjacent Blocks. UI labels Blocks by
  document order (`Block 01`, `Block 02`) while retaining opaque IDs in data;
  rail handles stay hidden until their hit area is hovered or focused.
- Each Cue row keeps one compact metadata line for Cue number, source time,
  Block ID, and the real Agent-mark state.
- While a row is being edited, playback follow/playhead-follow pauses. The
  reviewer can resume or seek deliberately after the edit; editing must not
  move the active row underneath the caret.

### Agent marking and approval

- This milestone attaches the existing `needs modification` flag to the Cue
  row. The explicit control records a category plus an optional note; `M` is
  the quick `other`-category toggle. A marked row visibly reads
  `待 Agent 修改`. The backend has no per-Cue Agent processing-status contract,
  so the UI does not claim that a Cue is queued or processing.
- A closed-by-default `交給 Agent` disclosure shares the compact `正在編輯`
  bar. Expanding it reveals category, optional note, mark/update, and cancel;
  marked state remains visible in the closed summary. `更多操作` is the second
  closed disclosure on that same line, and opening either closes the other.
- Mark-first remains the flow: FrameCue batches the marked ranges and direct
  edits when the lead completes the round. It does not invoke an Agent for each
  keystroke or each mark. The UI reuses the existing Work Order v2 operations
  and states; it does not add a new Agent execution capability.
- Reverse approval is unchanged: only marked or directly edited exceptions are
  sent for correction; unmarked Cues are approved when the round completes.

### Media and global controls

- In an ordinary video Subtitle Workspace, the video is the Cue context. Do not
  show a Cue still/video toggle or a second Cue image surface; clicking a row
  seeks the video and the paused frame is its visual context.
- No-video packages and special still/redraw/boundary/HyperFrames workflows
  retain the media/still modes required by those workflows.
- Playback remains in the media surface; complete-round and sync/reviewer state
  remain global. Search/replace and technical metadata stay outside the Cue row
  and are not expanded in this milestone.
- The compressed overview timeline is hidden during `content_review`, where
  timing is read-only and duplicates video/Cue timestamps. It remains available
  for later audiovisual stages where source/output timing differences matter.

### Explicitly deferred from this milestone

- speculative immediate Agent execution per Cue mark;
- per-Cue Agent processing-status backend/UI and a full Work Order composer in
  the browser; and
- server-owned Undo/Redo (native text undo before synchronization remains
  available).

### Done when

The implementation is complete only when every Cue is edited in-place as one
row/card, plain Enter splits and focuses the right Cue, Ctrl/Cmd+Enter keeps a
newline in the same Cue, Cue/Block merge and Block split preserve their stated
boundaries, mark/fallback actions work in the row, Block grouping stays
secondary, follow-play pauses during editing, ordinary video has no Cue
still/video toggle, existing special media modes still work, Agent handoff is
represented by the category-plus-note `待 Agent 修改` marker and batched at
round completion, global controls remain available, and reverse approval still
accepts unmarked content. The deferred items above must remain absent from this
milestone.

## Success Criteria

FrameCue v2 is ready to release only when:

- one FrameCue source builds every supported review bundle;
- every bundle is self-contained and can be served as static files;
- package and result files validate against versioned schemas;
- an exported result is a complete, checksum-bound snapshot;
- old v1 packages continue to work with their bundled v1 viewer;
- all four v2 fixtures and two real pilots pass browser review and result round-trip checks;
- AgenticDub can pin a FrameCue tag instead of copying viewer code.

## Why Refactor

The main problem is source-of-truth drift, not the raw number of files.

- Three viewer variants have diverged: this repository's `viewer.html`, AgenticDub's `scripts/subtitle_review_viewer.html`, and the global skill asset under `~/.codex/skills/framecue/assets/`.
- `framecue.py` and AgenticDub's `scripts/build_subtitle_review.py` overlap, but emit different contracts.
- The current `framecue.py` is behind the package used by AgenticDub: it lacks current fields such as `speech_text`, subtitle policy, and automatic semantic blocks.
- Generated review folders copy editable `index.html` files, so old packages silently become new viewer forks.
- The current viewer keeps most state and UI work in one script; `go()` updates navigation, editors, audio, lists, blocks, and player state together.
- There is no automated browser test suite.
- Actual use now includes subtitle, redraw, boundary, and HyperFrames review, beyond the original single-cue viewer.

Observed review data supports a deliberately small action model. In a 487-cue edited package, all three prompt notes asked for grouping, merging, or same-paragraph treatment. Two block-decision files contained 268 decisions without free-form action types. Retime appeared as a project-wide workflow rather than a recurring per-cue taxonomy.

## Product Boundary

| Owner | Responsibilities |
|---|---|
| FrameCue | Versioned package/result schemas, static viewer, immutable review revisions, Workspace draft/collaboration state, Work Orders and Candidate decisions, media review, approval, export, validation, and migration tooling |
| AgenticDub | STT, translation, glossary, punctuation policy, pipeline-side Cue/Block generation and re-realization, `speech_text`, pronunciation-risk generation, TTS, TTS/STT audit, and applying approved results |
| Global FrameCue skill | Invoke the pinned FrameCue CLI to build, serve, validate, and collect packages; record implementation feedback |
| HyperFrames project | Composition, project media, narration audio, and project-specific player configuration |

FrameCue never calls an LLM, STT service, TTS service, or renderer. It records reviewer edits and follow-up instructions. The upstream project produces a new immutable revision when regeneration is needed.

## Settled Decisions

### Runtime and source ownership

- The FrameCue repository is the only editable source for the viewer, schemas, and generic HyperFrames adapter.
- v2 uses Svelte with Vite and emits portable static files.
- The static v2 runtime does not use SvelteKit, a router, a state library, a UI
  kit, a backend, accounts, or a database. Subtitle Workspace is a separate
  local SQLite/HTTP extension and does not change portable bundle behavior.
- The global skill and downstream projects pin a Git tag. They do not follow `main`.
- The package records the viewer version used to build it.

### Review contracts

- All v2 workflows use one `framecue_package_v2` contract. Workflow is package metadata, not a separate viewer implementation.
- The input package is immutable.
- The formal output is one `framecue_review_result_v1` complete snapshot. Sparse patches and change lists are derived exports, never the truth source.
- A stable `review_id` identifies the review across immutable revisions such as r1, r2, and r3.
- Each revision records lineage to the previous package checksum and, when available, its approved result.
- Browser drafts are convenience state only. They are isolated by `review_id`, revision, and content checksum.
- Only an exported, approved result with matching identity and checksum may cross the HITL gate.

Exact JSON property names and validation constraints are frozen when the schemas land in Phase 1. The required contract information is:

| Contract area | Required information |
|---|---|
| Identity | schema version, review ID, revision, content checksum, viewer version |
| Workflow | review kind and upstream provenance |
| Media | bundled relative asset paths and dimensions/duration where relevant |
| Content | cues, optional semantic blocks, scenes, and subtitle policy |
| Review aids | pronunciation risks, original text, cue audio, redraw trace, or player config when applicable |
| Lineage | previous package checksum and previous approved result reference when applicable |

The result snapshot repeats the package identity, records final approval state, and contains the complete reviewed blocks, cues, and follow-up actions. A checksum mismatch is a hard rejection, not a warning.

### Block-owned semantics

- When semantic blocks exist, blocks own meaning and speech text.
- AgenticDub projects approved block content into display cues.
- Final approval requires each block's normalized `target_text`, child Cue display text, and `speech_text` to contain the same content; punctuation may differ.
- Editing or replacing a Cue invalidates its parent block approval. The CLI repeats the invariant check when collecting an approved result.
- Cues review display wording and visual segmentation; cue timing remains read-only in FrameCue v2.
- Editing a block invalidates that block's approval and final package approval. Any cues derived from the old block are stale and must be regenerated in a new revision.
- During `content_review`, an adjacent cross-Block merge is allowed as one atomic
  operation: both affected Semantic Blocks are merged/recomposed and their
  Cue/Block/`speech_text` invariants, checksums, and lineage are validated and
  recorded together.
- Block merge is a distinct operation that joins adjacent Blocks while retaining
  all Cue rows. Block split creates two Blocks before the selected non-first Cue
  while retaining every Cue and its order.
- After voice alignment, a cross-Block merge invalidates scoped voice evidence
  or remains gated by the current stage; stale evidence is never reused.
- Cue review state is browser-draft workflow state, not a new result-contract approval type. The formal result remains block approval followed by final package approval.
- The reviewer works Cue-first. Once every child Cue has been reviewed and the content invariant passes, FrameCue approves the parent block automatically.
- Packages without blocks use cues as the review source directly.
- Any edit after final approval invalidates final approval.

### Follow-up actions

The context is the currently selected block or cue; v2 does not add a separate target picker. The initial action menu has exactly four choices:

| UI choice | Meaning |
|---|---|
| Use edit | The reviewed text can be used directly; this is the default |
| Rewrite content | Upstream should rewrite or retranslate the selected content |
| Resegment | Upstream should merge, split, or regroup the selected content |
| Retime | Upstream should regenerate alignment outside FrameCue |

Free-form instructions remain available beside the action. FrameCue records intent; AgenticDub decides how to execute it.

### Approval and pronunciation

- Pronunciation risks are supplied by AgenticDub.
- FrameCue highlights, filters, and plays the supplied audio for those risks.
- A risk is a warning, not a separate approval object.
- Final pronunciation correctness is established by AgenticDub's post-TTS STT audit.
- AgenticDub must not start TTS from an unapproved or checksum-mismatched result.

## Supported v2 Workflows

The first v2 release supports exactly these current cases:

1. Cue and semantic-block subtitle review.
2. Redraw before/after review with generation trace.
3. Subtitle-boundary review.
4. HyperFrames playback review.

A fifth workflow waits until a real package cannot be represented by the common contract.

## UI Structure

All workflows use the same four areas:

1. **Top toolbar**: package selector, progress, final approval, and export.
2. **Media Stage**: still image, redraw comparison, boundary context, or HyperFrames playback.
3. **Review Workbench**: Cue-first list and editor. Subtitle Workspace content
   review follows the Inline Cue milestone contract below: one inline-editable
   Cue row/card is the primary surface, with compact interactive Block grouping
   around it. Risk and All are filters, not competing review modes.
4. **Details**: collapsible provenance, trace, policy, and advanced metadata.

Controls that require an upstream LLM or pipeline rerun must include an inline explanation marker. Explanations must remain visible without covering the media stage.

## HyperFrames Adapter

FrameCue owns one generic, versioned HyperFrames review-player adapter. A project supplies only its composition, audio, assets, and configuration.

- The adapter and all browser-loaded assets are copied into the v2 bundle.
- Communication remains same-origin and time-based.
- FrameCue owns review state; HyperFrames owns playable visuals and the full narration clock.
- Project-specific player forks are not supported after cutover.

The current protocol and pilot findings remain documented in [hyperframes-review-implementation-note.md](hyperframes-review-implementation-note.md). v2 may preserve that protocol while replacing the viewer implementation.

## Portable Bundle Rules

- Every browser-loaded path is relative and resolves inside the bundle.
- Absolute source paths may appear only as non-loadable provenance metadata.
- Only assets needed for the selected review are copied.
- A generated bundle contains immutable viewer assets, package data, relevant media, and optional adapter assets.
- Generated directories are build artifacts, never editable viewer sources.

## Minimal Source Layout

The implementation should begin with the fewest source areas that express the fixed boundary:

```text
FrameCue/
  src/
    App.svelte
    main.js
    app.css
  adapters/
    hyperframes-player.html
  schemas/
    framecue-package-v2.schema.json
    framecue-review-result-v1.schema.json
  tests/
    fixtures/
  dist/                     # generated release artifact
  framecue.py               # build, validate, collect, migrate entry point
```

Do not split `App.svelte` into components until a repeated or independently testable UI boundary makes the split smaller. Do not add a second application entry point for a workflow.

## Compatibility and Migration

- Existing v1 packages are frozen with their bundled viewer and remain usable as-is.
- The v2 viewer supports v2 packages only.
- A v1 package is upgraded only by an explicit CLI migration that creates a new directory and v2 review revision.
- Before migration, any browser-only v1 draft must be exported or explicitly retired.
- Migration never overwrites the v1 package.
- Duplicate viewers and the AgenticDub project-local FrameCue skill are removed only after v2 pilots pass and a release tag is pinned.

## Implementation Plan

### Phase 0 - Freeze and inventory

- Mark current viewer/package behavior as v1.
- Preserve representative packages and export any browser-only pilot drafts.
- Record the three viewer forks and two builder paths that must be retired.

Exit gate: four reproducible fixture inputs exist and no required review state lives only in localStorage.

### Phase 1 - Contracts first

- Add package and result JSON schemas.
- Add checksum, review identity, immutable revision, lineage, and approval validation.
- Create one fixture for each supported workflow.
- Add CLI validation for fixtures and exported results.

Exit gate: invalid identity, checksum, revision, asset paths, and approval states fail deterministically.

### Phase 2 - Core Svelte viewer

- Implement package loading, Block/Cue workbench, editing, risk filtering, action notes, draft isolation, final approval, and complete-snapshot export.
- Implement the fixed four-area UI and keyboard navigation.
- Keep cue timing read-only.

Exit gate: the subtitle fixture completes an edit, reload, approval, export, and validation round trip without losing state.

### Phase 3 - Media workflows

- Add redraw comparison and trace details.
- Add subtitle-boundary context.
- Port the generic HyperFrames adapter and playback arbitration.

Exit gate: all four fixtures pass browser interaction checks at desktop and mobile sizes; media, text, controls, and explanation markers do not overlap.

### Phase 4 - Portable CLI bundles

- Build immutable static viewer assets.
- Make the CLI copy only required relative assets into a self-contained review directory.
- Add explicit v1-to-v2 migration and result collection.

Exit gate: each fixture works from a newly generated directory served by a plain static server, with no source-tree dependency.

### Phase 5 - Parallel pilots

- Run one real AgenticDub subtitle review while retaining its v1 package.
- Run the current HyperFrames project through the same v2 viewer while retaining its existing candidate.
- Apply each approved result back to its upstream project and verify the produced next revision.

Exit gate: both result consumers reject stale checksums and correctly apply a valid complete snapshot.

### Phase 6 - Release and cutover

- Tag the validated FrameCue v2 release.
- Pin that tag in the global skill and AgenticDub integration.
- Stop copying viewer HTML into AgenticDub packages.
- Remove downstream viewer forks and the project-local FrameCue skill only after equivalent output is verified.

Exit gate: a clean checkout plus pinned FrameCue release can reproduce, serve, validate, and collect both pilots.

## Validation Gates

Each implementation phase must leave the smallest runnable check that proves its new behavior. Before release, validation includes:

- schema-positive and schema-negative fixture checks;
- checksum, revision, lineage, and approval rejection checks;
- browser smoke tests for keyboard controls, draft restore, package switching, export, and all four media workflows;
- desktop and mobile screenshots for clipping and overlap;
- result round trips through both real upstream consumers;
- a clean-build test proving that no global skill asset or source-tree absolute path is required.

## Non-goals

- Subtitle generation, translation, cue timing, TTS, STT audit, or video rendering.
- Hosted accounts, remote multi-tenant persistence, or comments. Subtitle
  Workspace collaboration uses short-lived local sessions and SQLite state.
- A general nonlinear editor or timeline editor.
- For the current Inline Cue Subtitle Workspace milestone: speculative
  per-mark Agent execution, a full browser Work Order composer, and
  server-owned Undo/Redo.
- Supporting arbitrary plugin-defined workflows in v2.
- Automatic v1 compatibility inside the v2 viewer.

### Review-time Agent suggestions (2026-08-24)

`交給 Agent` now evolves from a saved flag into an asynchronous suggestion
request. FrameCue owns the queued job, draft/checksum binding, reviewer-visible
status, and explicit apply/ignore decision. It still does not call an LLM or
allow an agent to overwrite subtitle truth.

AgenticDub owns the worker and invokes the installed Codex CLI as an ephemeral,
read-only structured-output subprocess. The worker uses scoped FrameCue HTTP
claim/submit/fail endpoints rather than SQLite. A returned suggestion is bound
to its frozen Cue context; a changed draft makes it stale instead of applying
against newer text.

The Cue row is the complete interaction surface on desktop, phone, and iPad:
queued/processing/ready/stale/failed status, before/after text, and accessible
apply/ignore controls appear inline. No separate Agent panel, direct database
access, persistent Codex session, automatic apply, or new UI dependency is
introduced in this milestone.

Implemented result: the suggestion lifecycle uses the existing agent bearer
permissions, five-minute lease, draft CAS, and SSE invalidation. Cancel/reflag
keeps historical rows for audit while exposing only the newest unresolved job
per Cue/category. Phone review actions remain available even though direct text
and structural editing stay read-only. Full Node/Python/build validation and a
disposable real-Codex end-to-end run passed.

Peter production review exposed a preservation gap: a completed suggestion is
stored separately from the draft until the reviewer applies it, so a Work Order
backup preserves applied text but not a ready proposal. A later merge changed
the proposal's frozen Block context and silently presented it as stale.
Structural draft operations now fail with HTTP 409 when they would stale a
currently ready suggestion; queued and processing work remains non-blocking.
Direct human text edits still take precedence and may stale an older proposal.

## Main Risks

| Risk | Control |
|---|---|
| Contract changes while both repositories implement against it | Finish and tag FrameCue v2 before AgenticDub integration code starts |
| Browser drafts become inaccessible after path/origin changes | Export or retire drafts before migration; isolate new drafts by identity and checksum |
| Svelte refactor reproduces current UI coupling | Keep state transitions explicit and test round trips before adding media adapters |
| Generated bundles become new source forks | Treat `dist` and review directories as immutable build artifacts |
| Block and Cue edits conflict | Keep semantic ownership in Blocks; atomically recompose adjacent cross-Block merges and validate projections, checksums, and lineage |

## Decision and Feedback Log

### 2026-07-24

- Audited FrameCue, AgenticDub, the global skill, and the HyperFrames pilot.
- Completed architecture grilling and confirmed the boundaries and decisions recorded above.
- Chose Svelte plus Vite for v2, with no application framework or state/UI libraries.
- Limited v2 to four observed review workflows and four follow-up actions.
- Confirmed FrameCue-first implementation order and prepared the bounded AgenticDub handoff.
- Implemented the Svelte/Vite static viewer, shared package/result schemas, checksum-gated CLI, explicit v1 migration, legacy v1 archive, and generic HyperFrames adapter.
- Added eight focused Python checks covering all four workflows, immutable checksum validation, complete approved snapshots, no-block cue `speech_text`, manifest assembly, and v1 migration.
- Built and validated a real AgenticDub OpenClaw bundle and a real HyperFrames bundle. AgenticDub now requires the exact `v2.0.0` FrameCue tag before building or consuming a v2 result.
- The local browser runtime was unavailable for interactive screenshots. Static builds, bundled asset checks, byte-range checks, CLI validation, and cross-project result validation passed; visual browser QA remains the only follow-up release check.

### 2026-07-29

- Added the `v2.1.0` consistency gate after production review found that independently editable Cue and Block text could authorize stale TTS wording.
- Cue edits now invalidate the parent block, browser approval checks normalized Cue/Block/Speech content, and the CLI repeats that validation at collection time.
- Added one native Node state-transition test and one Python approved-result regression test; no new runtime dependency was introduced.

### 2026-07-30 - AgenticDub production pilot

- Released `v2.3.0`: Traditional Chinese controls, source-video playback that follows the active Cue and Block, representative Cue imagery, bilingual subtitle presentation, and editor-focus playback pause. The release remains a static portable viewer with no new runtime dependency.
- Completed the Jensen Startup School production pilot at immutable revision `r12`: 414 Cues, 232 semantic blocks, an approved complete result, and an AgenticDub output that passed the two-provider STT cross-audit for all 232 blocks.
- The pilot confirmed that post-approval TTS pronunciation guidance is not subtitle content. When that guidance changes, AgenticDub creates a new immutable package and records an inherited-content approval only after asserting Cue, Block, Scene, Media, Risk, and subtitle-policy payloads are unchanged from the approved revision.
- The final QC video is a separate human gate. FrameCue approval authorizes reviewed subtitle content; it does not silently authorize publication. The r12 QC artifact is available through the read-only FBR handoff and remains `final_qc_pending`.
- Production timing did not clip speech: maximum fitted speed was `1.0714x`, the voice ended before the original-video fade, and visual checks confirmed video and bilingual captions after late Cue `c0359` and at the ending.

### 2026-08-06 - Cue-first review

- Removed Block as a competing review mode. The workbench now reviews Cues directly, while semantic blocks remain the meaning, speech, validation, and formal approval boundary.

### 2026-08-07 - Safe range resegmentation

- Added Shift-click Cue range resegmentation without changing the package/result schemas.
- Reset stale one-Cue anchors after keyboard or search navigation and require confirmation when a range exceeds 12 Cues or 60 seconds.
- Space marks the current Cue reviewed and advances. A valid parent block is approved automatically after all of its child Cues have been reviewed; editing a Cue resets that Cue and invalidates the parent and final approval.
- Block text, speech text, actions, and notes remain available as progressive disclosure under the selected Cue. Content mismatches open that context automatically.
- Fixed the local server root to open `index.html` instead of a directory listing and validated the UI against the 526-Cue AgenticDub r5 package.

### 2026-08-07 - Cue-range resegment and interactive help

- Added Shift-based contiguous Cue-range selection for subtitle-family review. The workbench shows an STL-style time track with combined source, display, and speech context before a reviewer marks the range for `resegment`.
- The batch action writes the existing `resegment` action and one shared instruction to every selected Cue. It does not edit timing, merge Cues, or change the package/result schema; AgenticDub creates a new immutable revision for the actual segmentation work.
- Replaced native-title `!` markers with click/focus-accessible help popovers. The same component is used for display text, speech text, follow-up action, range instruction, and search/replace help.

### 2026-08-21 - Block-aware Subtitle Workspace interaction (approved)

- Keep review Cue-first while making Block grouping visible as a compact,
  interactive Block container/rail/header around dense editable Cue cards. Block
  headers tag Block ID, Cue count, timing state, and review state.
- Make plain `Enter` at an interior caret split and focus the new right Cue;
  `Ctrl/Cmd+Enter` inserts a same-Cue newline; `Backspace` at Cue start merges
  the previous adjacent Cue; `Delete` at Cue end merges the next adjacent Cue;
  and `M` toggles the quick Agent mark. Ignore IME composition Enter and reject
  splits that would create an empty edge. Accessible contextual controls remain
  fallback; disabled actions explain why.
- During `content_review`, permit adjacent cross-Block merge as an atomic
  merge/recomposition of both Semantic Blocks with Cue/Block/`speech_text`
  invariants, checksums, and lineage. After voice alignment, invalidate scoped
  voice evidence or stay gated per stage.
- Preserve both parent Blocks' extension/provenance payloads inside the merged
  lineage and record parent/result IDs plus before/after projection checksums in
  the direct-change audit. Never let a left-Block copy silently erase right-Block
  metadata.
- Ignore IME composition key events, disable merges when either affected Cue is
  locked by another reviewer, and require a valid interior caret before split.
- Do not add arbitrary Cue reorder or drag in this pass.
- Add separate Block merge and Block split operations: merge retains Cue rows,
  and split inserts a Block boundary before the selected non-first Cue. Focused
  Python checks cover projection, lineage, audit hashes, stale/stage rejection,
  reference preservation, and completion targets; the Node shell check covers
  the implemented keyboard, Cue metadata, Agent fields, and Block controls.

### 2026-08-21 — Inline Cue row and Agent marking contract (approved)

- The next Subtitle Workspace pass makes each Cue one visible row/card and the
  only primary editing surface. Inline text editing, split, adjacent merge,
  needs-modification marking, and the accessible `⋯` fallback live on that row;
  there is no persistent separate editor column.
- Block remains a low-interference grouping rail/header. The ordinary video
  workspace uses the video as Cue context and removes the Cue still/video
  toggle; no-video and special media workflows retain their still/media modes.
- The existing needs-modification marker attaches to the marked Cue or
  contiguous range as a category plus optional note and is visibly labelled
  `待 Agent 修改`. Mark-first/batch-at-complete-round and reverse approval are
  unchanged, and the UI reuses existing Work Order v2 capabilities. The current
  backend has no per-Cue processing-status contract; instant execution and a
  processing-status UI remain deferred with the full Work Order composer.
- Editing pauses follow-play. The global toolbar keeps playback,
  search/replace, complete-round, and sync/reviewer controls.
- This milestone explicitly defers speculative immediate Agent execution,
  per-Cue Agent processing-status backend/UI, a full browser Work Order composer,
  and server-owned Undo/Redo. The preceding contract's done-when checklist is
  the acceptance test for this pass.

### 2026-08-21 — Structural edit interaction context

- Cue／Block 結構操作必須保留操作前的使用者脈絡：結果 Cue、字幕清單中的
  視覺位置，以及 textarea／邊界控制的鍵盤焦點。
- 根因是合併上一句刪除右側 Cue 時，SSE `reload()` 可能先把不存在的選取
  退回 Cue 1；POST 完成後又把這個暫時 fallback 當成正式結果。結果 Cue
  現在只由操作前選取與回應 lineage 決定，本地 transaction 期間收到的 SSE
  會在結果穩定後再 reconcile。
- 共用結構操作流程會在重繪後校正 Cue list／page scroll 並以
  `preventScroll` 恢復焦點；Cue split／merge 與 Block split／merge 不再各自
  呈現不同的跳動行為。
- 回歸驗證包含 450ms 延遲 POST＋SSE、Cue merge、Enter split 與 Block
  merge；Node 35/35、Python 64/64、production build 與 diff check 均通過。
  測試只使用 Boris Workspace 拷貝，未讀寫 Peter 的 package 或審查資料。

### 2026-08-21 — Global Cue keyboard navigation

- `ArrowUp`／`ArrowDown` 是 Workspace 瀏覽模式的全域 Cue 導覽，不受 button、
  summary、select 或其他非字幕編輯元素焦點限制；IME 組字事件仍直接略過。
- 導覽沿用既有 save／unlock／presence／lock 流程，完成後以原生 smooth
  scroll 將新 Cue 放到字幕清單中央，並以 `preventScroll` 把焦點交給新 Cue
  選取列。滑鼠點選與影片播放跟隨不強制置中。
- 字幕 textarea 只在明確進入修稿模式後存在；修稿中的上下鍵保留原生游標
  行為，不再觸發全域 Cue 導覽。

### 2026-08-21 — Separate Cue browse and subtitle edit modes

- 單擊 Cue 只選取；雙擊字幕才進入該 Cue 的行內修稿模式並取得既有鎖。
  選取狀態仍保留「交給 Agent」與「更多操作」，不需要先進入修稿。
- 瀏覽模式的 `ArrowUp`／`ArrowDown` 會切換 Cue、平滑置中並把焦點留在 Cue
  選取列。修稿模式則保留 textarea 的原生上下鍵游標移動。
- 在 collapsed caret 位於文字開頭時按 `ArrowLeft`，會先經既有
  save／unlock／presence／lock 流程，再進入上一 Cue 的文字結尾；位於文字
  結尾時按 `ArrowRight`，會進入下一 Cue 的文字開頭。IME 組字不攔截。
- `Escape` 結束修稿但保留選取；點選其他 Cue 會先儲存目前文字，再退出修稿
  並選取新 Cue。
- Node 35/35、Python 64/64、production build 與 diff check 通過。隔離 Boris
  Workspace 副本的 Chrome 實測確認單擊、雙擊、上下導覽、左右跨句、Esc，
  以及修改後點選下一 Cue 的儲存流程；Peter 資料未被操作。

### 2026-08-21 — Rapid navigation and selected-Cue menu stability

- 快速連按 `ArrowUp`／`ArrowDown` 時，每次按鍵先同步累積本地導覽目標，讓
  畫面立即選到使用者實際要求的 Cue；save／unlock／presence 仍以單一路徑
  序列化追上最新目標，只在穩定後做一次 smooth center。
- 原本 `selectCue()` 在更新選取前先等待 save／unlock，導致同一瞬間的所有
  keydown 都從舊索引算出相同下一句。未完成的舊選取也會在使用者點開
  `交給 Agent`／`更多操作` 後重建選中列，使選單看似無法點擊。
- 點選 Cue 或進入修稿會取消尚未提交的鍵盤導覽目標；若使用者已把焦點放進
  選中 Cue 的行內控制，最後置中不會搶走焦點。
- 隔離 Boris Workspace Chrome 回歸：同步 5 次 `ArrowDown` 前進 5 Cues、
  清單中心誤差 0px；快速導覽後 `交給 Agent` 與 `更多操作` 都能維持展開。
  Node 35/35、Python 64/64、production build 與 diff check 通過；Peter 未操作。

### 2026-08-21 — Persisted Agent instruction visibility

- `交給 Agent` 儲存後，選中 Cue 的操作列會持續顯示「已記錄給 Agent」以及
  伺服器 snapshot 回傳的問題分類與備註；不必重新展開 details 才能確認。
- details summary 同步改為 `✓ 已記錄`。完整內容保留在可見摘要與 title；過長
  備註在緊湊列中以 ellipsis 顯示，但資料本身不截斷。
- 顯示只以 `selectedOwnIssues` 的 authoritative Review Flag 為準，不另外建立
  local success state。重載後仍存在即代表資料庫已記錄；取消標記後摘要消失並
  回到 `交給 Agent`。
- 隔離 Boris Workspace Chrome 實測完成「翻譯錯誤＋備註」儲存、整頁重載與
  取消；Node 35/35、Python 64/64、production build 與 diff check 通過。
  Peter 未操作。

### 2026-08-24 — Whole-Cue deletion and truthful Agent delivery state

- Review Flag 與 Suggestion Job 現在是兩個明確狀態。既有 flag 若沒有 job，UI
  顯示「尚未送出給 Agent」，不再用「已記錄」暗示 worker 已接手；只有
  queued／processing／ready job 才顯示已送出狀態。
- 桌面與 iPad 的選中 Cue 在「更多操作」提供「刪除 Cue」，瀏覽模式亦可用
  `Shift+Delete`（macOS 相容 `Shift+Backspace`）。確認後以 draft-version CAS
  刪除整列；同 Block 尚有 Cue 時重算 Block，否則移除空 Block。最後一個 Cue
  禁止刪除，手機結構編輯仍維持唯讀。
- 刪除會移除 issue 中的舊 Cue reference，保留 `deleted_cue` 與 surviving
  correction anchor 在 direct-change audit；既有 Suggestion Job 因 Cue 消失而
  安全顯示 stale。完成本輪仍能建立 `content_correction_review` Work Order。
- Peter 的舊 `c0326` flag 經正式 Workspace API 補送，worker 已回傳 ready
  proposal，未自動套用。Node 40/40、Python 75/75、production build、
  py_compile 與 diff check 全部通過。

### 2026-08-24 — Reopen a pending content round

- 完成 Peter 內容輪次後才發現四組上游重複翻譯；既有 stage
  `content_agent_review_pending` 正確拒絕直接 draft edit，但規格所述的
  「完成後新發現的人工作業開新 round」缺少實作入口。
- 新增 `workspace-reopen` CLI。它只接受尚未 claim 的 pending
  `content_correction_review`，以單一 transaction 將 Work Order 標為
  `cancelled`、清除 draft freeze 並回到 `content_review`；processing、已有
  Candidate 或其他 stage 均 fail closed。
- Peter `req-0001` 已透過此入口取消，draft v100 的人工修改完整保留，後續修正
  皆走既有版本鎖定 Workspace API。聚焦回歸驗證 reopen 後仍可繼續 edit，且
  舊修改不遺失。

### 2026-08-24 — Agent prompt stays inside its Cue

- 展開 `Agent 設定` 時，提示輸入區不再以 absolute positioning 浮在字幕上；
  它改為 Cue 操作列中的全寬 normal-flow row，並將後續 Cue 往下推。
- 既有欄位、送出行為與收合狀態不變。桌面與 390px 手機 viewport 實測均未與
  當前字幕或下一個 Cue 重疊；Node 41/41 與 production build 通過。

### 2026-08-24 — Suggestion worker lifecycle

- Workspace server 與 AgenticDub suggestion worker 是兩個獨立程序；只恢復
  `workspace-serve` 時，Suggestion Job 會如實停在 queued，且 attempt count
  維持 0。啟動／交付 HITL Workspace 時必須同時檢查兩個程序。
- Peter Cue 235 的原 job 沒有 lease、沒有執行失敗，根因是 worker 不在線。
  恢復限定單一 Workspace 的 scoped worker 後，原 job 經一次 claim 進入 ready；
  沒有建立重複 job，也沒有自動套用建議。
- 後續 job 在後端已完成、畫面卻仍停在排隊中，確認是連續 SSE 事件造成多個
  snapshot reload 並行；較舊 response 可能較晚抵達並覆蓋新狀態。reload 現在
  依 `snapshot_version` 拒絕倒退，無須手動重整才能看見 ready。Node 42/42 與
  production build 通過。

### 2026-08-24 — Human edits are authoritative at round completion

- Reverse approval 的完成條件改以仍存在的 Review Flag 為準。已儲存的人工 edit、
  split、merge、delete 與 Block 結構調整保留 direct-change audit，但不再被送回
  Agent 當 correction target。
- 沒有 flag 的人工修稿直接封存為 `content` revision 並建立
  `realize_voice_timeline`；仍有 flag 時只包含 flagged ranges。新增人工 edit-only
  回歸並更新既有 completion／reopen／delete 契約；Python 78/78 通過。
- Peter draft v268 的 224 筆手改已依此封存為 checksum
  `2fc647bf…7746`，Workspace 進入 `voice_realization_pending`。

### 2026-08-26 — One FrameCue service for all Workspaces

- `workspace-import` now stores the immutable bundle directory with its
  `review_id`. One `workspace-serve --database … --port 3069` process serves
  every Workspace in that database; the toolbar switches the active review by
  the `review_id` URL parameter instead of allocating another port.
- Workspace HTTP routes resolve the active review per request. Collaboration,
  locks, lead state, snapshots, suggestions, candidate audio, and bundled media
  remain isolated by `review_id`. Bundle files use
  `/workspaces/<review_id>/…`, so relative HyperFrames assets also stay scoped.
- Existing databases gain a nullable `bundle_path` column automatically.
  `workspace-serve --dir <bundle>` is retained only to backfill a legacy row;
  new imports need no `--dir` when serving.
- Cue rows now expose character count, duration, CPS, and explicit warnings at
  `<1 s`, `>20 CPS`, or `>30` non-whitespace characters. Ready Agent
  suggestions mark actual word-level removals and additions while preserving
  the existing before/after decision UI.
- Verified seams: one server listed and switched two imported review IDs,
  returned review-scoped bundles, and failed closed for an unknown ID. Node
  46/46, Python 85/85, and the production build pass. Updating the pinned
  runtime skill remains a release step rather than pointing it at an untagged
  development checkout.

### 2026-08-26 — Session-safe one-level Undo and timeline ownership

- Subtitle Workspace now offers exactly one server-owned Undo for the latest
  `edit`, Cue split/merge/delete, or Block split/merge made by the same browser
  session. Undo restores the previous document, Review Flags, and direct-change
  audit while advancing `draft_version`; it never rewinds version history.
- Any later draft-changing operation invalidates every outstanding Undo. Other
  sessions cannot use it, service restart safely drops it, and textarea
  `Ctrl/Cmd+Z` remains native. The server advertises `can_undo` only in the
  session-bound HTTP snapshot; the generic snapshot and CLI stay session-free.
- Removed FrameCue's compressed audiovisual timeline. AgenticDub's reviewed
  audiovisual addon owns waveform/timeline presentation; FrameCue retains
  `MediaStage`, Cue playback-follow, native-video fallback, Candidate audio,
  and the addon event bridge instead of maintaining a second overview.
- Node 49/49, Python 89/89, production build, `py_compile`, and diff check pass.
