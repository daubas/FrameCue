<script>
  import { ACTIONS, formatTime, markedParts } from "../lib/review.js";

  export let packageData;
  export let draft;
  export let selectedCue;
  export let selectedBlock;
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

  $: riskCues = packageData.cues.filter((cue) => cue.risks?.length);
  $: listedCues = cueFilter === "risk" ? riskCues : packageData.cues;
  $: selectedCueState = selectedCue ? draft.cues[selectedCue.id] : null;
  $: selectedBlockState = selectedBlock ? draft.blocks[selectedBlock.id] : null;

  function findNext() {
    if (!searchTerm.trim()) {
      searchMessage = "Enter text to search";
      return;
    }
    const matches = packageData.cues.filter((cue) => draft.cues[cue.id].text.includes(searchTerm));
    if (!matches.length) {
      searchMessage = "No matches";
      return;
    }
    const currentIndex = matches.findIndex((cue) => cue.id === selectedCue?.id);
    const next = matches[(currentIndex + 1) % matches.length];
    onSelectCue(next.id);
    searchMessage = `${matches.indexOf(next) + 1} of ${matches.length}`;
  }

  function replaceAll() {
    if (!searchTerm) {
      searchMessage = "Enter text to replace";
      return;
    }
    const count = onReplaceAll(searchTerm, replaceTerm);
    searchMessage = `Updated ${count} cue${count === 1 ? "" : "s"}`;
  }
</script>

