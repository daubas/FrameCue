<script>
  import { tick } from "svelte";
  import { actionsForWorkflow, formatTime, markedParts, paperDecisionIssue, paperEditSections } from "../lib/review.js";

  export let packageData;
  export let draft;
  export let selectedCue;
  export let selectedBlock;
  export let blockIssue = "";
  export let activeScope;
  export let cueFilter;
  export let playbackCueId = "";
  export let onScopeChange = () => {};
  export let onFilterChange = () => {};
  export let onSelectCue = () => {};
  export let onSelectBlock = () => {};
  export let onCueChange = () => {};
  export let onBlockChange = () => {};
  export let onBlockApproval = () => {};
  export let onReplaceAll = () => {};

  let searchTerm = "";
  let replaceTerm = "";
  let searchMessage = "";
  let followedCueId = "";
  let followedBlockId = "";

  $: riskCues = packageData.cues.filter((cue) => cue.risks?.length);
  $: listedCues = cueFilter === "risk" ? riskCues : packageData.cues;
  $: workflowKind = packageData.workflow.kind;
  $: actions = actionsForWorkflow(workflowKind);
  $: isCarousel = workflowKind === "image_carousel";
  $: isMarkdown = workflowKind === "markdown";
  $: isPaperEdit = workflowKind === "paper_edit";
  $: cueLabel = isPaperEdit ? "Beat" : isCarousel ? "圖卡" : isMarkdown ? "Markdown 區塊" : "字幕 Cue";
  $: blockKind = selectedCue?.markdown?.kind === "heading" ? "標題" : selectedCue?.markdown?.kind === "list" ? "清單" : "段落";
  $: selectedCueState = selectedCue ? draft.cues[selectedCue.id] : null;
  $: selectedBlockState = selectedBlock ? draft.blocks[selectedBlock.id] : null;
  $: selectedPaperIssue = isPaperEdit ? paperDecisionIssue(selectedCueState) : "";
  $: paperSections = isPaperEdit ? paperEditSections(packageData.cues, draft.cues) : [];
  $: selectedPaperSection = isPaperEdit ? paperSections.find((section) => section.cues.some((cue) => cue.id === selectedCue?.id)) : null;
  $: if (selectedCue?.id && selectedCue.id !== followedCueId) {
    followedCueId = selectedCue.id;
    tick().then(() => document.getElementById(`cue-${followedCueId}`)?.scrollIntoView({ block: "nearest" }));
  }
  $: if (selectedBlock?.id && selectedBlock.id !== followedBlockId) {
    followedBlockId = selectedBlock.id;
    tick().then(() => document.getElementById(`block-${followedBlockId}`)?.scrollIntoView({ block: "nearest" }));
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

  function paperSourceLabel(sourceId) {
    const source = packageData.media?.paper_edit?.sources?.find((item) => item.id === sourceId);
    return source?.label || sourceId || "未定來源";
  }

  function paperRangeText(range) {
    const times = Number.isFinite(range?.start_ms) && Number.isFinite(range?.end_ms)
      ? `${formatTime(range.start_ms)} 至 ${formatTime(range.end_ms)}`
      : "時間待定";
    return `${paperSourceLabel(range?.source_id)} · ${times}${range?.availability ? ` · ${range.availability}` : ""}${range?.role ? ` · ${range.role}` : ""}`;
  }

  function narrationSpineRows(spine) {
    if (typeof spine === "string") return [spine];
    if (!spine) return ["未指定"];
    const reference = spine.source_id ? paperRangeText(spine) : "";
    return [
      spine.text,
      reference,
      spine.production_method ? `製作方式：${spine.production_method}` : "",
      spine.duration ? `時長：${spine.duration}` : "",
      spine.acceptance ? `驗收：${spine.acceptance}` : ""
    ].filter(Boolean) || ["未指定"];
  }

  function paperSlotRows(slot) {
    return [
      slot?.description || slot?.id || "未命名項目",
      paperRangeText(slot),
      slot?.production_method ? `製作方式：${slot.production_method}` : "",
      slot?.duration ? `時長：${slot.duration}` : "",
      slot?.acceptance ? `驗收：${slot.acceptance}` : ""
    ].filter(Boolean);
  }
</script>

<section class="review-workbench" aria-label="審閱工作區">
  <div class="workbench-header">
    <div>
      <span class="eyebrow">審閱工作區</span>
      <strong>{isPaperEdit && selectedPaperSection ? `Section ${selectedPaperSection.id} · ${selectedPaperSection.title}` : activeScope === "block" ? "語意塊" : cueLabel}</strong>
      {#if selectedPaperSection}<small>{selectedPaperSection.approved} / {selectedPaperSection.cues.length} Beats approved</small>{/if}
    </div>
    {#if !isPaperEdit}
      <div class="segmented" aria-label="審閱範圍">
        <button class:active={activeScope === "block"} disabled={!packageData.blocks.length} type="button" on:click={() => onScopeChange("block")}>語意塊 <span class="info" title="先審閱一個完整的口譯意思，再檢查它包含的顯示 Cue。">!</span></button>
        <button class:active={activeScope === "cue"} type="button" on:click={() => onScopeChange("cue")}>{isCarousel ? "圖卡" : isMarkdown ? "區塊" : "Cue"} <span class="info" title="審閱目前選取的內容。">!</span></button>
      </div>
    {/if}
  </div>

  {#if activeScope === "block" && selectedBlock}
    <div class="workbench-layout">
      <nav class="review-list" aria-label="語意塊">
        {#each packageData.blocks as block}
          {@const blockState = draft.blocks[block.id]}
          <button id={`block-${block.id}`} class:active={block.id === selectedBlock.id} class:approved={blockState.approved} type="button" on:click={() => onSelectBlock(block.id)}>
            <span>{block.id}</span>
            <strong>{blockState.target_text || "空白語意塊"}</strong>
            <small>{formatTime(block.start_ms)} 至 {formatTime(block.end_ms)}</small>
          </button>
        {/each}
      </nav>

      <div class="editor-pane">
        <div class="editor-heading">
          <span>{selectedBlock.id}</span>
          <span class:approved={selectedBlockState.approved} class="approval-pill">{selectedBlockState.approved ? "已核准" : "待審閱"}</span>
        </div>
        <div class="read-only-field">
          <span>原文 <span class="info" title="原文只能閱讀，用來核對意思與專有名詞。">!</span></span>
          <div class="source-text">{selectedBlock.source_text || "沒有原文"}</div>
        </div>
        <label>
          <span>顯示文字 <span class="info" title="這是去除標點的字幕文字，可在上游回寫到所屬的 Cue。">!</span></span>
          <textarea value={selectedBlockState.target_text} on:input={(event) => onBlockChange(selectedBlock.id, { target_text: event.currentTarget.value })}></textarea>
        </label>
        <label>
          <span>語音文字 <span class="info" title="這是保留標點、供 TTS 使用的口譯文字，不是畫面上的字幕。">!</span></span>
          <textarea value={selectedBlockState.speech_text} on:input={(event) => onBlockChange(selectedBlock.id, { speech_text: event.currentTarget.value })}></textarea>
        </label>
        {#if blockIssue}
          <p class="validation-message" role="alert">{blockIssue}</p>
        {/if}
        <label class="compact-field">
          <span>後續動作 <span class="info" title="FrameCue 只記錄需求；改寫、重新切分或調整時間會由 AgenticDub 在新修訂版完成。">!</span></span>
          <select value={selectedBlockState.action} on:change={(event) => onBlockChange(selectedBlock.id, { action: event.currentTarget.value })}>
            {#each actions as action}
              <option value={action.value}>{action.label}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>註記</span>
          <textarea class="short-textarea" value={selectedBlockState.instruction} placeholder="請說明上游要調整什麼" on:input={(event) => onBlockChange(selectedBlock.id, { instruction: event.currentTarget.value })}></textarea>
        </label>
        <button class:approved={selectedBlockState.approved} class="approve-button" disabled={Boolean(blockIssue) && !selectedBlockState.approved} type="button" on:click={() => onBlockApproval(selectedBlock.id, !selectedBlockState.approved)}>
          {selectedBlockState.approved ? "取消核准語意塊" : "核准語意塊"}
        </button>
      </div>
    </div>
  {:else if isPaperEdit && selectedCue}
    <div class="workbench-layout">
      <nav class="review-list paper-review-list" aria-label="Sections 與 Beats">
        {#each paperSections as section}
          <div class:active={section.id === selectedPaperSection?.id} class:approved={section.status === "approve"} class:needs-work={section.status === "revise" || section.status === "block"} class="paper-section">
            <button class="section-heading" type="button" on:click={() => onSelectCue(section.cues[0].id)}>
              <span>Section {section.id}</span>
              <strong>{section.title}</strong>
              <small>{section.approved} / {section.cues.length} approved{section.block ? ` · ${section.block} blocked` : section.revise ? ` · ${section.revise} revise` : ""}</small>
            </button>
            <div class="section-beats">
              {#each section.cues as beatCue}
                {@const beatState = draft.cues[beatCue.id]}
                <button id={`cue-${beatCue.id}`} class:active={beatCue.id === selectedCue.id} class:approved={beatState.decision === "approve"} class:needs-work={beatState.decision === "revise" || beatState.decision === "block"} type="button" on:click={() => onSelectCue(beatCue.id)}>
                  <span>Beat {beatCue.id.replace(/^beat-/, "")}</span>
                  <strong>{beatCue.beat?.teaching_purpose || beatCue.text}</strong>
                  <small>{formatTime(beatCue.start_ms)} 至 {formatTime(beatCue.end_ms)}</small>
                </button>
              {/each}
            </div>
          </div>
        {/each}
      </nav>

      <div class="editor-pane">
        <div class="editor-heading">
          <span>Section {selectedCue.beat?.chapter_id} · Beat {selectedCue.id.replace(/^beat-/, "")}</span>
          <span class:approved={selectedCueState.decision === "approve"} class="approval-pill">{selectedCueState.decision === "pending" ? "待審閱" : selectedCueState.decision}</span>
        </div>
        <div class="read-only-field">
          <span>教學目的</span>
          <div class="source-text">{selectedCue.beat?.teaching_purpose || "未指定"}</div>
        </div>
        <div class="read-only-field">
          <span>口說內容摘要</span>
          <div class="source-text">{selectedCue.beat?.spoken_content_summary || "未指定"}</div>
        </div>
        <div class="read-only-field">
          <span>敘事主線</span>
          <div class="source-text">{#each narrationSpineRows(selectedCue.beat?.narration_spine) as row}<div>{row}</div>{/each}</div>
        </div>
        <div class="read-only-field">
          <span>來源範圍</span>
          <div class="source-text">{#each selectedCue.beat?.source_ranges || [] as range}<div>{paperRangeText(range)}</div>{:else}<div>沒有</div>{/each}</div>
        </div>
        <div class="read-only-field">
          <span>視覺欄位</span>
          <div class="source-text">{#each selectedCue.beat?.visual_slots || [] as slot}{#each paperSlotRows(slot) as row}<div>{row}</div>{/each}{/each}</div>
        </div>
        <div class="read-only-field">
          <span>時長</span>
          <div class="source-text">{formatTime(selectedCue.beat?.duration_ms || 0)}</div>
        </div>
        <div class="read-only-field">
          <span>未解工作</span>
          <div class="source-text">{#each selectedCue.beat?.unresolved_work || [] as item}<div>{item}</div>{:else}<div>沒有</div>{/each}</div>
        </div>
        <div class="field-label">決策</div>
        <div class="segmented" aria-label="Beat 決策">
          <button class:active={selectedCueState.decision === "approve"} type="button" on:click={() => onCueChange(selectedCue.id, { decision: "approve" })}>Approve</button>
          <button class:active={selectedCueState.decision === "revise"} type="button" on:click={() => onCueChange(selectedCue.id, { decision: "revise" })}>Revise</button>
          <button class:active={selectedCueState.decision === "block"} type="button" on:click={() => onCueChange(selectedCue.id, { decision: "block" })}>Block</button>
        </div>
        <label>
          <span>註記（Revise / Block 必填）</span>
          <textarea class="short-textarea" value={selectedCueState.instruction} placeholder="說明下一步需要調整或阻擋的原因" on:input={(event) => onCueChange(selectedCue.id, { instruction: event.currentTarget.value })}></textarea>
        </label>
        {#if selectedPaperIssue}
          <p class="validation-message" role="alert">{selectedPaperIssue}</p>
        {/if}
      </div>
    </div>
  {:else if selectedCue}
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
          <button id={`cue-${cue.id}`} class:active={cue.id === selectedCue.id} class:playing={cue.id === playbackCueId} type="button" on:click={() => onSelectCue(cue.id)}>
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
          {#if selectedCue.risks?.length}
            <span class="risk-summary">{selectedCue.risks.join(" · ")}</span>
          {/if}
        </div>
        <div class="read-only-field">
          <span>{isCarousel ? "圖卡資產" : isMarkdown ? "區塊類型" : "原始字幕"}</span>
          <div class="source-text">{isCarousel ? selectedCue.text : isMarkdown ? blockKind : selectedCue.original_text || "沒有原始字幕"}</div>
        </div>
        <label>
          <span>{isCarousel ? "圖卡標籤" : isMarkdown ? "Markdown 內容" : "顯示字幕"} <span class="info" title="這段內容會寫入完整審閱結果。">!</span></span>
          <textarea value={selectedCueState.text} on:input={(event) => onCueChange(selectedCue.id, { text: event.currentTarget.value })}></textarea>
        </label>
        {#if !packageData.blocks.length && !isCarousel && !isMarkdown}
          <label>
            <span>語音文字 <span class="info" title="沒有語意塊時，這段保留標點的文字會直接交給 TTS。">!</span></span>
            <textarea value={selectedCueState.speech_text} on:input={(event) => onCueChange(selectedCue.id, { speech_text: event.currentTarget.value })}></textarea>
          </label>
        {/if}
        <label class="compact-field">
          <span>後續動作 <span class="info" title="這個 Cue 若需要上游處理而非直接修改，請選擇下一步。">!</span></span>
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
            <div class="field-label">搜尋與取代 <span class="info" title="會套用目前的顯示文字規則到所有變更的 Cue，並取消最終核准。">!</span></div>
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
