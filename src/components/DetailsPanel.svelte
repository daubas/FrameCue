<script>
  export let packageData;
  export let selectedCue = null;
  export let selectedScene = null;
  export let changed = 0;
  export let approvalAllowed = false;
  export let approvedAt = "";

  let open = false;
</script>

<section class="details-panel">
  <button class="details-toggle" type="button" aria-expanded={open} on:click={() => open = !open}>
    <span>詳細資料</span>
    <span>{open ? "收起" : "展開"}</span>
  </button>
  {#if open}
    <dl>
      <div><dt>審閱</dt><dd>{packageData.review_id} {packageData.revision}</dd></div>
      <div><dt>流程</dt><dd>{packageData.workflow.kind}</dd></div>
      <div><dt>檢視器</dt><dd>{packageData.viewer_version}</dd></div>
      <div><dt>變更</dt><dd>{changed}</dd></div>
      <div><dt>核准門檻</dt><dd>{approvalAllowed ? "可核准" : "請先完成必要審閱"}</dd></div>
      <div><dt>最終核准</dt><dd>{approvedAt || "尚未核准"}</dd></div>
      <div><dt>校驗碼</dt><dd class="checksum">{packageData.content_checksum}</dd></div>
    </dl>
    {#if Object.keys(packageData.provenance || {}).length}
      <div class="provenance">
        <span>來源紀錄</span>
        {#each Object.entries(packageData.provenance) as [key, value]}
          <div><strong>{key}</strong><code>{String(value)}</code></div>
        {/each}
      </div>
    {/if}
    {#if selectedCue?.redraw?.trace || selectedScene?.redraw?.trace}
      <div class="provenance">
        <span>產生紀錄</span>
        <code>{JSON.stringify(selectedCue?.redraw?.trace || selectedScene?.redraw?.trace, null, 2)}</code>
      </div>
    {/if}
  {/if}
</section>
