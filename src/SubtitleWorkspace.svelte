<script>
  import { onMount } from "svelte";
  import MediaStage from "./components/MediaStage.svelte";
  import {
    completeWorkspaceRound,
    loadWorkspaceSnapshot,
    openWorkspaceEvents,
    submitWorkspaceOperation
  } from "./lib/workspace.js";

  export let initialSnapshot;

  let snapshot = initialSnapshot;
  let selectedCueId = snapshot.document.cues[0]?.id || "";
  let editCueId = "";
  let editText = "";
  let editor;
  let stageMode = snapshot.document.source_package?.media?.video ? "video" : "still";
  let connected = true;
  let syncing = false;
  let completing = false;
  let phone = false;
  let message = "";

  $: cues = snapshot.document.cues || [];
  $: selectedCue = cues.find((cue) => cue.id === selectedCueId) || cues[0] || null;
  $: if (selectedCue && selectedCue.id !== editCueId) {
    editCueId = selectedCue.id;
    editText = selectedCue.display_text ?? selectedCue.text ?? "";
  }
  $: sourcePackage = snapshot.document.source_package || {};
  $: stageCues = cues.map((cue) => {
    const sourceId = cue.origin_cue_ids?.[0] || cue.id;
    const source = sourcePackage.cues?.find((item) => item.id === sourceId) || {};
    return {
      ...source,
      ...cue,
      start_ms: cue.source_start_ms ?? source.start_ms ?? 0,
      end_ms: cue.source_end_ms ?? source.end_ms ?? 0,
      text: cue.display_text ?? cue.text ?? "",
      original_text: cue.source_text ?? source.original_text ?? ""
    };
  });
  $: stageCue = stageCues.find((cue) => cue.id === selectedCue?.id) || null;
  $: mediaPackage = {
    ...sourcePackage,
    workflow: sourcePackage.workflow || { kind: "subtitle" },
    cues: stageCues,
    blocks: snapshot.document.blocks || [],
    scenes: sourcePackage.scenes || []
  };
  $: issueCount = new Set((snapshot.issues || []).map((issue) => issue.range_id || issue.flag_id)).size;
  $: selectedHasIssue = Boolean(selectedCue && (snapshot.issues || []).some((issue) => issue.cue_ids?.includes(selectedCue.id)));
  $: agentPending = snapshot.stage.endsWith("_pending");
  $: canEdit = snapshot.stage === "content_review" && connected && !syncing && !completing && !phone;
  $: nextMergeCue = (() => {
    const index = cues.findIndex((cue) => cue.id === selectedCue?.id);
    const next = cues[index + 1];
    return next && next.block_id === selectedCue?.block_id ? next : null;
  })();

  function assetUrl(path) {
    return new URL(path || "", window.location.href).href;
  }

  async function reload() {
    try {
      const next = await loadWorkspaceSnapshot({ baseHref: window.location.href });
      if (!next) throw new Error("Workspace 已停止提供資料。");
      snapshot = next;
      if (!next.document.cues.some((cue) => cue.id === selectedCueId)) {
        selectedCueId = next.document.cues[0]?.id || "";
      }
      message = "";
    } catch (cause) {
      message = cause?.message || "同步失敗";
    }
  }

  async function submit(operation) {
    if (!canEdit) return;
    syncing = true;
    message = "";
    try {
      const changed = await submitWorkspaceOperation(snapshot, operation, { baseHref: window.location.href });
      snapshot = { ...snapshot, ...changed, schema: snapshot.schema, csrf_token: snapshot.csrf_token };
      if (!snapshot.document.cues.some((cue) => cue.id === selectedCueId)) {
        selectedCueId = changed.document.cues.find((cue) => changed.operation?.cue_ids?.includes(cue.id))?.id
          || changed.document.cues[0]?.id
          || "";
      }
    } catch (cause) {
      message = cause?.message || "修改同步失敗";
      await reload();
    } finally {
      syncing = false;
    }
  }

  async function saveText() {
    if (!selectedCue || editText === selectedCue.display_text) return;
    await submit({ kind: "edit", cue_id: selectedCue.id, display_text: editText });
  }

  async function splitCue() {
    const cursor = editor?.selectionStart;
    if (!Number.isInteger(cursor)) return;
    await submit({ kind: "split", cue_id: selectedCue.id, cursor });
  }

  async function completeRound() {
    if (!canEdit) return;
    await saveText();
    completing = true;
    message = "";
    try {
      await completeWorkspaceRound(snapshot, { baseHref: window.location.href });
      await reload();
    } catch (cause) {
      message = cause?.message || "無法完成本輪";
    } finally {
      completing = false;
    }
  }

  function handleKeys(event) {
    const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName);
    if (!editing && event.key.toLowerCase() === "m" && canEdit && !selectedHasIssue) {
      event.preventDefault();
      submit({ kind: "flag", cue_ids: [selectedCue.id], categories: ["other"], author: snapshot.display_name || "reviewer" });
    }
    if (editing && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      splitCue();
    }
  }

  onMount(() => {
    const phoneQuery = window.matchMedia("(max-width: 600px)");
    const updatePhone = () => { phone = phoneQuery.matches; };
    updatePhone();
    phoneQuery.addEventListener("change", updatePhone);
    window.addEventListener("keydown", handleKeys);
    const stream = openWorkspaceEvents({
      baseHref: window.location.href,
      onChange: reload,
      onOpen: () => { connected = true; },
      onError: () => { connected = false; }
    });
    return () => {
      stream.close();
      phoneQuery.removeEventListener("change", updatePhone);
      window.removeEventListener("keydown", handleKeys);
    };
  });
