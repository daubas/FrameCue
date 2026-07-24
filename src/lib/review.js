export const ACTIONS = [
  { value: "use_edit", label: "Use edit" },
  { value: "rewrite", label: "Rewrite content" },
  { value: "resegment", label: "Resegment" },
  { value: "retime", label: "Retime upstream" }
];

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
    active_scope: packageData.blocks.length ? "block" : "cue",
    cue_filter: "risk",
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
  for (const cue of packageData.cues) {
    const saved = savedDraft.cues?.[cue.id];
    if (!saved) continue;
    fresh.cues[cue.id] = {
      text: textForDisplay(packageData, saved.text ?? cue.text),
      speech_text: String(saved.speech_text ?? cue.speech_text),
      action: ACTIONS.some((action) => action.value === saved.action) ? saved.action : "use_edit",
      instruction: String(saved.instruction || "")
    };
  }
  for (const block of packageData.blocks) {
    const saved = savedDraft.blocks?.[block.id];
    if (!saved) continue;
    fresh.blocks[block.id] = {
      target_text: textForDisplay(packageData, saved.target_text ?? block.target_text),
      speech_text: String(saved.speech_text ?? block.speech_text),
      action: ACTIONS.some((action) => action.value === saved.action) ? saved.action : "use_edit",
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
  fresh.active_scope = savedDraft.active_scope === "cue" || (savedDraft.active_scope === "block" && packageData.blocks.length)
    ? savedDraft.active_scope
    : fresh.active_scope;
  fresh.cue_filter = savedDraft.cue_filter === "all" ? "all" : "risk";
  return fresh;
}

export function withCueChange(draft, cueId, patch) {
  return {
    ...draft,
    cues: { ...draft.cues, [cueId]: { ...draft.cues[cueId], ...patch } },
    final_approval: null
  };
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

export function withBlockApproval(draft, blockId, approved) {
  return {
    ...draft,
    blocks: {
      ...draft.blocks,
      [blockId]: { ...draft.blocks[blockId], approved }
    },
    final_approval: null
  };
}

export function finalApprovalAllowed(packageData, draft) {
  return !packageData.blocks.length || packageData.blocks.every((block) => draft.blocks[block.id]?.approved);
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
