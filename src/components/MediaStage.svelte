<script>
  import { onMount } from "svelte";
  import { cueIndexAtTime, formatTime, markedParts } from "../lib/review.js";

  export let packageData;
  export let cue;
  export let cueDraft;
  export let stageMode = "still";
  export let assetUrl = (path) => path;
  export let onStageMode = () => {};
  export let onPlaybackCue = () => {};

  let cueAudio;
  let sourceVideoElement;
  let sourceVideoCueId = "";
  let cuePlaybackActive = false;
  let cuePlaybackEndMs = 0;
  let playerFrame;
  let playerReady = false;
  let playerPlaying = false;
  let playerError = "";
  let playerOrigin = "";

  $: scene = packageData?.scenes?.find((item) => item.id === cue?.scene_id) || null;
  $: redraw = cue?.redraw || scene?.redraw || null;
  $: redrawBefore = redraw?.before_image || redraw?.comparison_image || scene?.image || "";
  $: redrawAfter = redraw?.after_image || redraw?.current_image || scene?.image || "";
  $: boundary = cue?.boundary || scene?.boundary || null;
  $: sourceVideo = packageData?.media?.video || null;
  $: hyperframes = packageData?.media?.hyperframes || null;
  $: availableModes = [
    "still",
    ...(redraw ? ["redraw"] : []),
    ...(boundary ? ["boundary"] : []),
    ...(sourceVideo || hyperframes ? ["video"] : [])
  ];
  $: if (!availableModes.includes(stageMode)) onStageMode("still");
  $: playerSrc = hyperframes ? `${assetUrl(hyperframes.entry)}${hyperframes.config ? `?config=${encodeURIComponent(hyperframes.config)}` : ""}` : "";
  $: if (playerSrc) playerOrigin = new URL(playerSrc, window.location.href).origin;
  $: if (stageMode === "video" && playerReady && cue) {
    postPlayer("framecue:seek", { time: cue.start_ms / 1000 });
  }
  $: if (stageMode === "video" && sourceVideo && sourceVideoElement && cue?.id && cue.id !== sourceVideoCueId) {
    sourceVideoCueId = cue.id;
    alignSourceVideo();
  }

  function postPlayer(type, payload = {}) {
    if (!playerOrigin || !playerFrame?.contentWindow) return;
    playerFrame.contentWindow.postMessage({ type, ...payload }, playerOrigin);
  }

  function pausePlayer() {
    postPlayer("framecue:pause");
    sourceVideoElement?.pause();
    cuePlaybackActive = false;
    playerPlaying = false;
  }

  function pauseCueAudio() {
    cueAudio?.pause();
  }

  function setMode(mode) {
    if (mode === "video") {
      pauseCueAudio();
      sourceVideoCueId = "";
    } else {
      pausePlayer();
    }
    onStageMode(mode);
  }

  function toggleCueAudio() {
    if (!cueAudio?.src) return;
    if (cueAudio.paused) {
      pausePlayer();
      cueAudio.play();
    } else {
      cueAudio.pause();
    }
  }

  function toggleVideo() {
    if (sourceVideo) {
      toggleSourceVideo();
      return;
    }
    if (!playerReady) return;
    if (playerPlaying) postPlayer("framecue:pause");
    else postPlayer("framecue:play", { time: cue?.start_ms / 1000 });
  }

  function alignSourceVideo(force = false) {
    if (!sourceVideoElement || !cue) return;
    const currentMs = sourceVideoElement.currentTime * 1000;
    if (force || currentMs < cue.start_ms - 100 || currentMs >= cue.end_ms + 100) {
      sourceVideoElement.pause();
      cuePlaybackActive = false;
      sourceVideoElement.currentTime = cue.start_ms / 1000;
    }
  }

  function toggleSourceVideo() {
    if (!sourceVideoElement || !cue) return;
    if (!sourceVideoElement.paused) {
      sourceVideoElement.pause();
      cuePlaybackActive = false;
      return;
    }
    const currentMs = sourceVideoElement.currentTime * 1000;
    if (currentMs < cue.start_ms - 100 || currentMs >= cue.end_ms - 20) {
      sourceVideoElement.currentTime = cue.start_ms / 1000;
    }
    cuePlaybackEndMs = cue.end_ms;
    cuePlaybackActive = true;
    pauseCueAudio();
    sourceVideoElement.play().catch(() => {
      cuePlaybackActive = false;
      playerPlaying = false;
    });
  }

  function handleSourceTimeUpdate() {
    if (!sourceVideoElement) return;
    const currentMs = sourceVideoElement.currentTime * 1000;
    if (cuePlaybackActive && currentMs >= cuePlaybackEndMs - 20) {
      cuePlaybackActive = false;
      sourceVideoElement.pause();
      const duration = Number.isFinite(sourceVideoElement.duration) ? sourceVideoElement.duration : cuePlaybackEndMs / 1000;
      sourceVideoElement.currentTime = Math.min(cuePlaybackEndMs / 1000, duration);
      return;
    }
    if (packageData?.cues?.length) {
      onPlaybackCue(packageData.cues[cueIndexAtTime(packageData.cues, currentMs)]?.id);
    }
  }

  function togglePlayback() {
    if (stageMode === "video") toggleVideo();
    else toggleCueAudio();
  }

  function handleMessage(event) {
    if (!playerOrigin || event.source !== playerFrame?.contentWindow || event.origin !== playerOrigin || !event.data) return;
    if (event.data.type === "hyperframes:ready") {
      if (!(Number(event.data.duration) > 0)) {
        playerError = "Player did not report a usable duration";
        return;
      }
      playerReady = true;
      playerError = "";
      return;
    }
    if (event.data.type === "hyperframes:error") {
      playerReady = false;
      playerPlaying = false;
      playerError = event.data.message || "Player unavailable";
      return;
    }
    if (event.data.type === "hyperframes:timeupdate") {
      playerPlaying = Boolean(event.data.playing);
      const time = Number(event.data.time);
      if (Number.isFinite(time) && packageData?.cues?.length) {
        onPlaybackCue(packageData.cues[cueIndexAtTime(packageData.cues, time * 1000)]?.id);
      }
    }
  }

  function replayRisk() {
    if (!cueAudio?.src) return;
    cueAudio.currentTime = 0;
    toggleCueAudio();
  }

  onMount(() => {
    window.addEventListener("message", handleMessage);
    window.addEventListener("framecue:toggle-playback", togglePlayback);
    return () => {
      window.removeEventListener("message", handleMessage);
      window.removeEventListener("framecue:toggle-playback", togglePlayback);
    };
  });
