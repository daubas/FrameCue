const HISTORY_LIMIT = 100;

let generatedId = 0;

function clone(value) {
  if (typeof structuredClone === "function") return structuredClone(value);
  return JSON.parse(JSON.stringify(value));
}

function displayText(cue) {
  return String(cue?.display_text ?? cue?.text ?? "");
}

function setDisplayText(cue, value) {
  const text = String(value ?? "");
  if (Object.hasOwn(cue, "display_text") || !Object.hasOwn(cue, "text")) cue.display_text = text;
  if (Object.hasOwn(cue, "text")) cue.text = text;
}

function speechText(cue) {
  return String(cue?.speech_text ?? displayText(cue));
}

function speechIsLinked(cue) {
  return cue?.speech_linked !== false;
}

function setSpeechLinked(cue, linked) {
  cue.speech_linked = Boolean(linked);
}

function normalizeDisplay(values) {
  return values.map((value) => String(value ?? "").trim()).filter(Boolean).join(" ").trim();
}

function normalizeSpeech(values) {
  return values.map((value) => String(value ?? "")).filter(Boolean).join("").trim();
}

function cueIndex(document, cueId) {
  return document.cues.findIndex((cue) => cue.id === cueId);
}

function blockForCue(document, cueId) {
  return document.blocks.find((block) => block.cue_ids?.includes(cueId));
}

function recomputeBlocks(document) {
  const cues = new Map(document.cues.map((cue) => [cue.id, cue]));
  document.blocks = document.blocks.map((block) => {
    const children = (block.cue_ids || []).map((id) => cues.get(id)).filter(Boolean);
    const next = { ...block };
    next.target_text = normalizeDisplay(children.map(displayText));
    next.speech_text = normalizeSpeech(children.map(speechText));
    const starts = children.map((cue) => timingValue(cue, "start")).filter(Number.isFinite);
    const ends = children.map((cue) => timingValue(cue, "end")).filter(Number.isFinite);
    if (starts.length && Object.hasOwn(block, "start_ms")) next.start_ms = Math.min(...starts);
    if (ends.length && Object.hasOwn(block, "end_ms")) next.end_ms = Math.max(...ends);
    return next;
  });
}

function timingValue(cue, side) {
  const names = side === "start"
    ? ["source_start_ms", "start_ms", "output_start_ms"]
    : ["source_end_ms", "end_ms", "output_end_ms"];
  for (const name of names) {
    const raw = cue?.[name];
    if (raw === null || raw === undefined || raw === "") continue;
    const value = Number(raw);
    if (Number.isFinite(value)) return value;
  }
  return NaN;
}

function timingKeys(cue) {
  const pairs = [];
  if (Object.hasOwn(cue, "source_start_ms") || Object.hasOwn(cue, "source_end_ms")) {
    pairs.push(["source_start_ms", "source_end_ms"]);
  }
  if (Object.hasOwn(cue, "start_ms") || Object.hasOwn(cue, "end_ms")) {
    pairs.push(["start_ms", "end_ms"]);
  }
  if (Object.hasOwn(cue, "output_start_ms") || Object.hasOwn(cue, "output_end_ms")) {
    pairs.push(["output_start_ms", "output_end_ms"]);
  }
  return pairs;
}

function makeId(idFactory, context) {
  const factory = typeof idFactory === "function" ? idFactory : defaultIdFactory;
  const value = factory(context);
  if (typeof value !== "string" || !value) throw new Error("idFactory must return a non-empty string");
  return value;
}

function defaultIdFactory() {
  generatedId += 1;
  if (globalThis.crypto?.randomUUID) return `cue-${globalThis.crypto.randomUUID()}`;
  return `cue-${Date.now().toString(36)}-${generatedId.toString(36)}`;
}

function optionsOf(value) {
  if (typeof value === "function") return { idFactory: value };
  return value && typeof value === "object" ? value : {};
}

function authorOf(state, options) {
  return String(options.authorId ?? state.authorId ?? "local");
}

function assertVersion(state, options) {
  if (options.expectedVersion === undefined) return;
  const expected = options.expectedVersion;
  if (expected !== state.version) {
    throw new Error(`stale draft version: expected ${expected}, current ${state.version}`);
  }
}

