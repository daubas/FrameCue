# FrameCue Subtitle Workspace × AgenticDub 重構規格

Status: product decisions approved; cross-model findings incorporated; implementation not authorized
Last updated: 2026-08-20
Owners: FrameCue Workspace / AgenticDub realization flow

## 1. Outcome

FrameCue becomes the single human-review workspace for translated subtitles. The
same workspace supports two views over one revision lineage:

```text
translated draft
  → content exception review
  → agent correction/review when needed
  → immutable Content Revision
  → AgenticDub voice realization
  → audiovisual exception review
  → scoped correction when needed
  → immutable Voice-Aligned Revision
  → final_approved
```

The subtitle is not independently generated twice. The first review establishes
approved wording and display segmentation. AgenticDub then adds real speech,
word alignment, and output timing. The second review verifies the resulting
voice, subtitle timing, and rendered media.

This specification supersedes the earlier Workspace plan where it conflicts.
The existing static FrameCue v2 package/result workflow remains compatible and
unchanged.

## 2. Product invariants

1. FrameCue is the authority for the mutable review draft and every immutable
   subtitle revision.
2. AgenticDub owns translation generation, TTS, pronunciation handling, word
   alignment, timing fit, audio evidence, render, and downstream production.
3. AgenticDub reads and submits through the Workspace interface. It never reads
   or writes FrameCue SQLite tables.
4. Unmarked content is approved. Reviewers mark only exceptions that still need
   work.
5. Formal TTS cannot begin while a content issue or agent semantic suggestion is
   unresolved.
6. A Candidate never silently overwrites a newer human edit. Every submission is
   bound to the exact base revision and affected-value checksums.
7. Static v1/v2 bundles and immutable revisions are never rewritten in place.
8. Media and evidence stay in the filesystem and are SHA-256 bound from revision
   manifests; SQLite stores structured state, not large media blobs.
9. The complete subtitle, collaboration, agent-review, and audiovisual flow is
   delivered together as one acceptance build. Internal implementation
   checkpoints are not user-facing partial releases; production cutover follows
   one final human UAT session.
10. An audiovisual wording change is a content decision. It returns through the
    content gate before any replacement TTS is generated.
11. Portable import trusts only validators and schemas installed on the receiving
    FrameCue host; exported data never carries live authority.

## 3. Ownership and seams

### FrameCue owns

- Workspace identity, mutable draft, review stage, participants, lead reviewer,
  issue flags, activity audit, and recovery.
- Cue and Semantic Block review operations.
- Immutable Content and Voice-Aligned Revisions with lineage.
- Work Orders, Candidate validation, changed-range diff, accept/reject, and stale
  result handling.
- The central SQLite database, Workspace HTTP interface, browser UI, token
  enforcement, and portable snapshot import/export.
- Human approval authority.

### AgenticDub owns

- Source STT, translation, glossary and protected-term policy.
- TTS provider and voice choice, silence trim, pronunciation substitutions,
  `<=1.20x` timing fit, word alignment, and post-TTS audit.
- Candidate media, alignment evidence, and the rendered-review production master.
- Re-realization of the requested Cue/Block ranges.
- Final QC, metadata, thumbnail, publishing gates, and YouTube handoff after
  FrameCue reports `final_approved`; a content-changing re-render returns to
  FrameCue first.

### Workflow skill owns

- Starting or resuming the FrameCue host and AgenticDub worker.
- Selecting timing profile and production-specific voice/audit policy.
- Recovering a failed or interrupted Work Order.
- TS Preview and FBR handoff rules for local pages and files.

### External seam

The Workspace HTTP interface is the single network seam. Browsers and
AgenticDub use separate permissions over it. FrameCue's SQLite schema, Svelte
state, synchronization transport, and filesystem layout remain implementation
details.

The interface must support these few domain actions rather than expose database
CRUD:

- load a Workspace snapshot;
- apply one validated draft operation;
- complete the current review round;
- list, read, or submit a Work Order;
- claim, fail, retry, or cancel a Work Order;
- accept or reject independently identified Candidate change proposals;
- export, restore, or import a portable Workspace snapshot.

## 4. Workspace stages

