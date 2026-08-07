import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  actionsForWorkflow,
  approveBlockAndAdvance,
  blockContentIssue,
  createDraft,
  cueIndexAtTime,
  cueNeedsSeek,
  cuePlaybackEnded,
  finalApprovalAllowed,
  makeResult,
  reviewCueAndAdvance,
  reviewedCueCount,
  withBlockApproval,
  withCueChange,
  withCueRangeAction
} from "../src/lib/review.js";


const packageData = {
  cues: [{ id: "c0001" }, { id: "c0002" }],
  blocks: [{ id: "b0001", cue_ids: ["c0001", "c0002"] }]
};

test("App wires the search replacement callback into the workbench", () => {
  const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");
  assert.match(app, /onReplaceAll=\{replaceAll\}/);
});

function approvedDraft() {
  return {
    cues: {
      c0001: { text: "OpenClaw 保留審稿" },
      c0002: { text: "確認完整版本" }
    },
    blocks: {
      b0001: {
        target_text: "OpenClaw 保留審稿 確認完整版本",
        speech_text: "OpenClaw 保留審稿。確認完整版本。",
        approved: true
      }
    },
    reviewed_cues: { c0001: true, c0002: true },
    final_approval: { approved_at: "2026-07-29T00:00:00Z" }
  };
}

test("cue edits invalidate the parent block and final approval", () => {
  const changed = withCueChange(packageData, approvedDraft(), "c0001", {
    text: "OpenClaw 改成新字幕"
  });
  assert.equal(changed.blocks.b0001.approved, false);
  assert.equal(changed.blocks.b0001.target_text, "OpenClaw 改成新字幕 確認完整版本");
  assert.equal(changed.final_approval, null);
  assert.match(blockContentIssue(packageData, changed, "b0001"), /不一致/);
  assert.equal(finalApprovalAllowed(packageData, changed), false);
  assert.equal(withBlockApproval(packageData, changed, "b0001", true), changed);
});

test("range resegment marks every selected Cue with one upstream instruction", () => {
  const changed = withCueRangeAction(packageData, approvedDraft(), ["c0001", "c0002", "missing"], "resegment", "請避免在專有名詞中斷開。");
  for (const cueId of ["c0001", "c0002"]) {
    assert.equal(changed.cues[cueId].action, "resegment");
    assert.equal(changed.cues[cueId].instruction, "請避免在專有名詞中斷開。");
    assert.equal(changed.reviewed_cues[cueId], false);
  }
  assert.equal(changed.blocks.b0001.approved, false);
  assert.equal(changed.final_approval, null);
});

test("cue-first review advances one cue and auto-approves a valid completed block", () => {
  const reviewPackage = {
    workflow: { kind: "subtitle" },
    cues: [
      { id: "c0001", text: "第一句", speech_text: "第一句。", risks: [] },
      { id: "c0002", text: "第二句", speech_text: "第二句。", risks: [] },
      { id: "c0003", text: "第三句", speech_text: "第三句。", risks: [] }
    ],
    blocks: [
      { id: "b0001", cue_ids: ["c0001", "c0002"], target_text: "第一句 第二句", speech_text: "第一句。第二句。" },
      { id: "b0002", cue_ids: ["c0003"], target_text: "第三句", speech_text: "第三句。" }
    ]
  };
  let draft = createDraft(reviewPackage);
  draft = reviewCueAndAdvance(reviewPackage, draft, "c0001");
  assert.equal(reviewedCueCount(reviewPackage, draft), 1);
  assert.equal(draft.blocks.b0001.approved, false);
  assert.equal(draft.selected_cue_id, "c0002");

  draft = reviewCueAndAdvance(reviewPackage, draft, "c0002");
  assert.equal(draft.blocks.b0001.approved, true);
  assert.equal(draft.selected_cue_id, "c0003");
  assert.equal(finalApprovalAllowed(reviewPackage, draft), false);

  draft = withCueChange(reviewPackage, draft, "c0001", { text: "修改第一句" });
  assert.equal(draft.reviewed_cues.c0001, false);
  assert.equal(draft.blocks.b0001.approved, false);
});