function snapshot(state) {
  return {
    document: clone(state.document),
    issues: clone(state.issues),
    directEditCount: state.directEditCount
  };
}

function commit(state, next, options, kind, directEdit = true) {
  assertVersion(state, options);
  const before = snapshot(state);
  const nextDocument = clone(next.document ?? state.document);
  const nextIssues = clone(next.issues ?? state.issues);
  const nextCount = next.directEditCount ?? state.directEditCount + (directEdit ? 1 : 0);
  const after = {
    document: nextDocument,
    issues: nextIssues,
    directEditCount: nextCount
  };
  const entry = {
    kind,
    authorId: authorOf(state, options),
    before,
    after
  };
  return {
    ...state,
    ...after,
    version: state.version + 1,
    history: [...state.history, entry].slice(-HISTORY_LIMIT),
    redo: []
  };
}

export function createDraftState(document, options = {}) {
  if (!document || typeof document !== "object" || !Array.isArray(document.cues) || !Array.isArray(document.blocks)) {
    throw new Error("subtitle document must contain cues and blocks");
  }
  const opts = optionsOf(options);
  const nextDocument = clone(document);
  return {
    document: nextDocument,
    version: Number.isInteger(opts.version) ? opts.version : 0,
    issues: clone(opts.issues || []),
    directEditCount: Number.isInteger(opts.directEditCount) ? opts.directEditCount : 0,
    history: [],
    redo: [],
    authorId: String(opts.authorId ?? "local")
  };
}

export function editCueDisplayText(state, cueId, value, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const index = cueIndex(state.document, cueId);
  if (index < 0) throw new Error(`cue not found: ${cueId}`);
  const document = clone(state.document);
  const cue = document.cues[index];
  const nextValue = String(value ?? "");
  if (displayText(cue) === nextValue) return state;
  setDisplayText(cue, nextValue);
  if (speechIsLinked(cue)) cue.speech_text = nextValue;
  recomputeBlocks(document);
  return commit(state, { document }, opts, "edit_display_text");
}

export function editCueSpeechText(state, cueId, value, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const index = cueIndex(state.document, cueId);
  if (index < 0) throw new Error(`cue not found: ${cueId}`);
  const document = clone(state.document);
  const cue = document.cues[index];
  const nextValue = String(value ?? "");
  if (speechText(cue) === nextValue && !speechIsLinked(cue)) return state;
  cue.speech_text = nextValue;
  setSpeechLinked(cue, false);
  recomputeBlocks(document);
  return commit(state, { document }, opts, "edit_speech_text");
}

function wordTimingInfo(cue) {
  const words = cue.word_timestamps;
  const trusted = cue.word_timestamps_trusted === true;
  if (!Array.isArray(words) || words.length < 2 || !trusted) return null;
  const entries = words.map((word) => ({
    text: String(word?.word ?? ""),
    start: Number(word?.start_ms),
    end: Number(word?.end_ms)
  })).filter((word) => word.text && Number.isFinite(word.start) && Number.isFinite(word.end));
  return entries.length >= 2 ? entries : null;
}

function splitBoundary(cue, cursor) {
  const text = displayText(cue);
  const length = text.length;
  const position = Number(cursor);
  if (!Number.isInteger(position) || position <= 0 || position >= length) {
    throw new Error("split cursor must be inside cue text");
  }
  const words = wordTimingInfo(cue);
  if (!words) {
    const start = timingValue(cue, "start");
    const end = timingValue(cue, "end");
    const fraction = position / length;
    return {
      index: position,
      time: Number.isFinite(start) && Number.isFinite(end) ? start + (end - start) * fraction : undefined,
      provisional: true,
      wordCount: 0
    };
  }
  const boundaries = [];
  let offset = 0;
  for (let index = 0; index < words.length - 1; index += 1) {
    offset += words[index].text.length;
    if (offset > 0 && offset < length) {
      boundaries.push({ index: offset, time: words[index].end, wordCount: index + 1 });
    }
  }
  if (!boundaries.length) {
    const start = timingValue(cue, "start");
    const end = timingValue(cue, "end");
    const fraction = position / length;
    return {
      index: position,
      time: Number.isFinite(start) && Number.isFinite(end) ? start + (end - start) * fraction : undefined,
      provisional: true,
      wordCount: 0
    };
  }
  boundaries.sort((a, b) => Math.abs(a.index - position) - Math.abs(b.index - position) || a.index - b.index);
  return { ...boundaries[0], provisional: false };
}