<section class="review-workbench" aria-label="Review Workbench">
  <div class="workbench-header">
    <div>
      <span class="eyebrow">Review Workbench</span>
      <strong>{activeScope === "block" ? "Semantic blocks" : "Subtitle cues"}</strong>
    </div>
    <div class="segmented" aria-label="Review scope">
      <button class:active={activeScope === "block"} disabled={!packageData.blocks.length} type="button" on:click={() => onScopeChange("block")}>Block <span class="info" title="Review a complete interpreted idea before its display cues.">!</span></button>
      <button class:active={activeScope === "cue"} type="button" on:click={() => onScopeChange("cue")}>Cue <span class="info" title="Review the sentence shown on the media stage.">!</span></button>
    </div>
  </div>

  {#if activeScope === "block" && selectedBlock}
    <div class="workbench-layout">
      <nav class="review-list" aria-label="Semantic blocks">
        {#each packageData.blocks as block}
          {@const blockState = draft.blocks[block.id]}
          <button class:active={block.id === selectedBlock.id} class:approved={blockState.approved} type="button" on:click={() => onSelectBlock(block.id)}>
            <span>{block.id}</span>
            <strong>{blockState.target_text || "Empty block"}</strong>
            <small>{formatTime(block.start_ms)} to {formatTime(block.end_ms)}</small>
          </button>
        {/each}
      </nav>

      <div class="editor-pane">
        <div class="editor-heading">
          <span>{selectedBlock.id}</span>
          <span class:approved={selectedBlockState.approved} class="approval-pill">{selectedBlockState.approved ? "Approved" : "Needs review"}</span>
        </div>
        <div class="read-only-field">
          <span>Source <span class="info" title="Source text is read-only. It is used to check meaning and terminology.">!</span></span>
          <div class="source-text">{selectedBlock.source_text || "No source text"}</div>
        </div>
        <label>
          <span>Display text <span class="info" title="This is punctuation-free subtitle text. It can be projected into child cues upstream.">!</span></span>
          <textarea value={selectedBlockState.target_text} on:input={(event) => onBlockChange(selectedBlock.id, { target_text: event.currentTarget.value })}></textarea>
        </label>
        <label>
          <span>Speech text <span class="info" title="This punctuated interpretation text is the TTS-facing wording, not the subtitle overlay.">!</span></span>
          <textarea value={selectedBlockState.speech_text} on:input={(event) => onBlockChange(selectedBlock.id, { speech_text: event.currentTarget.value })}></textarea>
        </label>
        <label class="compact-field">
          <span>Follow-up action <span class="info" title="FrameCue records the request. AgenticDub performs any rewrite, resegmentation, or retiming in a new revision.">!</span></span>
          <select value={selectedBlockState.action} on:change={(event) => onBlockChange(selectedBlock.id, { action: event.currentTarget.value })}>
            {#each ACTIONS as action}
              <option value={action.value}>{action.label}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>Instruction</span>
          <textarea class="short-textarea" value={selectedBlockState.instruction} placeholder="What should upstream change?" on:input={(event) => onBlockChange(selectedBlock.id, { instruction: event.currentTarget.value })}></textarea>
        </label>
        <button class:approved={selectedBlockState.approved} class="approve-button" type="button" on:click={() => onBlockApproval(selectedBlock.id, !selectedBlockState.approved)}>
          {selectedBlockState.approved ? "Unapprove block" : "Approve block"}
        </button>
      </div>
    </div>
  {:else if selectedCue}
    <div class="workbench-layout">
      <nav class="review-list cue-list" aria-label="Subtitle cues">
        <div class="list-filter" aria-label="Cue filter">
          <button class:active={cueFilter === "risk"} type="button" on:click={() => onFilterChange("risk")}>Risk {riskCues.length}</button>
          <button class:active={cueFilter === "all"} type="button" on:click={() => onFilterChange("all")}>All {packageData.cues.length}</button>
        </div>
        {#if !listedCues.length}
          <p class="empty-list">No risk cues in this package.</p>
        {/if}
        {#each listedCues as cue}
          {@const cueState = draft.cues[cue.id]}
          <button class:active={cue.id === selectedCue.id} class:playing={cue.id === playbackCueId} type="button" on:click={() => onSelectCue(cue.id)}>
            <span>{cue.id}</span>
            <strong>
              {#each markedParts(cueState.text, cue.risks) as part}
                {#if part.risk}<mark>{part.text}</mark>{:else}{part.text}{/if}
              {/each}
            </strong>
            <small>{formatTime(cue.start_ms)} to {formatTime(cue.end_ms)}</small>
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
          <span>Original</span>
          <div class="source-text">{selectedCue.original_text || "No original subtitle"}</div>
        </div>
        <label>
          <span>Display subtitle <span class="info" title="This appears on screen and follows the package punctuation policy.">!</span></span>
          <textarea value={selectedCueState.text} on:input={(event) => onCueChange(selectedCue.id, { text: event.currentTarget.value })}></textarea>
        </label>
        {#if !packageData.blocks.length}
          <label>
            <span>Speech text <span class="info" title="Without semantic blocks, this punctuated wording is the TTS-facing interpretation.">!</span></span>
            <textarea value={selectedCueState.speech_text} on:input={(event) => onCueChange(selectedCue.id, { speech_text: event.currentTarget.value })}></textarea>
          </label>
        {/if}
        <label class="compact-field">
          <span>Follow-up action <span class="info" title="Select a next step when this cue needs work upstream rather than a direct edit.">!</span></span>
          <select value={selectedCueState.action} on:change={(event) => onCueChange(selectedCue.id, { action: event.currentTarget.value })}>
            {#each ACTIONS as action}
              <option value={action.value}>{action.label}</option>
            {/each}
          </select>
        </label>
        <label>
          <span>Instruction</span>
          <textarea class="short-textarea" value={selectedCueState.instruction} placeholder="Optional instruction for the next revision" on:input={(event) => onCueChange(selectedCue.id, { instruction: event.currentTarget.value })}></textarea>
        </label>
        <div class="replace-tools">
          <div class="field-label">Search and replace <span class="info" title="Applies the current display-text policy to every changed cue and invalidates final approval.">!</span></div>
          <input bind:value={searchTerm} type="search" placeholder="Search all display subtitles" />
          <input bind:value={replaceTerm} type="text" placeholder="Replace with" />
          <div class="inline-actions">
            <button type="button" on:click={findNext}>Find next</button>
            <button type="button" on:click={replaceAll}>Replace all</button>
            <span class="status-text">{searchMessage}</span>
          </div>
        </div>
      </div>
    </div>
  {:else}
    <div class="workbench-empty">This package has no selectable review content.</div>
  {/if}
</section>
