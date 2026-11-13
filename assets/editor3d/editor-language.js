/* Review an exact saved-scene plan before the canonical native transaction. */
export function mountSceneLanguage(context) {
  const { container, getSnapshot } = context;
  if (!container) throw Error('Scene language needs a dedicated container.');
  const title = document.createElement('h2');
  title.textContent = 'Scene language';
  const label = document.createElement('label');
  label.textContent = 'Supported scene instructions';
  const input = document.createElement('textarea');
  input.maxLength = 16384;
  input.rows = 4;
  input.setAttribute('aria-label', 'Supported scene instructions');
  label.append(input);
  const modeLabel = document.createElement('label');
  const mode = document.createElement('input');
  mode.type = 'checkbox';
  mode.setAttribute('aria-label', 'Environment template instructions');
  modeLabel.append(mode, document.createTextNode('Environment templates'));
  const planButton = document.createElement('button');
  planButton.textContent = 'Review language plan';
  const applyButton = document.createElement('button');
  applyButton.textContent = 'Apply reviewed language plan';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const report = document.createElement('pre');
  report.setAttribute('aria-label', 'Language plan details');
  container.append(title, label, modeLabel, planButton, applyButton, status, report);
  let plan = null;
  let busy = false;
  let disposed = false;
  let generation = 0;
  const identity = snapshot => `${snapshot?.revision}:${snapshot?.sha256}`;
  const guarded = typeof context.runGuardedAction === 'function';
  const update = () => {
    planButton.disabled = busy || !guarded || !getSnapshot();
    applyButton.disabled = busy || !guarded || !plan || plan.scene_plan?.status !== 'ready';
  };
  if (!guarded) status.textContent = 'Language editing awaits the shared editor action guard.';
  function invalidate() {
    generation += 1;
    plan = null;
    report.textContent = '';
    update();
  }
  input.addEventListener('input', invalidate);
  mode.addEventListener('change', invalidate);
  async function action(callback) {
    if (busy || disposed || !guarded) return;
    busy = true;
    update();
    try {
      await context.runGuardedAction(callback);
    } catch (error) {
      if (!disposed) status.textContent = String(error?.message || error);
    } finally {
      busy = false;
      if (!disposed) update();
    }
  }
  planButton.addEventListener('click', () => action(async ({snapshot, api: guardedApi, isCurrent}) => {
    const source = identity(snapshot);
    const request = ++generation;
    plan = null;
    report.textContent = '';
    status.textContent = 'Planning against the saved scene…';
    const result = await guardedApi('language/plan', { text: input.value, environment: mode.checked,
      expected_revision: snapshot.revision, expected_sha256: snapshot.sha256 });
    if (disposed || !isCurrent() || request !== generation || source !== identity(getSnapshot())) return;
    if (source !== identity({revision: result.expected_revision, sha256: result.expected_sha256})) {
      throw Error('The plan belongs to a different saved scene. Review a new plan.');
    }
    plan = result;
    report.textContent = JSON.stringify(result.scene_plan, null, 2);
    status.textContent = result.scene_plan?.status === 'ready'
      ? 'Review the commands before applying. Original sources remain preserved.'
      : 'Resolve the reported instructions before applying.';
  }));
  applyButton.addEventListener('click', () => action(async ({snapshot, api: guardedApi, reload: guardedReload}) => {
    const selected = plan;
    if (!selected || selected.scene_plan?.status !== 'ready') return;
    if (identity(snapshot) !== identity({revision: selected.expected_revision, sha256: selected.expected_sha256})) {
      invalidate();
      throw Error('The saved scene changed. Review a new language plan.');
    }
    status.textContent = 'Applying the reviewed language plan…';
    await guardedApi('language/apply', {plan: selected, transaction_id: `language-${crypto.randomUUID()}`});
    invalidate();
    await guardedReload();
    status.textContent = 'Language edit saved. Review the updated scene.';
  }));
  update();
  return {
    refresh(snapshot) {
      if (plan && identity(snapshot) !== identity({revision: plan.expected_revision, sha256: plan.expected_sha256})) {
        invalidate();
        status.textContent = 'The saved scene changed. Review a new language plan.';
      }
      update();
    },
    dispose() {
      disposed = true;
      generation += 1;
      container.replaceChildren();
    },
  };
}
