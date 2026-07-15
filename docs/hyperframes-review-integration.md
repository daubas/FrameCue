# HyperFrames Review Player 與 FrameCue 整合提案

狀態：MVP 整合候選已完成；正式替換既有 pilot 頁面仍待草稿保存
日期：2026-07-15

實作與驗證結果見 [implementation note](hyperframes-review-implementation-note.md)。

## 背景

目前 Yuanyuan 短影音專案有一個額外建立的 `player.html`。它不是成片，也不是 HyperFrames Studio 的內建介面，而是一個審片用的靜態播放器：

- 以 `iframe` 載入 HyperFrames composition。
- 以完整旁白音訊作為主時間軸。
- 使用 `requestAnimationFrame` 持續把音訊時間傳給 HyperFrames 的 `__hf.seek()`。
- 提供播放、暫停、時間拖曳、前後五秒及場景切換。
- 透過靜態檔案服務與 Tailscale 提供遠端審片。

這個播放器解決了「直接打開 HyperFrames Studio 時，尚未選取 composition 可能看到空白畫面」以及「需要一個含正式音訊的穩定審片入口」兩個問題。

## 現有技術路線

播放器目前採用純 HTML、CSS、JavaScript，沒有 React、Vue、後端或影片串流服務。它播放的是即時 DOM composition，不是 MP4：

```text
player.html
  ├─ iframe：載入 HyperFrames index.html
  ├─ audio：播放正式旁白 WAV
  └─ JavaScript：audio.currentTime → __hf.seek(time)
```

FrameCue 也採用純 HTML、CSS、JavaScript。Python CLI 只負責建立 review package；瀏覽器端載入 `review_package.json`、畫面、cue 音訊及 semantic blocks，並以 `localStorage` 保存草稿、匯出審核結果。

因此兩者技術上相容，不需要為整合引入新的前端框架。

## 決策

將播放器整理成 HyperFrames 專案中的 **Review Player** 是合適的，但不把 FrameCue 的審核資料管理複製進 HyperFrames。

責任分工如下：

| 元件 | 責任 |
|---|---|
| HyperFrames composition | 畫面、動畫、轉場、字幕呈現與時間軸 |
| HyperFrames Review Player | 完整播放、音訊同步、seek、場景定位與播放狀態 |
| FrameCue | cue／scene 意見、字幕修改、批准狀態、審核紀錄與結果匯出 |

核心原則：

> HyperFrames 是可播放畫面的唯一來源；FrameCue 是審核決策的唯一來源。

## 採用的整合方式

FrameCue 增加可選的「完整影片」stage 模式，透過 `iframe` 載入 Review Player。靜態畫面與完整影片只切換左側 stage；右側 cue 與 semantic block 審核仍持續可用，才能在播放時看見目前 cue 的位置。

```text
FrameCue Viewer
  ├─ Stage：靜態畫面／HyperFrames Review Player
  ├─ Cue Review：逐句字幕、畫面與音訊
  └─ Block Review：語意區塊與審核決策
```

`framecue_manifest.json` 可用一個可選欄位連結播放器：

```json
{
  "items": [
    {
      "id": "yuanyuan-computex-2026",
      "label": "Yuanyuan COMPUTEX 2026",
      "review_package": "review_package.json",
      "semantic_blocks": "semantic_blocks/semantic_blocks.json",
      "review_player": {
        "type": "hyperframes",
        "src": "../../hyperframes/player.html"
      }
    }
  ]
}
```

`review_player.src` 以 FrameCue 頁面的 URL 為基準解析；上例假設頁面位於 pilot 的 `review/framecue/`，並從專案根目錄提供靜態檔案。`review_player` 缺少時，FrameCue 應維持目前行為，不顯示完整影片模式。

## 播放與審核同步

目前播放器直接存取同網域 iframe 的 `contentWindow.__hf`。MVP 可以沿用同網域部署，但 FrameCue 與播放器之間應使用 `window.postMessage`，避免 FrameCue 綁死 HyperFrames 的內部 API。