</script>

<main class="workspace-shell">
  <header class="workspace-toolbar">
    <div>
      <strong>FrameCue</strong>
      <span>{snapshot.workspace_id}</span>
    </div>
    <div class="workspace-counts" aria-label="本輪摘要">
      <span>需修改 {issueCount}</span>
      <span>直接修改 {snapshot.direct_edit_count || 0}</span>
      <span class:warning={!connected}>{syncing ? "同步中" : connected ? "已同步" : "已離線"}</span>
    </div>
    <button type="button" class="complete" disabled={!canEdit} on:click={completeRound}>
      {completing ? "完成中…" : "完成本輪"}
    </button>
  </header>

  {#if message}<p class="workspace-message" role="alert">{message}</p>{/if}
  {#if agentPending}
    <p class="pending-note">Agent 正在處理，本輪內容暫時唯讀。</p>
  {:else if phone}
    <p class="pending-note">手機版提供唯讀檢視；請用平板或電腦修改。</p>
  {/if}

  <div class="workspace-grid">
    <MediaStage
      packageData={mediaPackage}
      cue={stageCue}
      cueDraft={{ text: editText }}
      {stageMode}
      {assetUrl}
      onStageMode={(mode) => { stageMode = mode; }}
      onPlaybackCue={(cueId) => { if (cues.some((cue) => cue.id === cueId)) selectedCueId = cueId; }}
    />

    <section class="cue-workspace" aria-label="字幕工作區">
      <div class="cue-list" aria-label="字幕清單">
        {#each cues as cue, index}
          <button class:active={cue.id === selectedCue?.id} class:needs-change={(snapshot.issues || []).some((issue) => issue.cue_ids?.includes(cue.id))} type="button" on:click={() => { selectedCueId = cue.id; }}>
            <small>{index + 1}</small>
            <span>{cue.display_text}</span>
          </button>
        {/each}
      </div>

      {#if selectedCue}
        <div class="cue-editor">
          <div class="editor-title">
            <strong>{selectedCue.id}</strong>
            <span>Space 播放 · M 標記</span>
          </div>
          <label>
            中文字幕
            <textarea
              bind:this={editor}
              bind:value={editText}
              readonly={!canEdit}
              on:input={() => window.dispatchEvent(new Event("framecue:pause-playback"))}
              on:blur={saveText}
            ></textarea>
          </label>
          <div class="cue-actions">
            <button type="button" disabled={!canEdit} on:click={splitCue}>從游標切開</button>
            <button type="button" disabled={!canEdit || !nextMergeCue} on:click={() => submit({ kind: "merge", cue_id: selectedCue.id, adjacent_cue_id: nextMergeCue.id })}>與下一句合併</button>
            <button type="button" class:flagged={selectedHasIssue} disabled={!canEdit || selectedHasIssue} on:click={() => submit({ kind: "flag", cue_ids: [selectedCue.id], categories: ["other"], author: snapshot.display_name || "reviewer" })}>
              {selectedHasIssue ? "已標記需修改" : "標記需修改"}
            </button>
          </div>
        </div>
      {/if}
    </section>
  </div>
</main>

<style>
  .workspace-shell { min-height: 100vh; background: #151817; color: #eff2ec; }
  .workspace-toolbar { position: sticky; top: 0; z-index: 12; display: grid; grid-template-columns: minmax(180px, 1fr) auto minmax(140px, 1fr); align-items: center; gap: 16px; min-height: 64px; padding: 10px 18px; border-bottom: 1px solid #3b443d; background: #1b201d; }
  .workspace-toolbar > div:first-child { display: flex; min-width: 0; align-items: baseline; gap: 9px; }
  .workspace-toolbar strong { font-size: 18px; }
  .workspace-toolbar span { color: #bac5b9; font-size: 13px; }
  .workspace-counts { display: flex; gap: 14px; white-space: nowrap; }
  .workspace-counts .warning { color: #ffc6be; }
  .complete { justify-self: end; border-color: #77946b; background: #30422e; padding: 7px 14px; }
  .workspace-message, .pending-note { margin: 0; padding: 8px 18px; border-bottom: 1px solid #4d4438; background: #2b271f; color: #efd9a9; font-size: 13px; }
  .workspace-message { color: #ffc6be; }
  .workspace-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(420px, 560px); height: calc(100vh - 64px); }
  .workspace-grid :global(.media-stage) { min-width: 0; }
  .cue-workspace { display: grid; grid-template-columns: minmax(145px, .7fr) minmax(240px, 1.3fr); min-width: 0; min-height: 0; background: #1b201d; }
  .cue-list { min-height: 0; overflow: auto; border-left: 1px solid #3b443d; border-right: 1px solid #3b443d; }
  .cue-list button { display: grid; width: 100%; min-height: 64px; grid-template-columns: 28px 1fr; align-items: start; gap: 6px; padding: 10px; border: 0; border-bottom: 1px solid #323a34; border-radius: 0; background: transparent; text-align: left; }
  .cue-list button.active { background: #394f40; }
  .cue-list button.needs-change { box-shadow: inset 3px 0 #cf765e; }
  .cue-list small { color: #9da99d; }
  .cue-list span { overflow-wrap: anywhere; color: #eef2ec; line-height: 1.4; }
  .cue-editor { min-width: 0; overflow: auto; padding: 16px; }
  .editor-title, .cue-actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .editor-title { margin-bottom: 14px; }
  .editor-title span { color: #aeb9ad; font-size: 11px; }
  .cue-editor label { display: grid; gap: 7px; color: #d4ddd1; font-size: 12px; font-weight: 700; }
  .cue-editor textarea { min-height: 180px; }
  .cue-actions { margin-top: 12px; justify-content: flex-start; flex-wrap: wrap; }
  .cue-actions button { padding: 6px 9px; font-size: 12px; }
  .cue-actions .flagged { border-color: #b46d4a; color: #ffd9bf; }
  @media (max-width: 980px) {
    .workspace-toolbar { grid-template-columns: 1fr auto; }
    .workspace-counts { grid-row: 2; grid-column: 1 / -1; justify-content: center; }
    .workspace-grid { grid-template-columns: 1fr; height: auto; }
    .workspace-grid :global(.media-stage) { min-height: 56vh; border-bottom: 1px solid #3b443d; }
    .cue-workspace { min-height: 44vh; }
  }
  @media (max-width: 600px) {
    .workspace-toolbar { position: static; }
    .workspace-counts { gap: 8px; overflow-x: auto; justify-content: flex-start; }
    .cue-workspace { grid-template-columns: 1fr; }
    .cue-list { max-height: 38vh; border-left: 0; }
  }
</style>
