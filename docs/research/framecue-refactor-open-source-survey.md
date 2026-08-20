# FrameCue refactor: open-source survey

Status: research note
Date: 2026-08-19
Scope: official specifications, documentation, and project repositories only

## Recommendation in one page

FrameCue should remain a small, static review gate—not become a general annotation platform. Its current strongest decisions are already the right ones: an immutable, checksum-bound source package; browser draft state that is not authoritative; and a complete approved result snapshot. The refactor should sharpen those contracts and add narrow seams, not import task servers, accounts, databases, or arbitrary plugins.

The smallest useful target model is:

```text
immutable package revision
  resources[]       stable ID, bundled path, media type, checksum
  segments[]        cue/block/scene ID + source resource + [start_ms, end_ms)
  source content    original timing/text/assets

mutable review snapshot
  decisions[]       target segment ID + edited value + action + instruction
  review state      reviewed/approved + timestamps
  package identity  review_id + revision + package checksum
```

Adopt these ideas:

1. **ELAN's explicit structural relations, in a much smaller vocabulary.** ELAN distinguishes directly time-aligned annotations from child annotations that inherit or subdivide a parent's interval, and enforces constraints such as one-to-one symbolic association or contained time subdivision ([ELAN annotation model](https://www.mpi.nl/tools/elan/docs/manual/Sec_Basic_Information_Annotations_tiers_and_linguistic_types.html)). FrameCue only needs explicit `block -> cue` membership and declared timing ownership; it does not need user-defined tier hierarchies.
2. **W3C's separation of annotation body, target, selector, and source state.** Web Annotation treats an annotation as a relationship between a body and one or more targets, with selectors identifying a segment of a source ([Web Annotation Data Model](https://www.w3.org/TR/annotation-model/)). Use that vocabulary as design guidance, not as FrameCue's internal wire format. Represent temporal targets as half-open intervals, matching W3C Media Fragments' `[begin,end)` semantics ([Media Fragments 1.0](https://www.w3.org/TR/media-frags/)).
3. **Label Studio's read-only prediction versus human annotation split.** Imported predictions are read-only and are copied into a new editable annotation for review; stable region/result IDs connect the machine proposal to the human result ([predictions](https://labelstud.io/guide/predictions.html), [export format](https://labelstud.io/guide/export.html)). FrameCue's package should likewise remain untouched while the result records the reviewed replacement.
4. **CVAT's explicit validation transitions, reduced to FrameCue's real workflow.** CVAT separates job stage (`annotation`, `validation`, `acceptance`) from state (`new`, `in progress`, `rejected`, `completed`) and supports issues that must be resolved before completion ([task/job model](https://docs.cvat.ai/docs/manual/basics/create-annotation-task/), [manual review](https://docs.cvat.ai/docs/qa-analytics/manual-qa/)). FrameCue needs only `draft | approved`, plus deterministic invalidation when edited. Do not reproduce the two-dimensional enterprise workflow.
5. **Subtitle editors' fast, media-led interaction.** Subtitle Edit couples a list editor, video, waveform, direct manipulation, selection, and keyboard navigation; its timing model prefers time-based subtitles because variable frame-rate video makes frame-based formats brittle ([official documentation](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/index.md), [shortcuts](https://github.com/SubtitleEdit/subtitleedit/blob/main/docs/reference/keyboard-shortcuts.md)). Aegisub likewise centers audio timing and real-time video preview ([official site](https://aegisub.org/)). Copy interaction lessons—selection follows playback, replay current range, predictable shortcuts—not either application's broad editing surface.

## Comparison

| Source | Data model | Source vs annotations | Timeline/player | Workflow/validation | Extension/export | FrameCue lesson |
|---|---|---|---|---|---|---|
| ELAN | Tiers of annotations; independent annotations own time, dependent tiers inherit or subdivide parent time | Media is linked; annotations live in an EAF document | One time axis drives video, waveform, timeline, grid, and subtitle viewers | Tier stereotypes enforce containment, adjacency, ordering, or one-to-one association | Many exports, including preliminary W3C Web Annotation JSON ([export docs](https://www.mpi.nl/tools/elan/docs/manual/Sec_Exporting_WebAnnotation_JSON.html)) | Make ownership and parent/child constraints explicit; avoid unlimited tiers and cascading edit semantics |
| CVAT | Project → task → segmented jobs → annotations/issues | Uploaded task data and mutable annotations are distinct | Frame-oriented player and issue navigation | Rich stage/state, assignees, rejection/correction, ground truth, consensus, QA reports ([QA docs](https://docs.cvat.ai/docs/qa-analytics/)) | Dataset import/export and SDK/API | Borrow visible gates and unresolved-issue blocking only if a real workflow needs them; skip server/team machinery |
| Label Studio | Task `data`, read-only `predictions`, editable `annotations`, each with typed region/results | Strong machine-proposal/human-result separation | Object tags expose audio/video regions; result IDs bind related controls | Drafts, submissions, cancelled annotations; broader workflow depends on edition | XML-like labeling config, many export formats; standalone frontend is deprecated ([frontend notice](https://labelstud.io/guide/frontend.html)) | Preserve proposal/result separation and stable IDs; avoid a runtime-configurable UI DSL and deprecated embeddable frontend |
| Subtitle Edit / Aegisub | Subtitle rows/events with time ranges, text, and style metadata | Original/translation may be displayed together; editing remains file-oriented | Video + waveform/spectrogram + range replay + keyboard timing; Aegisub handles variable-frame-rate timecodes explicitly ([video docs](https://aegisub.org/docs/latest/video/)) | Primarily editor undo/save, not a formal approval gate | Many subtitle formats and potentially lossy exports ([Aegisub export docs](https://aegisub.org/docs/latest/exporting/)); Aegisub has Lua Automation ([official repository](https://github.com/Aegisub/Aegisub)) | Copy media-led ergonomics, not timing/style authoring or script execution |
| W3C Web Annotation + Media Fragments | Annotation → body + target; SpecificResource → source + selector/state | The source is external; annotation describes a relationship to a selected representation | Temporal fragments use `t` and half-open intervals | Exchange model, not workflow | Interoperable JSON-LD and selector vocabulary | Keep FrameCue JSON small; optionally add a Web Annotation export adapter later |
| JSON Schema 2020-12 | Declarative structural assertions and annotations | N/A | N/A | Validates local structure; each schema object is evaluated against its instance location ([validation spec](https://json-schema.org/draft/2020-12/json-schema-validation)) | `$defs`, vocabularies, bundling, `unevaluatedProperties` ([2020-12 overview](https://json-schema.org/draft/2020-12)) | Use schema for shape and closed vocabularies; keep cross-reference, checksum, lineage, interval, and approval invariants in package-aware code |
| Automerge (counterexample) | Immutable document snapshots backed by CRDT changes/history | Local document is primary and can merge concurrent edits | Application-defined | Branching/merging rather than approval semantics | Pluggable storage/network adapters and offline sync ([concepts](https://automerge.org/docs/reference/concepts/)) | Do not add CRDT/event history while FrameCue is single-reviewer and file-handoff based |

## Concrete refactor guidance

### 1. Contract and identity

- Keep `review_id`, immutable `revision`, `content_checksum`, and full-snapshot result as the gate authority.
- Add a small `resources[]` registry only if the same media asset is referenced from several workflows. Each resource should have a stable ID, relative bundled path, media type, and checksum. Otherwise keep the current nested media object; duplicating a registry for one reference is worse.
- Make every editable decision point address a stable target ID. For timed targets, define intervals once as integer milliseconds with `start_ms >= 0`, `end_ms > start_ms`, and half-open `[start_ms,end_ms)`. This removes boundary ambiguity and agrees with Media Fragments.
- Keep source values and reviewed values in different documents. Never mutate `packageData` to represent a draft.
- Do not adopt JSON-LD internally. If another tool needs it, write an export adapter. ELAN itself treats Web Annotation as an import/export format, and its importer supports only the subset that maps naturally to ELAN ([ELAN import note](https://www.mpi.nl/tools/elan/docs/manual/Sec_Importing_a_WebAnnotation_file.html)).

### 2. Structural and semantic validation

- Close stable core objects against misspelled fields (`additionalProperties: false`, or `unevaluatedProperties: false` where composition requires it), but reserve one explicit `extensions` object for forward-compatible workflow metadata. Draft 2020-12 defines `unevaluatedProperties` for properties not handled by adjacent applicators ([core specification](https://json-schema.org/draft/2020-12/json-schema-core.html#name-unevaluatedproperties)).
- Do not rely on `format` alone for timestamps or URIs: Draft 2020-12 separates format annotation from format assertion, and the default meta-schema treats format as annotation ([release notes](https://json-schema.org/draft/2020-12/release-notes)). Validate important formats in code or configure the validator explicitly.
- Retain package-aware validation for: unique IDs; every `cue.scene_id` and `block.cue_ids[]` resolving; child ranges contained by the parent when the parent owns time; sorted/non-inverted ranges; asset containment; allowed actions per workflow; exact package identity/checksum; edit-invalidates-approval; and approved-result completeness.
- Keep the formal state machine tiny: `draft -> approved`; any content/action change returns it to `draft`. If unresolved issues are later added, approval requires zero blocking issues. Do not add CVAT's assignee/stage/state matrix until multiple roles actually exist.

### 3. Player synchronization

- Define one authoritative clock per stage. The native `<video>` element owns time for bundled video; an adapter owns time for an embedded composition. Selection follows that clock, while explicit cue selection performs one seek.
- Keep the adapter protocol narrow and capability-based: `ready {duration}`, `seek {time}`, `play`, `pause`, and `time {time, playing}`. Include protocol version and clamp/validate all received times. Do not expose review state to the player.
- Use binary search over sorted cue starts when packages become large; the current linear lookup is acceptable until profiling says otherwise. Treat gaps explicitly (no active cue) rather than silently selecting the previous cue.
- Use the native media clock and `timeupdate` for ordinary list highlighting ([HTML media events](https://html.spec.whatwg.org/multipage/media.html#event-media-timeupdate)). Add `requestVideoFrameCallback` only for proven frame-accurate overlays; the WICG draft describes best-effort presentation callbacks that may still arrive one vertical refresh late ([draft specification](https://wicg.github.io/video-rvfc/)).
- Keep the most valuable subtitle-editor interactions: replay selected interval, previous/next cue, play/pause, and visible current-range feedback. Timing mutation remains upstream.

### 4. Extension seams

Support declarative capabilities, not executable plugins:

- workflow declares required stage capabilities (`still`, `compare`, `video`, `iframe_time_adapter`);
- action vocabulary is chosen by workflow and validated by the collector;
- optional detail panels read namespaced metadata;
- exporters map the canonical result snapshot to downstream formats.

This is enough to support new review packages without accepting arbitrary JavaScript from a package. Label Studio's XML labeling DSL and Aegisub's Lua automation are powerful because they serve general-purpose editors; both would expand FrameCue's security and compatibility surface far beyond its static-gate job.

### 5. Export, versions, and local-first behavior

- Keep the canonical export as a complete JSON snapshot bound to package checksum and viewer version. Derived diffs, SRT, Web Annotation JSON, or downstream patches are adapters, never gate evidence.
- Preserve browser autosave as disposable convenience state keyed by review/revision/checksum. Export must be explicit and portable; loading a draft from a different checksum must fail closed.
- Add an append-only event log only if audit requirements need who/when/why for intermediate actions. Even then, keep the approved snapshot authoritative and make the log a separately validated adjunct. Automerge provides immutable history, merging, storage, and network adapters, but those solve concurrent local-first collaboration that FrameCue does not currently have ([Automerge overview](https://automerge.org/docs/hello/)).
- Version the package schema, result schema, and player protocol independently. A viewer may support a declared range; a package build pins one viewer version.

## What not to copy

- ELAN's unlimited user-configured tiers, controlled vocabularies, lexicon services, and cascade-heavy editing.
- CVAT's backend, organizations, roles, segmented jobs, assignment, consensus, analytics, or ground-truth subsystem.
- Label Studio's XML UI configuration, database-shaped task envelope, ML backend, or deprecated standalone frontend.
- Subtitle Edit/Aegisub timing and styling authoring, dozens of formats in the core, or arbitrary script/plugin execution.
- Web Annotation's complete JSON-LD graph, IRIs everywhere, collections/pages, agents, rights, audiences, and selector zoo.
- CRDTs, multi-peer sync, or event sourcing before concurrent editing or forensic audit is a demonstrated requirement.

The refactor ceiling should remain clear: one static app, one immutable package contract, one complete result contract, one package-aware validator, and a handful of declarative stage/export adapters.