```text
content_review
  ├─ complete with no edits/issues
  │    → Content Revision
  └─ complete with edits or issues
       → content_agent_review_pending
       → content_candidate_review (semantic decisions or mechanical undo grace)
       ├─ still needs changes → content_agent_review_pending
       → Content Revision
       → voice_realization_pending

voice_realization_pending
  ├─ valid voice Candidate → audiovisual_review
  └─ rewrite_required → scoped content_review

audiovisual_review
  ├─ complete with wording/translation issues
  │    → scoped content_review
  │    → new Content Revision
  │    → voice_realization_pending
  ├─ complete with voice/timing/segmentation issues
  │    → audiovisual_correction_pending
  │    → audiovisual_review (changed ranges only)
  └─ complete with no issues
       → Voice-Aligned Revision
       → final_approved
```

An execution failure is Work Order state, not a new Workspace stage. Retrying a
failed order does not mutate its base revision. A stale result remains retained
for audit but cannot be applied.

Completing a round freezes its full draft snapshot. Agent-pending stages are
read-only except for Candidate decisions. A human change made after completion
starts a new draft round; it never mutates the snapshot bound to an in-flight
Work Order.

`rewrite_required` is a non-retryable content outcome, not an execution failure.
It names the affected ranges, reopens them in `content_review`, and blocks voice
or final approval until a new Content Revision resolves them.

## 5. Reverse approval

FrameCue removes `reviewed_cues`, per-Cue approval progress, Space-to-approve,
and the requirement to mark every Cue reviewed.

During a review round:

- unmarked Cues are approved when the lead reviewer completes the round;
- a direct edit, split, or merge is recorded as a human-handled change;
- a `needs modification` flag identifies one Cue or a Shift-selected contiguous
  range and may include multiple issue categories plus an optional note;
- duplicate flags on the same range and category merge while retaining every
  author and note;
- the completion summary shows direct edits, unresolved issues, and what the
  next transition will do;
- completion is blocked while any client has unsynchronized edits.

If the round contains issues, one Work Order carries every range from that
round. Unflagged content is frozen as approved context. The Candidate can change
only requested ranges. FrameCue may recompute derived consistency fields while
applying a proposal, but those fields cannot introduce content outside its
authorized range. Each returned range is accepted or sent back independently.

Each Work Order target has an opaque, immutable `range_id`, exact base Cue and
Block IDs, source span, lineage anchors, allowed operations, allowed fields, and
its own before-checksum. `Context` is readable evidence, not write authority.
Unmarked or already accepted content cannot be changed as a context projection.

A Candidate returns one or more independently hash-bound `change_proposals`.
Each proposal names one `range_id`, repeats its before-checksum, carries the
replacement document slice, and lists any proposal dependencies. FrameCue
recomposes the authoritative full draft from the frozen base plus accepted
proposals only. Rejected proposals leave the base slice unchanged. A structural
proposal that crosses another target range must declare one dependency group;
the group is accepted or rejected atomically. An undeclared cross-range change
rejects the entire Candidate.

## 6. Draft operations

The first direct-edit release has exactly three structural operations:

1. edit subtitle display text;
2. split the selected Cue at the text cursor;
3. merge the selected Cue with an adjacent Cue in the same Semantic Block.

It does not provide free Cue creation, deletion, timing drag, waveform editing,
or manual cross-Block merge. Cross-Block restructuring is expressed as an agent
instruction.

### Split

- With trusted word timestamps, snap the split to the nearest valid word
  boundary.
- Without them, divide source timing proportionally using the cursor position
  and mark both ranges `provisional`.
- AgenticDub replaces provisional output timing after voice alignment.
- The two derived Cues retain lineage to the parent Cue.

### Merge

- Both Cues must be adjacent and share one `block_id`.
- The merged Cue spans their source ranges and retains lineage to both parents.
- A merge cannot move or combine Semantic Blocks.

### IDs and lineage

Unchanged Cue IDs remain stable. Structural operations create opaque new Cue
IDs plus `origin_cue_ids`; consumers must not infer order or ancestry from the
ID string. A Candidate may replace Cue structure only inside its authorized
range. The current Candidate rule requiring identical Cue IDs therefore applies
only to legacy Workspace contract v1.

### Autosave and undo

- The browser reports `dirty=true` immediately on the first local change, then
  submits operations after about 800 ms of idle time. It reports `dirty=false`
  only after the server acknowledges the resulting draft version.
- Completion uses compare-and-swap on the current draft version and requires
  every connected session to be clean and every affected Cue unlocked.
