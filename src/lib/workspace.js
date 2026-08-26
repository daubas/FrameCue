import { makeResult } from "./review.js";

function wordTokens(text) {
  if (typeof Intl?.Segmenter === "function") {
    return [...new Intl.Segmenter("zh-Hant", { granularity: "word" }).segment(String(text))]
      .map(({ segment }) => segment);
  }
  return String(text).match(/\s+|[\p{L}\p{N}]+|[^\s]/gu) || [];
}

export function diffWords(before, after) {
  const left = wordTokens(before);
  const right = wordTokens(after);
  // ponytail: Cue text is short; replace this O(n²) LCS only if long-form diffs are added.
  const lengths = Array.from({ length: left.length + 1 }, () => Array(right.length + 1).fill(0));
  for (let i = left.length - 1; i >= 0; i -= 1) {
    for (let j = right.length - 1; j >= 0; j -= 1) {
      lengths[i][j] = left[i] === right[j]
        ? lengths[i + 1][j + 1] + 1
        : Math.max(lengths[i + 1][j], lengths[i][j + 1]);
    }
  }
  const parts = [];
  const add = (type, text) => {
    if (parts.at(-1)?.type === type) parts.at(-1).text += text;
    else parts.push({ type, text });
  };
  let i = 0;
  let j = 0;
  while (i < left.length || j < right.length) {
    if (left[i] === right[j]) {
      add("same", left[i]); i += 1; j += 1;
    } else if (j < right.length && (i === left.length || lengths[i][j + 1] >= lengths[i + 1][j])) {
      add("add", right[j]); j += 1;
    } else {
      add("remove", left[i]); i += 1;
    }
  }
  return parts;
}

export function cueReadingMetrics(cue) {
  const text = String(cue?.display_text ?? cue?.text ?? "");
  const characters = [...text.replace(/\s/gu, "")].length;
  const start = cue?.output_start_ms ?? cue?.source_start_ms ?? cue?.start_ms ?? 0;
  const end = cue?.output_end_ms ?? cue?.source_end_ms ?? cue?.end_ms ?? start;
  const duration_ms = Math.max(0, end - start);
  const cps = duration_ms ? Number((characters * 1000 / duration_ms).toFixed(1)) : 0;
  return {
    characters,
    duration_ms,
    cps,
    risks: [
      ...(duration_ms < 1000 ? ["少於 1 秒"] : []),
      ...(cps > 20 ? ["超過 20 CPS"] : []),
      ...(characters > 30 ? ["超過 30 字"] : [])
    ]
  };
}

export function isWorkspaceSubmitted(workspace) {
  return workspace?.mode === "server"
    && typeof workspace.stage === "string"
    && workspace.stage !== "content_review";
}

function resolveEndpoint(endpoint, baseHref) {
  if (typeof endpoint !== "string" || !endpoint.trim()) {
    throw new Error("workspace content-complete endpoint is required");
  }
  let resolved;
  let base;
  try {
    base = new URL(baseHref);
    resolved = new URL(endpoint, base);
  } catch {
    throw new Error("workspace content-complete endpoint is invalid");
  }
  if (resolved.origin !== base.origin) {
    throw new Error("workspace content-complete endpoint must be same-origin");
  }
  return resolved.href;
}

function resolveWorkspaceEndpoint(path, baseHref) {
  let base;
  let endpoint;
  try {
    base = new URL(baseHref);
    endpoint = new URL(path, base);
    const reviewId = base.searchParams.get("review_id");
    if (reviewId) endpoint.searchParams.set("review_id", reviewId);
  } catch {
    throw new Error("workspace endpoint is invalid");
  }
  if (endpoint.origin !== base.origin) throw new Error("workspace endpoint must be same-origin");
  return endpoint.href;
}

