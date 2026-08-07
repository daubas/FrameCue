<script>
  export let text;
  let open = false;
</script>

<span class="info-tip">
  <button type="button" class="info" aria-label="說明" aria-expanded={open} on:click|stopPropagation={() => open = !open} on:blur={() => open = false}>!</button>
  {#if open}<span class="info-popover" role="tooltip">{text}</span>{/if}
</span>