建議的最小訊息介面：

| 方向 | 訊息 | 用途 |
|---|---|---|
| FrameCue → Player | `framecue:seek` | 跳到 cue 或 scene 的開始時間 |
| FrameCue → Player | `framecue:play` | 從指定時間播放 |
| FrameCue → Player | `framecue:pause` | 暫停播放 |
| Player → FrameCue | `hyperframes:ready` | 播放器與 composition 已可用 |
| Player → FrameCue | `hyperframes:timeupdate` | 回報目前播放時間 |
| Player → FrameCue | `hyperframes:error` | 回報設定、composition、時間軸或音訊載入失敗 |

`hyperframes:ready` 只能在設定已載入、內層 composition iframe 已載入、`contentWindow.__hf.seek` 可呼叫、完整旁白音訊 metadata 可讀，而且 composition 內的 narration 已暫停並靜音時送出。MVP 維持同 origin；FrameCue 與 Player 都必須驗證訊息的 `source` 與確切 origin。

FrameCue 點選 cue 時，Review Player 應跳到該 cue 的開始時間；播放器進入另一個 cue 時，FrameCue 只同步醒目顯示對應項目，不得重設編輯器、切換逐 cue 音訊、重建列表或自動修改審核狀態。MVP 以數字時間為唯一跨元件同步依據，不傳遞兩套不相容的 scene ID。

## 為什麼不把完整審核功能放進 HyperFrames

HyperFrames 適合檢查動畫、轉場、節奏、音訊同步及特定時間的畫面，但目前的 review 能力主要是 Studio preview、lint、inspect 與 snapshot。它不是審核紀錄系統。

若再把 cue 評語、批准狀態、`localStorage` 草稿與 JSON 匯出複製進 HyperFrames，會產生兩套審核資料來源，難以判斷哪一份才是正式結果。

因此 HyperFrames 應提供真實播放與定位能力，FrameCue 繼續負責所有人工作業與審核輸出。

## 原播放器需要泛化的部分（已完成）

整合前的 Yuanyuan 播放器是單一影片專用，以下資料寫在頁面內：

- 總長度 `81.09` 秒。
- 正式音訊路徑。
- 十二個視覺段落的名稱與時間。
- composition 路徑。

整合候選已將音訊、場景、composition 與畫面尺寸改由播放器設定檔提供；總長度直接由音訊 metadata 取得，不在設定檔重複保存。Review Player 不再知道 Yuanyuan、COMPUTEX 或特定音訊檔名。

## MVP 範圍

1. 將現有播放器改成可讀設定的通用 HyperFrames Review Player。
2. 在 FrameCue viewer 加入可選的「完整影片」stage 模式。
3. 以 `postMessage` 完成 seek、play、pause、ready、error 與 timeupdate。
4. 讓 cue 選取與播放器時間雙向同步。
5. 保留 FrameCue 既有的草稿、下載與多 review package 行為。
6. Review Player 不存在或載入失敗時，FrameCue 仍能進行原本的逐 cue 審核。

## 非目標

- 不在 FrameCue 內渲染或輸出正式 MP4。
- 不把 HyperFrames Studio 編輯能力搬進 FrameCue。
- 不在 HyperFrames 內建立第二套評論或批准資料。
- 不改變 FrameCue 停在人工 review gate 的工作邊界。

## 驗收條件

- 可在 FrameCue 同一頁切換靜態畫面與完整影片，同時保留 cue／semantic block 審核操作。
- 點選 cue 後，影片定位誤差小於 100 ms。
- 播放時 FrameCue 能正確顯示目前 cue，且不造成列表跳動或意外寫入。
- 音訊只播放一次；Review Player 旁白、composition timeline audio、逐 cue audio 與 redraw full video 不會互相疊聲。
- 無 Review Player 的舊 package 仍可正常使用。
- 多 package 切換後，播放器、草稿與匯出檔名仍依 item ID 隔離。
