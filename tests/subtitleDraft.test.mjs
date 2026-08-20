import assert from "node:assert/strict";
import test from "node:test";

import {
  createDraftState,
  editCueDisplayText,
  editCueSpeechText,
  splitCueAtCursor,
  mergeAdjacentCues,
  toggleIssueRange,
  searchReplace,
  undoDraftOperation,
  redoDraftOperation
} from "../src/lib/subtitleDraft.js";

const document = {
  schema: "framecue_subtitle_document_v2",
  workspace_id: "workspace-1",
  cues: [
    {
      id: "c1",
      source_start_ms: 0,
      source_end_ms: 1000,
      output_start_ms: null,
      output_end_ms: null,
      source_text: "hello world",
      display_text: "你好世界",
      speech_text: "你好世界",
      speech_linked: true,
      block_id: "b1"
    },
    {
      id: "c2",
      source_start_ms: 1000,
      source_end_ms: 2000,
      output_start_ms: null,
      output_end_ms: null,
      source_text: "again",
      display_text: "再次確認",
      speech_text: "再次確認",
      speech_linked: true,
      block_id: "b1"
    },
    {
      id: "c3",
      source_start_ms: 2000,
      source_end_ms: 3000,
      source_text: "separate",
      display_text: "獨立文字",
      speech_text: "獨立語音",
      speech_linked: false,
      block_id: "b2"
    }
  ],
  blocks: [
    { id: "b1", cue_ids: ["c1", "c2"], target_text: "你好世界 再次確認", speech_text: "你好世界再次確認" },
    { id: "b2", cue_ids: ["c3"], target_text: "獨立文字", speech_text: "獨立語音" }
  ]
};

test("creates a complete draft state and edits linked or separated speech correctly", () => {
  const state = createDraftState(document, { authorId: "alice" });
  assert.equal(state.version, 0);
  assert.equal(state.directEditCount, 0);
  assert.deepEqual(state.issues, []);
  assert.deepEqual(state.document, document);

  const linked = editCueDisplayText(state, "c1", "你好，世界！", { expectedVersion: 0 });
  assert.equal(linked.document.cues[0].display_text, "你好，世界！");
  assert.equal(linked.document.cues[0].speech_text, "你好，世界！");
  assert.equal(linked.document.blocks[0].target_text, "你好，世界！ 再次確認");
  assert.equal(linked.document.blocks[0].speech_text, "你好，世界！再次確認");

  const separated = editCueDisplayText(linked, "c3", "獨立顯示", { expectedVersion: 1 });
  assert.equal(separated.document.cues[2].speech_text, "獨立語音");
  assert.equal(separated.document.blocks[1].speech_text, "獨立語音");
  assert.equal(separated.directEditCount, 2);
});

test("splits at a trusted word boundary and preserves lineage with injected IDs", () => {
  const source = structuredClone(document);
  source.cues[0].word_timestamps = [
    { word: "你好", start_ms: 0, end_ms: 400 },
    { word: "世界", start_ms: 400, end_ms: 1000 }
  ];
  source.cues[0].word_timestamps_trusted = true;
  const state = createDraftState(source);
  const split = splitCueAtCursor(state, "c1", 3, {
    idFactory: (() => {
      const ids = ["new-left", "new-right"];
      return () => ids.shift();
    })()
  });

  assert.deepEqual(split.document.cues.map((cue) => cue.id), ["new-left", "new-right", "c2", "c3"]);
  assert.deepEqual(split.document.cues[0].origin_cue_ids, ["c1"]);
  assert.deepEqual(split.document.cues[1].origin_cue_ids, ["c1"]);
  assert.equal(split.document.cues[0].display_text, "你好");
  assert.equal(split.document.cues[1].display_text, "世界");
  assert.equal(split.document.cues[0].source_end_ms, 400);
  assert.equal(split.document.cues[1].source_start_ms, 400);
  assert.equal(split.document.cues[0].output_end_ms, 400);
  assert.equal(split.document.cues[1].output_start_ms, 400);
  assert.equal(split.document.cues[0].provisional, false);
  assert.deepEqual(split.document.blocks[0].cue_ids, ["new-left", "new-right", "c2"]);
});