</script>

<section class:portraitStage={stageMode === "video" && hyperframes && !sourceVideo} class="media-stage" aria-label="Media Stage">
  <div class="stage-topline">
    <div>
      <span class="eyebrow">Media Stage</span>
      <strong>{cue?.id || "No cue"}</strong>
    </div>
    <div class="mode-strip" aria-label="Media mode">
      {#each availableModes as mode}
        <button class:active={stageMode === mode} type="button" on:click={() => setMode(mode)}>
          {mode === "still" ? "Still" : mode === "redraw" ? "Redraw" : mode === "boundary" ? "Boundary" : "Video"}
        </button>
      {/each}
    </div>
  </div>

  <div class="stage-canvas">
    {#if stageMode === "video" && sourceVideo}
      <div class="source-video-stage">
        <video
          bind:this={sourceVideoElement}
          src={assetUrl(sourceVideo.src)}
          poster={scene?.image ? assetUrl(scene.image) : ""}
          preload="metadata"
          controls
          playsinline
          on:loadedmetadata={() => alignSourceVideo(true)}
          on:play={() => { pauseCueAudio(); playerPlaying = true; }}
          on:pause={() => playerPlaying = false}
          on:ended={() => { cuePlaybackActive = false; playerPlaying = false; }}
          on:timeupdate={handleSourceTimeUpdate}
        >
          <track kind="captions" src={assetUrl(sourceVideo.captions)} srclang="zh-Hant" label="FrameCue bilingual subtitles" />
        </video>
        <div class="subtitle-overlay">
          {#if cue?.original_text}
            <div class="subtitle source"><span>{cue.original_text}</span></div>
          {/if}
          <div class="subtitle target">
            <span>
              {#each markedParts(cueDraft?.text || "", cue?.risks || []) as part}
                {#if part.risk}<mark>{part.text}</mark>{:else}{part.text}{/if}
              {/each}
            </span>
          </div>
        </div>
      </div>
    {:else if stageMode === "video" && hyperframes}
      <div class="player-shell">
        <iframe bind:this={playerFrame} src={playerSrc} title="HyperFrames review player" allow="autoplay"></iframe>
        {#if !playerReady}
          <div class:error={Boolean(playerError)} class="player-overlay">{playerError || "Loading HyperFrames player"}</div>
        {/if}
      </div>
    {:else if stageMode === "redraw" && redraw}
      <div class="compare-stage">
        <figure>
          <img src={assetUrl(redrawBefore)} alt="Redraw comparison" />
          <figcaption>{redraw.before_image ? "Before" : "Comparison"}</figcaption>
        </figure>
        <figure>
          <img src={assetUrl(redrawAfter)} alt="Current redraw" />
          <figcaption>{redraw.after_image ? "After" : "Current"}</figcaption>
        </figure>
      </div>
    {:else if stageMode === "boundary" && boundary}
      <div class="compare-stage">
        <figure>
          <img src={assetUrl(boundary.before_image || scene?.image)} alt="Before subtitle boundary" />
          <figcaption>Before {formatTime(cue?.start_ms || 0)}</figcaption>
        </figure>
        <figure>
          <img src={assetUrl(boundary.after_image || scene?.image)} alt="After subtitle boundary" />
          <figcaption>After {formatTime(cue?.end_ms || 0)}</figcaption>
        </figure>
      </div>
    {:else if scene}
      <div class="still-stage">
        <img src={assetUrl(scene.image)} alt={`Representative frame for ${cue.id}`} />
        <div class="subtitle-overlay">
          {#if cue?.original_text}
            <div class="subtitle source"><span>{cue.original_text}</span></div>
          {/if}
          <div class="subtitle target">
            <span>
              {#each markedParts(cueDraft?.text || "", cue?.risks || []) as part}
                {#if part.risk}<mark>{part.text}</mark>{:else}{part.text}{/if}
              {/each}
            </span>
          </div>
        </div>
      </div>
    {:else}
      <div class="stage-empty">No scene image is available for this cue.</div>
    {/if}
  </div>

  <div class="stage-footer">
    <span>{formatTime(cue?.start_ms || 0)} to {formatTime(cue?.end_ms || 0)}</span>
    <div class="stage-actions">
      {#if stageMode === "video" && (sourceVideo || hyperframes)}
        <button type="button" on:click={toggleVideo}>{playerPlaying ? "Pause cue" : "Play cue"}</button>
      {:else if cue?.audio}
        <button type="button" on:click={toggleCueAudio}>{cueAudio?.paused === false ? "Pause cue" : "Play cue"}</button>
      {/if}
      {#if (cue?.risks || []).length && cue?.audio}
        <button class="warning-button" type="button" on:click={replayRisk}>Replay risk</button>
      {/if}
    </div>
  </div>
  {#if cue?.audio}
    <audio bind:this={cueAudio} src={assetUrl(cue.audio)} preload="metadata" on:play={pausePlayer}></audio>
  {/if}
</section>