function validateWorkspaceSnapshot(snapshot) {
  if (!snapshot || typeof snapshot !== "object" || snapshot.schema !== "framecue_workspace_snapshot_v2") {
    throw new Error("workspace snapshot is invalid");
  }
  if (typeof snapshot.workspace_id !== "string" || !snapshot.workspace_id.trim()) {
    throw new Error("workspace snapshot workspace_id is required");
  }
  if (typeof snapshot.stage !== "string" || !snapshot.stage.trim()) {
    throw new Error("workspace snapshot stage is required");
  }
  if (!Number.isInteger(snapshot.draft_version) || snapshot.draft_version < 0) {
    throw new Error("workspace snapshot draft_version is invalid");
  }
  if (typeof snapshot.csrf_token !== "string" || !snapshot.csrf_token.trim()) {
    throw new Error("workspace snapshot CSRF token is required");
  }
  if (!snapshot.document || !Array.isArray(snapshot.document.cues) || !Array.isArray(snapshot.document.blocks)) {
    throw new Error("workspace snapshot document is invalid");
  }
  if (typeof snapshot.session_id !== "string" || !snapshot.session_id.trim()) {
    throw new Error("workspace snapshot session_id is required");
  }
  if (typeof snapshot.lead_session_id !== "string" || typeof snapshot.lead_active !== "boolean"
    || !Array.isArray(snapshot.participants) || !Array.isArray(snapshot.locks)) {
    throw new Error("workspace snapshot collaboration state is invalid");
  }
  if (!snapshot.participants.every((participant) => participant
    && typeof participant.session_id === "string"
    && typeof participant.display_name === "string"
    && typeof participant.dirty === "boolean"
    && typeof participant.selected_cue_id === "string")
    || !snapshot.locks.every((lock) => lock && typeof lock.cue_id === "string" && typeof lock.session_id === "string")) {
    throw new Error("workspace snapshot collaboration entries are invalid");
  }
  if (!Number.isInteger(snapshot.snapshot_version) || snapshot.snapshot_version < 0) {
    throw new Error("workspace snapshot snapshot_version is invalid");
  }
  return snapshot;
}

async function workspacePost(path, workspace, body, {
  fetchImpl = globalThis.fetch,
  baseHref = globalThis.location?.href
} = {}) {
  if (typeof fetchImpl !== "function") throw new Error("workspace fetch is unavailable");
  validateWorkspaceSnapshot(workspace);
  const endpoint = resolveWorkspaceEndpoint(path, baseHref);
  const response = await fetchImpl(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-FrameCue-CSRF": workspace.csrf_token
    },
    credentials: "same-origin",
    body: JSON.stringify(body)
  });
  if (!response?.ok) {
    throw new Error(`workspace request failed (${response?.status ?? "unknown"})`);
  }
  try {
    return await response.json();
  } catch {
    throw new Error("workspace response is invalid JSON");
  }
}

export async function loadWorkspaceSnapshot({
  fetchImpl = globalThis.fetch,
  baseHref = globalThis.location?.href
} = {}) {
  if (typeof fetchImpl !== "function") throw new Error("workspace fetch is unavailable");
  const endpoint = resolveWorkspaceEndpoint("/api/workspace/snapshot", baseHref);
  const response = await fetchImpl(endpoint, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin"
  });
  if (response?.status === 404) return null;
  if (!response?.ok) {
    throw new Error(`workspace snapshot request failed (${response?.status ?? "unknown"})`);
  }
  let snapshot;
  try {
    snapshot = await response.json();
  } catch {
    throw new Error("workspace snapshot response is invalid JSON");
  }
  return validateWorkspaceSnapshot(snapshot);
}

export function submitWorkspaceOperation(workspace, operation, options = {}) {
  if (!operation || typeof operation !== "object" || Array.isArray(operation)) {
    throw new Error("workspace operation is invalid");
  }
  return workspacePost("/api/workspace/operation", workspace, {
    ...operation,
    draft_version: workspace.draft_version
  }, options);
}

export function resolveStructuralCueId(cues, operation, selectedBefore) {
  const byId = (cueId) => cues.find((cue) => cue.id === cueId);
  if (operation.kind === "split") {
    const children = cues.filter((cue) => cue.lineage?.parent_cue_ids?.includes(operation.cue_id));
    return children[1]?.id || children[0]?.id || byId(selectedBefore)?.id || cues[0]?.id || "";
  }
  if (operation.kind === "merge") {
    const parentIds = [operation.cue_id, operation.adjacent_cue_id].filter(Boolean);
    const merged = byId(operation.cue_id) || cues.find((cue) =>
      parentIds.every((cueId) => cue.lineage?.parent_cue_ids?.includes(cueId))
    );
    return merged?.id || byId(selectedBefore)?.id || cues[0]?.id || "";
  }
  if (operation.kind === "delete") {
    return nearestRemainingCueId(cues, operation.selection_index);
  }
  return byId(selectedBefore)?.id || byId(operation.cue_id)?.id || cues[0]?.id || "";
}

