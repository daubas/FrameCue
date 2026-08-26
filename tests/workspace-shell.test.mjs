import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { nearestRemainingCueId, resolveStructuralCueId } from "../src/lib/workspace.js";

test("structural result selection ignores a concurrent reload fallback", () => {
  const merged = [
    { id: "c1" },
    { id: "c10", lineage: { parent_cue_ids: ["c10", "c11"] } },
    { id: "c12" }
  ];
  assert.equal(resolveStructuralCueId(
    merged,
    { kind: "merge", cue_id: "c10", adjacent_cue_id: "c11" },
    "c11"
  ), "c10");

  const split = [
    { id: "c1" },
    { id: "c5a", lineage: { parent_cue_ids: ["c5"] } },
    { id: "c5b", lineage: { parent_cue_ids: ["c5"] } }
  ];
  assert.equal(resolveStructuralCueId(split, { kind: "split", cue_id: "c5" }, "c5"), "c5b");
  assert.equal(resolveStructuralCueId(merged, { kind: "block_merge" }, "c12"), "c12");
  assert.equal(nearestRemainingCueId([{ id: "c1" }, { id: "c3" }], 1), "c3");
  assert.equal(resolveStructuralCueId(
    [{ id: "c1" }, { id: "c3" }],
    { kind: "delete", selection_index: 2 },
    "c2"
  ), "c3");
});

