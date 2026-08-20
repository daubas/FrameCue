# VidScribe as a reference for FrameCue

Status: research note
Date: 2026-08-20
Audited revisions: VidScribe [`0881fef`](https://github.com/AinxietyLab/VidScribe/tree/0881fef97ecac1e9da621b89b23598121663a548); FrameCue [`a1be950`](https://github.com/daubas/FrameCue/tree/a1be9509c1340d5712d761839058c824c97a4f70)
Scope: first-party repository README, specification, source, and package files only

## Verdict

VidScribe is a useful **interaction reference** for timed-subtitle review, but the wrong **product architecture** for FrameCue. VidScribe is a mutable, local, end-to-end subtitle authoring application: import media, transcribe, edit text and timing, optionally proofread, then export or burn a video. FrameCue is a static human-review gate over an immutable, checksum-bound package and must keep generation, retiming, rendering, and authoritative persistence upstream.

Borrow four narrow ideas:

1. one media clock drives the playhead, active Cue, and list scrolling;
2. keyboard-first navigation and review around the selected Cue;
3. a small bounded undo/redo history for browser-draft edits;
4. stale-safe proposal acceptance: apply a proposed replacement only while its expected old value still matches.

Reject VidScribe's backend, mutable project files, timing authoring, job system, and direct SRT/video export. Those erase the boundary that makes a FrameCue approval trustworthy.

## What VidScribe actually is

### User journey

The advertised journey is “drop in a video → GPU transcription → keyboard proofreading → SRT/transcript/burned-video export,” entirely on the user's machine and without an account ([README](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/README.md#L1-L24)). The home page creates a project from an uploaded media file; the backend immediately starts transcription ([API](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/main.py#L82-L97)). The editor then supports direct text editing, split/merge/delete, timing drag, new segments, marks, scene-cut snapping, AI-proposed corrections, and several exports ([README shortcuts](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/README.md#L59-L73), [editor operations](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L340-L509), [export formats](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/exporter.py#L92-L108)). This is authoring, not approval.

### Architecture and data model

VidScribe is a React/Vite frontend served by a local FastAPI backend. The backend owns faster-whisper, ffmpeg, OpenCC, and optional Claude Code integration; project directories and JSON replace a database ([README architecture](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/README.md#L86-L102), [Python dependencies](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/requirements.txt#L1-L7)).

Its core persisted objects are deliberately small:

- `Project`: identity, media filename, processing status/progress, duration, language, video flag, model, and device ([frontend types](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/types.ts#L15-L36));
- `Segment`: random ID, floating-point `start`/`end` seconds, text, and optional word timestamps ([frontend types](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/types.ts#L1-L13));
- `subtitles.json`: `version`, the full mutable segment array, and marks ([storage](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/storage.py#L96-L110), [save endpoint](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/main.py#L133-L152));
- background transcription, burn, cut-detection, and AI-fix jobs, mostly held in process memory; AI suggestions additionally persist as `fix.json` so review can resume ([AI persistence](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/llm.py#L83-L154)).

The frontend is thinly modularized into API, pure segment operations, history, waveform, and display components, while `Editor.tsx` remains the central state/orchestration surface ([editor imports and state](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L1-L120)). This is acceptable for one editor, but is not a model for expanding FrameCue's contract.

## Time synchronization and editing state

VidScribe uses the native `<video>` element as the authoritative playback clock. While playing, `requestAnimationFrame` copies `currentTime` into React state for smoother motion than `timeupdate`; selecting a row seeks the video, active-Cue lookup uses binary search over sorted segments, and playback scrolls the active row and waveform into view ([clock update](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L251-L262), [seek/selection](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L426-L445), [active lookup](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/segments.ts#L130-L146), [follow-scroll](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Waveform.tsx#L107-L116)).

Timing is fully editable. Segment edges or whole ranges can be dragged; boundaries snap within eight pixels to neighboring segments, manual marks, or detected cuts. Empty waveform space can create a segment, and a paused hover scrubs the player ([waveform editing](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Waveform.tsx#L201-L261), [create and hover](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Waveform.tsx#L313-L379)). Text splitting estimates time by character ratio and optionally snaps to nearby word timestamps; time splitting finds a matching word or falls back to proportional text position ([segment operations](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/segments.ts#L31-L118)).

The working segment array has a 100-snapshot in-memory undo/redo history ([history](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/history.ts#L1-L43)). Changes autosave after 800 ms, retry after failure, and trigger a page-exit warning while unsaved ([autosave](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L264-L294), [exit guard](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L332-L338)). The server atomically replaces JSON through a temporary file and makes one backup before retranscription overwrites subtitles ([storage](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/storage.py#L22-L42), [backup](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/storage.py#L106-L110)).

AI corrections are proposals, not automatic writes: each suggestion carries segment ID, old text, and new text; acceptance changes the segment only if its current text still equals the proposal's old text ([proposal validation](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/llm.py#L196-L238), [acceptance guard](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/src/Editor.tsx#L662-L700)). That small stale-write guard is the best transferable data-integrity idea in the project.

## Persistence, export, tests, and deployment

VidScribe persists the latest mutable project state, not a revisioned decision record. The subtitle PUT endpoint checks only that segments are an array containing `start`, `end`, and `text`; it has no checksum, optimistic-concurrency token, approval state, completeness rule, or lineage ([save endpoint](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/main.py#L139-L152)). SRT, VTT, transcripts, ASS, and burned video are generated directly from the latest segments ([exporter](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/exporter.py#L1-L108), [burn pipeline](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/burn.py#L67-L95)).

At the audited revision, the frontend package exposes only `dev`, `build`, and TypeScript `check` scripts—no test command—and the repository tree contains no automated test suite ([frontend package](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/frontend/package.json#L1-L21)). The useful pure logic in `segments.ts` and `history.ts` therefore demonstrates a good seam, not sufficient verification.

Deployment is a Windows-oriented local web app. Prebuilt frontend assets are committed so normal users do not need Node; setup installs Python/ffmpeg and dependencies, and start launches a loopback FastAPI server ([README install](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/README.md#L26-L52), [setup](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/setup.ps1#L43-L78)). The server restricts Host and write Origins because it owns mutable local data ([server guards](https://github.com/AinxietyLab/VidScribe/blob/0881fef97ecac1e9da621b89b23598121663a548/backend/main.py#L39-L59)).

## What FrameCue should borrow

| VidScribe idea | Minimal FrameCue adaptation |
|---|---|
| Native media clock drives UI | Keep one clock owner per media-stage adapter. Derive active Cue, playhead, and auto-scroll from it. Use animation-frame updates only during playback; retain `timeupdate` as the semantic media event. |
| Binary-search active segment | Use it for large sorted Cue sets, with gaps returning “no active Cue.” VidScribe already does both. |
| Keyboard-first flow | Preserve play/pause, previous/next Cue, seek-to-Cue, and review-and-advance. Do not import timing-edit shortcuts. |
| Bounded undo/redo | Add a browser-draft-only history if reviewer feedback shows accidental edits are costly. A fixed 100-snapshot ceiling is adequate; it must never become audit history or package lineage. |
| Old-value acceptance guard | When applying an imported/previous-revision proposal, require stable Cue ID **and** expected prior value (or source checksum) to match. On mismatch, show it as stale instead of silently applying it. |
| Pure timed-text helpers | Keep active-Cue lookup and other deterministic transforms outside UI components and cover them with focused tests. FrameCue already has the right precedent in its tested review helpers ([FrameCue package scripts](https://github.com/daubas/FrameCue/blob/a1be9509c1340d5712d761839058c824c97a4f70/package.json#L6-L10)). |

These changes fit FrameCue's existing model: drafts are keyed by review ID, revision, and content checksum; every edit invalidates relevant approval; exported results repeat identity and checksum ([FrameCue draft and invalidation](https://github.com/daubas/FrameCue/blob/a1be9509c1340d5712d761839058c824c97a4f70/src/lib/review.js#L56-L87), [FrameCue result snapshot](https://github.com/daubas/FrameCue/blob/a1be9509c1340d5712d761839058c824c97a4f70/src/lib/review.js#L247-L280)).

## What FrameCue should explicitly reject

- **Mutable timing, split/merge/create/delete, waveform marks, and scene-cut detection.** FrameCue should display time and let reviewers request `retime` or `resegment`; upstream must produce a new immutable revision. Its package already models integer-millisecond Cue ranges, while its result records actions and reviewed text rather than rewritten timing ([package schema](https://github.com/daubas/FrameCue/blob/a1be9509c1340d5712d761839058c824c97a4f70/schemas/framecue-package-v2.schema.json#L103-L157), [result schema](https://github.com/daubas/FrameCue/blob/a1be9509c1340d5712d761839058c824c97a4f70/schemas/framecue-review-result-v1.schema.json#L28-L57)).
- **The local FastAPI generation/backend.** Do not absorb upload, STT, LLM, cut detection, burn jobs, project deletion, or mutable storage. AgenticDub and other producers own that work; FrameCue needs only static assets plus its existing build/collect tooling.
- **Autosave as authority.** Browser storage remains disposable convenience state. Never replace the canonical package or treat the newest draft as approved evidence. Only a complete, approved, checksum-matching result may cross the gate.
- **Direct SRT/VTT/video export from the draft.** Those are downstream transformations. FrameCue exports the canonical complete review snapshot; adapters elsewhere may derive operational artifacts after collection validates it.
- **VidScribe's weak save contract.** Do not accept shape-only segment arrays, floating-point time mutation, or last-writer-wins overwrite. Preserve stable IDs, schema versions, revision, checksum, workflow action vocabulary, completeness, approval, and lineage.
- **Background-job recovery semantics.** “Interrupted, please rerun” is appropriate for transcription, not review evidence. A FrameCue package is immutable and should reopen deterministically; browser draft recovery must remain bound to the exact package checksum.
- **A growing central editor component.** Borrow VidScribe's pure helpers, not its all-state-in-one-editor orchestration. Keep FrameCue's existing seams—media stage, workbench, details, review rules, storage, and download—and split further only when behavior is independently testable or reused.
- **Optional executable extensions.** VidScribe can shell out to Claude and ffmpeg because it is a trusted local authoring application. FrameCue packages must not carry executable plugins. Extension seams stay declarative: workflow/action vocabularies, media-stage adapters owned by FrameCue, metadata panels, and downstream exporters.

## Refactor implication

VidScribe does not justify a larger FrameCue. It justifies making the current static viewer feel more like a focused subtitle tool while preserving the gate boundary:

```text
immutable package + checksum
          ↓
one media clock → active Cue → keyboard review
          ↓
checksum-scoped disposable draft (+ optional bounded undo)
          ↓
complete approved result → collect validates → upstream acts
```

The ceiling remains one static runtime, one immutable package contract, one complete result contract, and narrow declarative adapters. Add waveform or timing-authoring machinery only if FrameCue's product boundary is intentionally changed; VidScribe shows that those features quickly pull in a backend, mutable persistence, generation jobs, and operational export.
