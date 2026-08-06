<script>
  import { onMount } from "svelte";
  import MediaStage from "./components/MediaStage.svelte";
  import ReviewWorkbench from "./components/ReviewWorkbench.svelte";
  import DetailsPanel from "./components/DetailsPanel.svelte";
  import { downloadText, resultFileName } from "./lib/download.js";
  import { loadDraft, removeDraft, saveDraft } from "./lib/storage.js";
  import {
    approveReviewedBlock,
    blockContentIssue,
    changedCount,
    createDraft,
    finalApprovalAllowed,
    formatTime,
    makeResult,
    mergeDraft,
    reviewedCueCount,
    reviewCueAndAdvance,
    textForDisplay,
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
  $: reviewed = packageData && draft ? reviewedCueCount(packageData, draft) : 0;

  function validateBrowserPackage(value) {
    if (!value || value.schema_version !== "framecue_package_v2") {
      throw new Error("這個頁面只能開啟不可變動的 FrameCue v2 審閱套件。");
    }
    if (!value.review_id || !value.revision || !value.content_checksum || !Array.isArray(value.cues) || !Array.isArray(value.blocks)) {
      throw new Error("這份 FrameCue v2 審閱套件不完整。");
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
      if (!response.ok) throw new Error(`無法載入 ${item.review_package}`);
      const nextPackage = await response.json();
      validateBrowserPackage(nextPackage);
      packageData = nextPackage;
      packageBase = new URL(".", packageUrl).href;
      draft = mergeDraft(nextPackage, loadDraft(nextPackage));
      currentItemId = item.id;
    } catch (cause) {
      packageData = null;
      draft = null;
      error = cause.message || "FrameCue 無法載入這份審閱套件。";
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
          throw new Error("framecue_manifest.json 不是 v2 審閱清單。");
        }
        items = manifest.items;
      }
    } catch (cause) {
      error = cause.message || "FrameCue 無法載入審閱清單。";
      loading = false;
      return;
    }
    if (!items.length) {
      items = [{ id: "default", label: "字幕審閱", review_package: "review_package.json" }];
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

  function navigateCue(direction) {
    if (!packageData || !selectedCue) return;
    const current = packageData.cues.findIndex((cue) => cue.id === selectedCue.id);
    const next = Math.max(0, Math.min(packageData.cues.length - 1, current + direction));
    selectCue(packageData.cues[next].id);
  }

  function setFilter(cueFilter) {
    persist({ ...draft, cue_filter: cueFilter });
  }

  function followPlaybackCue(cueId) {
    playbackCueId = cueId || "";
    if (cueId && cueId !== selectedCue?.id) selectCue(cueId);
  }

  function updateCue(cueId, patch) {
    const next = { ...patch };
    if (Object.hasOwn(next, "text")) next.text = textForDisplay(packageData, next.text);
    if (["image_carousel", "markdown"].includes(packageData.workflow.kind) && Object.hasOwn(next, "text")) {
      next.speech_text = next.text;
    }
    persist(withCueChange(packageData, draft, cueId, next));
  }

  function updateBlock(blockId, patch) {
    const next = { ...patch };
    if (Object.hasOwn(next, "target_text")) next.target_text = textForDisplay(packageData, next.target_text);
    if (Object.hasOwn(next, "speech_text")) next.speech_text = String(next.speech_text || "");
    persist(approveReviewedBlock(packageData, withBlockChange(draft, blockId, next), blockId));
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
    if (!window.confirm("要捨棄這個修訂版只存在瀏覽器中的草稿嗎？")) return;
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
      if (event.repeat || !selectedCue) return;
      window.dispatchEvent(new Event("framecue:pause-playback"));
      persist(reviewCueAndAdvance(packageData, draft, selectedCue.id));
    }
  }

  function pausePlaybackForEditor(event) {
    if (event.target?.matches?.("input, textarea, select")) {
      window.dispatchEvent(new Event("framecue:pause-playback"));
    }
  }

  onMount(() => {
    window.addEventListener("keydown", handleKeydown);
    window.addEventListener("focusin", pausePlaybackForEditor);
    boot();
    return () => {
      window.removeEventListener("keydown", handleKeydown);
      window.removeEventListener("focusin", pausePlaybackForEditor);
    };
  });
</script>

{#if loading}
  <main class="loading-state">正在載入 FrameCue 審閱套件</main>
{:else if error}
  <main class="error-state">
    <h1>FrameCue 無法開啟這份套件</h1>
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
            <span>套件</span>
            <select value={currentItemId} on:change={(event) => loadItem(items.find((item) => item.id === event.currentTarget.value))}>
              {#each items as item}
                <option value={item.id}>{item.label}</option>
              {/each}
            </select>
          </label>
        {/if}
        <span class="progress">已審 {reviewed} / {packageData.cues.length}</span>
        <span class="change-count">已變更 {changed} 項</span>
      </div>
      <div class="toolbar-actions">
        {#if ["subtitle", "redraw", "boundary", "hyperframes"].includes(packageData.workflow.kind)}
          <button type="button" on:click={downloadSrt}>輸出 SRT</button>
        {/if}
        <button type="button" on:click={downloadResult}>輸出審閱結果</button>
        <button class:approved={Boolean(draft.final_approval)} class="approve-package" disabled={!approvalAllowed || Boolean(draft.final_approval)} type="button" on:click={approvePackage}>
          {draft.final_approval ? "套件已核准" : "核准套件"}
        </button>
        <button class="icon-button" type="button" title="只捨棄這個修訂版的瀏覽器草稿" aria-label="捨棄瀏覽器草稿" on:click={resetDraft}>↺</button>
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
        onPlaybackCue={followPlaybackCue}
      />
      <ReviewWorkbench
        {packageData}
        {draft}
        {selectedCue}
        {selectedBlock}
        blockIssue={selectedBlockIssue}
        cueFilter={draft.cue_filter}
        {playbackCueId}
        onFilterChange={setFilter}
        onSelectCue={selectCue}
        onCueChange={updateCue}
        onBlockChange={updateBlock}
        onReplaceAll={replaceAll}
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
