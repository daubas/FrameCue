import assert from "node:assert/strict";
import test from "node:test";

import {
  blockContentIssue,
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
  assert.match(blockContentIssue(packageData, changed, "b0001"), /differs/);
  assert.equal(finalApprovalAllowed(packageData, changed), false);
  assert.equal(withBlockApproval(packageData, changed, "b0001", true), changed);
});
