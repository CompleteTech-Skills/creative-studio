import * as THREE from "three";

export function mountCameraWorkbench({container, api, getSnapshot}) {
  if (!container) throw Error("Camera comparison container is unavailable.");
  const heading = document.createElement("h2");
  heading.textContent = "Camera comparison";
  const note = document.createElement("p");
  note.textContent = "Saved camera views · navigation does not edit shot cameras. Browser lighting and antialiasing are previews; authored DOF and camera animation are unavailable in this comparison.";
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  const selectors = [0, 1].map(index => {
    const label = document.createElement("label");
    label.textContent = index === 0 ? "Active comparison camera" : "Secondary comparison camera";
    const select = document.createElement("select");
    select.setAttribute("aria-label", label.textContent);
    label.append(select); container.append(label);
    return select;
  });
  const compare = document.createElement("button");
  compare.textContent = "Compare saved cameras";
  const guideLabel = document.createElement("label");
  guideLabel.textContent = "Safe area and thirds";
  const guides = document.createElement("input"); guides.type = "checkbox"; guides.checked = true;
  guides.setAttribute("aria-label", "Show comparison safe area and thirds"); guideLabel.append(guides);
  const outputs = document.createElement("div");
  outputs.style.cssText = "display:flex;flex-wrap:wrap;gap:8px";
  const panels = [0, 1].map(() => {
    const figure = document.createElement("figure"); figure.style.margin = "0";
    const frame = document.createElement("div"); frame.style.cssText = "position:relative;width:256px;height:144px";
    const canvas = document.createElement("canvas"); canvas.width = 256; canvas.height = 144;
    canvas.style.cssText = "width:256px;height:144px";
    const overlay = document.createElement("div"); overlay.style.cssText = "position:absolute;inset:0;pointer-events:none";
    overlay.setAttribute("aria-hidden", "true");
    const safe = document.createElement("div"); safe.style.cssText = "position:absolute;inset:10%;border:1px solid #fff8";
    overlay.append(safe);
    for (const position of [100/3, 200/3]) {
      for (const vertical of [true, false]) {
        const line = document.createElement("div");
        line.style.cssText = vertical ? `position:absolute;top:0;bottom:0;left:${position}%;border-left:1px solid #fff8` : `position:absolute;left:0;right:0;top:${position}%;border-top:1px solid #fff8`;
        overlay.append(line);
      }
    }
    const caption = document.createElement("figcaption");
    frame.append(canvas, overlay); figure.append(frame, caption); outputs.append(figure);
    return {figure, canvas, overlay, caption, renderer:null};
  });
  container.prepend(heading, note);
  container.append(compare, guideLabel, outputs, status);
  let epoch = 0, disposed = false;
  function updateGuides() { panels.forEach(panel => panel.overlay.hidden = !guides.checked); }
  guides.addEventListener("change", updateGuides);
  async function renderComparison() {
    const current = getSnapshot(), ticket = ++epoch;
    if (!current) return;
    const ids = [...new Set(selectors.map(select => select.value).filter(Boolean))];
    if (!ids.length) {status.textContent = "Add a native scene camera before comparing."; return;}
    compare.disabled = true;
    try {
      const result = await api("camera-workbench/evaluate", {expected_revision:current.revision,
        expected_sha256:current.sha256, camera_ids:ids, width:256, height:144});
      if (disposed || ticket !== epoch || getSnapshot()?.sha256 !== result.sha256) return;
      const editor = window.hatchEditor;
      const first = editor.objects.values().next().value;
      if (!first) throw Error("Native viewport geometry is unavailable.");
      let scene = first; while (scene.parent) scene = scene.parent;
      scene.updateMatrixWorld(true);
      for (let index=0; index<panels.length; index++) {
        const panel = panels[index], state = result.cameras[index];
        panel.figure.hidden = !state;
        if (!state) continue;
        if (!panel.renderer) {
          panel.renderer = new THREE.WebGLRenderer({canvas:panel.canvas, antialias:true, preserveDrawingBuffer:true});
          panel.renderer.setSize(256,144,false);
          panel.renderer.outputColorSpace = THREE.SRGBColorSpace;
        }
        const camera = new THREE.Camera();
        camera.matrixAutoUpdate = false;
        camera.matrix.set(...state.world_matrix.flat());
        camera.matrixWorld.copy(camera.matrix);
        camera.matrixWorldInverse.set(...state.view_matrix.flat());
        camera.projectionMatrix.set(...state.projection_matrix.flat());
        camera.projectionMatrixInverse.copy(camera.projectionMatrix).invert();
        panel.renderer.render(scene,camera);
        panel.caption.textContent = `${state.name} · ${state.projection.type} · saved base pose · DOF unavailable`;
        panel.canvas.setAttribute("aria-label", `Saved ${state.name} camera preview`);
        panel.canvas.dataset.cameraId = state.id;
        panel.canvas.dataset.snapshotSha256 = result.sha256;
        panel.canvas.dataset.projectionMatrix = JSON.stringify(state.projection_matrix);
      }
      updateGuides();
      status.textContent = `Revision ${result.revision}: ${result.cameras.length} saved camera view(s).`;
    } catch (error) { if (ticket === epoch) status.textContent = error.message; }
    finally { if (ticket === epoch) compare.disabled = false; }
  }
  compare.addEventListener("click", renderComparison);
  return {
    refresh(value) {
      ++epoch; compare.disabled = false;
      const cameras = value.scene.nodes.filter(node => node.type === "camera");
      selectors.forEach((select,index) => {
        const previous = select.value;
        select.replaceChildren(...cameras.map(camera => {
          const option = document.createElement("option"); option.value = camera.id; option.textContent = camera.name; return option;
        }));
        select.value = cameras.some(camera => camera.id === previous) ? previous : (cameras[index]?.id || cameras[0]?.id || "");
      });
      panels.forEach(panel => panel.figure.hidden = true);
      status.textContent = cameras.length ? "Choose saved cameras and compare." : "No native scene cameras.";
    },
    dispose() { disposed = true; ++epoch; panels.forEach(panel => panel.renderer?.dispose()); }
  };
}
