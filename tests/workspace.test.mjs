import assert from "node:assert/strict";
import test from "node:test";

import {
  completeWorkspaceRound,
  isWorkspaceSubmitted,
  loadWorkspaceConfig,
  loadWorkspaceSnapshot,
  openWorkspaceEvents,
  submitWorkspaceOperation,
  submitApprovedResult
} from "../src/lib/workspace.js";


const packageData = {
  review_id: "fixture-workspace",
  revision: "r1",
  content_checksum: "a".repeat(64),
  viewer_version: "2.6.0",
  workflow: { kind: "subtitle" },
  cues: [
    { id: "c0001", text: "哈囉", speech_text: "哈囉。" },
    { id: "c0002", text: "世界", speech_text: "世界。" }
  ],
  blocks: [
    {
      id: "b0001",
      cue_ids: ["c0001", "c0002"],
      target_text: "哈囉 世界",
      speech_text: "哈囉。世界。"
    }
  ]
};

const approvedDraft = {
  cues: {
    c0001: { text: "哈囉", speech_text: "哈囉。", action: "use_edit", instruction: "" },
    c0002: { text: "世界", speech_text: "世界。", action: "use_edit", instruction: "" }
  },
  blocks: {
    b0001: {
      target_text: "哈囉 世界",
      speech_text: "哈囉。世界。",
      action: "use_edit",
      instruction: "",
      approved: true
    }
  },
  reviewed_cues: { c0001: true, c0002: true },
  final_approval: { approved_at: "2026-08-20T00:00:00Z" }
};

const serverWorkspace = {
  mode: "server",
  workspace_id: "fixture-workspace",
  stage: "content_review",
  content_complete_endpoint: "/api/content-complete",
  csrf_token: "token"
};

test("marks only advanced server workspaces as already submitted", () => {
  assert.equal(isWorkspaceSubmitted(serverWorkspace), false);
  assert.equal(isWorkspaceSubmitted({ ...serverWorkspace, stage: "voice_realization_pending" }), true);
  assert.equal(isWorkspaceSubmitted({ ...serverWorkspace, stage: "audiovisual_review" }), true);
  assert.equal(isWorkspaceSubmitted({ mode: "static", stage: "voice_realization_pending" }), false);
  assert.equal(isWorkspaceSubmitted(null), false);
});

test("loads same-origin workspace configuration", async () => {
  const calls = [];
  const config = { ...serverWorkspace };
  const fetchMock = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, status: 200, json: async () => config };
  };

  const loaded = await loadWorkspaceConfig({
    fetchImpl: fetchMock,
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.deepEqual(loaded, config);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://framecue.test/api/workspace");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.cache, "no-store");
});