function splitSpeech(cue, displayIndex, displayLength) {
  const speech = speechText(cue);
  if (!speech) return ["", ""];
  const ratio = displayLength ? displayIndex / displayLength : 0.5;
  const index = Math.max(1, Math.min(speech.length - 1, Math.round(speech.length * ratio)));
  return [speech.slice(0, index), speech.slice(index)];
}

function splitWords(cue, wordCount) {
  if (!Array.isArray(cue.word_timestamps) || !wordCount) return [undefined, undefined];
  return [cue.word_timestamps.slice(0, wordCount), cue.word_timestamps.slice(wordCount)];
}

function setSplitTiming(cue, boundaryTime, provisional, parent) {
  for (const [startKey, endKey] of timingKeys(parent)) {
    let start = Number(parent[startKey]);
    let end = Number(parent[endKey]);
    if (!Number.isFinite(start) || !Number.isFinite(end) || (start === 0 && end === 0 && (parent[startKey] == null || parent[endKey] == null))) {
      start = timingValue(parent, "start");
      end = timingValue(parent, "end");
    }
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const boundary = Number.isFinite(boundaryTime) ? boundaryTime : start + (end - start) / 2;
    cue[startKey] = start;
    cue[endKey] = boundary;
  }
  cue.provisional = Boolean(provisional);
}

function setSplitTimingRight(cue, boundaryTime, provisional, parent) {
  for (const [startKey, endKey] of timingKeys(parent)) {
    let start = Number(parent[startKey]);
    let end = Number(parent[endKey]);
    if (!Number.isFinite(start) || !Number.isFinite(end) || (start === 0 && end === 0 && (parent[startKey] == null || parent[endKey] == null))) {
      start = timingValue(parent, "start");
      end = timingValue(parent, "end");
    }
    if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
    const boundary = Number.isFinite(boundaryTime) ? boundaryTime : start + (end - start) / 2;
    cue[startKey] = boundary;
    cue[endKey] = end;
  }
  cue.provisional = Boolean(provisional);
}

export function splitCueAtCursor(state, cueId, cursor, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const index = cueIndex(state.document, cueId);
  if (index < 0) throw new Error(`cue not found: ${cueId}`);
  const parent = state.document.cues[index];
  const boundary = splitBoundary(parent, cursor);
  const leftId = makeId(opts.idFactory, { operation: "split", parentCueId: cueId, side: "left" });
  const rightId = makeId(opts.idFactory, { operation: "split", parentCueId: cueId, side: "right" });
  if (leftId === rightId) throw new Error("split cue IDs must be distinct");
  if (state.document.cues.some((cue) => cue.id === leftId || cue.id === rightId)) {
    throw new Error("split cue IDs must be unique");
  }
  const text = displayText(parent);
  const [leftSpeech, rightSpeech] = splitSpeech(parent, boundary.index, text.length);
  const [leftWords, rightWords] = splitWords(parent, boundary.wordCount);
  const lineage = [...new Set([...(parent.origin_cue_ids || []), cueId])];
  const left = clone(parent);
  const right = clone(parent);
  left.id = leftId;
  right.id = rightId;
  setDisplayText(left, text.slice(0, boundary.index).trim());
  setDisplayText(right, text.slice(boundary.index).trim());
  left.speech_text = leftSpeech.trim();
  right.speech_text = rightSpeech.trim();
  if (typeof parent.source_text === "string") {
    const sourceIndex = Math.max(1, Math.min(parent.source_text.length - 1,
      Math.round(parent.source_text.length * boundary.index / text.length)));
    left.source_text = parent.source_text.slice(0, sourceIndex).trim();
    right.source_text = parent.source_text.slice(sourceIndex).trim();
  }
  left.origin_cue_ids = lineage;
  right.origin_cue_ids = lineage;
  if (leftWords) left.word_timestamps = leftWords;
  if (rightWords) right.word_timestamps = rightWords;
  setSplitTiming(left, boundary.time, boundary.provisional, parent);
  setSplitTimingRight(right, boundary.time, boundary.provisional, parent);

  const document = clone(state.document);
  document.cues.splice(index, 1, left, right);
  for (const block of document.blocks) {
    if (!block.cue_ids?.includes(cueId)) continue;
    const childIndex = block.cue_ids.indexOf(cueId);
    block.cue_ids.splice(childIndex, 1, leftId, rightId);
  }
  recomputeBlocks(document);
  return commit(state, { document }, opts, "split_cue");
}