- A disconnected browser cannot contribute unacknowledged input; it warns that
  such input is not durable and becomes read-only until it reloads the server
  snapshot.
- The current user's latest 100 reversible draft operations are available for
  Undo/Redo.
- Undo never changes an immutable revision and never undoes another user's
  operation. It is disabled when later structural changes make the inverse
  stale.
- The lead may also Undo an auto-applied Agent proposal during its 10-second
  grace period; this is the only operation outside the lead's own history that
  Undo can target.
- The server snapshot is recovery state, not approval.

## 7. Content and speech text

The primary editor is `display_text` (Traditional Chinese subtitle text).
`speech_text` follows it automatically until a deliberate spoken-only difference
exists. The explicit `Separate speech text` action breaks that link and exposes
the speech editor plus a difference indicator; relinking replaces speech text
with the current display text after confirmation.

Semantic Blocks remain authoritative for meaning and TTS speech:

- `block.target_text` is the normalized concatenation of its child Cue display
  text;
- `block.speech_text` is the exact TTS input and is the normalized concatenation
  of its child Cue speech projections;
- a Cue-level `speech_text` is an editable projection, not an independent TTS
  authority;
- split/merge updates `block.cue_ids` transactionally and recomputes both Block
  projections;
- after applying declared pronunciation/speech mappings, normalized display,
  Block meaning, and speech content must represent the same approved words.

Any difference outside a declared mapping is semantic and requires human
confirmation. Cross-Block instructions provide context only; a Candidate needs
explicit target authority for every Block it changes.

Each Cue row shows muted source text and prominent editable Chinese text. Cue
IDs, milliseconds, checksums, and provenance stay in the secondary tools
drawer. Semantic Block boundaries appear as subtle group separators and expand
only for an inconsistency, cross-Block request, or explicit user action.

Search and replace live in the secondary drawer. Replace shows the match count,
requires confirmation, acquires every affected Cue lock, and records the batch
as one undoable all-or-nothing operation. A missing lock or stale Cue rejects the
whole replacement.

## 8. Agent review of human edits

Every human edit is included when the content round completes. FrameCue sends
one `content_correction_review` Work Order containing:

- all direct human edits;
- all unresolved issue ranges;
- source and target text;
- parent Block meaning and neighboring context;
- before-values and checksums;
- the checksum-bound protected-term/glossary snapshot used by this draft;
- optional agent instructions.

FrameCue does not send an agent request after each keystroke.

Agent suggestions are classified by deterministic FrameCue rules:

- punctuation, whitespace, full/half-width normalization, and pre-authorized
  exact mechanical replacements may auto-apply;
- any word addition/removal, number, name, protected term, terminology, speech
  content, or word-order change requires human confirmation;
- Candidate self-reported confidence or `minor` labels never grant auto-apply.

Auto-applied changes briefly highlight, appear in the activity audit with the
before/after value, and enter a fixed 10-second lead-review grace period. The
lead may Undo during that period; an Undo cancels automatic completion. If no
Undo occurs, FrameCue creates the Content Revision automatically. Semantic
changes show an inline old/new diff with `Accept` and `Still needs changes`.

If AgenticDub reports no semantic change and all issue ranges are resolved,
FrameCue creates the Content Revision immediately when it applied no mechanical
change, or after the mechanical grace period. If no human edit and no issue
existed, FrameCue skips agent review and creates it directly.

## 9. Audiovisual review

The second stage keeps reverse approval. Reviewers mark only problematic Cue
ranges; unmarked media is approved at completion. A problem can carry multiple
categories:

- translation;
- pronunciation;
- voice;
- timing;
- subtitle segmentation;
- other.

The selected Cue is the default time range and Shift extends to contiguous
Cues. The first release does not add arbitrary timeline dragging or a waveform.
AgenticDub returns a complete new Candidate and full review media; FrameCue
opens directly on changed ranges and shows local old/new evidence.

Changing Chinese text during audiovisual review invalidates TTS, alignment, and
timing for the affected range. FrameCue accumulates such changes during the
round, returns them through `content_correction_review`, and creates a new
Content Revision before replacement TTS. An `audiovisual_correction` Work Order
cannot change `display_text`, `speech_text`, Block meaning, or protected terms.
It handles only approved-text pronunciation, voice, timing, and display
segmentation corrections.

