<script>
  import { tick } from "svelte";
  import { actionsForWorkflow, formatTime, markedParts, rangeNeedsConfirmation } from "../lib/review.js";
  import InfoTip from "./InfoTip.svelte";

  export let packageData;
  export let draft;
  export let selectedCue;
  export let selectedBlock;
  export let blockIssue = "";
  export let cueFilter;
  export let playbackCueId = "";
  export let onFilterChange = () => {};
  export let onSelectCue = () => {};
  export let onCueChange = () => {};
  export let onBlockChange = () => {};
  export let onReplaceAll = () => {};
  export let onResegmentRange = () => {};

  let searchTerm = "";
  let replaceTerm = "";
  let searchMessage = "";
  let followedCueId = "";
  let rangeAnchorId = "";
  let selectedCueIds = [];
  let rangeInstruction = "";
  let previousRangeKey = "";

  $: riskCues = packageData.cues.filter((cue) => cue.risks?.length);
  $: listedCues = cueFilter === "risk" ? riskCues : packageData.cues;
  $: workflowKind = packageData.workflow.kind;
  $: actions = actionsForWorkflow(workflowKind);
  $: isCarousel = workflowKind === "image_carousel";
  $: isMarkdown = workflowKind === "markdown";
  $: cueLabel = isCarousel ? "圖卡" : isMarkdown ? "Markdown 區塊" : "字幕 Cue";
  $: blockKind = selectedCue?.markdown?.kind === "heading" ? "標題" : selectedCue?.markdown?.kind === "list" ? "清單" : "段落";
  $: selectedCueState = selectedCue ? draft.cues[selectedCue.id] : null;
  $: selectedBlockState = selectedBlock ? draft.blocks[selectedBlock.id] : null;
  $: selectedRangeCues = packageData.cues.filter((cue) => selectedCueIds.includes(cue.id));
  $: rangeKey = selectedRangeCues.map((cue) => cue.id).join(",");
  $: if (rangeKey && rangeKey !== previousRangeKey) {
    previousRangeKey = rangeKey;
    rangeInstruction = `請將 ${selectedRangeCues[0].id} 至 ${selectedRangeCues.at(-1).id} 重新切分，避免將句子或專有名詞切開。`;
  }
  $: if (selectedCue?.id && selectedCue.id !== followedCueId) {
    followedCueId = selectedCue.id;
    tick().then(() => document.getElementById(`cue-${followedCueId}`)?.scrollIntoView({ block: "nearest" }));
  }

  function findNext() {
    if (!searchTerm.trim()) {
      searchMessage = "請輸入搜尋文字";
      return;
    }
    const matches = packageData.cues.filter((cue) => draft.cues[cue.id].text.includes(searchTerm));
    if (!matches.length) {
      searchMessage = "找不到符合項目";
      return;
    }
    const currentIndex = matches.findIndex((cue) => cue.id === selectedCue?.id);
    const next = matches[(currentIndex + 1) % matches.length];
    onSelectCue(next.id);
    searchMessage = `${matches.indexOf(next) + 1} / ${matches.length}`;
  }

  function replaceAll() {
    if (!searchTerm) {
      searchMessage = "請輸入要取代的文字";
      return;
    }
    const count = onReplaceAll(searchTerm, replaceTerm);
    searchMessage = `已更新 ${count} 個 Cue`;
  }

  function selectCueRange(cue, event) {
    const cueIndex = packageData.cues.findIndex((item) => item.id === cue.id);
    const keepRangeAnchor = selectedCueIds.length > 1 && selectedCueIds.includes(selectedCue?.id);
    const anchorIndex = packageData.cues.findIndex((item) => item.id === (keepRangeAnchor ? rangeAnchorId : selectedCue?.id));
    if (event.shiftKey && anchorIndex >= 0) {
      const [start, end] = [anchorIndex, cueIndex].sort((left, right) => left - right);
      selectedCueIds = packageData.cues.slice(start, end + 1).map((item) => item.id);
    } else {
      rangeAnchorId = cue.id;
      selectedCueIds = [cue.id];
    }
    onSelectCue(cue.id);
  }

  function rangeSpeech(cues) {
    const usedBlocks = new Set();
    return cues.map((cue) => {
      const direct = draft.cues[cue.id]?.speech_text;
      if (direct) return direct;
      const block = packageData.blocks.find((item) => item.cue_ids.includes(cue.id));
      if (!block || usedBlocks.has(block.id)) return "";
      usedBlocks.add(block.id);
      return draft.blocks[block.id]?.speech_text || "";
    }).filter(Boolean).join(" ");
  }

  function markRangeForResegment() {
    if (selectedCueIds.length < 2) return;
    if (rangeNeedsConfirmation(selectedRangeCues) && !window.confirm(`這次會標記 ${selectedRangeCues.length} 個 Cue，確定要繼續嗎？`)) return;
    onResegmentRange(selectedCueIds, rangeInstruction.trim() || "請重新切分這段內容，避免將句子或專有名詞切開。");
  }
