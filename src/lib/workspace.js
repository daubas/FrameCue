import { makeResult } from "./review.js";

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
