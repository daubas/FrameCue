import assert from "node:assert/strict";
import test from "node:test";

import {
  blockContentIssue,
  cueIndexAtTime,
  cueNeedsSeek,
  cuePlaybackEnded,
  finalApprovalAllowed,
  withBlockApproval,
  withCueChange
} from "../src/lib/review.js";


const packageData = {
  blocks: [{ id: "b0001", cue_ids: ["c0001", "c0002"] }]
};

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
