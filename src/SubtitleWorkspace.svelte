<script>
  import { onMount, tick } from "svelte";
  import MediaStage from "./components/MediaStage.svelte";
  import { formatTime } from "./lib/review.js";
  import {
    completeWorkspaceRound,
    loadWorkspaceSnapshot,
    nearestRemainingCueId,
    openWorkspaceEvents,
    resolveStructuralCueId,
    submitWorkspaceOperation
  } from "./lib/workspace.js";

  export let initialSnapshot;

  let snapshot = initialSnapshot;
  let selectedCueId = snapshot.document.cues[0]?.id || "";
  let editingCueId = "";
  let editCueId = "";
  let editText = "";
  let editor;
  let caretStart = null;
  let caretEnd = null;
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
  let agentCueId = "";
  let agentCategory = "other";
  let agentNote = "";
  let agentNoteOverrides = new Map();
  let agentMenu;
  let actionsMenu;
  let cueList;
  let agentSuggestions;
  let reloadQueued = false;
  let pendingNavigationIndex = -1;
  let navigationPromise = null;

  const agentCategories = [
    ["translation", "翻譯錯誤"],
    ["terminology", "用詞／術語"],
    ["segmentation", "字幕切分"],
    ["block_structure", "Block 分組"],
    ["tone", "語氣"],
    ["other", "其他"]
  ];

  const agentSuggestionStatuses = ["queued", "processing", "ready", "stale", "failed"];
  const agentSuggestionStatusLabels = {
    queued: "排隊中",
    processing: "處理中",
    ready: "可套用",
    stale: "已過期",
    failed: "失敗"
  };

  function normalizeAgentSuggestion(row) {
    const proposal = row?.proposal && typeof row.proposal === "object" ? row.proposal : {};
    const rawStatus = String(row?.status || "queued").toLowerCase();
    const status = {
      pending: "queued",
      candidate_ready: "ready",
      ready_for_review: "ready",
      suggestion_ready: "ready",
      suggestion_stale: "stale",
      suggestion_failed: "failed",
      error: "failed"
    }[rawStatus] || rawStatus;
    const cueIds = Array.isArray(row?.cue_ids)
      ? row.cue_ids.filter((cueId) => typeof cueId === "string" && cueId)
      : [row?.cue_id || proposal.cue_id].filter((cueId) => typeof cueId === "string" && cueId);
    return {
      ...row,
      job_id: row?.job_id || row?.id || "",
      cue_ids: cueIds,
      status,
      before: row?.before ?? row?.before_text ?? proposal.before ?? "",
      after: row?.after ?? row?.after_text ?? proposal.text ?? row?.proposed_text ?? "",
      explanation: row?.explanation ?? proposal.explanation ?? row?.note ?? "",
      error: typeof row?.error === "object"
        ? row.error?.message || row.error?.detail || ""
        : row?.error ?? row?.last_error ?? row?.message ?? ""
    };
  }

  function normalizeAgentSuggestions(rows) {
    return (Array.isArray(rows) ? rows : [])
      .map(normalizeAgentSuggestion)
      .filter((suggestion) => suggestion.job_id && suggestion.cue_ids.length
        && agentSuggestionStatuses.includes(suggestion.status));
  }

  function agentSuggestionStatusLabel(status) {
    return agentSuggestionStatusLabels[status] || "排隊中";
  }

  function cueAgentSuggestions(cueId) {
    return agentSuggestions.filter((suggestion) => suggestion.cue_ids.includes(cueId));
  }

  function selectedAgentJob(cueId, category) {
    return cueAgentSuggestions(cueId)
      .filter((suggestion) => suggestion.category === category
        && ["queued", "processing", "ready"].includes(suggestion.status))
      .at(-1) || null;
  }

  function syncAgentSuggestions(next) {
    if (Array.isArray(next?.suggestions)) agentSuggestions = normalizeAgentSuggestions(next.suggestions);
  }

  agentSuggestions = normalizeAgentSuggestions(snapshot.suggestions);

  $: cues = snapshot.document.cues || [];
  $: blocks = snapshot.document.blocks || [];
  $: blockById = new Map(blocks.map((block) => [block.id, block]));
  $: blockNumberById = new Map(blocks.map((block, index) => [block.id, index + 1]));
  $: cueGroups = (() => {
    const groups = [];
    cues.forEach((cue, index) => {
      const block = blockById.get(cue.block_id);
      const key = cue.block_id || `unassigned-${cue.id}`;
      let group = groups[groups.length - 1];
      if (!group || group.key !== key) {
        const startMs = snapshot.stage === "audiovisual_review" ? cue.output_start_ms : cue.source_start_ms;
        group = {
          key,
          block,
          cues: [],
          first_index: index,
          start_ms: startMs ?? cue.source_start_ms,
          end_ms: (snapshot.stage === "audiovisual_review" ? cue.output_end_ms : cue.source_end_ms) ?? cue.source_end_ms
        };
        groups.push(group);
      }
      group.cues.push(cue);
      group.end_ms = (snapshot.stage === "audiovisual_review" ? cue.output_end_ms : cue.source_end_ms) ?? cue.source_end_ms;
    });
    return groups;
  })();
  $: selectedCue = cues.find((cue) => cue.id === selectedCueId) || cues[0] || null;
  $: if (editingCueId && !cues.some((cue) => cue.id === editingCueId)) editingCueId = "";
  $: if (selectedCue && selectedCue.id !== editCueId) {
    editCueId = selectedCue.id;
    editText = selectedCue.display_text ?? selectedCue.text ?? "";
    caretStart = null;
    caretEnd = null;
  }
  $: sourcePackage = snapshot.document.source_package || {};
  $: outputBlockStartMs = new Map(blocks.map((block) => [
    block.id,
    Math.min(...cues.filter((cue) => cue.block_id === block.id).map((cue) => cue.output_start_ms).filter(Number.isInteger))
  ]));
  $: stageCues = cues.map((cue) => {
    const sourceId = cue.origin_cue_ids?.[0] || cue.id;
    const source = sourcePackage.cues?.find((item) => item.id === sourceId) || {};
    const outputTiming = snapshot.stage === "audiovisual_review"
      && Number.isInteger(cue.output_start_ms) && Number.isInteger(cue.output_end_ms);
    const blockStartMs = outputBlockStartMs.get(cue.block_id) || 0;
    return {
      ...source,
      ...cue,
      start_ms: outputTiming ? cue.output_start_ms : cue.source_start_ms ?? source.start_ms ?? 0,
      end_ms: outputTiming ? cue.output_end_ms : cue.source_end_ms ?? source.end_ms ?? 0,
      text: cue.display_text ?? cue.text ?? "",
      original_text: cue.source_text ?? source.original_text ?? "",
      audio: snapshot.candidate_audio?.[cue.block_id] || cue.audio,
      audio_start_ms: outputTiming ? cue.output_start_ms - blockStartMs : 0,
      audio_end_ms: outputTiming ? cue.output_end_ms - blockStartMs : null
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
  $: selectedIssues = selectedCue ? (snapshot.issues || []).filter((issue) =>
    issue.cue_ids?.includes(selectedCue.id)
  ) : [];
  $: selectedHasIssue = selectedOwnIssues.length > 0;
  $: activeSelectedAgentJob = selectedAgentJob(selectedCue?.id, agentCategory);
  $: if (selectedCue && selectedCue.id !== agentCueId) {
    agentCueId = selectedCue.id;
    const nextCategory = selectedOwnIssues[0]?.category || "other";
    agentCategory = nextCategory;
    agentNote = latestAgentNote(selectedCue.id, nextCategory);
  }
  $: agentPending = snapshot.stage.endsWith("_pending");
  $: selectedLock = snapshot.locks.find((lock) => lock.cue_id === selectedCue?.id) || null;
  $: lockedByOther = Boolean(selectedLock && selectedLock.session_id !== snapshot.session_id);
  $: isLead = snapshot.lead_session_id === snapshot.session_id;
  $: leadName = snapshot.participants.find((participant) => participant.session_id === snapshot.lead_session_id)?.display_name || "lead";
  $: reviewActionReason = !["content_review", "audiovisual_review"].includes(snapshot.stage)
    ? "目前不是可審查階段。"
    : !connected
      ? "目前離線，請重新同步後再修改。"
      : busy || completing
        ? "上一個操作仍在處理中。"
        : lockedByOther
          ? "這句 Cue 正由其他審稿者修改。"
          : "";
  $: canReview = !reviewActionReason;
  $: editReason = snapshot.stage !== "content_review"
    ? "配音審查只標記需修改，字幕結構維持唯讀。"
    : reviewActionReason || (phone ? "手機版僅供唯讀檢視。" : "");
  $: canEdit = !editReason;
  $: canType = canEdit && editingCueId === selectedCue?.id && heldCueIds.includes(selectedCue?.id);
  $: canDecideAgentSuggestion = connected && !busy && !completing && !lockedByOther
    && snapshot.stage === "content_review";
  $: canComplete = canEdit && isLead && !localDirty && !snapshot.participants.some((participant) => participant.dirty) && !snapshot.locks.length;
  $: selectedCueIndex = cues.findIndex((cue) => cue.id === selectedCue?.id);
  $: previousMergeCue = selectedCueIndex > 0 ? cues[selectedCueIndex - 1] : null;
  $: nextMergeCue = selectedCueIndex >= 0 ? cues[selectedCueIndex + 1] || null : null;
  $: splitReason = splitAvailabilityReason(selectedCue, editReason, caretStart, caretEnd, editText);
  $: previousMergeReason = mergeReason(previousMergeCue, selectedCue, editReason, blocks);
  $: nextMergeReason = mergeReason(selectedCue, nextMergeCue, editReason, blocks);
  $: flagReason = !selectedCue ? "沒有可標記的 Cue。" : reviewActionReason;
  $: deleteReason = !selectedCue
    ? "沒有可刪除的 Cue。"
    : cues.length <= 1
      ? "Workspace 至少要保留一個 Cue。"
      : editReason;
  $: selectedBlockIndex = blocks.findIndex((block) => block.id === selectedCue?.block_id);
  $: selectedBlock = selectedBlockIndex >= 0 ? blocks[selectedBlockIndex] : null;
  $: previousBlock = selectedBlockIndex > 0 ? blocks[selectedBlockIndex - 1] : null;
  $: nextBlock = selectedBlockIndex >= 0 ? blocks[selectedBlockIndex + 1] || null : null;
  $: previousBlockMergeReason = blockMergeReason(previousBlock, selectedBlock, editReason);
  $: nextBlockMergeReason = blockMergeReason(selectedBlock, nextBlock, editReason);
  $: blockSplitReason = blockSplitAvailabilityReason(selectedBlock, selectedCue, editReason);

  function blockTimingState(cueRows) {
    if (cueRows.every((cue) => Number.isInteger(cue.output_start_ms) && Number.isInteger(cue.output_end_ms))) {
      return "配音已對齊";
    }
    return cueRows.some((cue) => cue.timing_state === "provisional") ? "暫定切分" : "來源時間";
  }

  function blockReviewState(cueRows) {
    const cueIds = new Set(cueRows.map((cue) => cue.id));
    const count = (snapshot.issues || []).filter((issue) => issue.cue_ids?.some((cueId) => cueIds.has(cueId))).length;
    return count ? `需修改 ${count}` : "未標記";
  }

  function cueIssues(cueId) {
    return (snapshot.issues || []).filter((issue) => issue.cue_ids?.includes(cueId));
  }

  function latestAgentNote(cueId, category) {
    const key = `${cueId}:${category}`;
    if (agentNoteOverrides.has(key)) return agentNoteOverrides.get(key);
    const jobs = agentSuggestions.filter((suggestion) =>
      suggestion.cue_ids.includes(cueId) && suggestion.category === category
    );
    if (jobs.length) return jobs.at(-1).note || "";
    const notes = (snapshot.issues || [])
      .filter((issue) => issue.cue_ids?.includes(cueId)
        && issue.authors?.includes(snapshot.display_name)
        && issue.category === category)
      .flatMap((issue) => Array.isArray(issue.notes) ? issue.notes : []);
    return notes.at(-1) || "";
  }

  function syncAgentCategoryNote() {
    agentNote = latestAgentNote(selectedCue?.id, agentCategory);
  }

  function agentIssueSummary(issue, noteOverride) {
    const category = agentCategories.find(([value]) => value === issue.category)?.[1] || issue.category;
    const note = noteOverride === undefined
      ? latestAgentNote(selectedCue?.id, issue.category)
      : noteOverride;
    return note ? `${category} · ${note}` : category;
  }

  function blockLockReason(blockRows) {
    const cueIds = blockRows.flatMap((block) => block?.cue_ids || []);
    const lock = snapshot.locks.find((item) => item.session_id !== snapshot.session_id && cueIds.includes(item.cue_id));
    if (!lock) return "";
    const owner = snapshot.participants.find((participant) => participant.session_id === lock.session_id)?.display_name || "其他審稿者";
    return `${owner} 正在修改這個 Block 的字幕。`;
  }

  function blockMergeReason(left, right, currentEditReason) {
    if (currentEditReason) return currentEditReason;
    if (!left || !right) return "沒有相鄰 Block 可合併。";
    return blockLockReason([left, right]);
  }

  function blockSplitAvailabilityReason(block, cue, currentEditReason) {
    if (currentEditReason) return currentEditReason;
    if (!block || !cue || !Array.isArray(block.cue_ids)) return "Cue 缺少可切分的 Semantic Block。";
    if (block.cue_ids[0] === cue.id) return "請選擇 Block 第一句以外的 Cue。";
    if (!block.cue_ids.includes(cue.id)) return "Cue 與 Semantic Block 的歸屬不一致。";
    return blockLockReason([block]);
  }

  function mergeReason(left, right, currentEditReason, blockRows) {
    if (currentEditReason) return currentEditReason;
    if (!left || !right) return "沒有相鄰 Cue 可合併。";
    const adjacentLock = snapshot.locks.find((lock) =>
      lock.session_id !== snapshot.session_id && [left.id, right.id].includes(lock.cue_id)
    );
    if (adjacentLock) {
      const owner = snapshot.participants.find((participant) => participant.session_id === adjacentLock.session_id)?.display_name || "其他審稿者";
      return `${owner} 正在修改相鄰 Cue。`;
    }
    const leftBlockIndex = blockRows.findIndex((block) => block.id === left.block_id);
    const rightBlockIndex = blockRows.findIndex((block) => block.id === right.block_id);
    if (leftBlockIndex < 0 || rightBlockIndex < 0) return "Cue 缺少可合併的 Semantic Block。";
    const leftBlock = blockRows[leftBlockIndex];
    const rightBlock = blockRows[rightBlockIndex];
    if (!Array.isArray(leftBlock.cue_ids) || !Array.isArray(rightBlock.cue_ids)) {
      return "Semantic Block 的 Cue 清單不完整。";
    }
    const leftCueIndex = leftBlock.cue_ids.indexOf(left.id);
    const rightCueIndex = rightBlock.cue_ids.indexOf(right.id);
    if (leftCueIndex < 0 || rightCueIndex < 0) return "Cue 與 Semantic Block 的歸屬不一致。";
    if (left.block_id === right.block_id) {
      return rightCueIndex === leftCueIndex + 1 ? "" : "只能合併同一 Semantic Block 中相鄰的 Cue。";
    }
    if (
      rightBlockIndex !== leftBlockIndex + 1
      || leftCueIndex !== leftBlock.cue_ids.length - 1
      || rightCueIndex !== 0
    ) {
      return "只能合併相鄰 Semantic Blocks 邊界上的 Cue。";
    }
    return "";
  }

  function splitAvailabilityReason(cue, currentEditReason, start, end, text) {
    if (!cue) return "沒有可切分的 Cue。";
    if (currentEditReason) return currentEditReason;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start !== end) {
      return "請先在字幕文字中放置單一游標。";
    }
    if (start <= 0 || start >= text.length) return "游標需位於字幕文字中間。";
    return "";
  }

  function updateCaret(target = editor) {
    caretStart = target?.selectionStart;
    caretEnd = target?.selectionEnd;
  }

  function mergeLabel(left, right, direction) {
    return left?.block_id && right?.block_id && left.block_id !== right.block_id
      ? `跨區塊：合併字幕與區塊（${direction}）`
      : `同區塊：與${direction}合併`;
  }

  function assetUrl(path) {
    return new URL(path || "", window.location.href).href;
  }

  function mergeSnapshot(changed) {
    if (!changed?.document || changed.snapshot_version < snapshot.snapshot_version) return;
    snapshot = { ...snapshot, ...changed, schema: snapshot.schema, csrf_token: snapshot.csrf_token };
    syncAgentSuggestions(changed);
  }

  async function post(operation) {
    const changed = await submitWorkspaceOperation(snapshot, operation, { baseHref: window.location.href });
    mergeSnapshot(changed);
    return changed;
  }

  async function reload() {
    if (busy) {
      reloadQueued = true;
      return;
    }
    try {
      const next = await loadWorkspaceSnapshot({ baseHref: window.location.href });
      if (!next) throw new Error("Workspace 已停止提供資料。");
      if (next.snapshot_version < snapshot.snapshot_version) return;
      snapshot = next;
      syncAgentSuggestions(next);
      if (!next.document.cues.some((cue) => cue.id === selectedCueId)) {
        selectedCueId = next.document.cues[0]?.id || "";
      }
      message = "";
    } catch (cause) {
      message = cause?.message || "同步失敗";
    }
  }

  async function flushQueuedReload() {
    if (!reloadQueued) return;
    reloadQueued = false;
    await reload();
  }

  async function submit(operation) {
    const allowed = ["flag"].includes(operation?.kind) ? canReview
      : ["suggestion_apply", "suggestion_ignore"].includes(operation?.kind)
        ? canDecideAgentSuggestion
        : canEdit;
    if (!allowed) return null;
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

  async function selectCue(cueId, { center = false } = {}) {
    pendingNavigationIndex = -1;
    if (cueId === selectedCueId) return;
    await leaveEditor();
    editingCueId = "";
    selectedCueId = cueId;
    playbackMs = cues.find((cue) => cue.id === cueId)?.source_start_ms || 0;
    await post({ kind: "presence", selected_cue_id: cueId });
    if (center) await centerBrowseCue(cueId);
  }

  async function centerBrowseCue(cueId) {
    await tick();
    const row = [...(cueList?.querySelectorAll(".cue-row") || [])]
      .find((item) => item.dataset.cueId === cueId);
    row?.scrollIntoView({ behavior: "smooth", block: "center" });
    if (!document.activeElement?.closest?.(".cue-inline-editor")) {
      row?.querySelector(".cue-row-select")?.focus({ preventScroll: true });
    }
  }

  function flushCueNavigation() {
    if (navigationPromise) return navigationPromise;
    navigationPromise = (async () => {
      try {
        await tick();
        await leaveEditor();
        while (pendingNavigationIndex >= 0) {
          const targetIndex = pendingNavigationIndex;
          const cueId = cues[targetIndex]?.id;
          if (!cueId) break;
          await post({ kind: "presence", selected_cue_id: cueId });
          if (targetIndex !== pendingNavigationIndex) continue;
          pendingNavigationIndex = -1;
          if (!editingCueId && selectedCueId === cueId) await centerBrowseCue(cueId);
        }
      } catch (cause) {
        pendingNavigationIndex = -1;
        message = cause?.message || "無法切換字幕";
        await reload();
      } finally {
        navigationPromise = null;
        if (pendingNavigationIndex >= 0) flushCueNavigation();
      }
    })();
    return navigationPromise;
  }

  async function enterEditMode(cueId, caret = "end") {
    pendingNavigationIndex = -1;
    if (!canEdit) {
      message = editReason;
      return;
    }
    if (cueId !== selectedCueId) await selectCue(cueId);
    editingCueId = cueId;
    await tick();
    const row = [...(cueList?.querySelectorAll(".cue-row") || [])]
      .find((item) => item.dataset.cueId === cueId);
    row?.scrollIntoView({ behavior: "smooth", block: "center" });
    editor?.focus({ preventScroll: true });
    if (editor) {
      const offset = caret === "start" ? 0 : editor.value.length;
      editor.setSelectionRange(offset, offset);
      updateCaret(editor);
    }
  }

  async function exitEditMode() {
    if (!editingCueId) return;
    const cueId = editingCueId;
    await leaveEditor();
    editingCueId = "";
    await tick();
    const row = [...(cueList?.querySelectorAll(".cue-row") || [])]
      .find((item) => item.dataset.cueId === cueId);
    row?.querySelector(".cue-row-select")?.focus({ preventScroll: true });
  }

  function followPlaybackCue(cueId) {
    if (editingCueId || document.activeElement === editor || heldCueIds.length || localDirty) return;
    if (cues.some((cue) => cue.id === cueId)) selectCue(cueId);
  }

  async function structuralOperation(operation, cueIds) {
    if (!canEdit) return;
    let result = null;
    const selectedBefore = selectedCueId;
    const focused = document.activeElement;
    const anchorRow = focused?.closest?.(".cue-row")
      || focused?.closest?.(".block-group")?.querySelector(".cue-row")
      || editor?.closest?.(".cue-row");
    const anchorCueId = anchorRow?.dataset.cueId || selectedBefore;
    const anchorTop = anchorRow?.getBoundingClientRect().top;
    const scrollsInList = cueList && cueList.scrollHeight > cueList.clientHeight + 1;
    const restoreEditor = editingCueId === selectedBefore
      && (focused === editor || Boolean(focused?.closest?.(".cue-inline-editor")));
    await leaveEditor();
    busy = true;
    message = "";
    try {
      await lockCues(cueIds);
      const changed = await post(operation);
      result = changed;
      heldCueIds = [];
      selectedCueId = resolveStructuralCueId(changed.document.cues, operation, selectedBefore);
      await post({ kind: "presence", selected_cue_id: selectedCueId });
    } catch (cause) {
      message = cause?.message || "字幕結構修改失敗";
      try { await unlockCues(); } catch { /* server may have already released transformed Cue IDs */ }
      await reload();
    } finally {
      busy = false;
    }
    editingCueId = restoreEditor ? selectedCueId : "";
    await flushQueuedReload();
    await tick();
    const resultAnchorId = snapshot.document.cues.some((cue) => cue.id === anchorCueId)
      ? anchorCueId
      : selectedCueId;
    const resultRow = [...(cueList?.querySelectorAll(".cue-row") || [])]
      .find((row) => row.dataset.cueId === resultAnchorId);
    if (Number.isFinite(anchorTop) && resultRow) {
      const delta = resultRow.getBoundingClientRect().top - anchorTop;
      if (scrollsInList) cueList.scrollTop += delta;
      else window.scrollBy(0, delta);
    }
    const focusTarget = restoreEditor ? editor : resultRow?.querySelector(".cue-row-select");
    focusTarget?.focus({ preventScroll: true });
    return result;
  }

  async function splitCue() {
    if (splitReason || !selectedCue) {
      if (splitReason) message = splitReason;
      return;
    }
    const cursor = caretStart;
    if (!Number.isInteger(cursor)) return;
    await structuralOperation({ kind: "split", cue_id: selectedCue.id, cursor }, [selectedCue.id]);
  }

  async function mergeCues(left, right, reason) {
    if (reason || !left || !right) {
      if (reason) message = reason;
      return;
    }
    await structuralOperation(
      { kind: "merge", cue_id: left.id, adjacent_cue_id: right.id },
      [left.id, right.id]
    );
  }

  function mergePreviousCue() {
    return mergeCues(previousMergeCue, selectedCue, previousMergeReason);
  }

  function mergeNextCue() {
    return mergeCues(selectedCue, nextMergeCue, nextMergeReason);
  }

  async function deleteCue() {
    if (deleteReason || !selectedCue) {
      if (deleteReason) message = deleteReason;
      return;
    }
    const cueId = selectedCue.id;
    const selectionIndex = selectedCueIndex;
    if (!window.confirm(`刪除整個 Cue #${selectionIndex + 1}？這會移除這句字幕文字。`)) return;
    const changed = await structuralOperation(
      { kind: "delete", cue_id: cueId, selection_index: selectionIndex },
      [cueId]
    );
    if (!changed) return;
    selectedCueId = nearestRemainingCueId(snapshot.document.cues, selectionIndex);
    message = "已刪除整個 Cue";
  }

  function mergeBlocks(left, right, reason) {
    if (reason || !left || !right) {
      if (reason) message = reason;
      return;
    }
    return structuralOperation(
      { kind: "block_merge", block_id: left.id, adjacent_block_id: right.id },
      [...left.cue_ids, ...right.cue_ids]
    );
  }

  function mergePreviousBlock() {
    return mergeBlocks(previousBlock, selectedBlock, previousBlockMergeReason);
  }

  function mergeNextBlock() {
    return mergeBlocks(selectedBlock, nextBlock, nextBlockMergeReason);
  }

  function splitBlock() {
    if (blockSplitReason || !selectedBlock || !selectedCue) {
      if (blockSplitReason) message = blockSplitReason;
      return;
    }
    return structuralOperation(
      { kind: "block_split", block_id: selectedBlock.id, cue_id: selectedCue.id },
      selectedBlock.cue_ids
    );
  }

  function mergeBlockBoundary(groupIndex) {
    const left = cueGroups[groupIndex - 1]?.block;
    const right = cueGroups[groupIndex]?.block;
    return mergeBlocks(left, right, blockMergeReason(left, right, editReason));
  }

  function splitBlockBoundary(block, cue) {
    const reason = blockSplitAvailabilityReason(block, cue, editReason);
    if (reason || !block || !cue) {
      if (reason) message = reason;
      return;
    }
    return structuralOperation(
      { kind: "block_split", block_id: block.id, cue_id: cue.id },
      block.cue_ids
    );
  }

  function closeOtherMenu(opened, other) {
    if (opened?.open && other) other.open = false;
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
    await flushQueuedReload();
  }

  async function toggleSelectedIssue() {
    if (flagReason || !selectedCue) {
      if (flagReason) message = flagReason;
      return;
    }
    await leaveEditor();
    if (!selectedOwnIssues.length) {
      const saved = await submit({ kind: "flag", cue_ids: [selectedCue.id], categories: ["other"], author: snapshot.display_name || "reviewer" });
      if (saved && snapshot.stage === "content_review") queueLocalAgentSuggestion(selectedCue, "other", "");
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

  function queueLocalAgentSuggestion(cue, category, note) {
    if (!cue || agentSuggestions.some((suggestion) =>
      suggestion.cue_ids.includes(cue.id) && !suggestion.job_id.startsWith("local-")
    )) return;
    agentSuggestions = [
      ...agentSuggestions.filter((suggestion) =>
        !(suggestion.job_id.startsWith("local-") && suggestion.cue_ids.includes(cue.id))
      ),
      normalizeAgentSuggestion({
        job_id: `local-${cue.id}`,
        status: "queued",
        cue_ids: [cue.id],
        category,
        note,
        before: cue.display_text ?? cue.text ?? ""
      })
    ];
  }

  async function decideAgentSuggestion(suggestion, decision) {
    if (!suggestion || suggestion.status !== "ready" || !["apply", "ignore"].includes(decision)) return;
    if (!canDecideAgentSuggestion) {
      message = reviewActionReason || "目前階段無法決定 Agent 建議。";
      return;
    }
    syncing = true;
    message = "";
    try {
      await post({
        kind: `suggestion_${decision}`,
        job_id: suggestion.job_id
      });
      agentSuggestions = agentSuggestions.filter((item) => item.job_id !== suggestion.job_id);
      message = decision === "apply" ? "已套用 Agent 建議" : "已忽略 Agent 建議";
    } catch (cause) {
      message = cause?.message || "Agent 建議同步失敗";
      await reload();
    } finally {
      syncing = false;
    }
  }

  async function saveAgentRequest() {
    if (flagReason || !selectedCue) {
      if (flagReason) message = flagReason;
      return;
    }
    await leaveEditor();
    for (const issue of selectedOwnIssues.filter((issue) => issue.category !== agentCategory)) {
      await submit({
        kind: "flag",
        cue_ids: issue.cue_ids,
        categories: [issue.category],
        author: snapshot.display_name || "reviewer",
        enabled: false
      });
    }
    const saved = await submit({
      kind: "flag",
      cue_ids: [selectedCue.id],
      categories: [agentCategory],
      author: snapshot.display_name || "reviewer",
      note: agentNote.trim()
    });
    if (saved) {
      const nextOverrides = new Map(agentNoteOverrides);
      for (const issue of selectedOwnIssues) {
        if (issue.category !== agentCategory) nextOverrides.delete(`${selectedCue.id}:${issue.category}`);
      }
      nextOverrides.set(`${selectedCue.id}:${agentCategory}`, agentNote.trim());
      agentNoteOverrides = nextOverrides;
      queueLocalAgentSuggestion(selectedCue, agentCategory, agentNote.trim());
      message = `已記錄給 Agent：${agentIssueSummary({ category: agentCategory }, agentNote.trim())}`;
    }
  }

  async function clearAgentRequest() {
    if (!selectedOwnIssues.length) return;
    await leaveEditor();
    for (const issue of selectedOwnIssues) {
      await submit({
        kind: "flag",
        cue_ids: issue.cue_ids,
        categories: [issue.category],
        author: snapshot.display_name || "reviewer",
        enabled: false
      });
    }
    const nextOverrides = new Map(agentNoteOverrides);
    for (const issue of selectedOwnIssues) nextOverrides.delete(`${selectedCue.id}:${issue.category}`);
    agentNoteOverrides = nextOverrides;
    agentSuggestions = agentSuggestions.filter((suggestion) =>
      !(suggestion.job_id.startsWith("local-") && suggestion.cue_ids.includes(selectedCue?.id))
    );
    message = "已取消交給 Agent 的記錄";
  }

  function insertCueLineBreak(target) {
    const start = target.selectionStart;
    const end = target.selectionEnd;
    target.setRangeText("\n", start, end, "end");
    editText = target.value;
    updateCaret(target);
    handleInput();
  }

  function handleKeys(event) {
    if (event.isComposing || event.keyCode === 229) return;
    const editing = event.target === editor && editingCueId === selectedCueId;
    const interactive = ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "SUMMARY"].includes(event.target?.tagName);
    const mShortcutAllowed = (!interactive || event.target?.closest?.(".cue-row-select"))
      && !event.target?.closest?.("details");
    const deleteShortcutAllowed = !editingCueId
      && (!interactive || event.target?.closest?.(".cue-row-select"))
      && !event.target?.closest?.("details");
    if (editing) updateCaret(event.target);
    const enterSplitReason = editing
      ? splitAvailabilityReason(selectedCue, editReason, event.target.selectionStart, event.target.selectionEnd, event.target.value)
      : "";
    if (!interactive && event.key === " ") {
      event.preventDefault();
      window.dispatchEvent(new Event("framecue:toggle-playback"));
    }
    const cueNavigationAllowed = !interactive || Boolean(event.target?.closest?.(".cue-row-select"));
    if (!editingCueId && cueNavigationAllowed && ["ArrowUp", "ArrowDown"].includes(event.key) && selectedCueIndex >= 0) {
      const navigationIndex = pendingNavigationIndex >= 0 ? pendingNavigationIndex : selectedCueIndex;
      const nextIndex = Math.min(cues.length - 1, Math.max(0, navigationIndex + (event.key === "ArrowUp" ? -1 : 1)));
      if (nextIndex !== navigationIndex) {
        event.preventDefault();
        pendingNavigationIndex = nextIndex;
        selectedCueId = cues[nextIndex].id;
        editingCueId = "";
        playbackMs = cues[nextIndex].source_start_ms || 0;
        flushCueNavigation();
      }
    }
    if (mShortcutAllowed && event.key.toLowerCase() === "m" && canReview) {
      event.preventDefault();
      toggleSelectedIssue();
    }
    if (deleteShortcutAllowed && event.shiftKey && ["Delete", "Backspace"].includes(event.key) && canEdit) {
      event.preventDefault();
      deleteCue();
    }
    if (editing && event.key === "Escape") {
      event.preventDefault();
      exitEditMode();
      return;
    }
    if (editing && event.key === "Enter" && !canType) {
      event.preventDefault();
      message = editReason || "正在取得這句字幕的編輯權限。";
      return;
    }
    if (editing && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      insertCueLineBreak(event.target);
    } else if (editing && event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.altKey) {
      event.preventDefault();
      if (enterSplitReason) message = enterSplitReason;
      else structuralOperation({ kind: "split", cue_id: selectedCue.id, cursor: event.target.selectionStart }, [selectedCue.id]);
    }
    if (editing && !event.metaKey && !event.ctrlKey && !event.altKey
      && event.target.selectionStart === event.target.selectionEnd) {
      if (event.key === "ArrowLeft" && event.target.selectionStart === 0 && previousMergeCue) {
        event.preventDefault();
        enterEditMode(previousMergeCue.id, "end");
        return;
      }
      if (event.key === "ArrowRight" && event.target.selectionStart === event.target.value.length && nextMergeCue) {
        event.preventDefault();
        enterEditMode(nextMergeCue.id, "start");
        return;
      }
      if (event.key === "Backspace" && event.target.selectionStart === 0 && !previousMergeReason) {
        event.preventDefault();
        mergePreviousCue();
      }
      if (event.key === "Delete" && event.target.selectionStart === event.target.value.length && !nextMergeReason) {
        event.preventDefault();
        mergeNextCue();
      }
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
    <div class="workspace-counts" role="status" aria-label="本輪摘要">
      <span>待 Agent {issueCount}</span>
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
    <p class="pending-note">修改工作單已建立，等待 Agent 接手；本輪內容暫時唯讀。</p>
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
      subtitleOnlyVideo={Boolean(sourcePackage.media?.video)}
      audiovisualAddon={snapshot.audiovisual_addon}
      {assetUrl}
      onStageMode={(mode) => { stageMode = mode; }}
      onPlaybackCue={followPlaybackCue}
      onPlaybackTime={(currentMs, durationMs = 0) => {
        playbackMs = currentMs;
        if (durationMs > 0) mediaDurationMs = durationMs;
      }}
    />

    <section class="cue-workspace" class:content-review={snapshot.stage === "content_review"} aria-label="字幕工作區">
      {#if snapshot.stage !== "content_review"}
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
      {/if}

      <div class="cue-list" bind:this={cueList} role="region" aria-label="字幕清單">
        {#each cueGroups as group, groupIndex}
          <section class="block-group" class:active={group.cues.some((cue) => cue.id === selectedCue?.id)} aria-label={`Semantic Block ${group.block?.id || "未歸屬"}`}>
            {#if groupIndex > 0 && !blockMergeReason(cueGroups[groupIndex - 1]?.block, group.block, editReason)}
              <button class="block-rail-handle merge" type="button" aria-label={`合併 Block ${cueGroups[groupIndex - 1]?.block?.id} 與 ${group.block?.id}`} title="移除這個 Block 邊界" on:click={() => mergeBlockBoundary(groupIndex)}>
                +<span>合併上方 Block</span>
              </button>
            {/if}
            <header class="block-header">
              <strong>Block {String(groupIndex + 1).padStart(2, "0")}</strong>
              <span>{group.cues.length} Cue · 時長 {formatTime(Math.max(0, group.end_ms - group.start_ms))}</span>
              <span>{blockTimingState(group.cues)}</span>
              <span>{blockReviewState(group.cues)}</span>
            </header>
            <div class="block-cues">
              {#each group.cues as cue, cueIndex}
                <article
                  class="cue-row"
                  data-cue-id={cue.id}
                  class:active={cue.id === selectedCue?.id}
                  class:needs-change={(snapshot.issues || []).some((issue) => issue.cue_ids?.includes(cue.id))}
                  class:locked={snapshot.locks.some((lock) => lock.cue_id === cue.id && lock.session_id !== snapshot.session_id)}
                  aria-current={cue.id === selectedCue?.id ? "true" : undefined}
                >
                  {#if cueIndex > 0 && !blockSplitAvailabilityReason(group.block, cue, editReason)}
                    <button class="block-rail-handle split" type="button" aria-label={`從 Cue ${group.first_index + cueIndex + 1} 前切開 Block`} title="從這裡切開 Block" on:click={() => splitBlockBoundary(group.block, cue)}>
                      −<span>從這裡切開 Block</span>
                    </button>
                  {/if}
                  <button
                    class="cue-row-select"
                    type="button"
                    aria-label={`選擇 Cue ${group.first_index + cueIndex + 1}：${cue.display_text}`}
                    on:click={() => cue.id !== selectedCue?.id && selectCue(cue.id)}
                    on:dblclick={() => enterEditMode(cue.id)}
                  >
                    <span class="cue-meta">
                      <small>#{group.first_index + cueIndex + 1}</small>
                      <small>{formatTime(cue.source_start_ms)}–{formatTime(cue.source_end_ms)}</small>
                      <small>Block {String(blockNumberById.get(cue.block_id) || "?").padStart(2, "0")}</small>
                      {#if cueIssues(cue.id).length}<small class="cue-state needs-change">待 Agent 修改</small>{/if}
                      {#each cueAgentSuggestions(cue.id) as suggestion}
                        <small class={`cue-state agent-status ${suggestion.status}`} aria-label={`Agent 建議：${agentSuggestionStatusLabel(suggestion.status)}`}>{agentSuggestionStatusLabel(suggestion.status)}</small>
                      {/each}
                      {#if snapshot.locks.some((lock) => lock.cue_id === cue.id && lock.session_id !== snapshot.session_id)}<small class="cue-state locked">他人編輯中</small>{/if}
                      {#if snapshot.participants.some((participant) => participant.selected_cue_id === cue.id && participant.session_id !== snapshot.session_id)}
                        <small class="cue-presence">{snapshot.participants.filter((participant) => participant.selected_cue_id === cue.id && participant.session_id !== snapshot.session_id).map((participant) => participant.display_name).join(", ")}</small>
                      {/if}
                    </span>
                    {#if editingCueId !== cue.id}
                      <span class="cue-text">{cue.display_text}</span>
                    {/if}
                  </button>

                  {#if cue.id === selectedCue?.id}
                    <div class="cue-inline-editor">
                      <div class="cue-editing-bar">
                        <strong>{editingCueId === cue.id ? "正在編輯" : "已選取 · 雙擊字幕修稿"}</strong>
                        <details class="agent-inline" bind:this={agentMenu} on:toggle={() => closeOtherMenu(agentMenu, actionsMenu)}>
                          <summary>{activeSelectedAgentJob ? `Agent · ${agentSuggestionStatusLabel(activeSelectedAgentJob.status)}` : selectedOwnIssues.length ? "Agent · 尚未送出" : selectedIssues.length ? "他人已標記" : "Agent 設定"}</summary>
                          <div class="agent-controls">
                            <label><span class="sr-only">Agent 問題類型</span>
                              <select name="agent-category" bind:value={agentCategory} on:change={syncAgentCategoryNote} disabled={Boolean(flagReason)} aria-label="Agent 問題類型">
                                {#each agentCategories as category}<option value={category[0]}>{category[1]}</option>{/each}
                              </select>
                            </label>
                            <label class="agent-note"><span class="sr-only">給 Agent 的補充說明</span>
                              <input name="agent-note" bind:value={agentNote} disabled={Boolean(flagReason)} placeholder="給 Agent 的補充說明（選填）" />
                            </label>
                            <button type="button" disabled={Boolean(flagReason)} on:click={saveAgentRequest}>{activeSelectedAgentJob ? "更新提示" : "送出給 Agent"}</button>
                            {#if selectedHasIssue}<button type="button" class="quiet" on:click={clearAgentRequest}>取消標記</button>{/if}
                          </div>
                        </details>
                        {#if selectedOwnIssues.length}
                          <span class="agent-recorded" aria-live="polite" title={selectedOwnIssues.map((issue) => agentIssueSummary(issue)).join("；")}>
                            <strong>{activeSelectedAgentJob ? "已送出給 Agent" : "尚未送出給 Agent"}</strong>
                            <span>{selectedOwnIssues.map((issue) => agentIssueSummary(issue)).join("；")}</span>
                          </span>
                        {/if}
                      </div>
                      {#if cueAgentSuggestions(cue.id).length}
                        <section class="agent-suggestions" aria-label="Agent 建議狀態">
                          {#each cueAgentSuggestions(cue.id) as suggestion}
                            <article class="agent-suggestion" class:ready={suggestion.status === "ready"} data-job-id={suggestion.job_id}>
                              <div class="agent-suggestion-heading">
                                <strong>Agent 建議</strong>
                                <span class={`agent-status ${suggestion.status}`} role="status" aria-live="polite">{agentSuggestionStatusLabel(suggestion.status)}</span>
                              </div>
                              {#if suggestion.category}<small class="agent-suggestion-category">{agentCategories.find(([value]) => value === suggestion.category)?.[1] || suggestion.category}</small>{/if}
                              {#if suggestion.status === "ready"}
                                <div class="agent-diff" aria-label="Agent 建議差異">
                                  <div>
                                    <strong>修改前</strong>
                                    <p>{suggestion.before || cue.display_text || "（無文字）"}</p>
                                  </div>
                                  <div>
                                    <strong>修改後</strong>
                                    <p>{suggestion.after || "（沒有替換文字）"}</p>
                                  </div>
                                </div>
                                {#if suggestion.explanation}<p class="agent-suggestion-note">{suggestion.explanation}</p>{/if}
                                <div class="agent-suggestion-actions" role="group" aria-label="Agent 建議操作">
                                  <button type="button" disabled={!canDecideAgentSuggestion} aria-label="套用 Agent 建議" on:click={() => decideAgentSuggestion(suggestion, "apply")}>套用</button>
                                  <button type="button" class="quiet" disabled={!canDecideAgentSuggestion} aria-label="忽略 Agent 建議" on:click={() => decideAgentSuggestion(suggestion, "ignore")}>忽略</button>
                                </div>
                              {:else if suggestion.status === "stale"}
                                <p class="agent-suggestion-error">這個建議已過期，請重新標記。</p>
                              {:else if suggestion.status === "failed"}
                                <p class="agent-suggestion-error" role="alert">{suggestion.error || "Agent 處理失敗，請重新標記。"}</p>
                              {/if}
                            </article>
                          {/each}
                        </section>
                      {/if}
                      {#if editingCueId === cue.id}
                        <label>
                          <span class="sr-only">Cue {group.first_index + cueIndex + 1} 中文字幕</span>
                          <textarea
                            bind:this={editor}
                            bind:value={editText}
                            readonly={!canType}
                            on:focus={focusEditor}
                            on:input={() => { updateCaret(); handleInput(); }}
                            on:select={() => updateCaret()}
                            on:click={() => updateCaret()}
                            on:keyup={() => updateCaret()}
                            on:blur={leaveEditor}
                          ></textarea>
                        </label>
                      {/if}
                      <details class="cue-actions" bind:this={actionsMenu} on:toggle={() => closeOtherMenu(actionsMenu, agentMenu)}>
                        <summary>更多操作</summary>
                        <div class="cue-actions-panel">
                          <p class="shortcut-help">瀏覽：↑/↓ 換句 · Shift+Delete 刪除整句 · 雙擊字幕修稿 · 修稿：←/→ 在首尾跨句 · Esc 結束修稿 · Enter 切成兩句 · Ctrl/Cmd+Enter 同 Cue 換行 · Backspace/Delete 首尾合併 · M 快速標記</p>
                          <div class="action-grid" role="group" aria-label="Cue 操作">
                          <div class="action-control">
                            <button type="button" disabled={Boolean(splitReason)} aria-describedby={splitReason ? "split-reason" : undefined} title={splitReason || "從目前游標切開 Cue"} on:click={splitCue}>從游標切成兩句</button>
                            {#if splitReason}<small id="split-reason" class="action-reason">{splitReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" disabled={Boolean(previousMergeReason)} aria-describedby={previousMergeReason ? "previous-merge-reason" : undefined} title={previousMergeReason || mergeLabel(previousMergeCue, selectedCue, "上一句")} on:click={mergePreviousCue}>{mergeLabel(previousMergeCue, selectedCue, "上一句")}</button>
                            {#if previousMergeReason}<small id="previous-merge-reason" class="action-reason">{previousMergeReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" disabled={Boolean(nextMergeReason)} aria-describedby={nextMergeReason ? "next-merge-reason" : undefined} title={nextMergeReason || mergeLabel(selectedCue, nextMergeCue, "下一句")} on:click={mergeNextCue}>{mergeLabel(selectedCue, nextMergeCue, "下一句")}</button>
                            {#if nextMergeReason}<small id="next-merge-reason" class="action-reason">{nextMergeReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" class:flagged={selectedHasIssue} disabled={Boolean(flagReason)} aria-describedby={flagReason ? "flag-reason" : undefined} title={flagReason || "標記或取消這句的需修改狀態（M）"} on:click={toggleSelectedIssue}>
                              {selectedHasIssue ? "取消需修改（M）" : "標記需修改（M）"}
                            </button>
                            {#if flagReason}<small id="flag-reason" class="action-reason">{flagReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" class="cue-delete" disabled={Boolean(deleteReason)} aria-describedby={deleteReason ? "delete-reason" : undefined} title={deleteReason || "刪除整個 Cue（Shift+Delete）"} on:click={deleteCue}>刪除 Cue（Shift+Delete）</button>
                            {#if deleteReason}<small id="delete-reason" class="action-reason">{deleteReason}</small>{/if}
                          </div>
                          </div>
                          <strong class="action-section">Block</strong>
                          <div class="action-grid" role="group" aria-label="Block 操作">
                          <div class="action-control">
                            <button type="button" disabled={Boolean(previousBlockMergeReason)} aria-describedby={previousBlockMergeReason ? "previous-block-reason" : undefined} on:click={mergePreviousBlock}>合併前一 Block</button>
                            {#if previousBlockMergeReason}<small id="previous-block-reason" class="action-reason">{previousBlockMergeReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" disabled={Boolean(nextBlockMergeReason)} aria-describedby={nextBlockMergeReason ? "next-block-reason" : undefined} on:click={mergeNextBlock}>合併後一 Block</button>
                            {#if nextBlockMergeReason}<small id="next-block-reason" class="action-reason">{nextBlockMergeReason}</small>{/if}
                          </div>
                          <div class="action-control">
                            <button type="button" disabled={Boolean(blockSplitReason)} aria-describedby={blockSplitReason ? "block-split-reason" : undefined} on:click={splitBlock}>從這句前切開 Block</button>
                            {#if blockSplitReason}<small id="block-split-reason" class="action-reason">{blockSplitReason}</small>{/if}
                          </div>
                          </div>
                        </div>
                      </details>
                    </div>
                  {/if}
                </article>
              {/each}
            </div>
          </section>
        {/each}
      </div>
    </section>
  </div>
</main>

<style>
  .workspace-shell { min-height: 100vh; overflow-x: hidden; background: #151817; color: #eff2ec; }
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
  .cue-workspace { display: grid; grid-template-rows: auto minmax(0, 1fr); min-width: 0; min-height: 0; background: #1b201d; }
  .cue-workspace.content-review { grid-template-rows: minmax(0, 1fr); }
  .cue-timeline { padding: 10px 12px 9px; border-left: 1px solid #3b443d; border-bottom: 1px solid #3b443d; }
  .timeline-heading, .timeline-status { display: flex; justify-content: space-between; gap: 10px; color: #aeb9ad; font-size: 11px; }
  .timeline-heading strong { color: #eef2ec; font-size: 12px; }
  .timeline-track { position: relative; height: 22px; margin: 7px 0 5px; overflow: hidden; border-radius: 4px; background: #101311; }
  .timeline-track button { position: absolute; top: 4px; height: 14px; min-width: 2px; padding: 0; border: 1px solid #607061; border-radius: 2px; background: #39443b; }
  .timeline-track button.active { z-index: 2; border-color: #d7efcd; background: #6b9068; }
  .timeline-track button.needs-change { background: #975d4c; }
  .timeline-track .playhead { position: absolute; z-index: 3; top: 0; bottom: 0; width: 2px; transform: translateX(-1px); background: #f3d26d; pointer-events: none; }
  .cue-list { min-height: 0; overflow: auto; border-left: 1px solid #3b443d; border-right: 1px solid #3b443d; }
  .block-group { position: relative; margin-left: 9px; border-left: 3px solid #526555; border-bottom: 1px solid #323a34; border-radius: 2px 0 0 2px; }
  .block-group:nth-child(even) { border-left-color: #6a7157; }
  .block-group + .block-group { margin-top: 10px; border-top: 1px solid #465148; }
  .block-group.active { border-left-color: #9bc490; }
  .block-header { display: flex; flex-wrap: wrap; align-items: baseline; gap: 3px 8px; padding: 7px 9px 6px; background: #202820; color: #aeb9ad; font-size: 10px; }
  .block-header strong { color: #dbe7d6; font-size: 11px; }
  .block-header span:nth-last-child(2) { color: #b7d3ae; }
  .block-header > span:last-of-type { margin-left: auto; color: #e6c784; }
  .cue-list .cue-row { position: relative; border-bottom: 1px solid #323a34; background: transparent; }
  .block-rail-handle { position: absolute; z-index: 5; left: 0; top: 0; width: 22px; height: 22px; padding: 0; transform: translate(-58%, -50%); border: 1px solid #819082; border-radius: 50%; background: #273229; color: #dce7d9; font-size: 14px; line-height: 20px; opacity: 0; transition: opacity .12s ease; }
  .block-rail-handle.split { border-color: #76976f; background: #30422e; }
  .block-rail-handle span { position: absolute; left: 21px; top: 50%; width: max-content; max-width: 180px; padding: 4px 6px; transform: translateY(-50%); border: 1px solid #59665b; border-radius: 4px; background: #171c19; color: #e4ebe1; font-size: 10px; line-height: 1.2; opacity: 0; pointer-events: none; }
  .block-rail-handle:hover, .block-rail-handle:focus-visible { opacity: 1; }
  .block-rail-handle:hover span, .block-rail-handle:focus-visible span { opacity: 1; }
  .cue-list .cue-row.active { background: #394f40; }
  .cue-row-select { display: block; width: 100%; min-height: 58px; padding: 9px 10px; border: 0; border-radius: 0; background: transparent; color: inherit; text-align: left; }
  .cue-row-select:hover { background: #232b25; }
  .cue-list .cue-row.needs-change { box-shadow: inset 3px 0 #cf765e; }
  .cue-list .cue-row.locked { opacity: .68; }
  .cue-list small { color: #9da99d; }
  .cue-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 5px 9px; margin-bottom: 5px; }
  .cue-meta small { font-size: 10px; }
  .cue-state, .cue-presence { width: max-content; font-size: 10px; }
  .cue-state.needs-change { color: #ffd9bf; }
  .cue-state.locked { color: #e6c784; }
  .cue-presence { color: #b7d3ae; }
  .cue-list span { overflow-wrap: anywhere; color: #eef2ec; line-height: 1.4; }
  .cue-text { display: block; }
  .cue-inline-editor { position: relative; padding: 0 10px 12px; }
  .cue-inline-editor label { display: block; }
  .cue-inline-editor textarea { width: 100%; min-height: 78px; box-sizing: border-box; }
  .cue-editing-bar { position: relative; display: flex; flex-wrap: wrap; align-items: center; gap: 9px; min-height: 26px; margin-bottom: 7px; padding-right: 86px; }
  .cue-editing-bar strong { margin-right: 2px; color: #dbe7d6; font-size: 11px; }
  .agent-inline { flex: 0 0 auto; }
  .agent-inline[open] { flex: 1 0 100%; order: 2; }
  .agent-inline summary { width: max-content; cursor: pointer; color: #e8d096; font-size: 11px; font-weight: 700; }
  .agent-recorded { display: flex; min-width: 0; align-items: center; gap: 5px; color: #d8e6d4; font-size: 11px; }
  .agent-recorded > strong { flex: 0 0 auto; color: #a9d39f; }
  .agent-recorded > span { overflow: hidden; color: #c6d0c3; text-overflow: ellipsis; white-space: nowrap; }
  .agent-suggestions { display: grid; min-width: 0; gap: 8px; margin: 0 0 9px; }
  .agent-suggestion { min-width: 0; padding: 9px; border: 1px solid #536254; border-radius: 6px; background: #202820; }
  .agent-suggestion.ready { border-color: #76976f; }
  .agent-suggestion-heading { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 6px 10px; }
  .agent-suggestion-heading strong { color: #dbe7d6; font-size: 12px; }
  .agent-status { width: max-content; padding: 2px 5px; border-radius: 4px; background: #39443b; color: #dbe7d6; font-size: 10px; line-height: 1.3; }
  .agent-status.processing { background: #4a4431; color: #f0d58e; }
  .agent-status.ready { background: #304c32; color: #cde8c6; }
  .agent-status.stale, .agent-status.failed { background: #4c322f; color: #ffd1c7; }
  .agent-suggestion-category { display: block; margin-top: 5px; color: #aeb9ad; font-size: 10px; }
  .agent-diff { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 8px; }
  .agent-diff > div { min-width: 0; padding: 7px; border: 1px solid #3f4b41; border-radius: 4px; background: #171c19; }
  .agent-diff strong { color: #b7d3ae; font-size: 10px; }
  .agent-diff p, .agent-suggestion-note, .agent-suggestion-error { margin: 4px 0 0; overflow-wrap: anywhere; white-space: pre-wrap; color: #eef2ec; font-size: 12px; line-height: 1.4; }
  .agent-suggestion-note { color: #c6d0c3; }
  .agent-suggestion-error { color: #ffc6be; }
  .agent-suggestion-actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 9px; }
  .agent-suggestion-actions button { min-width: 100px; min-height: 44px; padding: 7px 12px; }
  .agent-suggestion-actions .quiet { border-color: #4a554b; background: transparent; color: #b9c4b7; }
  .agent-controls { position: static; display: flex; width: 100%; box-sizing: border-box; flex-wrap: wrap; align-items: center; gap: 6px; margin-top: 5px; padding: 8px; border: 1px solid #536254; border-radius: 5px; background: #202820; box-shadow: 0 8px 20px #0b0e0cbb; }
  .agent-controls .agent-note { flex: 1 1 180px; }
  .cue-editing-bar select, .cue-editing-bar input { width: 100%; box-sizing: border-box; min-height: 29px; }
  .cue-editing-bar button { padding: 5px 8px; font-size: 11px; }
  .cue-editing-bar .quiet { border-color: #4a554b; background: transparent; color: #b9c4b7; }
  .cue-delete { margin-top: 2px; border-color: #75463f; background: transparent; color: #ffc6be; }
  .cue-actions { position: absolute; z-index: 7; top: 5px; right: 10px; margin: 0; }
  .cue-actions summary { width: max-content; cursor: pointer; color: #d4ddd1; font-size: 12px; font-weight: 700; }
  .cue-actions-panel { position: absolute; top: calc(100% + 5px); right: 0; width: min(500px, calc(100vw - 48px)); padding: 8px; border: 1px solid #536254; border-radius: 5px; background: #202820; box-shadow: 0 8px 20px #0b0e0cbb; }
  .shortcut-help { margin: 8px 0; color: #aeb9ad; font-size: 11px; line-height: 1.4; }
  .action-section { display: block; margin: 11px 0 6px; color: #cbd8c7; font-size: 11px; }
  .action-grid { display: flex; align-items: flex-start; gap: 8px; flex-wrap: wrap; }
  .cue-actions button { padding: 6px 9px; font-size: 12px; }
  .cue-actions .flagged { border-color: #b46d4a; color: #ffd9bf; }
  .action-control { display: grid; gap: 4px; align-content: start; }
  .action-reason { max-width: 220px; color: #e6c784; font-size: 10px; line-height: 1.3; }
  .sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
  @media (max-width: 980px) {
    .workspace-toolbar { grid-template-columns: 1fr auto; }
    .workspace-counts { grid-row: 2; grid-column: 1 / -1; justify-content: center; }
    .workspace-grid { grid-template-columns: 1fr; height: auto; }
    .workspace-grid :global(.media-stage) { min-height: 56vh; border-bottom: 1px solid #3b443d; }
    .cue-workspace { min-height: 44vh; }
  }
  @media (max-width: 600px) {
    .workspace-toolbar { position: static; display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 12px; }
    .workspace-toolbar > div:first-child { flex: 1 1 100%; flex-wrap: wrap; }
    .workspace-counts { order: 3; flex: 1 1 100%; width: 100%; gap: 8px; overflow-x: auto; justify-content: flex-start; }
    .complete { flex: 1 1 100%; width: 100%; justify-self: stretch; }
    .workspace-grid :global(.media-stage),
    .workspace-grid :global(.stage-canvas),
    .workspace-grid :global(.still-stage),
    .workspace-grid :global(.source-video-stage) { width: 100%; min-width: 0; max-width: 100%; }
    .cue-list { max-height: 38vh; border-left: 0; }
    .cue-editing-bar { align-items: flex-start; flex-wrap: wrap; padding-right: 0; }
    .agent-inline { width: 100%; }
    .agent-controls { right: 0; }
    .agent-recorded { flex-basis: 100%; }
    .agent-diff { grid-template-columns: 1fr; }
    .agent-suggestion-actions { flex-direction: column; }
    .agent-suggestion-actions button { width: 100%; }
    .cue-actions-panel { width: min(500px, calc(100vw - 24px)); max-width: calc(100vw - 24px); }
  }
  @media (max-width: 600px), (pointer: coarse) {
    .agent-inline summary,
    .agent-controls select,
    .agent-controls input,
    .agent-controls button,
    .cue-delete { min-height: 44px; }
    .agent-inline summary { display: flex; align-items: center; }
  }
</style>