export function mergeAdjacentCues(state, firstCueId, secondCueId, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const firstIndex = cueIndex(state.document, firstCueId);
  const secondIndex = cueIndex(state.document, secondCueId);
  if (firstIndex < 0 || secondIndex < 0) throw new Error("cue not found");
  if (Math.abs(firstIndex - secondIndex) !== 1) throw new Error("cues must be adjacent");
  const first = state.document.cues[Math.min(firstIndex, secondIndex)];
  const second = state.document.cues[Math.max(firstIndex, secondIndex)];
  const block = blockForCue(state.document, first.id);
  if (!block || block.id !== blockForCue(state.document, second.id)?.id) {
    throw new Error("cues must belong to the same block");
  }
  const blockIndex = block.cue_ids.indexOf(first.id);
  if (blockIndex < 0 || block.cue_ids[blockIndex + 1] !== second.id) {
    throw new Error("cues must be adjacent in the same block");
  }
  const mergedId = makeId(opts.idFactory, { operation: "merge", cueIds: [first.id, second.id] });
  if (state.document.cues.some((cue) => cue.id === mergedId)) throw new Error("merged cue ID must be unique");
  const merged = clone(first);
  merged.id = mergedId;
  setDisplayText(merged, normalizeDisplay([displayText(first), displayText(second)]));
  merged.speech_text = normalizeSpeech([speechText(first), speechText(second)]);
  if (typeof first.source_text === "string" || typeof second.source_text === "string") {
    merged.source_text = normalizeDisplay([first.source_text, second.source_text]);
  }
  merged.origin_cue_ids = [...new Set([
    ...(first.origin_cue_ids || []), first.id,
    ...(second.origin_cue_ids || []), second.id
  ])];
  if (timingKeys(first).length) {
    for (const [startKey, endKey] of timingKeys(first)) {
      const start = first[startKey] == null ? NaN : Number(first[startKey]);
      const end = second[endKey] == null ? NaN : Number(second[endKey]);
      if (Number.isFinite(start)) merged[startKey] = start;
      if (Number.isFinite(end)) merged[endKey] = end;
    }
  }
  merged.provisional = Boolean(first.provisional || second.provisional);
  setSpeechLinked(merged, speechIsLinked(first) && speechIsLinked(second));
  const document = clone(state.document);
  document.cues.splice(Math.min(firstIndex, secondIndex), 2, merged);
  const targetBlock = document.blocks.find((item) => item.id === block.id);
  const targetIndex = targetBlock.cue_ids.indexOf(first.id);
  targetBlock.cue_ids.splice(targetIndex, 2, mergedId);
  recomputeBlocks(document);
  return commit(state, { document }, opts, "merge_cues");
}

function canonicalRange(document, cueIds) {
  if (!Array.isArray(cueIds) || !cueIds.length) throw new Error("issue range must contain cues");
  const indexes = cueIds.map((id) => cueIndex(document, id));
  if (indexes.some((index) => index < 0)) throw new Error("issue range cue not found");
  indexes.sort((a, b) => a - b);
  for (let index = 1; index < indexes.length; index += 1) {
    if (indexes[index] !== indexes[index - 1] + 1) throw new Error("issue range must be contiguous");
  }
  return indexes.map((index) => document.cues[index].id);
}

function issueValues(issue, state, options) {
  const authorValue = issue.author ?? issue.authors ?? options.authorId ?? state.authorId;
  const authors = Array.isArray(authorValue) ? authorValue : [authorValue];
  const hasCategories = issue.category !== undefined || issue.categories !== undefined;
  const categoryValue = issue.category ?? issue.categories ?? [];
  const categories = Array.isArray(categoryValue) ? categoryValue : [categoryValue];
  const noteValue = issue.note ?? issue.notes ?? [];
  const notes = Array.isArray(noteValue) ? noteValue : [noteValue];
  return {
    authors: [...new Set(authors.map((value) => String(value ?? "").trim()).filter(Boolean))],
    categories: [...new Set(categories.map((value) => String(value ?? "").trim()).filter(Boolean))],
    notes: [...new Set(notes.map((value) => String(value ?? "").trim()).filter(Boolean))],
    hasCategories
  };
}