Partially corrected Candidate ranges are accepted independently. Accepted
ranges stay accepted; only unresolved ranges return to AgenticDub.

The Candidate review render is the production master proposed for Final QC. It
is bound to the Candidate document checksum, subtitle artifact, audio hashes,
and render profile. Acceptance promotes that exact media file; downstream may
package or upload it without changing audible or visible content. Any required
re-render with materially different audio, subtitle pixels, timing, or render
profile becomes a new Candidate and returns to audiovisual review.

## 10. User interface

Subtitle Workspace has a separate minimal shell. Existing redraw, Markdown,
boundary, HyperFrames, and static bundle workflows keep the current v2 UI.

### Content review layout

- fixed video player on the left;
- Cue list and inline Chinese editor on the right;
- selected Cue exposes split, same-Block merge, and needs-modification controls;
- Block, search/replace, metadata, agent instruction, and technical data stay in
  a secondary drawer;
- no persistent third column.

Clicking a Cue seeks to its start. Playback highlights and follows the active
Cue. Starting text input pauses playback. Keyboard controls are:

| Key | Action |
|---|---|
| Space | play/pause outside an editor |
| Up / Down | previous/next Cue |
| M | toggle needs modification |
| Cmd/Ctrl+Z | undo own latest operation |
| Cmd/Ctrl+Shift+Z | redo |
| Cmd/Ctrl+Enter | split at cursor |

Only Space and M remain visibly hinted; the rest live in shortcut help.

The toolbar shows `needs changes N`, `direct edits N`, and synchronization
state. It never shows `approved 0/N` or a completion percentage.

### Audiovisual layout

The video and changed-range navigation lead. Block audio, actual duration,
alignment/timing warnings, issue categories, and Candidate comparison are
visible only in this stage.

### Device support

Latest Chrome, Edge, and Safari support full editing on desktop and tablet.
Phones are read-only. The homepage lists recent Workspaces and also accepts a
direct Workspace link.

## 11. Collaboration

One always-on FrameCue host owns the database. Tailnet clients collaborate in
real time using a display name and browser session ID; duplicate names receive
a short disambiguating suffix.

This is Cue-granular collaboration, not character-level CRDT:

- edits to different Cues merge automatically;
- a Cue being edited is held by a 15-second lock renewed every 5 seconds and
  shows its editor;
- split/merge briefly locks every affected Cue;
- another client cannot edit a locked Cue;
- current participants and selected Cues are visible;
- disconnected clients become read-only until reconnected;
- agent results wait while an affected Cue is locked.

The server uses the existing HTTP runtime plus browser-native event streaming;
no collaboration framework is required. Presence and locks are ephemeral.
Draft snapshots, operations, authors, issues, lead transfer, and revision events
are durable.

A disconnected session expires after 20 seconds. Its server-visible dirty flag
then clears, its locks expire, and any input the server never acknowledged is
discarded as described in section 6.

One participant is the lead reviewer. Only the lead can complete a round. The
role can be transferred; if the lead disconnects, another participant can take
over through compare-and-swap against the previous lead session and current
draft version; the audit records it. Completion is blocked while any connected
participant reports dirty state or an affected Cue remains locked.

There is no formal range assignment. Participants work freely and active-Cue
visibility prevents accidental overlap.

## 12. Persistence

SQLite remains internal to FrameCue. The minimum durable records are:

| Record | Purpose |
|---|---|
| Workspace | identity, current stage/revision, timing profile, lead reviewer |
| Draft | complete current document snapshot, draft version, issues |
| Revision | immutable complete document, parent, kind, checksum, asset manifest |
| Work Order | base revision/draft version, operation, targets, status, request/Candidate, lease owner/expiry |
| Activity | author, operation summary, affected IDs, before/after checksums, timestamp |
| Agent token | hashed credential, label, permissions, revoked state |

Cues and Blocks remain inside complete document JSON snapshots. The Workspace
module applies validated operations transactionally and emits updated versions;
callers do not manipulate JSON or rows independently.

Activity stores meaningful operations, not keystrokes, cursor movements, or
presence heartbeats. Immutable revisions keep a final diff summary and author
attribution.

The Subtitle Document also carries an immutable AgenticDub glossary snapshot:
protected display terms, approved speech mappings, and the source artifact
SHA-256. FrameCue uses this snapshot for content invariants and mechanical
auto-apply classification; it does not invent or maintain a second glossary.

