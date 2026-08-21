<script>
  import { onMount } from "svelte";
  import MediaStage from "./components/MediaStage.svelte";
  import { formatTime } from "./lib/review.js";
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
  let playbackMs = snapshot.document.cues[0]?.source_start_ms || 0;
  let mediaDurationMs = 0;
  let connected = true;
  let syncing = false;
  let localDirty = false;
  let busy = false;
  let completing = false;
  let phone = false;
  let message = "";
  let heldCueIds = [];
  let saveTimer;
  let renewTimer;
  let dirtyPromise = Promise.resolve();
  let savePromise = null;

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
  $: timelineEndMs = Math.max(mediaDurationMs, ...stageCues.map((cue) => cue.end_ms), 1);
  $: playheadPercent = Math.min(100, Math.max(0, playbackMs / timelineEndMs * 100));
  $: selectedTimingLabel = selectedCue?.output_start_ms != null && selectedCue?.output_end_ms != null
    ? "配音時間已對齊"
    : selectedCue?.timing_state === "provisional"
      ? "來源時間 · 暫定切分 · 配音未對齊"
      : "來源時間 · 配音未對齊";
  $: issueCount = new Set((snapshot.issues || []).map((issue) => issue.range_id || issue.flag_id)).size;
  $: selectedOwnIssues = selectedCue ? (snapshot.issues || []).filter((issue) =>
    issue.cue_ids?.includes(selectedCue.id) && issue.authors?.includes(snapshot.display_name)
  ) : [];
  $: selectedHasIssue = selectedOwnIssues.length > 0;
  $: agentPending = snapshot.stage.endsWith("_pending");
  $: selectedLock = snapshot.locks.find((lock) => lock.cue_id === selectedCue?.id) || null;
  $: lockedByOther = Boolean(selectedLock && selectedLock.session_id !== snapshot.session_id);
  $: isLead = snapshot.lead_session_id === snapshot.session_id;
  $: leadName = snapshot.participants.find((participant) => participant.session_id === snapshot.lead_session_id)?.display_name || "lead";
  $: canEdit = snapshot.stage === "content_review" && connected && !busy && !completing && !phone && !lockedByOther;
  $: canType = canEdit && heldCueIds.includes(selectedCue?.id);
  $: canComplete = canEdit && isLead && !localDirty && !snapshot.participants.some((participant) => participant.dirty) && !snapshot.locks.length;
  $: nextMergeCue = (() => {
    const index = cues.findIndex((cue) => cue.id === selectedCue?.id);
    const next = cues[index + 1];
    return next && next.block_id === selectedCue?.block_id ? next : null;
  })();

  function assetUrl(path) {
    return new URL(path || "", window.location.href).href;
  }

  function mergeSnapshot(changed) {
    if (!changed?.document || changed.snapshot_version < snapshot.snapshot_version) return;
    snapshot = { ...snapshot, ...changed, schema: snapshot.schema, csrf_token: snapshot.csrf_token };
  }

  async function post(operation) {
    const changed = await submitWorkspaceOperation(snapshot, operation, { baseHref: window.location.href });
    mergeSnapshot(changed);
    return changed;
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
    if (!canEdit) return null;
    syncing = true;
    message = "";
    try {
      return await post(operation);
    } catch (cause) {
      message = cause?.message || "修改同步失敗";
      await reload();
      return null;
    } finally {
      syncing = false;
    }
  }

  function stopRenewing() {
    clearInterval(renewTimer);
    renewTimer = undefined;
  }

  async function lockCues(cueIds) {
    const changed = await post({ kind: "lock", cue_ids: cueIds });
    heldCueIds = [...cueIds];
    return changed;
  }

  async function unlockCues(cueIds = heldCueIds) {
    const existing = cueIds.filter((cueId) => snapshot.document.cues.some((cue) => cue.id === cueId));
    heldCueIds = [];
    if (existing.length) await post({ kind: "unlock", cue_ids: existing });
  }

  async function focusEditor() {
    if (!canEdit || !selectedCue) return;
    try {
      await lockCues([selectedCue.id]);
      stopRenewing();
      renewTimer = setInterval(async () => {
        if (document.activeElement !== editor || !heldCueIds.length) return;
        try {
          await lockCues(heldCueIds);
        } catch {
          connected = false;
          stopRenewing();
        }
      }, 5000);
    } catch (cause) {
      message = cause?.message || "這句字幕正由其他人修改";
      await reload();
      editor?.blur();
    }
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(saveText, 800);
  }

  function handleInput() {
    if (!canType) return;
    window.dispatchEvent(new Event("framecue:pause-playback"));
    if (!localDirty) {
      localDirty = true;
      dirtyPromise = post({ kind: "dirty", dirty: true }).catch(async (cause) => {
        message = cause?.message || "無法同步編輯狀態";
        await reload();
      });
    }
    scheduleSave();
  }

  async function saveText() {
    if (savePromise) return savePromise;
    clearTimeout(saveTimer);
    savePromise = (async () => {
      await dirtyPromise;
      const cue = snapshot.document.cues.find((item) => item.id === editCueId);
      try {
        if (cue && editText !== cue.display_text) {
          syncing = true;
          await post({ kind: "edit", cue_id: cue.id, display_text: editText });
          heldCueIds = [];
          if (document.activeElement === editor) await lockCues([cue.id]);
        }
        if (localDirty) await post({ kind: "dirty", dirty: false });
        localDirty = false;
        message = "";
      } catch (cause) {
        message = cause?.message || "修改同步失敗";
        await reload();
        if (document.activeElement === editor) scheduleSave();
      } finally {
        syncing = false;
      }
    })();
    try {
      await savePromise;
    } finally {
      savePromise = null;
    }
  }

  async function leaveEditor() {
    stopRenewing();
    await saveText();
    try {
      await unlockCues();
    } catch {
      await reload();
    }
  }

  async function selectCue(cueId) {
    if (cueId === selectedCueId) return;
    await leaveEditor();
    selectedCueId = cueId;
    playbackMs = cues.find((cue) => cue.id === cueId)?.source_start_ms || 0;
    await post({ kind: "presence", selected_cue_id: cueId });
  }

  async function structuralOperation(operation, cueIds) {
    if (!canEdit) return;
    await leaveEditor();
    busy = true;
    message = "";
    try {
      await lockCues(cueIds);
      const changed = await post(operation);
      heldCueIds = [];
      selectedCueId = changed.document.cues.find((cue) => operation.cue_id === cue.id)?.id
        || changed.document.cues.find((cue) => operation.cue_id && cue.lineage?.parent_cue_ids?.includes(operation.cue_id))?.id
        || changed.document.cues[0]?.id
        || "";
      await post({ kind: "presence", selected_cue_id: selectedCueId });
    } catch (cause) {
      message = cause?.message || "字幕結構修改失敗";
      try { await unlockCues(); } catch { /* server may have already released transformed Cue IDs */ }
      await reload();
    } finally {
      busy = false;
    }
  }

  async function splitCue() {
    const cursor = editor?.selectionStart;
    if (!Number.isInteger(cursor)) return;
    await structuralOperation({ kind: "split", cue_id: selectedCue.id, cursor }, [selectedCue.id]);
  }

  async function completeRound() {
    if (!canComplete) return;
    await leaveEditor();
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

  async function claimLead() {
    if (!canEdit || snapshot.lead_active) return;
    busy = true;
    message = "";
    try {
      await post({
        kind: "lead",
        expected_lead_session_id: snapshot.lead_session_id,
        new_lead_session_id: snapshot.session_id
      });
    } catch (cause) {
      message = cause?.message || "無法接手 lead";
      await reload();
    } finally {
      busy = false;
    }
  }

  async function toggleSelectedIssue() {
    if (!selectedOwnIssues.length) {
      await submit({ kind: "flag", cue_ids: [selectedCue.id], categories: ["other"], author: snapshot.display_name || "reviewer" });
      return;
    }
    for (const issue of selectedOwnIssues) {
      await submit({
        kind: "flag",
        cue_ids: issue.cue_ids,
        categories: [issue.category],
        author: snapshot.display_name || "reviewer",
        enabled: false
      });
    }
  }

  function handleKeys(event) {
    const editing = ["INPUT", "TEXTAREA", "SELECT"].includes(event.target?.tagName);
    if (!editing && event.key.toLowerCase() === "m" && canEdit) {
      event.preventDefault();
      toggleSelectedIssue();
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
    post({ kind: "presence", selected_cue_id: selectedCueId }).catch(() => { connected = false; });
    const stream = openWorkspaceEvents({
      baseHref: window.location.href,
      onChange: reload,
      onOpen: () => { connected = true; },
      onError: () => { connected = false; }
    });
    return () => {
      clearTimeout(saveTimer);
      stopRenewing();
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
      <span>{snapshot.participants.map((participant) => participant.display_name).join(" · ")}</span>
    </div>
    <div class="workspace-counts" aria-label="本輪摘要">
      <span>需修改 {issueCount}</span>
      <span>直接修改 {snapshot.direct_edit_count || 0}</span>
      <span class:warning={!connected || localDirty}>{localDirty ? "尚未同步" : syncing ? "同步中" : connected ? "已同步" : "已離線"}</span>
    </div>
    {#if !isLead && !snapshot.lead_active}
      <button type="button" class="complete" disabled={!canEdit} on:click={claimLead}>接手 lead</button>
    {:else}
      <button type="button" class="complete" disabled={!canComplete} on:click={completeRound}>
        {isLead ? completing ? "完成中…" : "完成本輪" : `等待 ${leadName} lead`}
      </button>
    {/if}
  </header>

  {#if message}<p class="workspace-message" role="alert">{message}</p>{/if}
  {#if agentPending}
    <p class="pending-note">Agent 正在處理，本輪內容暫時唯讀。</p>
  {:else if phone}
    <p class="pending-note">手機版提供唯讀檢視；請用平板或電腦修改。</p>
  {:else if lockedByOther}
    <p class="pending-note">{snapshot.participants.find((participant) => participant.session_id === selectedLock.session_id)?.display_name || "其他審稿者"} 正在修改這句字幕。</p>
  {:else if !isLead && !snapshot.lead_active}
    <p class="pending-note">原 lead 已離線；接手後即可完成本輪。</p>
  {:else if !isLead}
    <p class="pending-note">你可以修改字幕；完成本輪需等待 {leadName} lead。</p>
  {/if}

  <div class="workspace-grid">
    <MediaStage
      packageData={mediaPackage}
      cue={stageCue}
      cueDraft={{ text: editText }}
      {stageMode}
      {assetUrl}
      onStageMode={(mode) => { stageMode = mode; }}
      onPlaybackCue={(cueId) => { if (cues.some((cue) => cue.id === cueId)) selectCue(cueId); }}
      onPlaybackTime={(currentMs, durationMs = 0) => {
        playbackMs = currentMs;
        if (durationMs > 0) mediaDurationMs = durationMs;
      }}
    />

    <section class="cue-workspace" aria-label="字幕工作區">
      <section class="cue-timeline" aria-label="影片字幕時間軸">
        <div class="timeline-heading">
          <strong>字幕時間軸</strong>
          <span>{formatTime(playbackMs)} / {formatTime(timelineEndMs)}</span>
        </div>
        <div class="timeline-track">
          {#each stageCues as cue}
            <button
              type="button"
              class:active={cue.id === selectedCue?.id}
              class:needs-change={(snapshot.issues || []).some((issue) => issue.cue_ids?.includes(cue.id))}
              style={`left:${cue.start_ms / timelineEndMs * 100}%;width:${Math.max(.25, (cue.end_ms - cue.start_ms) / timelineEndMs * 100)}%`}
              aria-label={`${cue.id} ${formatTime(cue.start_ms)} 至 ${formatTime(cue.end_ms)}`}
              title={`${cue.id} · ${formatTime(cue.start_ms)}–${formatTime(cue.end_ms)}`}
              on:click={() => selectCue(cue.id)}
            ></button>
          {/each}
          <span class="playhead" style={`left:${playheadPercent}%`}></span>
        </div>
        <div class="timeline-status">
          <span>{selectedTimingLabel}</span>
          <span>{sourcePackage.media?.video ? "影片時間軸" : "目前套件未附影片 · 依 Cue 來源時間顯示"}</span>
        </div>
      </section>

      <div class="cue-list" aria-label="字幕清單">
        {#each cues as cue, index}
          <button class:active={cue.id === selectedCue?.id} class:needs-change={(snapshot.issues || []).some((issue) => issue.cue_ids?.includes(cue.id))} class:locked={snapshot.locks.some((lock) => lock.cue_id === cue.id && lock.session_id !== snapshot.session_id)} type="button" on:click={() => selectCue(cue.id)}>
            <small>{index + 1}</small>
            <span>{cue.display_text}</span>
            <small class="cue-time">{formatTime(cue.source_start_ms)}–{formatTime(cue.source_end_ms)}</small>
            {#if snapshot.participants.some((participant) => participant.selected_cue_id === cue.id && participant.session_id !== snapshot.session_id)}
              <small>{snapshot.participants.filter((participant) => participant.selected_cue_id === cue.id && participant.session_id !== snapshot.session_id).map((participant) => participant.display_name).join(", ")}</small>
            {/if}
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
              readonly={!canType}
              on:focus={focusEditor}
              on:input={handleInput}
              on:blur={leaveEditor}
            ></textarea>
          </label>
          <div class="cue-actions">
            <button type="button" disabled={!canEdit} on:click={splitCue}>從游標切開</button>
            <button type="button" disabled={!canEdit || !nextMergeCue} on:click={() => structuralOperation({ kind: "merge", cue_id: selectedCue.id, adjacent_cue_id: nextMergeCue.id }, [selectedCue.id, nextMergeCue.id])}>與下一句合併</button>
            <button type="button" class:flagged={selectedHasIssue} disabled={!canEdit} on:click={toggleSelectedIssue}>
              {selectedHasIssue ? "取消需修改" : "標記需修改"}
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
  .cue-workspace { display: grid; grid-template-columns: minmax(145px, .7fr) minmax(240px, 1.3fr); grid-template-rows: auto minmax(0, 1fr); min-width: 0; min-height: 0; background: #1b201d; }
  .cue-timeline { grid-column: 1 / -1; padding: 10px 12px 9px; border-left: 1px solid #3b443d; border-bottom: 1px solid #3b443d; }
  .timeline-heading, .timeline-status { display: flex; justify-content: space-between; gap: 10px; color: #aeb9ad; font-size: 11px; }
  .timeline-heading strong { color: #eef2ec; font-size: 12px; }
  .timeline-track { position: relative; height: 22px; margin: 7px 0 5px; overflow: hidden; border-radius: 4px; background: #101311; }
  .timeline-track button { position: absolute; top: 4px; height: 14px; min-width: 2px; padding: 0; border: 1px solid #607061; border-radius: 2px; background: #39443b; }
  .timeline-track button.active { z-index: 2; border-color: #d7efcd; background: #6b9068; }
  .timeline-track button.needs-change { background: #975d4c; }
  .timeline-track .playhead { position: absolute; z-index: 3; top: 0; bottom: 0; width: 2px; transform: translateX(-1px); background: #f3d26d; pointer-events: none; }
  .cue-list { min-height: 0; overflow: auto; border-left: 1px solid #3b443d; border-right: 1px solid #3b443d; }
  .cue-list button { display: grid; width: 100%; min-height: 64px; grid-template-columns: 28px 1fr; align-items: start; gap: 6px; padding: 10px; border: 0; border-bottom: 1px solid #323a34; border-radius: 0; background: transparent; text-align: left; }
  .cue-list button.active { background: #394f40; }
  .cue-list button.needs-change { box-shadow: inset 3px 0 #cf765e; }
  .cue-list button.locked { opacity: .68; }
  .cue-list small { color: #9da99d; }
  .cue-list .cue-time { grid-column: 2; font-size: 10px; }
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
