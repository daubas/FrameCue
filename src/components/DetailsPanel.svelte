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
    <span>Details</span>
    <span>{open ? "Hide" : "Show"}</span>
  </button>
  {#if open}
    <dl>
      <div><dt>Review</dt><dd>{packageData.review_id} {packageData.revision}</dd></div>
      <div><dt>Workflow</dt><dd>{packageData.workflow.kind}</dd></div>
      <div><dt>Viewer</dt><dd>{packageData.viewer_version}</dd></div>
      <div><dt>Changes</dt><dd>{changed}</dd></div>
      <div><dt>Block gate</dt><dd>{approvalAllowed ? "Ready" : "Review blocks first"}</dd></div>
      <div><dt>Final approval</dt><dd>{approvedAt || "Not approved"}</dd></div>
      <div><dt>Checksum</dt><dd class="checksum">{packageData.content_checksum}</dd></div>
    </dl>
    {#if Object.keys(packageData.provenance || {}).length}
      <div class="provenance">
        <span>Provenance</span>
        {#each Object.entries(packageData.provenance) as [key, value]}
          <div><strong>{key}</strong><code>{String(value)}</code></div>
        {/each}
      </div>
    {/if}
    {#if selectedCue?.redraw?.trace || selectedScene?.redraw?.trace}
      <div class="provenance">
        <span>Generation trace</span>
        <code>{JSON.stringify(selectedCue?.redraw?.trace || selectedScene?.redraw?.trace, null, 2)}</code>
      </div>
    {/if}
  {/if}
</section>