test("semantic block approval advances only after validation passes", () => {
  const reviewPackage = {
    blocks: [
      { id: "b0001", cue_ids: ["c0001"] },
      { id: "b0002", cue_ids: ["c0002"] }
    ]
  };
  const draft = {
    selected_block_id: "b0001",
    selected_cue_id: "c0001",
    active_scope: "block",
    cues: {
      c0001: { text: "第一句" },
      c0002: { text: "第二句" }
    },
    blocks: {
      b0001: { target_text: "第一句", speech_text: "第一句。", approved: false },
      b0002: { target_text: "第二句", speech_text: "第二句。", approved: false }
    },
    final_approval: null
  };

  const advanced = approveBlockAndAdvance(reviewPackage, draft, "b0001");
  assert.equal(advanced.blocks.b0001.approved, true);
  assert.equal(advanced.selected_block_id, "b0002");
  assert.equal(advanced.selected_cue_id, "c0002");

  const invalid = approveBlockAndAdvance(reviewPackage, {
    ...draft,
    blocks: { ...draft.blocks, b0001: { ...draft.blocks.b0001, speech_text: "不同內容" } }
  }, "b0001");
  assert.equal(invalid.blocks.b0001.approved, false);
  assert.equal(invalid.selected_block_id, "b0001");
});

test("cue playback seeks outside the cue and stops at its end", () => {
  const cue = { start_ms: 1000, end_ms: 2500 };
  assert.equal(cueNeedsSeek(cue, 800), true);
  assert.equal(cueNeedsSeek(cue, 1500), false);
  assert.equal(cueNeedsSeek(cue, 2700), true);
  assert.equal(cuePlaybackEnded(2479, cue.end_ms), false);
  assert.equal(cuePlaybackEnded(2480, cue.end_ms), true);
});

test("video time selects the current cue at each boundary", () => {
  const cues = [
    { id: "c0001", start_ms: 0, end_ms: 1000 },
    { id: "c0002", start_ms: 1000, end_ms: 2000 },
    { id: "c0003", start_ms: 2000, end_ms: 3000 }
  ];

  assert.equal(cueIndexAtTime(cues, 0), 0);
  assert.equal(cueIndexAtTime(cues, 999), 0);
  assert.equal(cueIndexAtTime(cues, 1000), 1);
  assert.equal(cueIndexAtTime(cues, 2500), 2);
});

test("new workflows expose only their own follow-up actions", () => {
  assert.deepEqual(actionsForWorkflow("subtitle").map((action) => action.value), ["use_edit", "rewrite", "resegment", "retime"]);
  assert.deepEqual(actionsForWorkflow("image_carousel").map((action) => action.value), ["use_edit", "replace_asset", "rewrite_copy", "recrop", "reorder"]);
  assert.deepEqual(actionsForWorkflow("markdown").map((action) => action.value), ["use_edit", "rewrite", "cut", "split", "needs_source"]);
});

test("cue-first drafts always open the complete cue list", () => {
  const draft = createDraft({
    workflow: { kind: "markdown" },
    cues: [{ id: "c0001", text: "Waymo", speech_text: "Waymo", risks: ["Waymo"] }],
    blocks: []
  });
  assert.equal(draft.cue_filter, "all");
});

test("new-mode browser drafts export the frozen result contract", () => {
  const packageData = {
    review_id: "cards",
    revision: "r1",
    content_checksum: "a".repeat(64),
    viewer_version: "2.4.0",
    workflow: { kind: "image_carousel" },
    cues: [{ id: "c0001", text: "slide-01.png", speech_text: "slide-01.png", risks: [] }],
    blocks: []
  };
  const draft = createDraft(packageData);
  draft.cues.c0001.action = "reorder";
  draft.cues.c0001.instruction = "整組意見寫在第一張";
  const result = makeResult(packageData, draft, "2026-08-04T00:00:00Z");
  assert.equal(result.schema_version, "framecue_review_result_v1");
  assert.equal(result.cues[0].action, "reorder");
  assert.equal(result.cues.length, 1);
});
