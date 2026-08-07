const subtitleActions = [
  { value: "use_edit", label: "採用直接修改" },
  { value: "rewrite", label: "重新改寫內容" },
  { value: "resegment", label: "重新切分" },
  { value: "retime", label: "回上游調整時間" }
];

export const ACTIONS_BY_WORKFLOW = {
  subtitle: subtitleActions,
  redraw: subtitleActions,
  boundary: subtitleActions,
  hyperframes: subtitleActions,
  image_carousel: [
    { value: "use_edit", label: "無需後續處理" },
    { value: "replace_asset", label: "替換素材" },
    { value: "rewrite_copy", label: "改寫圖卡文案" },
    { value: "recrop", label: "重新裁切" },
    { value: "reorder", label: "調整順序" }
  ],
  markdown: [
    { value: "use_edit", label: "採用直接修改" },
    { value: "rewrite", label: "改寫" },
    { value: "cut", label: "刪減" },
    { value: "split", label: "拆分區塊" },
    { value: "needs_source", label: "需要來源" }
  ]
};

export function actionsForWorkflow(kind) {
  return ACTIONS_BY_WORKFLOW[kind] || subtitleActions;
}

const protectedLatinToken = /(?<![A-Za-z0-9])(?:[A-Za-z0-9]+(?:[._/+\-][A-Za-z0-9+#]+)+|[A-Za-z][+#]{1,2})(?![A-Za-z0-9])/g;
const displayPunctuation = /[，。？！、；："'（）【】《》「」『』·・･•…—–\-!?,.:;()\[\]<>’]+/g;

export function cleanDisplayText(value) {
  const tokens = [];
  const protectedText = String(value || "").replace(protectedLatinToken, (token) => {
    tokens.push(token);
    return `FRAMECUETOKEN${tokens.length - 1}X`;
  });
  let cleaned = protectedText.replace(displayPunctuation, " ").replace(/\s+/g, " ").trim();
  tokens.forEach((token, index) => {
    cleaned = cleaned.replace(`FRAMECUETOKEN${index}X`, token);
  });
  return cleaned;
}

export function textForDisplay(packageData, value) {
  if (packageData?.subtitle_policy?.display_punctuation === "stripped_before_framecue") {
    return cleanDisplayText(value);
  }
  return String(value || "");
}

export function draftKey(packageData) {
  return [
    "framecue-v2",
    packageData.review_id,
    packageData.revision,
    packageData.content_checksum
  ].join(":");
}

export function createDraft(packageData) {
  return {
    schema_version: "framecue_browser_draft_v1",
    selected_cue_id: packageData.cues[0]?.id || "",
    selected_block_id: packageData.blocks[0]?.id || "",
    active_scope: "cue",
    cue_filter: "all",
    reviewed_cues: Object.fromEntries(packageData.cues.map((cue) => [cue.id, false])),
    cues: Object.fromEntries(packageData.cues.map((cue) => [cue.id, {
      text: cue.text,
      speech_text: cue.speech_text,
      action: "use_edit",
      instruction: ""
    }])),
    blocks: Object.fromEntries(packageData.blocks.map((block) => [block.id, {
      target_text: block.target_text,
      speech_text: block.speech_text,
      action: "use_edit",
      instruction: "",
      approved: false
    }])),
    final_approval: null
  };
}

export function mergeDraft(packageData, savedDraft) {
  const fresh = createDraft(packageData);
  if (!savedDraft || savedDraft.schema_version !== fresh.schema_version) return fresh;
  const actions = actionsForWorkflow(packageData.workflow?.kind);
  for (const cue of packageData.cues) {
    const saved = savedDraft.cues?.[cue.id];
    if (!saved) continue;
    fresh.cues[cue.id] = {
      text: textForDisplay(packageData, saved.text ?? cue.text),
      speech_text: String(saved.speech_text ?? cue.speech_text),
      action: actions.some((action) => action.value === saved.action) ? saved.action : "use_edit",
      instruction: String(saved.instruction || "")
    };
    fresh.reviewed_cues[cue.id] = Boolean(savedDraft.reviewed_cues?.[cue.id]);
  }
  for (const block of packageData.blocks) {
    const saved = savedDraft.blocks?.[block.id];
    if (!saved) continue;
    fresh.blocks[block.id] = {
      target_text: textForDisplay(packageData, saved.target_text ?? block.target_text),
      speech_text: String(saved.speech_text ?? block.speech_text),
      action: actions.some((action) => action.value === saved.action) ? saved.action : "use_edit",
      instruction: String(saved.instruction || ""),
      approved: Boolean(saved.approved)
    };
  }
  fresh.selected_cue_id = packageData.cues.some((cue) => cue.id === savedDraft.selected_cue_id)
    ? savedDraft.selected_cue_id
    : fresh.selected_cue_id;
  fresh.selected_block_id = packageData.blocks.some((block) => block.id === savedDraft.selected_block_id)
    ? savedDraft.selected_block_id
    : fresh.selected_block_id;
  fresh.active_scope = "cue";
  fresh.cue_filter = "all";
  return fresh;
}

export function contentKey(value) {
  return String(value || "")
    .normalize("NFKC")
    .toLocaleLowerCase()
    .replace(/[\p{P}\p{Z}\s]/gu, "");
}

export function blockContentIssue(packageData, draft, blockId) {
  const block = packageData.blocks.find((item) => item.id === blockId);
  const state = draft.blocks[blockId];
  if (!block || !state) return "";
  const cueText = block.cue_ids.map((cueId) => draft.cues[cueId]?.text || "").join(" ");
  if (contentKey(state.target_text) !== contentKey(cueText)) {
    return "顯示文字與所屬 Cue 不一致，請先統整語意塊再核准。";
  }
  if (contentKey(state.speech_text) !== contentKey(state.target_text)) {
    return "語音文字與顯示內容不一致，請先統整再核准。";
  }
  return "";
}

export function withCueChange(packageData, draft, cueId, patch) {
  const cues = { ...draft.cues, [cueId]: { ...draft.cues[cueId], ...patch } };
  const blocks = { ...draft.blocks };
  for (const block of packageData.blocks.filter((item) => item.cue_ids.includes(cueId))) {
    blocks[block.id] = {
      ...blocks[block.id],
      target_text: block.cue_ids.map((childId) => cues[childId]?.text || "").join(" ").trim(),
      approved: false
    };
  }
  return {
    ...draft,
    cues,
    blocks,
    reviewed_cues: { ...draft.reviewed_cues, [cueId]: false },
    final_approval: null
  };
}

export function withCueRangeAction(packageData, draft, cueIds, action, instruction) {
  return [...new Set(cueIds)].reduce((next, cueId) => (
    next.cues[cueId] ? withCueChange(packageData, next, cueId, { action, instruction }) : next
  ), draft);
}

export function withBlockChange(draft, blockId, patch) {
  return {
    ...draft,
    blocks: {
      ...draft.blocks,
      [blockId]: { ...draft.blocks[blockId], ...patch, approved: false }
    },
    final_approval: null
  };
}

export function withBlockApproval(packageData, draft, blockId, approved) {
  if (approved && blockContentIssue(packageData, draft, blockId)) return draft;
  return {
    ...draft,
    blocks: {
      ...draft.blocks,
      [blockId]: { ...draft.blocks[blockId], approved }
    },
    final_approval: null
  };
}

export function approveReviewedBlock(packageData, draft, blockId) {
  const block = packageData.blocks.find((item) => item.id === blockId);
  if (!block || !block.cue_ids.every((cueId) => draft.reviewed_cues?.[cueId])) return draft;
  return withBlockApproval(packageData, draft, blockId, true);
}

export function approveBlockAndAdvance(packageData, draft, blockId) {
  const nextDraft = draft.blocks[blockId]?.approved ? draft : withBlockApproval(packageData, draft, blockId, true);
  if (!nextDraft.blocks[blockId]?.approved || blockContentIssue(packageData, nextDraft, blockId)) return nextDraft;
  const nextBlock = packageData.blocks[packageData.blocks.findIndex((block) => block.id === blockId) + 1];
  if (!nextBlock) return nextDraft;
  return {
    ...nextDraft,
    active_scope: "block",
    selected_block_id: nextBlock.id,
    selected_cue_id: nextBlock.cue_ids[0] || nextDraft.selected_cue_id
  };
}

export function reviewedCueCount(packageData, draft) {
  return packageData.cues.filter((cue) => draft.reviewed_cues?.[cue.id]).length;
}

export function reviewCueAndAdvance(packageData, draft, cueId) {
  const cueIndex = packageData.cues.findIndex((cue) => cue.id === cueId);
  if (cueIndex < 0) return draft;
  const reviewedCues = { ...draft.reviewed_cues, [cueId]: true };
  let nextDraft = { ...draft, active_scope: "cue", reviewed_cues: reviewedCues };
  const blocks = { ...draft.blocks };
  for (const block of packageData.blocks.filter((item) => item.cue_ids.includes(cueId))) {
    if (block.cue_ids.every((childId) => reviewedCues[childId]) && !blockContentIssue(packageData, nextDraft, block.id)) {
      blocks[block.id] = { ...blocks[block.id], approved: true };
    }
  }
  nextDraft = { ...nextDraft, blocks };
  const nextCue = packageData.cues[cueIndex + 1];
  if (!nextCue) return nextDraft;
  const nextBlock = packageData.blocks.find((block) => block.cue_ids.includes(nextCue.id));
  return {
    ...nextDraft,
    selected_cue_id: nextCue.id,
    selected_block_id: nextBlock?.id || nextDraft.selected_block_id
  };
}

export function finalApprovalAllowed(packageData, draft) {
  if (reviewedCueCount(packageData, draft) !== packageData.cues.length) return false;
  return !packageData.blocks.length || packageData.blocks.every((block) =>
    draft.blocks[block.id]?.approved && !blockContentIssue(packageData, draft, block.id)
  );
}

export function makeResult(packageData, draft, approvedAt = "") {
  const approved = Boolean(approvedAt);
  return {
    schema_version: "framecue_review_result_v1",
    review_id: packageData.review_id,
    revision: packageData.revision,
    package_checksum: packageData.content_checksum,
    viewer_version: packageData.viewer_version,
    status: approved ? "approved" : "draft",
    approved_at: approvedAt,
    generated_at: new Date().toISOString(),
    blocks: packageData.blocks.map((block) => ({
      id: block.id,
      target_text: draft.blocks[block.id].target_text,
      speech_text: draft.blocks[block.id].speech_text,
      action: draft.blocks[block.id].action,
      instruction: draft.blocks[block.id].instruction,
      approved: Boolean(draft.blocks[block.id].approved)
    })),
    cues: packageData.cues.map((cue) => ({
      id: cue.id,
      text: draft.cues[cue.id].text,
      speech_text: draft.cues[cue.id].speech_text,
      action: draft.cues[cue.id].action,
      instruction: draft.cues[cue.id].instruction
    }))
  };
}

export function changedCount(packageData, draft) {
  let count = 0;
  for (const cue of packageData.cues) {
    const current = draft.cues[cue.id];
    if (
      current.text !== cue.text ||
      current.speech_text !== cue.speech_text ||
      current.action !== "use_edit" ||
      current.instruction.trim()
    ) count += 1;
  }
  for (const block of packageData.blocks) {
    const current = draft.blocks[block.id];
    if (
      current.target_text !== block.target_text ||
      current.speech_text !== block.speech_text ||
      current.action !== "use_edit" ||
      current.instruction.trim()
    ) count += 1;
  }
  return count;
}

export function cueIndexAtTime(cues, milliseconds) {
  let found = 0;
  cues.forEach((cue, index) => {
    if (milliseconds >= cue.start_ms) found = index;
  });
  return found;
}

export function cueNeedsSeek(cue, milliseconds, tolerance = 100) {
  return !cue || milliseconds < cue.start_ms - tolerance || milliseconds >= cue.end_ms + tolerance;
}

export function cuePlaybackEnded(milliseconds, endMilliseconds, tolerance = 20) {
  return milliseconds >= endMilliseconds - tolerance;
}

export function formatTime(milliseconds) {
  const total = Math.max(0, Math.round(Number(milliseconds) || 0));
  const hours = String(Math.floor(total / 3600000)).padStart(2, "0");
  const minutes = String(Math.floor((total % 3600000) / 60000)).padStart(2, "0");
  const seconds = String(Math.floor((total % 60000) / 1000)).padStart(2, "0");
  const remainder = String(total % 1000).padStart(3, "0");
  return `${hours}:${minutes}:${seconds}.${remainder}`;
}

export function markedParts(text, risks = []) {
  const terms = [...new Set(risks.filter((term) => term && term !== "英文混中文" && term !== "數字"))]
    .sort((left, right) => right.length - left.length);
  if (!terms.length) return [{ text: String(text || ""), risk: false }];
  const expression = new RegExp(`(${terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "g");
  return String(text || "").split(expression).filter((part) => part !== "").map((part) => ({
    text: part,
    risk: terms.includes(part)
  }));
}