## 13. Contracts

Static `framecue_package_v2` and `framecue_review_result_v1` stay frozen. The
Workspace evolution receives new schema versions instead of weakening legacy
validators:

- `framecue_subtitle_document_v2`;
- `framecue_work_order_v2`;
- `framecue_candidate_revision_v2`;
- `framecue_workspace_snapshot_v1` for portable export/import.

### Subtitle Document v2

It contains Workspace/revision identity, source checksum, timing profile,
the checksum-bound glossary snapshot, complete Cues and Blocks, and a
checksum-bound asset manifest. Blocks store `target_text`, authoritative
`speech_text`, ordered `cue_ids`, and their content checksum. A Cue separates:

- immutable source timing/text;
- provisional or aligned output timing;
- `display_text`;
- `speech_text`;
- `block_id`;
- `origin_cue_ids` and structural lineage.

### Work Order v2

It contains request identity, operation, exact base revision or draft version,
base checksum, immutable target records, expected before-values, context,
instructions, required outputs, glossary checksum, and timing/voice policy. One
review round creates one Work Order with multiple ranges. Each target record has
`range_id`, exact Cue/Block IDs, source span, lineage anchors, allowed
operations/fields, before-checksum, and read-only context.

Operations are limited to:

- `content_correction_review`;
- `realize_voice_timeline`;
- `audiovisual_correction`.

### Candidate Revision v2

It contains the full proposed document, independently hash-bound
`change_proposals`, validation results, and SHA-256-bound assets. Each proposal
contains `proposal_id`, `range_id`, base before-checksum, replacement document
slice, changed fields/IDs, dependency proposal IDs, and evidence references. A
patch may drive the UI diff but is never the truth source. The authoritative
next draft is always recomposed from the frozen base plus accepted proposals.

Submission fails closed for wrong request/base identity, unauthorized ranges,
stale before-values, invalid Cue/Block lineage, overlapping output ranges,
missing evidence, mismatched hashes, or content changes outside the operation's
allowed fields.

Operation rules are fixed:

| Operation | Allowed document changes | Required evidence |
|---|---|---|
| `content_correction_review` | target-range display/speech text, target Block meaning, authorized split/merge lineage | full document, proposals, glossary checksum, content/lineage validation; no audio evidence |
| `realize_voice_timeline` | output timing and media references only; wording, Block meaning, structure, and source fields immutable | complete Block audio, word alignment, timing audit, subtitle artifact, review render, and every SHA-256 |
| `audiovisual_correction` | authorized pronunciation/voice/timing and display segmentation only; semantic text immutable | new evidence for changed ranges, retained approved hashes for unchanged ranges, complete subtitle artifact and review render |

FrameCue validates each proposal independently, then validates the recomposed
full document and asset graph. A failed proposal is never partially written.

### Portable Workspace snapshot

Export includes the complete content state, immutable revisions, inert copies of
open Work Orders, installed-schema version requirements, and a manifest of
included assets. Every asset is contained by a relative path and hash. Export
excludes agent credentials, browser sessions, presence, locks, dirty state, and
lead authority; it never treats snapshot-bundled schemas or external asset
references as trusted input.

Import validates with schemas installed on the receiving host, rejects absolute
or escaping paths and identity collisions, rehashes every asset, and revalidates
every revision and Work Order before one transaction commits. Import-as-copy
mints a new Workspace ID, request IDs, and local credentials, resets imported
open Work Orders to pending with no claim, and retains source revision lineage.
Explicit restore-in-place is allowed only when the target Workspace is absent
and the backup identity/checksum match; it also mints credentials and clears
claims. Final subtitle-only exports remain available separately.

## 14. Agent interface and security

AgenticDub obtains pending Work Orders and submits Candidates through the
Workspace HTTP interface. FrameCue does not launch AgenticDub. When no worker is
online, orders remain pending and resume when a worker returns.

Obtaining work atomically changes `pending → processing` under a five-minute
renewable worker lease. A second worker cannot claim it. Lease expiry returns a
retryable order to pending. `fail` records a classified error; `retry` keeps the
same base checksum but creates a new request ID; `cancel` is lead-only and cannot
cancel an accepted Candidate. Sequential correction rounds may use the same
operation with new draft versions and request IDs. `rewrite_required` follows
the content transition defined in section 4 and is never lease-retried.