export function nearestRemainingCueId(cues, deletedIndex) {
  if (!cues.length) return "";
  const index = Number.isInteger(deletedIndex) ? Math.min(deletedIndex, cues.length - 1) : 0;
  return cues[Math.max(0, index)]?.id || "";
}

export function completeWorkspaceRound(workspace, options = {}) {
  return workspacePost("/api/workspace/complete", workspace, {
    draft_version: workspace.draft_version
  }, options);
}

export function openWorkspaceEvents({
  EventSourceImpl = globalThis.EventSource,
  baseHref = globalThis.location?.href,
  onChange = () => {},
  onOpen = () => {},
  onError = () => {}
} = {}) {
  if (typeof EventSourceImpl !== "function") throw new Error("workspace events are unavailable");
  const stream = new EventSourceImpl(resolveWorkspaceEndpoint("/api/workspace/events", baseHref));
  stream.addEventListener("snapshot", onChange);
  stream.addEventListener("open", onOpen);
  stream.addEventListener("error", onError);
  return stream;
}

function validateWorkspaceConfig(config, baseHref) {
  if (!config || typeof config !== "object") {
    throw new Error("workspace config is invalid");
  }
  if (config.mode !== "server") {
    throw new Error("workspace config mode must be server");
  }
  if (typeof config.workspace_id !== "string" || !config.workspace_id.trim()) {
    throw new Error("workspace config workspace_id is required");
  }
  if (typeof config.stage !== "string" || !config.stage.trim()) {
    throw new Error("workspace config stage is required");
  }
  if (typeof config.csrf_token !== "string" || !config.csrf_token.trim()) {
    throw new Error("workspace config CSRF token is required");
  }
  resolveEndpoint(config.content_complete_endpoint, baseHref);
  return config;
}

export async function loadWorkspaceConfig({
  fetchImpl = globalThis.fetch,
  baseHref = globalThis.location?.href
} = {}) {
  if (typeof fetchImpl !== "function") {
    throw new Error("workspace fetch is unavailable");
  }
  let endpoint;
  try {
    endpoint = new URL("/api/workspace", new URL(baseHref));
  } catch {
    throw new Error("workspace config endpoint is invalid");
  }
  const response = await fetchImpl(endpoint.href, {
    method: "GET",
    cache: "no-store",
    credentials: "same-origin"
  });
  if (response?.status === 404) return null;
  if (!response?.ok) {
    throw new Error(`workspace config request failed (${response?.status ?? "unknown"})`);
  }
  let config;
  try {
    config = await response.json();
  } catch {
    throw new Error("workspace config response is invalid JSON");
  }
  return validateWorkspaceConfig(config, baseHref);
}

export async function submitApprovedResult(
  packageData,
  draft,
  workspace,
  { fetchImpl = globalThis.fetch, baseHref = globalThis.location?.href } = {}
) {
  const approvedAt = draft?.final_approval?.approved_at || "";
  const result = makeResult(packageData, draft, approvedAt);
  if (!workspace || workspace.mode !== "server") {
    return { submitted: false, result };
  }
  if (typeof workspace.csrf_token !== "string" || !workspace.csrf_token.trim()) {
    throw new Error("workspace CSRF token is required");
  }
  if (typeof fetchImpl !== "function") {
    throw new Error("workspace fetch is unavailable");
  }

  const endpoint = resolveEndpoint(workspace.content_complete_endpoint, baseHref);
  const response = await fetchImpl(endpoint, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-FrameCue-CSRF": workspace.csrf_token
    },
    credentials: "same-origin",
    body: JSON.stringify(result)
  });
  if (!response?.ok) {
    throw new Error(`workspace content-complete request failed (${response?.status ?? "unknown"})`);
  }
  return { submitted: true, result, response };
}
