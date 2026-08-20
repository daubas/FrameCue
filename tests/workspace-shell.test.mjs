import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
  assert.doesNotMatch(shell, /已審|reviewed_cues|完成百分比/);
});