Browser access remains tailnet-only. Agent writes require both tailnet access
and a revocable per-agent token. Tokens are stored hashed, are never placed in
URLs, and carry an explicit Workspace allowlist plus least-privilege
`list/claim/read/submit/fail/retry` permissions. Work Order reads and writes both
require the token. Browser CSRF and same-origin protections remain separate from
agent authentication.

The UI exposes only `waiting`, `processing`, `waiting for human`, `failed`, and
`complete` plus a short error. Full AgenticDub logs stay outside FrameCue.

JSON download/import remains the recovery and cross-machine path. The normal
same-host or tailnet path requires no manual download.

After cutover, a failed in-flight task can be restored from a validated
Workspace backup or exported as a finishable portable Work Order. The old UI is
read-only evidence, not the recovery mechanism.

## 15. Timing and invalidation

AgenticDub applies the existing production timing order:

1. trim TTS head/tail silence;
2. apply the selected timing profile and permitted inter-Block silence;
3. fit with acceleration capped at `1.20x`;
4. return `rewrite_required` rather than trim speech that cannot fit;
5. extend only the final fading tail under the existing bounded policy;
6. produce trusted word alignment before final timing acceptance.

| Change | Invalidated output |
|---|---|
| display-only text or line break | subtitle review/render for affected range |
| `speech_text` | TTS, alignment, timing, audiovisual review |
| pronunciation, voice, or speed | TTS, alignment, timing, audiovisual review |
| split/merge or Block boundary | affected projection, TTS, alignment, timing |
| aligned timing | audiovisual review and render |
| source content | every downstream artifact for the affected lineage |

Invalidation expands forward only until timing becomes stable again. Any change
after `final_approved` starts a new `content_review` draft from the approved
Revision; the Revision itself remains immutable.

## 16. Compatibility and cutover

- Existing static redraw, Markdown, boundary, HyperFrames, v1, and v2 bundles
  remain usable.
- Subtitle Workspace is a separate application route backed by the same
  FrameCue repository and host.
- Existing subtitle bundles can be explicitly imported once. Import records
  lineage and leaves the source unchanged.
- During development the current FrameCue route remains available. The new
  route becomes an acceptance build only after every feature is implemented;
  it becomes the default only after final human UAT passes.
- After cutover, the old subtitle UI remains read-only as an emergency fallback
  and receives no new features.
- No new repository, cloud database, shared SQLite file, project-management
  suite, plugin registry, waveform editor, or generic executor framework is
  introduced.

## 17. Internal implementation sequence

These are engineering checkpoints, not partial user handoffs.

### A. Freeze contracts and transition model

- Add Workspace v2 schema fixtures and negative validation cases.
- Encode reverse approval, correction loops, stale Candidate rules, and
  immutable revision transitions through one Workspace module interface.
- Freeze stable range/proposal identity, the per-operation allowed-field/evidence
  matrix, Block/Cue/speech invariants, and import validation.
- Preserve the current Workspace v1 tests as legacy compatibility coverage.

Exit: every allowed transition succeeds through the interface; stale identity,
range, lineage, or evidence fails before state changes.

### B. Mutable draft operations

- Reuse the existing Svelte editor and Python/SQLite runtime.
- Add server-owned edit/split/merge/flag operations, provisional split timing,
  operation audit, autosave, and own-operation Undo/Redo.
- Replace positive Cue review state only inside Subtitle Workspace.

Exit: reload restores the same draft; edit/split/merge round-trip without content
loss; immutable revisions remain unchanged.

### C. Collaboration

- Add display-name sessions, presence stream, Cue locks, lead transfer, and
  synchronization completion guard using the existing server and browser
  primitives.
- Verify automatic merge across distinct Cues and rejection of locked/stale
  same-Cue operations.
- Add atomic Work Order claim/lease/retry and all-or-nothing multi-Cue edits.

Exit: two browsers can edit distinct Cues, observe each other, transfer lead,
and complete one consistent round without last-writer data loss.

### D. Content agent loop

- Emit one scoped Work Order for edits/issues.
- Add deterministic mechanical auto-apply, semantic diff confirmation,
  per-range resolution, and stale result handling.
- Bind the existing glossary snapshot and keep completed-round snapshots
  read-only while AgenticDub works.
- Automatically create Content Revision only when no unresolved decision
  remains.

