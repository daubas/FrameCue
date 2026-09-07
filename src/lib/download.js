export function downloadText(name, type, text) {
  const blob = new Blob([text], { type });
  const href = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = href;
  link.download = name;
  link.click();
  URL.revokeObjectURL(href);
}

export function resultFileName(packageData) {
  return `${packageData.review_id}_${packageData.revision}_framecue_review_result.json`;
}

export function resultBundleFileName(results) {
  const revisions = [...new Set(results.map((result) => result.revision))];
  return `framecue_${revisions.length === 1 ? revisions[0] : "mixed"}_all_review_results.json`;
}