test("keeps static mode on a missing workspace endpoint", async () => {
  let calls = 0;
  const loaded = await loadWorkspaceConfig({
    fetchImpl: async () => {
      calls += 1;
      return { ok: false, status: 404 };
    },
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.equal(loaded, null);
  assert.equal(calls, 1);
});

test("fails clearly on workspace config errors or unsafe config", async () => {
  await assert.rejects(
    () => loadWorkspaceConfig({
      fetchImpl: async () => ({ ok: false, status: 503 }),
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /503/
  );

  await assert.rejects(
    () => loadWorkspaceConfig({
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          ...serverWorkspace,
          content_complete_endpoint: "https://evil.example/api/content-complete"
        })
      }),
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /same-origin/
  );

  await assert.rejects(
    () => loadWorkspaceConfig({
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => ({
          mode: "server",
          workspace_id: "fixture-workspace",
          stage: "content_review",
          content_complete_endpoint: "/api/content-complete"
        })
      }),
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /CSRF/
  );

  await assert.rejects(
    () => loadWorkspaceConfig({
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => ({ ...serverWorkspace, stage: undefined })
      }),
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /stage/
  );
});

test("posts a complete approved result to the workspace endpoint", async () => {
  const calls = [];
  const fetchMock = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, status: 200 };
  };

  const response = await submitApprovedResult(packageData, approvedDraft, serverWorkspace, {
    fetchImpl: fetchMock,
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.equal(response.submitted, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://framecue.test/api/content-complete");
  assert.equal(calls[0].init.method, "POST");
  const contentType = typeof calls[0].init.headers?.get === "function"
    ? calls[0].init.headers.get("content-type")
    : calls[0].init.headers?.["Content-Type"] || calls[0].init.headers?.["content-type"];
  const csrf = typeof calls[0].init.headers?.get === "function"
    ? calls[0].init.headers.get("x-framecue-csrf")
    : calls[0].init.headers?.["X-FrameCue-CSRF"] || calls[0].init.headers?.["x-framecue-csrf"];
  assert.equal(contentType, "application/json");
  assert.equal(csrf, "token");

  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.schema_version, "framecue_review_result_v1");
  assert.equal(body.status, "approved");
  assert.equal(body.review_id, packageData.review_id);
  assert.equal(body.revision, packageData.revision);
  assert.equal(body.package_checksum, packageData.content_checksum);
  assert.equal(body.approved_at, approvedDraft.final_approval.approved_at);
  assert.deepEqual(body.cues.map((cue) => cue.id), packageData.cues.map((cue) => cue.id));
  assert.deepEqual(body.blocks.map((block) => block.id), packageData.blocks.map((block) => block.id));
});

test("reports a workspace POST failure", async () => {
  const fetchMock = async () => ({ ok: false, status: 503 });

  await assert.rejects(
    () => submitApprovedResult(packageData, approvedDraft, serverWorkspace, {
      fetchImpl: fetchMock,
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /503/
  );
});

test("does not submit without workspace server configuration", async () => {
  let calls = 0;
  const fetchMock = async () => {
    calls += 1;
    return { ok: true, status: 200 };
  };

  const response = await submitApprovedResult(packageData, approvedDraft, null, {
    fetchImpl: fetchMock,
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.equal(response.submitted, false);
  assert.equal(calls, 0);
});

const workspaceSnapshot = {
  schema: "framecue_workspace_snapshot_v2",
  workspace_id: "fixture-workspace",
  stage: "content_review",
  draft_version: 3,
  snapshot_version: 7,
  csrf_token: "workspace-token",
  session_id: "session-alice",
  lead_session_id: "session-alice",
  participants: [{ session_id: "session-alice", display_name: "Alice", dirty: false, selected_cue_id: "c0001" }],
  locks: [],
  document: {
    schema: "framecue_subtitle_document_v2",
    cues: [{ id: "c0001", display_text: "哈囉", speech_text: "哈囉。", source_start_ms: 0, source_end_ms: 1000 }],
    blocks: []
  },
  issues: [],
  direct_edit_count: 0
};

test("loads the Workspace v2 snapshot and keeps a missing endpoint in static mode", async () => {
  const calls = [];
  const loaded = await loadWorkspaceSnapshot({
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => workspaceSnapshot };
    },
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.deepEqual(loaded, workspaceSnapshot);
  assert.equal(calls[0].url, "https://framecue.test/api/workspace/snapshot");
  assert.equal(calls[0].init.method, "GET");

  assert.equal(await loadWorkspaceSnapshot({
    fetchImpl: async () => ({ ok: false, status: 404 }),
    baseHref: "https://framecue.test/reviews/index.html"
  }), null);
});

test("rejects Workspace snapshots without collaboration identity", async () => {
  await assert.rejects(
    () => loadWorkspaceSnapshot({
      fetchImpl: async () => ({
        ok: true,
        status: 200,
        json: async () => ({ ...workspaceSnapshot, session_id: undefined })
      }),
      baseHref: "https://framecue.test/reviews/index.html"
    }),
    /session_id/
  );
});

test("submits one version-bound Workspace operation with same-origin CSRF", async () => {
  const calls = [];
  const response = await submitWorkspaceOperation(workspaceSnapshot, {
    kind: "edit",
    cue_id: "c0001",
    display_text: "你好"
  }, {
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => ({ ...workspaceSnapshot, draft_version: 4 }) };
    },
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.equal(response.draft_version, 4);
  assert.equal(calls[0].url, "https://framecue.test/api/workspace/operation");
  assert.equal(calls[0].init.headers["X-FrameCue-CSRF"], "workspace-token");
  assert.deepEqual(JSON.parse(calls[0].init.body), {
    kind: "edit",
    cue_id: "c0001",
    display_text: "你好",
    draft_version: 3
  });
});

test("completes the current Workspace round with its observed draft version", async () => {
  const calls = [];
  await completeWorkspaceRound(workspaceSnapshot, {
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), init });
      return { ok: true, status: 200, json: async () => ({ stage: "voice_realization_pending" }) };
    },
    baseHref: "https://framecue.test/reviews/index.html"
  });

  assert.equal(calls[0].url, "https://framecue.test/api/workspace/complete");
  assert.deepEqual(JSON.parse(calls[0].init.body), { draft_version: 3 });
});

test("Workspace events only notify the caller to reload the authoritative snapshot", () => {
  const opened = [];
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      opened.push(this);
    }
    addEventListener(type, listener) {
      this[type] = listener;
    }
    close() {}
  }
  let reloads = 0;
  const stream = openWorkspaceEvents({
    EventSourceImpl: FakeEventSource,
    baseHref: "https://framecue.test/reviews/index.html",
    onChange: () => { reloads += 1; }
  });

  assert.equal(opened[0].url, "https://framecue.test/api/workspace/events");
  opened[0].snapshot(new Event("snapshot"));
  assert.equal(reloads, 1);
  assert.equal(stream, opened[0]);
});