Exit: a real reviewed draft reaches one immutable Content Revision without JSON
download, and an old Candidate cannot overwrite a newer edit.

### E. Voice realization and audiovisual review

- Adapt the existing AgenticDub realization path instead of rebuilding TTS or
  alignment.
- Require full Candidate media and hash-bound audio/alignment/timing evidence.
- Add stage-specific issue marking, changed-range navigation, partial acceptance,
  and scoped correction loops.
- Route audiovisual text/translation changes and `rewrite_required` through a
  new content gate before generating replacement speech.
- Promote the exact approved review render as the production master.

Exit: the same real task reaches `final_approved`; every unmarked range is
accepted, every marked range is resolved, and no speech is hard-trimmed.

### F. Portability, compatibility, and full handoff

- Add complete Workspace snapshot export/import and recent-Workspace home.
- Keep current static and old subtitle routes available as specified.
- Run automated and browser preflight, deliver one complete acceptance build for
  a real HITL UAT, update FrameCue/AgenticDub ImNotes and global/project skills,
  then switch the default subtitle route only after UAT passes.

Exit: every automated, browser, compatibility, and security preflight passes
before the user receives the complete acceptance build; production cutover
waits for the real HITL UAT.

## 18. Acceptance criteria

Acceptance has four independently checkable gates.

### Automated contract and state gate

- content edit, split, same-Block merge, reverse issue marking, autosave, reload,
  search/replace, and 100-operation own-history behavior;
- two concurrent reviewers editing separate Cues, same-Cue locking, presence,
  lead transfer, duplicate-issue merge, and unsynchronized-completion blocking;
- human edits and issues submitted in one Work Order without JSON download;
- deterministic mechanical auto-apply, semantic human confirmation, per-range
  correction, and stale Candidate rejection;
- immutable Content Revision followed by AgenticDub voice/audio/alignment/timing
  evidence and full Candidate media;
- audiovisual reverse review, multi-category flags, text-triggered scoped TTS
  invalidation, partial correction acceptance, and final approval;
- recovery after browser reload, FrameCue restart, AgenticDub absence/failure,
  and Candidate resubmission;
- portable snapshot export/import to a new Workspace without changing the source;
- tailnet-only browser access, valid agent-token enforcement, CSRF/origin checks,
  ranged media HTTP 206, and malicious import rejection.

### Browser matrix gate

- latest Chrome, Edge, and Safari complete the desktop/tablet interaction suite;
- phone remains read-only;
- two real browser sessions prove dirty-state blocking, Cue locks, presence, and
  lead transfer.

### Compatibility gate

- static v1/v2 and non-subtitle FrameCue workflows remain unchanged;
- old subtitle UI remains read-only and can open historical evidence;
- one portable export imports as a new, re-identified Workspace with no carried
  credential or claim authority.

### Real HITL UAT gate

One still-active HITL task uses the complete acceptance build to demonstrate at
least one content edit, split or merge, semantic Candidate decision,
audiovisual correction, interruption/resume, and final approval. Synthetic and
browser suites prove the remaining failure matrix. The UAT also proves:

- AgenticDub downstream can consume only `final_approved` and continues through
  existing Final QC and pre-publication gates.

The acceptance build is delivered only after the first three gates pass. The
real HITL UAT is the final release and cutover gate, not a partial-feature
handoff.

## 19. Explicit non-goals

- Character-level CRDT, offline editing, comments, formal review assignments, or
  an account/password system.
- Free Cue creation/deletion, arbitrary timeline selection, timing drag, or
  waveform editing.
- Direct SQLite access from AgenticDub or a shared SQLite file across machines.
- Rewriting AgenticDub providers, renderer, Final QC, metadata, thumbnail, or
  publishing flows.
- Refactoring non-subtitle FrameCue workflows.
- Automatically starting Codex, Claude, Grok, or AgenticDub from the browser.
- A generic job queue, executor registry, plugin protocol, or cloud sync layer.

## 20. Rollback

The new route, schemas, and SQLite records are additive. Existing bundles and
the current subtitle route remain unchanged during implementation. Before
cutover, failure returns the task to the existing exported-result workflow.
After cutover, recovery uses validated restore-in-place or a portable Work Order;
the old subtitle route remains read-only evidence. No rollback step rewrites an
immutable revision or source bundle, imports live authority, or bypasses the
current host validators.
