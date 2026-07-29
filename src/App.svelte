<script>
  import { onMount } from "svelte";
  import MediaStage from "./components/MediaStage.svelte";
  import ReviewWorkbench from "./components/ReviewWorkbench.svelte";
  import DetailsPanel from "./components/DetailsPanel.svelte";
  import { downloadText, resultFileName } from "./lib/download.js";
  import { loadDraft, removeDraft, saveDraft } from "./lib/storage.js";
  import {
    blockContentIssue,
    changedCount,
    createDraft,
    finalApprovalAllowed,
    formatTime,
    makeResult,
    mergeDraft,
    textForDisplay,
    withBlockApproval,
    withBlockChange,
    withCueChange
  } from "./lib/review.js";

  let packageData = null;
  let packageBase = window.location.href;
  let draft = null;
  let items = [];
  let currentItemId = "";
  let loading = true;
  let error = "";
  let stageMode = "still";
  let playbackCueId = "";

  $: selectedCue = packageData?.cues.find((cue) => cue.id === draft?.selected_cue_id) || packageData?.cues[0] || null;
  $: selectedBlock = packageData?.blocks.find((block) => block.id === draft?.selected_block_id) || packageData?.blocks[0] || null;
  $: selectedScene = packageData?.scenes.find((scene) => scene.id === selectedCue?.scene_id) || null;
  $: approvalAllowed = packageData && draft ? finalApprovalAllowed(packageData, draft) : false;
  $: selectedBlockIssue = packageData && draft && selectedBlock ? blockContentIssue(packageData, draft, selectedBlock.id) : "";
  $: changed = packageData && draft ? changedCount(packageData, draft) : 0;

  function validateBrowserPackage(value) {
    if (!value || value.schema_version !== "framecue_package_v2") {
      throw new Error("This page only opens immutable FrameCue v2 packages.");
    }
    if (!value.review_id || !value.revision || !value.content_checksum || !Array.isArray(value.cues) || !Array.isArray(value.blocks)) {
      throw new Error("The FrameCue v2 package is incomplete.");
    }
  }

  function assetUrl(path) {
    return new URL(path || "", packageBase).href;
  }

  function persist(next) {
    draft = next;
    saveDraft(packageData, next);
  }

  async function loadItem(item) {
    loading = true;
    error = "";
    playbackCueId = "";
    stageMode = "still";
    try {
      const packageUrl = new URL(item.review_package, window.location.href);
      const response = await fetch(packageUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(`Could not load ${item.review_package}`);
      const nextPackage = await response.json();
      validateBrowserPackage(nextPackage);
      packageData = nextPackage;
      packageBase = new URL(".", packageUrl).href;
      draft = mergeDraft(nextPackage, loadDraft(nextPackage));
      currentItemId = item.id;
    } catch (cause) {
      packageData = null;
      draft = null;
      error = cause.message || "FrameCue could not load this review package.";
    } finally {
      loading = false;
    }
  }

  async function boot() {
    try {
      const response = await fetch("framecue_manifest.json", { cache: "no-store" });
      if (response.ok) {
        const manifest = await response.json();
        if (manifest.schema_version !== "framecue_manifest_v2" || !Array.isArray(manifest.items)) {
          throw new Error("framecue_manifest.json is not a v2 manifest.");
        }
        items = manifest.items;
      }
    } catch (cause) {
      error = cause.message || "FrameCue could not load its manifest.";
      loading = false;
      return;
    }
    if (!items.length) {
      items = [{ id: "default", label: "Review", review_package: "review_package.json" }];
    }
    await loadItem(items[0]);
  }

  function selectCue(cueId) {
    if (!packageData || !draft) return;
    const cue = packageData.cues.find((item) => item.id === cueId);
    if (!cue) return;
    const block = packageData.blocks.find((item) => item.cue_ids.includes(cueId));
    persist({
      ...draft,
      selected_cue_id: cueId,
      selected_block_id: block?.id || draft.selected_block_id
    });
  }

  function selectBlock(blockId) {
    if (!packageData || !draft) return;
    const block = packageData.blocks.find((item) => item.id === blockId);
    if (!block) return;
    persist({
      ...draft,
      active_scope: "block",
      selected_block_id: blockId,
      selected_cue_id: block.cue_ids[0] || draft.selected_cue_id
    });
  }

  function navigateCue(direction) {
    if (!packageData || !selectedCue) return;
    const current = packageData.cues.findIndex((cue) => cue.id === selectedCue.id);
    const next = Math.max(0, Math.min(packageData.cues.length - 1, current + direction));
    selectCue(packageData.cues[next].id);
  }

  function setScope(scope) {
    if (scope === "block" && !packageData.blocks.length) return;
    persist({ ...draft, active_scope: scope });
  }

  function setFilter(cueFilter) {
    persist({ ...draft, cue_filter: cueFilter });
  }

  function updateCue(cueId, patch) {
    const next = { ...patch };
    if (Object.hasOwn(next, "text")) next.text = textForDisplay(packageData, next.text);
    persist(withCueChange(packageData, draft, cueId, next));
  }

  function updateBlock(blockId, patch) {
    const next = { ...patch };
    if (Object.hasOwn(next, "target_text")) next.target_text = textForDisplay(packageData, next.target_text);
    if (Object.hasOwn(next, "speech_text")) next.speech_text = String(next.speech_text || "");
    persist(withBlockChange(draft, blockId, next));
  }

  function toggleBlockApproval(blockId, approved) {
    persist(withBlockApproval(packageData, draft, blockId, approved));
  }

  function replaceAll(search, replacement) {
    let count = 0;
    let next = draft;
    const changedBlocks = new Set();
    for (const cue of packageData.cues) {
      const current = next.cues[cue.id];
      if (!current.text.includes(search)) continue;
      next = withCueChange(packageData, next, cue.id, {
        text: textForDisplay(packageData, current.text.split(search).join(replacement))
      });
      packageData.blocks
        .filter((block) => block.cue_ids.includes(cue.id))
        .forEach((block) => changedBlocks.add(block.id));
      count += 1;
    }
    for (const blockId of changedBlocks) {
      const block = next.blocks[blockId];
      next = withBlockChange(next, blockId, {
        target_text: textForDisplay(packageData, block.target_text.split(search).join(replacement)),
        speech_text: block.speech_text.split(search).join(replacement)
      });
    }
    if (count) persist(next);
    return count;
  }

  function approvePackage() {
    if (!approvalAllowed) return;
    const approvedAt = new Date().toISOString();
    persist({ ...draft, final_approval: { approved_at: approvedAt } });
  }

  function downloadResult() {
    if (!packageData || !draft) return;
    const result = makeResult(packageData, draft, draft.final_approval?.approved_at || "");
    downloadText(resultFileName(packageData), "application/json", JSON.stringify(result, null, 2));
  }

  function downloadSrt() {
    if (!packageData || !draft) return;
    const srt = packageData.cues.map((cue, index) => [
      String(index + 1),
      `${srtTime(cue.start_ms)} --> ${srtTime(cue.end_ms)}`,
      draft.cues[cue.id].text,
      ""
    ].join("\n")).join("\n");
    downloadText(`${packageData.review_id}_${packageData.revision}_display.srt`, "application/x-subrip", srt);
  }

  function srtTime(milliseconds) {
    return formatTime(milliseconds).replace(".", ",");
  }

  function resetDraft() {
    if (!window.confirm("Discard this browser-only draft for the current immutable revision?")) return;
    removeDraft(packageData);
    draft = createDraft(packageData);
    stageMode = "still";
  }

  function handleKeydown(event) {
    if (["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(event.target.tagName)) return;
    if (["ArrowLeft", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      navigateCue(-1);
    }
    if (["ArrowRight", "ArrowDown"].includes(event.key)) {
      event.preventDefault();
      navigateCue(1);
    }
    if (event.key === " ") {
      event.preventDefault();
      window.dispatchEvent(new Event("framecue:toggle-playback"));
    }
  }

  onMount(() => {
    window.addEventListener("keydown", handleKeydown);
    boot();
    return () => window.removeEventListener("keydown", handleKeydown);
  });
</script>

{#if loading}
  <main class="loading-state">Loading FrameCue review package</main>
{:else if error}
  <main class="error-state">
    <h1>FrameCue could not open this package</h1>
    <p>{error}</p>
  </main>
{:else if packageData && draft}
  <main class="app-shell">
    <header class="top-toolbar">
      <div class="identity">
        <span class="wordmark">FrameCue</span>
        <span>{packageData.review_id}</span>
        <span class="revision">{packageData.revision}</span>
      </div>
      <div class="toolbar-center">
        {#if items.length > 1}
          <label class="package-select">
            <span>Package</span>
            <select value={currentItemId} on:change={(event) => loadItem(items.find((item) => item.id === event.currentTarget.value))}>
              {#each items as item}
                <option value={item.id}>{item.label}</option>
              {/each}
            </select>
          </label>
        {/if}
        <span class="progress">{selectedCue ? `${packageData.cues.findIndex((cue) => cue.id === selectedCue.id) + 1} / ${packageData.cues.length}` : "0 / 0"}</span>
        <span class="change-count">{changed} changed</span>
      </div>
      <div class="toolbar-actions">
        <button type="button" on:click={downloadSrt}>Export SRT</button>
        <button type="button" on:click={downloadResult}>Export result</button>
        <button class:approved={Boolean(draft.final_approval)} class="approve-package" disabled={!approvalAllowed || Boolean(draft.final_approval)} type="button" on:click={approvePackage}>
          {draft.final_approval ? "Package approved" : "Approve package"}
        </button>
        <button class="icon-button" type="button" title="Discard only the browser draft for this revision" aria-label="Discard browser draft" on:click={resetDraft}>↺</button>
      </div>
    </header>

    <div class="review-grid">
      <MediaStage
        {packageData}
        cue={selectedCue}
        cueDraft={selectedCue ? draft.cues[selectedCue.id] : null}
        {stageMode}
        {assetUrl}
        onStageMode={(mode) => stageMode = mode}
        onPlaybackCue={(cueId) => playbackCueId = cueId || ""}
      />
      <ReviewWorkbench
        {packageData}
        {draft}
        {selectedCue}
        {selectedBlock}
        blockIssue={selectedBlockIssue}
        activeScope={draft.active_scope}
        cueFilter={draft.cue_filter}
        {playbackCueId}
        onScopeChange={setScope}
        onFilterChange={setFilter}
        onSelectCue={selectCue}
        onSelectBlock={selectBlock}
        onCueChange={updateCue}
        onBlockChange={updateBlock}
        onBlockApproval={toggleBlockApproval}
        {replaceAll}
      />
    </div>
    <DetailsPanel
      {packageData}
      {selectedCue}
      {selectedScene}
      changed={changed}
      approvalAllowed={approvalAllowed}
      approvedAt={draft.final_approval?.approved_at || ""}
    />
  </main>
{/if}