test("Workspace v2 has its own reverse-approval shell while static pages keep App", () => {
  const main = readFileSync(new URL("../src/main.js", import.meta.url), "utf8");
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(main, /loadWorkspaceSnapshot/);
  assert.match(main, /snapshot \? SubtitleWorkspace : App/);
  assert.match(shell, /MediaStage/);
  assert.match(shell, /需修改/);
  assert.match(shell, /直接修改/);
  assert.match(shell, /完成本輪/);
  assert.match(shell, /submitWorkspaceOperation/);
  assert.match(shell, /completeWorkspaceRound/);
  assert.match(shell, /kind: "presence"/);
  assert.match(shell, /kind: "lock"/);
  assert.match(shell, /kind: "unlock"/);
  assert.match(shell, /kind: "dirty", dirty: true/);
  assert.match(shell, /800/);
  assert.match(shell, /5000/);
  assert.match(shell, /lead_session_id === snapshot\.session_id/);
  assert.match(shell, /lead_active/);
  assert.match(shell, /kind: "lead"/);
  assert.match(shell, /接手 lead/);
  assert.match(shell, /issue\.authors\?\.includes\(snapshot\.display_name/);
  assert.match(shell, /等待 .*lead/i);
  assert.match(shell, /aria-label="影片字幕時間軸"/);
  assert.match(shell, /onPlaybackTime/);
  assert.match(shell, /timing_state/);
  assert.match(shell, /來源時間.*配音未對齊/);
  assert.doesNotMatch(shell, /已審|reviewed_cues|完成百分比/);
});

test("Workspace exposes Block-aware, reasoned structural controls and keyboard fallbacks", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");
  const mediaStage = readFileSync(new URL("../src/components/MediaStage.svelte", import.meta.url), "utf8");

  assert.match(shell, /class="block-group"/);
  assert.match(shell, /Block \{String\(groupIndex \+ 1\)\.padStart/);
  assert.match(shell, /Cue · 時長/);
  assert.match(shell, /blockTimingState/);
  assert.match(shell, /blockReviewState/);
  assert.match(shell, /<summary>更多操作<\/summary>/);
  assert.match(shell, /跨區塊：合併字幕與區塊/);
  assert.match(shell, /同區塊：與\$\{direction\}合併/);
  assert.match(shell, /previousMergeReason/);
  assert.match(shell, /nextMergeReason/);
  assert.match(shell, /class="action-reason"/);
  assert.match(shell, /event\.key === "Enter" && \(event\.metaKey \|\| event\.ctrlKey\)/);
  assert.match(shell, /event\.key === "Backspace" && event\.target\.selectionStart === 0/);
  assert.match(shell, /event\.key === "Delete" && event\.target\.selectionStart === event\.target\.value\.length/);
  assert.match(shell, /event\.key\.toLowerCase\(\) === "m"/);
  assert.match(shell, /const mShortcutAllowed = \(!interactive \|\| event\.target\?\.closest\?\.\("\.cue-row-select"\)\)\s*&& !event\.target\?\.closest\?\.\("details"\)/);
  assert.match(shell, /if \(mShortcutAllowed && event\.key\.toLowerCase\(\) === "m" && canReview\)/);
  assert.match(shell, /event\.isComposing \|\| event\.keyCode === 229/);
  assert.match(shell, /正在修改相鄰 Cue/);
  assert.match(shell, /splitAvailabilityReason/);
  assert.match(shell, /splitAvailabilityReason\(selectedCue, editReason, caretStart, caretEnd, editText\)/);
  assert.match(shell, /framecue:toggle-playback/);
  assert.match(shell, /\["ArrowUp", "ArrowDown"\]/);
  assert.doesNotMatch(shell, /!interactive && \["ArrowUp", "ArrowDown"\]/);
  assert.match(shell, /const cueNavigationAllowed = !interactive \|\| Boolean\(event\.target\?\.closest\?\.\("\.cue-row-select"\)\)/);
  assert.match(shell, /if \(!editingCueId && cueNavigationAllowed && \["ArrowUp", "ArrowDown"\]\.includes\(event\.key\)/);
  assert.doesNotMatch(shell, /if \(!editingCueId && \["ArrowUp", "ArrowDown"\]\.includes\(event\.key\)/);
  assert.match(shell, /pendingNavigationIndex >= 0 \? pendingNavigationIndex : selectedCueIndex/);
  assert.match(shell, /pendingNavigationIndex = nextIndex;[\s\S]*?selectedCueId = cues\[nextIndex\]\.id/);
  assert.match(shell, /flushCueNavigation\(\)/);
  assert.doesNotMatch(shell, /selectCue\(cues\[nextIndex\]\.id, \{ center: true \}\)/);
  assert.match(shell, /scrollIntoView\(\{ behavior: "smooth", block: "center" \}\)/);
  assert.match(shell, /on:dblclick=\{\(\) => enterEditMode\(cue\.id\)\}/);
  assert.match(shell, /\{#if editingCueId === cue\.id\}[\s\S]*?<textarea/);
  assert.match(shell, /event\.key === "ArrowLeft" && event\.target\.selectionStart === 0/);
  assert.match(shell, /event\.key === "ArrowRight" && event\.target\.selectionStart === event\.target\.value\.length/);
  assert.match(shell, /enterEditMode\(previousMergeCue\.id, "end"\)/);
  assert.match(shell, /enterEditMode\(nextMergeCue\.id, "start"\)/);
  assert.match(shell, /event\.key === "Escape"[\s\S]*?exitEditMode\(\)/);
  assert.match(shell, /class="cue-inline-editor"/);
  assert.match(shell, /class="cue-row-select"/);
  assert.doesNotMatch(shell, /class="cue-editor"/);
  assert.match(shell, /function followPlaybackCue/);
  assert.match(shell, /document\.activeElement === editor \|\| heldCueIds\.length \|\| localDirty/);
  assert.match(mediaStage, /export let subtitleOnlyVideo = false/);
  assert.match(mediaStage, /subtitleVideoOnly/);
  assert.match(mediaStage, /\{#if availableModes\.length > 1\}/);
  assert.doesNotMatch(shell, /draggable|dragstart|dragover/);
});

test("Workspace keeps Cue, Block, and Agent interactions visibly distinct", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /event\.key === "Enter" && !event\.metaKey && !event\.ctrlKey/);
  assert.match(shell, /insertCueLineBreak/);
  assert.match(shell, /resolveStructuralCueId\(changed\.document\.cues, operation, selectedBefore\)/);
  assert.match(shell, /focus\(\{ preventScroll: true \}\)/);
  assert.match(shell, /cueList\.scrollTop \+= delta/);
  assert.match(shell, /if \(busy\) \{\s*reloadQueued = true;/);
  assert.match(shell, /event\.key === "Enter" && \(event\.metaKey \|\| event\.ctrlKey\)/);
  assert.match(shell, /從游標切成兩句/);
  assert.match(shell, /class="cue-meta"/);
  assert.match(shell, /Block \{String\(blockNumberById\.get\(cue\.block_id\)/);
  assert.match(shell, /待 Agent 修改/);
  assert.match(shell, /交給 Agent/);
  assert.match(shell, /class="cue-editing-bar"/);
  assert.match(shell, /class="agent-inline"/);
  assert.doesNotMatch(shell, /<details class="agent-inline" open/);
  assert.match(shell, /class="agent-recorded"/);
  assert.match(shell, /已記錄給 Agent/);
  assert.match(shell, /issue\.notes/);
  assert.match(shell, /class="block-rail-handle merge"/);
  assert.match(shell, /class="block-rail-handle split"/);
  assert.match(shell, /\+<span>合併上方 Block<\/span>/);
  assert.match(shell, /−<span>從這裡切開 Block<\/span>/);
  assert.match(shell, /mergeBlockBoundary/);
  assert.match(shell, /splitBlockBoundary/);
  assert.match(shell, /blockNumberById/);
  assert.match(shell, /snapshot\.stage !== "content_review"/);
  assert.match(shell, /class:content-review/);
  assert.match(shell, /\.block-rail-handle \{[^}]*opacity: 0/);
  assert.match(shell, /\.cue-actions \{ position: absolute/);
  assert.doesNotMatch(shell, /class="agent-request"|<summary>Block 操作<\/summary>/);
  assert.match(shell, /name="agent-category"/);
  assert.match(shell, /name="agent-note"/);
  assert.match(shell, /async function saveAgentRequest\(\)[\s\S]*?await leaveEditor\(\);[\s\S]*?kind: "flag"/);
  assert.match(shell, /修改工作單已建立，等待 Agent 接手/);
  assert.match(shell, /aria-describedby=\{previousBlockMergeReason/);
  assert.match(shell, /kind: "block_merge"/);
  assert.match(shell, /kind: "block_split"/);
});

test("Workspace renders non-destructive Agent suggestion states and decisions", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  for (const status of ["queued", "processing", "ready", "stale", "failed"]) {
    assert.match(shell, new RegExp(status));
  }
  assert.match(shell, /snapshot\.suggestions/);
  assert.match(shell, /修改前/);
  assert.match(shell, /修改後/);
  assert.match(shell, /kind: `suggestion_\$\{decision\}`/);
  assert.match(shell, /job_id: suggestion\.job_id/);
  assert.match(shell, /on:click=\{\(\) => decideAgentSuggestion\(suggestion, "apply"\)\}/);
  assert.match(shell, /on:click=\{\(\) => decideAgentSuggestion\(suggestion, "ignore"\)\}/);
  assert.match(shell, /min-height: 44px/);
  assert.match(shell, /overflow-x: hidden/);
  assert.match(shell, /proposal\.text/);
  assert.match(shell, /尚未送出給 Agent/);
  assert.match(shell, /function selectedAgentJob/);
  assert.match(shell, /送出給 Agent/);
});

test("Workspace keeps the Agent prompt in Cue flow instead of covering subtitles", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /\.cue-editing-bar \{[^}]*flex-wrap: wrap/);
  assert.match(shell, /\.agent-inline\[open\] \{[^}]*flex: 1 0 100%/);
  assert.match(shell, /\.agent-controls \{[^}]*position: static/);
  assert.doesNotMatch(shell, /\.agent-controls \{[^}]*position: absolute/);
});

test("Workspace ignores an older async reload after a newer Agent status", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /if \(next\.snapshot_version < snapshot\.snapshot_version\) return;/);
  assert.match(shell, /if \(next\.snapshot_version < snapshot\.snapshot_version\) return;[\s\S]*?snapshot = next;/);
});

test("Workspace exposes a confirmed whole-Cue delete and selects a survivor", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /kind: "delete"/);
  assert.match(shell, /刪除整個 Cue/);
  assert.match(shell, /window\.confirm/);
  assert.match(shell, /nearestRemainingCueId/);
  assert.match(shell, /<details class="cue-actions"[\s\S]*?on:click=\{deleteCue\}[\s\S]*?<\/details>/);
  assert.match(shell, /Shift\+Delete 刪除整句/);
  assert.match(shell, /event\.shiftKey && \["Delete", "Backspace"\]\.includes\(event\.key\)/);
  assert.match(shell, /deleteShortcutAllowed/);
});

test("Workspace keeps phone editing read-only while allowing review decisions", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /reviewActionReason/);
  assert.match(shell, /\$:\s*canReview = !reviewActionReason/);
  assert.match(shell, /\$:\s*editReason = snapshot\.stage !== "content_review"/);
  assert.match(shell, /"配音審查只標記需修改，字幕結構維持唯讀。"/);
  assert.match(shell, /\$:\s*flagReason = !selectedCue \? "沒有可標記的 Cue。" : reviewActionReason/);
  assert.match(shell, /\["flag"\]\.includes\(operation\?\.kind\) \? canReview/);
  assert.match(shell, /\["suggestion_apply", "suggestion_ignore"\]\.includes\(operation\?\.kind\)/);
  assert.match(shell, /\$:\s*canDecideAgentSuggestion = connected/);
  assert.doesNotMatch(shell, /canDecideAgentSuggestion = !phone/);
});

test("Workspace preserves native arrow keys and enlarges Agent touch targets", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /@media \(max-width: 600px\), \(pointer: coarse\)/);
  assert.match(shell, /\.agent-inline summary,[\s\S]*?\.agent-controls button,[\s\S]*?\.cue-delete \{ min-height: 44px; \}/);
  assert.match(shell, /\.agent-inline summary \{ display: flex; align-items: center; \}/);
  assert.match(shell, /cueNavigationAllowed/);
  assert.match(shell, /Boolean\(event\.target\?\.closest\?\.\("\.cue-row-select"\)\)/);
});

test("Workspace displays the latest Agent note and preserves blank updates", () => {
  const shell = readFileSync(new URL("../src/SubtitleWorkspace.svelte", import.meta.url), "utf8");

  assert.match(shell, /let agentNoteOverrides = new Map\(\)/);
  assert.match(shell, /function latestAgentNote\(cueId, category\)/);
  assert.match(shell, /agentNoteOverrides\.has\(key\)/);
  assert.match(shell, /jobs\.at\(-1\)\.note \|\| ""/);
  assert.match(shell, /notes\.at\(-1\) \|\| ""/);
  assert.match(shell, /agentIssueSummary\(issue, noteOverride\)/);
  assert.match(shell, /latestAgentNote\(selectedCue\?\.id, issue\.category\)/);
  assert.match(shell, /nextOverrides\.set\(`\$\{selectedCue\.id\}:\$\{agentCategory\}`, agentNote\.trim\(\)\)/);
  assert.match(shell, /on:change=\{syncAgentCategoryNote\}/);
  assert.match(shell, /selectedOwnIssues\.map\(\(issue\) => agentIssueSummary\(issue\)\)/);
});