test("proportionally splits timing and marks both derived cues provisional without words", () => {
  const state = createDraftState(document);
  const split = splitCueAtCursor(state, "c1", 2, {
    idFactory: (() => {
      const ids = ["left", "right"];
      return () => ids.shift();
    })()
  });
  assert.equal(split.document.cues[0].display_text, "你好");
  assert.equal(split.document.cues[1].display_text, "世界");
  assert.equal(split.document.cues[0].source_end_ms, 500);
  assert.equal(split.document.cues[1].source_start_ms, 500);
  assert.equal(split.document.cues[0].provisional, true);
  assert.equal(split.document.cues[1].provisional, true);
});

test("merges only adjacent cues from the same block and recomputes projections", () => {
  const state = createDraftState(document);
  const merged = mergeAdjacentCues(state, "c1", "c2", {
    idFactory: () => "merged"
  });
  assert.deepEqual(merged.document.cues.map((cue) => cue.id), ["merged", "c3"]);
  assert.deepEqual(merged.document.cues[0].origin_cue_ids, ["c1", "c2"]);
  assert.equal(merged.document.cues[0].display_text, "你好世界 再次確認");
  assert.equal(merged.document.blocks[0].cue_ids[0], "merged");
  assert.equal(merged.document.blocks[0].target_text, "你好世界 再次確認");
  assert.equal(merged.document.blocks[0].speech_text, "你好世界再次確認");
  assert.throws(() => mergeAdjacentCues(state, "c1", "c3", { idFactory: () => "nope" }), /same block|adjacent/);
});

test("toggles one contiguous issue range while merging authors, categories, and notes", () => {
  const state = createDraftState(document);
  const first = toggleIssueRange(state, ["c1", "c2"], {
    author: "alice",
    category: "translation",
    note: "請確認術語"
  });
  const merged = toggleIssueRange(first, ["c2", "c1"], {
    author: "bob",
    categories: ["timing"],
    notes: ["語音稍快"]
  });
  assert.deepEqual(merged.issues, [{
    cue_ids: ["c1", "c2"],
    authors: ["alice", "bob"],
    categories: ["translation", "timing"],
    notes: ["請確認術語", "語音稍快"]
  }]);
  const removed = toggleIssueRange(merged, ["c1", "c2"], {
    author: "alice",
    categories: ["translation"],
    enabled: false
  });
  assert.deepEqual(removed.issues, [{
    cue_ids: ["c1", "c2"],
    authors: ["bob"],
    categories: ["timing"],
    notes: ["語音稍快"]
  }]);
  assert.deepEqual(toggleIssueRange(removed, ["c1", "c2"], { enabled: false }).issues, []);
  assert.throws(() => toggleIssueRange(state, ["c1", "c3"], { category: "other" }), /contiguous/);
});

test("search and replace is one undoable operation and stale versions fail closed", () => {
  const state = createDraftState(document, { authorId: "alice" });
  const replaced = searchReplace(state, "再次", "重新", { expectedVersion: 0 });
  assert.equal(replaced.document.cues[1].display_text, "重新確認");
  assert.equal(replaced.document.cues[1].speech_text, "重新確認");
  assert.equal(replaced.history.length, 1);
  assert.equal(replaced.directEditCount, 1);
  assert.throws(
    () => editCueDisplayText(replaced, "c1", "不應寫入", { expectedVersion: 0 }),
    /stale draft version/
  );

  const undone = undoDraftOperation(replaced, { authorId: "alice", expectedVersion: 1 });
  assert.equal(undone.document.cues[1].display_text, "再次確認");
  assert.equal(undone.history.length, 0);
  assert.equal(undone.redo.length, 1);
  const redone = redoDraftOperation(undone, { authorId: "alice", expectedVersion: 2 });
  assert.equal(redone.document.cues[1].display_text, "重新確認");
  assert.equal(redone.history.length, 1);
});

test("undo only targets the latest operation authored by the current user and keeps 100 entries", () => {
  let state = createDraftState(document, { authorId: "alice" });
  for (let index = 0; index < 101; index += 1) {
    state = editCueDisplayText(state, "c1", `文字${index}`, { authorId: "alice" });
  }
  assert.equal(state.history.length, 100);
  assert.throws(() => undoDraftOperation(state, { authorId: "bob" }), /own operation/);
  const undone = undoDraftOperation(state, { authorId: "alice" });
  assert.equal(undone.document.cues[0].display_text, "文字99");
});