export function toggleIssueRange(state, cueIds, issue = {}, options = {}) {
  if (!Array.isArray(cueIds) && cueIds && typeof cueIds === "object") {
    options = issue;
    issue = cueIds;
    cueIds = issue.cue_ids ?? issue.cueIds;
  }
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const range = canonicalRange(state.document, cueIds);
  const values = issueValues(issue, state, opts);
  const issues = clone(state.issues);
  const existingIndex = issues.findIndex((item) => item.cue_ids?.length === range.length
    && item.cue_ids.every((id, index) => id === range[index]));
  const removing = issue.enabled === false || issue.active === false || opts.enabled === false;
  if (existingIndex < 0 && removing) return state;
  if (existingIndex < 0) {
    issues.push({
      cue_ids: range,
      authors: values.authors,
      categories: values.hasCategories ? values.categories : ["other"],
      notes: values.notes
    });
  } else if (removing) {
    const existing = issues[existingIndex];
    const categories = values.hasCategories ? existing.categories.filter((category) => !values.categories.includes(category)) : [];
    const authors = values.authors.length ? existing.authors.filter((author) => !values.authors.includes(author)) : [];
    const removedAuthorIndexes = new Set(existing.authors
      .map((author, index) => values.authors.includes(author) ? index : -1)
      .filter((index) => index >= 0));
    const notes = values.notes.length
      ? existing.notes.filter((note) => !values.notes.includes(note))
      : existing.notes.filter((note, index) => !removedAuthorIndexes.has(index));
    if (!categories.length) issues.splice(existingIndex, 1);
    else issues[existingIndex] = { ...existing, authors, categories, notes };
  } else {
    const existing = issues[existingIndex];
    issues[existingIndex] = {
      cue_ids: range,
      authors: [...new Set([...existing.authors, ...values.authors])],
      categories: [...new Set([...existing.categories, ...values.categories])],
      notes: [...new Set([...existing.notes, ...values.notes])]
    };
  }
  return commit(state, { issues }, opts, "toggle_issue_range", false);
}

export function searchReplace(state, search, replacement, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const needle = String(search ?? "");
  const value = String(replacement ?? "");
  if (!needle) throw new Error("search text must not be empty");
  const document = clone(state.document);
  let changed = false;
  for (const cue of document.cues) {
    const current = displayText(cue);
    if (!current.includes(needle)) continue;
    setDisplayText(cue, current.split(needle).join(value));
    if (speechIsLinked(cue)) cue.speech_text = displayText(cue);
    changed = true;
  }
  if (!changed) return state;
  recomputeBlocks(document);
  return commit(state, { document }, opts, "search_replace");
}

function restoreSnapshot(state, snapshotValue, history, redo) {
  return {
    ...state,
    document: clone(snapshotValue.document),
    issues: clone(snapshotValue.issues),
    directEditCount: snapshotValue.directEditCount,
    version: state.version + 1,
    history,
    redo
  };
}

function historyAuthor(state, options) {
  return authorOf(state, options);
}

export function undoDraftOperation(state, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const entry = state.history.at(-1);
  if (!entry) return state;
  if (entry.authorId !== historyAuthor(state, opts)) throw new Error("undo is limited to your own operation");
  return restoreSnapshot(
    state,
    entry.before,
    state.history.slice(0, -1),
    [...state.redo, entry]
  );
}

export function redoDraftOperation(state, options = {}) {
  const opts = optionsOf(options);
  assertVersion(state, opts);
  const entry = state.redo.at(-1);
  if (!entry) return state;
  if (entry.authorId !== historyAuthor(state, opts)) throw new Error("redo is limited to your own operation");
  return restoreSnapshot(
    state,
    entry.after,
    [...state.history, entry].slice(-HISTORY_LIMIT),
    state.redo.slice(0, -1)
  );
}
