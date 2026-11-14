/* Director extensions use the existing saved-scene action guard and HTTP API. */
export function mountDirectorDashboard(context) {
  const { container } = context;
  if (!container) throw Error("Director panel container is missing.");
  container.hidden = false;
  const sheet = document.createElement("link");
  sheet.rel = "stylesheet";
  sheet.href = "/editor/director-dashboard.css";
  document.head.append(sheet);
  let state = null, snapshot = null, pending = false, mediaURL = null;
  const element = (tag, text, name) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (name) node.id = "director-" + name;
    return node;
  };
  const title = element("h2", "Director production");
  const notice = element("p", "Load the saved project to inspect production evidence.", "notice");
  notice.setAttribute("role", "status");
  const error = element("p", "", "error");
  error.setAttribute("role", "alert");
  const refresh = element("button", "Refresh production", "refresh");
  const qa = element("button", "Run structural QA", "qa");
  const milestones = element("div", undefined, "milestones");
  const sections = element("div", undefined, "sections");
  const qaResult = element("pre", "No QA has been executed in this session.", "qa-result");
  const previewSelect = element("select", undefined, "preview-select");
  previewSelect.setAttribute("aria-label", "Exact preview artifact");
  const previewButton = element("button", "Preview exact artifact", "preview");
  const preview = element("div", undefined, "preview-output");
  const dimensions = element("select", undefined, "dimension");
  dimensions.setAttribute("aria-label", "Review scope");
  const reviewer = element("input", undefined, "reviewer");
  reviewer.placeholder = "Actual reviewer ID";
  reviewer.setAttribute("aria-label", "Actual reviewer ID");
  reviewer.maxLength = 128;
  const authority = element("select", undefined, "authority");
  authority.setAttribute("aria-label", "Review authority");
  for (const kind of ["human", "assistant"]) authority.append(new Option(kind, kind));
  const verdict = element("select", undefined, "verdict");
  verdict.setAttribute("aria-label", "Inspected decision");
  verdict.append(new Option("Needs correction", "rejected"), new Option("Approved for this scope", "approved"));
  const notes = element("textarea", undefined, "notes");
  notes.placeholder = "What you actually inspected, the observation, and any uncertainty";
  notes.setAttribute("aria-label", "Actual inspection observations");
  notes.maxLength = 4096;
  const inspectedLabel = element("label");
  const inspected = element("input", undefined, "inspected");
  inspected.type = "checkbox";
  inspectedLabel.append(inspected, document.createTextNode(" I inspected the exact evidence for this scope."));
  const review = element("button", "Record scoped review", "review");
  const reviewState = element("pre", "No visual review is implied by structural QA.", "review-state");
  const method = element("select", undefined, "method");
  method.setAttribute("aria-label", "Configured native operation");
  const request = element("textarea", undefined, "request");
  request.placeholder = "Prepared request from the selected native owner";
  request.setAttribute("aria-label", "Prepared native request");
  request.maxLength = 1024 * 1024;
  const execute = element("button", "Execute configured action", "execute");
  const actionResult = element("pre", "Only installed executable owners are available.", "action-result");
  const details = (label, children) => {
    const node = element("details");
    node.append(element("summary", label), ...children);
    return node;
  };
  container.append(title, notice, error, refresh, qa, milestones, sections,
    details("QA results", [qaResult]), details("Exact frame and movie previews", [previewSelect, previewButton, preview]),
    details("Evidence-bound reviews", [dimensions, reviewer, authority, verdict, notes, inspectedLabel, review, reviewState]),
    details("Configured native actions", [method, request, execute, actionResult]));
  const guarded = typeof context.runGuardedAction === "function";
  const controls = () => {
    refresh.disabled = pending || !snapshot || !guarded;
    qa.disabled = pending || !state || !guarded;
    previewButton.disabled = pending || !previewSelect.value || !guarded;
    review.disabled = pending || !dimensions.value || !guarded;
    execute.disabled = pending || !method.value || !guarded;
  };
  async function action(work) {
    if (!guarded) throw Error("Director actions are unavailable in this editor build.");
    if (pending) return;
    pending = true; error.textContent = ""; controls();
    try {
      return await context.runGuardedAction(async scope => {
        const native = { native_revision: scope.snapshot.revision, native_sha256: scope.snapshot.sha256 };
        return await work(scope, native);
      });
    } catch (failure) {
      error.textContent = failure?.message || String(failure);
    } finally { pending = false; controls(); }
  }
  function render(value) {
    state = value;
    container.dataset.status = value.production.status;
    notice.textContent = "Saved scene revision " + value.native.revision + ". Milestones and reviews are independent; no artistic approval is inferred.";
    milestones.replaceChildren();
    for (const row of value.production.milestones) {
      const node = element("div");
      node.className = "director-milestone";
      node.dataset.milestone = row.name;
      node.append(element("strong", row.name.replaceAll("_", " ")), element("span", row.status),
        element("small", (row.remaining_work || []).join(" ")));
      milestones.append(node);
    }
    sections.replaceChildren();
    for (const [name, data] of Object.entries(value.documents)) {
      const block = element("pre", JSON.stringify(data, null, 2));
      sections.append(details(name.replaceAll("_", " "), [block]));
    }
    sections.append(details("Native scene objects", [element("pre", JSON.stringify(value.native_nodes, null, 2))]),
      details("Revision history", [element("pre", JSON.stringify(value.revisions, null, 2))]));
    const refs = [...value.inventory, ...value.exports, ...Object.values(value.review_dependencies).flat()];
    const previewRefs = new Map();
    for (const ref of refs) if (/^(image\/(png|jpeg)|video\/(mp4|webm))$/.test(ref.media_type)) previewRefs.set(ref.artifact_id, ref);
    previewSelect.replaceChildren(...[...previewRefs.values()].map(ref => new Option(ref.artifact_id + " · " + ref.sha256.slice(0, 12), ref.artifact_id)));
    dimensions.replaceChildren(...Object.keys(value.review_dependencies).map(name => new Option(name, name)));
    reviewState.textContent = JSON.stringify(value.reviews, null, 2);
    method.replaceChildren(...Object.entries(value.operations).filter(([name, item]) => item.available && !name.startsWith("QAService.")).map(([name, item]) =>
      new Option(name + " · " + item.context_owner, name)));
    if (!method.options.length) method.append(new Option("No additional native owner configured", ""));
    controls();
  }
  refresh.onclick = () => action(async (scope, native) => render(await scope.api("director/status", native)));
  qa.onclick = () => action(async (scope, native) => {
    const result = await scope.api("director/qa", native);
    qaResult.textContent = JSON.stringify(result, null, 2);
    qaResult.dataset.status = result.result.status;
    render(await scope.api("director/status", native));
  });
  previewButton.onclick = () => action(async (scope, native) => {
    const data = await scope.api("director/preview", { ...native, artifact_id: previewSelect.value });
    const raw = Uint8Array.from(atob(data.data_base64), char => char.charCodeAt(0));
    if (mediaURL) URL.revokeObjectURL(mediaURL);
    mediaURL = URL.createObjectURL(new Blob([raw], { type: data.artifact.media_type }));
    const media = document.createElement(data.artifact.media_type.startsWith("video/") ? "video" : "img");
    if (media.tagName === "VIDEO") { media.controls = true; media.preload = "metadata"; }
    else media.alt = "Exact artifact " + data.artifact.artifact_id;
    media.src = mediaURL;
    media.dataset.sha256 = data.artifact.sha256;
    preview.replaceChildren(media, element("p", "Previewing exact bytes does not record an inspection or approval."));
  });
  review.onclick = () => action(async (scope, native) => {
    const result = await scope.api("director/review-form", { ...native, reviewer_id: reviewer.value,
      authority: authority.value, dimension: dimensions.value, verdict: verdict.value, notes: notes.value,
      inspected: inspected.checked, transaction_id: "review-" + crypto.randomUUID() });
    reviewState.textContent = JSON.stringify(result.review_status, null, 2);
    inspected.checked = false;
    render(await scope.api("director/status", native));
  });
  execute.onclick = () => action(async (scope, native) => {
    const prepared = JSON.parse(request.value);
    if (prepared.method !== method.value) throw Error("The prepared request does not match the selected native owner.");
    const result = await scope.api("director/action", { ...native, request: prepared });
    actionResult.textContent = JSON.stringify(result, null, 2);
    await scope.reload();
    const current = context.getSnapshot();
    render(await scope.api("director/status", { native_revision: current.revision, native_sha256: current.sha256 }));
  });
  for (const node of [previewSelect, dimensions, method]) node.onchange = controls;
  controls();
  return {
    refresh(value) { snapshot = value; controls(); },
    dispose() { if (mediaURL) URL.revokeObjectURL(mediaURL); sheet.remove(); container.replaceChildren(); },
  };
}
