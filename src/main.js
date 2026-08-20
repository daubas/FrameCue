import "./app.css";
import App from "./App.svelte";
import SubtitleWorkspace from "./SubtitleWorkspace.svelte";
import { loadWorkspaceSnapshot } from "./lib/workspace.js";
import { mount } from "svelte";

async function boot() {
  const target = document.getElementById("app");
  try {
    const snapshot = await loadWorkspaceSnapshot({ baseHref: window.location.href });
    mount(snapshot ? SubtitleWorkspace : App, {
      target,
      props: snapshot ? { initialSnapshot: snapshot } : {}
    });
  } catch (cause) {
    target.textContent = cause?.message || "FrameCue Workspace 無法載入。";
    target.className = "error-state";
  }
}

boot();