</script>

<section class="review-workbench" aria-label="審閱工作區">
  <div class="workbench-header">
    <div>
      <span class="eyebrow">審閱工作區</span>
      <strong>{cueLabel}</strong>
    </div>
    <span class="shortcut-hint">空白鍵：核准並下一句</span>
  </div>

  {#if selectedCue}
    <div class="workbench-layout">
      <nav class="review-list cue-list" aria-label={cueLabel}>
        <div class="list-filter" aria-label="Cue 篩選">
          <button class:active={cueFilter === "risk"} type="button" on:click={() => onFilterChange("risk")}>需留意 {riskCues.length}</button>
          <button class:active={cueFilter === "all"} type="button" on:click={() => onFilterChange("all")}>全部 {packageData.cues.length}</button>
        </div>
        {#if !listedCues.length}
          <p class="empty-list">這份套件沒有風險 Cue。</p>
        {/if}
        {#each listedCues as cue}
          {@const cueState = draft.cues[cue.id]}
          <button id={`cue-${cue.id}`} class:active={cue.id === selectedCue.id} class:playing={cue.id === playbackCueId} class:reviewed={draft.reviewed_cues?.[cue.id]} class:in-range={selectedCueIds.includes(cue.id)} type="button" on:click={(event) => selectCueRange(cue, event)}>
            <span>{cue.id}</span>
            <strong>
              {#each markedParts(cueState.text, cue.risks) as part}
                {#if part.risk}<mark>{part.text}</mark>{:else}{part.text}{/if}
              {/each}
            </strong>
            <small>{formatTime(cue.start_ms)} 至 {formatTime(cue.end_ms)}</small>
          </button>
        {/each}
      </nav>

      <div class="editor-pane">
        <div class="editor-heading">
          <span>{selectedCue.id}</span>
          <div class="editor-status">
            {#if selectedCue.risks?.length}<span class="risk-summary">{selectedCue.risks.join(" · ")}</span>{/if}
            <span class:approved={draft.reviewed_cues?.[selectedCue.id]} class="approval-pill">{draft.reviewed_cues?.[selectedCue.id] ? "已審" : "待審"}</span>
          </div>
        </div>
        {#if !isCarousel && !isMarkdown && selectedRangeCues.length > 1}
          <section class="segment-range" aria-label="重新切分範圍">
            <div class="segment-range-heading">
              <strong>重新切分範圍</strong>
              <span>{selectedRangeCues.length} 個 Cue · Shift 點選最後一句可調整範圍</span>
            </div>
            <div class="segment-track" aria-label="選取 Cue 時間軸">
              {#each selectedRangeCues as cue}
                <span><b>{cue.id}</b><small>{formatTime(cue.start_ms)}–{formatTime(cue.end_ms)}</small></span>
              {/each}
            </div>
            <div class="range-preview"><span>合併原文</span><p>{selectedRangeCues.map((cue) => cue.original_text || "").join(" ")}</p></div>
            <div class="range-preview"><span>合併顯示字幕</span><p>{selectedRangeCues.map((cue) => draft.cues[cue.id].text).join(" ｜ ")}</p></div>
            <div class="range-preview"><span>配音稿</span><p>{rangeSpeech(selectedRangeCues) || "沒有配音稿"}</p></div>
            <label>
              <span>上游指示 <InfoTip text="會套用到這段所有 Cue，作為下一個 immutable revision 的重新切分要求。" /></span>
              <textarea class="short-textarea" bind:value={rangeInstruction}></textarea>
            </label>
            <div class="inline-actions"><button class="warning-button" type="button" on:click={markRangeForResegment}>標記這段為重新切分</button></div>
          </section>
        {/if}
        <div class="read-only-field">
          <span>{isCarousel ? "圖卡資產" : isMarkdown ? "區塊類型" : "原始字幕"}</span>
          <div class="source-text">{isCarousel ? selectedCue.text : isMarkdown ? blockKind : selectedCue.original_text || "沒有原始字幕"}</div>
        </div>
        <label>
          <span>{isCarousel ? "圖卡標籤" : isMarkdown ? "Markdown 內容" : "顯示字幕"} <InfoTip text="這段內容會寫入完整審閱結果。" /></span>
          <textarea value={selectedCueState.text} on:input={(event) => onCueChange(selectedCue.id, { text: event.currentTarget.value })}></textarea>
        </label>
        {#if !packageData.blocks.length && !isCarousel && !isMarkdown}
          <label>
            <span>語音文字 <InfoTip text="沒有語意塊時，這段保留標點的文字會直接交給 TTS。" /></span>
            <textarea value={selectedCueState.speech_text} on:input={(event) => onCueChange(selectedCue.id, { speech_text: event.currentTarget.value })}></textarea>
          </label>
        {/if}
        {#if selectedBlock}
          <details class:issue={Boolean(blockIssue)} class="block-context" open={Boolean(blockIssue)}>
            <summary>
              <span>整句與配音稿 · {selectedBlock.id}</span>
              <span class:approved={selectedBlockState.approved} class="approval-pill">{selectedBlockState.approved ? "已自動核准" : "隨 Cue 自動核准"}</span>
            </summary>
            <div class="block-context-body">
              <div class="read-only-field">
                <span>整句原文</span>
                <div class="source-text">{selectedBlock.source_text || "沒有原文"}</div>
              </div>
              <div class="read-only-field">
                <span>合併顯示文字</span>
                <div class="source-text">{selectedBlockState.target_text}</div>
              </div>
              <label>
                <span>配音文字 <InfoTip text="保留標點、供 TTS 使用；內容必須與這組 Cue 一致。" /></span>
                <textarea value={selectedBlockState.speech_text} on:input={(event) => onBlockChange(selectedBlock.id, { speech_text: event.currentTarget.value })}></textarea>
              </label>
              {#if blockIssue}<p class="validation-message" role="alert">{blockIssue}</p>{/if}
              <label class="compact-field">
                <span>整句後續動作</span>
                <select value={selectedBlockState.action} on:change={(event) => onBlockChange(selectedBlock.id, { action: event.currentTarget.value })}>
                  {#each actions as action}<option value={action.value}>{action.label}</option>{/each}
                </select>
              </label>
              <label>
                <span>整句註記</span>
                <textarea class="short-textarea" value={selectedBlockState.instruction} placeholder="只有需要上游處理時填寫" on:input={(event) => onBlockChange(selectedBlock.id, { instruction: event.currentTarget.value })}></textarea>
              </label>
            </div>
          </details>
        {/if}
        <label class="compact-field">
          <span>後續動作 <InfoTip text="這個 Cue 若需要上游處理而非直接修改，請選擇下一步。" /></span>
          <select value={selectedCueState.action} on:change={(event) => onCueChange(selectedCue.id, { action: event.currentTarget.value })}>
            {#each actions as action}
              <option value={action.value}>{action.label}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>註記</span>
          <textarea class="short-textarea" value={selectedCueState.instruction} placeholder="可選填，說明下一版要處理什麼" on:input={(event) => onCueChange(selectedCue.id, { instruction: event.currentTarget.value })}></textarea>
        </label>
        {#if !isCarousel}
          <div class="replace-tools">
            <div class="field-label">搜尋與取代 <InfoTip text="會套用目前的顯示文字規則到所有變更的 Cue，並取消最終核准。" /></div>
            <input bind:value={searchTerm} type="search" placeholder={isMarkdown ? "搜尋全部 Markdown 區塊" : "搜尋全部顯示字幕"} />
            <input bind:value={replaceTerm} type="text" placeholder="取代為" />
            <div class="inline-actions">
              <button type="button" on:click={findNext}>尋找下一個</button>
              <button type="button" on:click={replaceAll}>全部取代</button>
              <span class="status-text">{searchMessage}</span>
            </div>
          </div>
        {/if}
      </div>
    </div>
  {:else}
    <div class="workbench-empty">這份套件沒有可選取的審閱內容。</div>
  {/if}
</section>
