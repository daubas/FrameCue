# FrameCue Subtitle Workspace

FrameCue Subtitle Workspace 是字幕內容、配音實現與人工核准的共同工作語境。它區分來源內容、工作中的 revision、agent 候選結果與正式核准結果。

## Language

**Workspace**:
同一支媒體從內容審查、配音對齊到最終核准的持續工作範圍。
_Avoid_: Review package, task folder

**Cue**:
依時間顯示的一段字幕；它是畫面閱讀單位，不是語音生成單位。
_Avoid_: Line, sentence

**Semantic Block**:
一組連續 Cues 的語意與配音單位，擁有完整意思與 `speech_text`。內容審查時可隱藏其操作，仍保留其語意約束。
_Avoid_: Cue group, time block, Voice Block

**Content Revision**:
完成翻譯內容審查、尚未以實際語音時間對齊的不可變字幕版本。
_Avoid_: First subtitle, draft SRT

**Voice-Aligned Revision**:
綁定實際語音資產、字詞時間與輸出時間軸的不可變字幕版本。
_Avoid_: Second subtitle, AgenticDub subtitle

**Work Order**:
由已保存 revision 產生、交給 agent 繼續處理的 checksum-bound 工作要求。
_Avoid_: Note, prompt JSON, downloaded result

**Candidate Revision**:
Agent 針對一份 Work Order 回傳、尚待 FrameCue 人工接受的完整候選版本。
_Avoid_: Patch, correction file

**Review Flag**:
審閱者在影音審查期間標記為需要調整的範圍；完成本輪審查時，沒有 Review Flag 的範圍視為通過。
_Avoid_: Rejection, unreviewed block

**Approval Snapshot**:
完成整輪審查後、精確綁定 revision checksum 的不可變核准結果。
_Avoid_: Browser draft, saved state
