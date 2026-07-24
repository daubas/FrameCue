import { draftKey } from "./review.js";

export function loadDraft(packageData) {
  try {
    return JSON.parse(localStorage.getItem(draftKey(packageData)) || "null");
  } catch {
    return null;
  }
}

export function saveDraft(packageData, draft) {
  localStorage.setItem(draftKey(packageData), JSON.stringify(draft));
}

export function removeDraft(packageData) {
  localStorage.removeItem(draftKey(packageData));
}
