# FrameCue v2 Refactor - Implementation Note

Status: implementation complete; release pinned at `v2.0.0`
Last updated: 2026-07-24
Owner: FrameCue
Related handoff: `AgenticDub/docs/architecture/framecue-v2-agenticdub-handoff.md`

## Purpose

This is the canonical implementation note for the FrameCue v2 refactor. It records the agreed product boundary, contracts, rollout order, validation gates, and implementation feedback. Update this document as implementation or user feedback changes the plan.

FrameCue v2 has one job: provide a portable, static human-review gate for media-aligned content. Subtitle generation, translation, speech synthesis, and rendering stay outside FrameCue.

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
| FrameCue | Versioned package/result schemas, static viewer, immutable review revisions, draft autosave, media review, approval, export, validation, and migration tooling |
| AgenticDub | STT, translation, glossary, punctuation policy, cue split/merge, semantic blocks, `speech_text`, pronunciation-risk generation, TTS, TTS/STT audit, and applying approved results |
| Global FrameCue skill | Invoke the pinned FrameCue CLI to build, serve, validate, and collect packages; record implementation feedback |
| HyperFrames project | Composition, project media, narration audio, and project-specific player configuration |

FrameCue never calls an LLM, STT service, TTS service, or renderer. It records reviewer edits and follow-up instructions. The upstream project produces a new immutable revision when regeneration is needed.

## Settled Decisions

### Runtime and source ownership

- The FrameCue repository is the only editable source for the viewer, schemas, and generic HyperFrames adapter.
- v2 uses Svelte with Vite and emits portable static files.
- v2 does not use SvelteKit, a router, a state library, a UI kit, a backend, accounts, or a database.
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

### Block-first semantics

- When semantic blocks exist, blocks own meaning and speech text.
- AgenticDub projects approved block content into display cues.
- Cues review display wording and visual segmentation; cue timing remains read-only in FrameCue v2.
- Editing a block invalidates that block's approval and final package approval. Any cues derived from the old block are stale and must be regenerated in a new revision.
- There is no formal per-cue approval. Blocks may be approved individually, followed by one final package approval.
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
3. **Review Workbench**: Block first when present, then Cue. Risk and All are filters, not competing review modes.
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
- Collaborative accounts, remote persistence, comments, or a review backend.
- A general nonlinear editor or timeline editor.
- Supporting arbitrary plugin-defined workflows in v2.
- Automatic v1 compatibility inside the v2 viewer.

## Main Risks

| Risk | Control |
|---|---|
| Contract changes while both repositories implement against it | Finish and tag FrameCue v2 before AgenticDub integration code starts |
| Browser drafts become inaccessible after path/origin changes | Export or retire drafts before migration; isolate new drafts by identity and checksum |
| Svelte refactor reproduces current UI coupling | Keep state transitions explicit and test round trips before adding media adapters |
| Generated bundles become new source forks | Treat `dist` and review directories as immutable build artifacts |
| Block and cue edits conflict | Keep semantic ownership in blocks and regenerate projections as a new revision |

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
