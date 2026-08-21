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
