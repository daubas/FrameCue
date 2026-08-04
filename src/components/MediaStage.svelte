<script>
  import { onMount } from "svelte";
  import { cueIndexAtTime, cueNeedsSeek, cuePlaybackEnded, formatTime, markedParts } from "../lib/review.js";

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
  $: carousel = packageData?.media?.carousel || null;
  $: markdown = packageData?.media?.markdown || null;
  $: workflowKind = packageData?.workflow?.kind || "subtitle";
  $: cueNumber = Math.max(0, packageData?.cues?.findIndex((item) => item.id === cue?.id) + 1);
  $: markdownKind = cue?.markdown?.kind === "heading" ? "標題" : cue?.markdown?.kind === "list" ? "清單" : "段落";
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
    if (force || cueNeedsSeek(cue, currentMs)) {
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
    if (currentMs < cue.start_ms - 100 || cuePlaybackEnded(currentMs, cue.end_ms)) {
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
    if (cuePlaybackActive && cuePlaybackEnded(currentMs, cuePlaybackEndMs)) {
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
        playerError = "播放器沒有回報可用的影片長度";
        return;
      }
      playerReady = true;
      playerError = "";
      return;
    }
    if (event.data.type === "hyperframes:error") {
      playerReady = false;
      playerPlaying = false;
      playerError = "播放器目前無法使用";
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
    window.addEventListener("framecue:pause-playback", pausePlayer);
    return () => {
      window.removeEventListener("message", handleMessage);
      window.removeEventListener("framecue:toggle-playback", togglePlayback);
      window.removeEventListener("framecue:pause-playback", pausePlayer);
    };
  });
</script>

<section class:portraitStage={stageMode === "video" && hyperframes && !sourceVideo} class:contextMedia={Boolean(carousel || markdown)} class="media-stage" aria-label="媒體畫面">
  <div class="stage-topline">
    <div>
      <span class="eyebrow">{carousel ? "圖卡審閱" : markdown ? "Markdown 審閱" : "媒體畫面"}</span>
      <strong>{cue?.id || "沒有 Cue"}</strong>
    </div>
    <div class="mode-strip" aria-label="畫面模式">
      {#each availableModes as mode}
        <button class:active={stageMode === mode} type="button" on:click={() => setMode(mode)}>
          {mode === "still" ? "Cue 畫面" : mode === "redraw" ? "重繪比較" : mode === "boundary" ? "切點比較" : "影片"}
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
          <track kind="captions" src={assetUrl(sourceVideo.captions)} srclang="zh-Hant" label="FrameCue 中英字幕" />
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
        <iframe bind:this={playerFrame} src={playerSrc} title="HyperFrames 審閱播放器" allow="autoplay"></iframe>
        {#if !playerReady}
          <div class:error={Boolean(playerError)} class="player-overlay">{playerError || "正在載入 HyperFrames 播放器"}</div>
        {/if}
      </div>
    {:else if stageMode === "redraw" && redraw}
      <div class="compare-stage">
        <figure>
          <img src={assetUrl(redrawBefore)} alt="重繪比較畫面" />
          <figcaption>{redraw.before_image ? "修改前" : "比較畫面"}</figcaption>
        </figure>
        <figure>
          <img src={assetUrl(redrawAfter)} alt="目前的重繪畫面" />
          <figcaption>{redraw.after_image ? "修改後" : "目前畫面"}</figcaption>
        </figure>
      </div>
    {:else if stageMode === "boundary" && boundary}
      <div class="compare-stage">
        <figure>
          <img src={assetUrl(boundary.before_image || scene?.image)} alt="字幕切點前畫面" />
          <figcaption>前：{formatTime(cue?.start_ms || 0)}</figcaption>
        </figure>
        <figure>
          <img src={assetUrl(boundary.after_image || scene?.image)} alt="字幕切點後畫面" />
          <figcaption>後：{formatTime(cue?.end_ms || 0)}</figcaption>
        </figure>
      </div>
    {:else if carousel && scene}
      <figure class="carousel-stage">
        <img src={assetUrl(scene.image)} alt={`${cue?.text || cue?.id} 圖卡`} />
        <figcaption>{cue?.text}</figcaption>
      </figure>
    {:else if markdown && cue}
      <article class="markdown-stage">
        <span>{markdownKind}</span>
        <div class:heading={cue?.markdown?.kind === "heading"} class="markdown-block">{cueDraft?.text || ""}</div>
      </article>
    {:else if scene}
      <div class="still-stage">
        <img src={assetUrl(scene.image)} alt={`${cue.id} 的代表畫面`} />
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
      <div class="stage-empty">這個 Cue 沒有可用的代表畫面。</div>
    {/if}
  </div>

  {#if carousel}
    <aside class="context-strip" aria-label="圖卡組參考畫面">
      <span class="context-label">參考（不列入審閱）</span>
      <div class="context-images">
        <a href={assetUrl(carousel.contact_sheet)} target="_blank" rel="noreferrer">
          <img src={assetUrl(carousel.contact_sheet)} alt="整組圖卡 contact sheet" />
          <span>整組順序</span>
        </a>
        <a href={assetUrl(carousel.mobile_audit)} target="_blank" rel="noreferrer">
          <img src={assetUrl(carousel.mobile_audit)} alt="390px 手機可讀性檢查" />
          <span>390px 手機檢查</span>
        </a>
      </div>
    </aside>
  {:else if markdown}
    <aside class="markdown-context" aria-label="文章參考內容">
      <span class="context-label">參考（不列入審閱） · {markdown.source_name}</span>
      <details>
        <summary>YAML frontmatter</summary>
        <pre>{markdown.frontmatter || "沒有 frontmatter"}</pre>
      </details>
      <details>
        <summary>編輯備註</summary>
        <pre>{markdown.editorial_notes || "沒有編輯備註"}</pre>
      </details>
    </aside>
  {/if}

  <div class="stage-footer">
    <span>{carousel ? `第 ${cueNumber} 張，共 ${packageData.cues.length} 張` : markdown ? `第 ${cueNumber} 個區塊，共 ${packageData.cues.length} 個` : `${formatTime(cue?.start_ms || 0)} 至 ${formatTime(cue?.end_ms || 0)}`}</span>
    <div class="stage-actions">
      {#if stageMode === "video" && (sourceVideo || hyperframes)}
        <button type="button" on:click={toggleVideo}>{playerPlaying ? "暫停 Cue" : "播放 Cue"}</button>
      {:else if cue?.audio}
        <button type="button" on:click={toggleCueAudio}>{cueAudio?.paused === false ? "暫停 Cue" : "播放 Cue"}</button>
      {/if}
      {#if (cue?.risks || []).length && cue?.audio}
        <button class="warning-button" type="button" on:click={replayRisk}>重播風險字</button>
      {/if}
    </div>
  </div>
  {#if cue?.audio}
    <audio bind:this={cueAudio} src={assetUrl(cue.audio)} preload="metadata" on:play={pausePlayer}></audio>
  {/if}
</section>
