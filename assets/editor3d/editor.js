import * as THREE from "three";
import { OrbitControls } from "/vendor/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "/vendor/examples/jsm/loaders/GLTFLoader.js";
import { TransformControls } from "./TransformControls.js";
const $ = (id) => document.getElementById(id),
  token = location.hash.slice(1);
// The bearer token lives in memory only, never in a query, asset URL or saved document.
history.replaceState(null, "", location.pathname);
const world = new THREE.Scene(),
  content = new THREE.Group(),
  overlays = new THREE.Group();
world.add(content, overlays);
let renderer,
  orbit,
  gizmo,
  camera,
  doc,
  revision,
  sha256,
  selection = new Set(),
  objects = new Map(),
  selectedBone = null,
  nodes = new Map(),
  boneObjects = new Map(),
  busy = false,
  drag = null,
  bookmark = null,
  copied = null,
  disposed = false,
  lost = false,
  mixers = [],
  playing = false,
  animationTime = 0,
  clipDuration = 0,
  loadEpoch = 0,
  currentSnapshot = null,
  previewActive = false;
const collapsed = new Set();
const extensions = [];
let evaluatedStates = new Map(), savedNativeState = null, nativeTransformReadonly = {};
let activeNativeCameraId = null, focusedOutlinerKey = null;
const editorNode = Symbol("native-scene-node");
const errors = [],
  nativeMaterials = new Map(),
  helpers = [],
  selectionHelpers = [],
  normalMaterial = new THREE.MeshNormalMaterial(),
  solidMaterial = new THREE.MeshStandardMaterial({
    color: 0x9ab5c5,
    roughness: 0.8,
  });
const perspective = new THREE.PerspectiveCamera(45, 1, 0.01, 10000),
  orthographic = new THREE.OrthographicCamera(-5, 5, 5, -5, 0.01, 10000);
camera = perspective;
camera.position.set(6, 4, 7);
const grid = new THREE.GridHelper(20, 40, 0x527587, 0x293e4b),
  axes = new THREE.AxesHelper(1);
overlays.add(grid, axes);
world.add(new THREE.HemisphereLight(0xdcefff, 0x273040, 1.4));
const key = new THREE.DirectionalLight(0xffffff, 2);
key.position.set(5, 8, 6);
world.add(key);
const overlayState = {
  grid: true,
  axes: true,
  bounds: true,
  bones: false,
  cameras: false,
  lights: false,
  safe: false,
  thirds: false,
};
const MAX_NATIVE_WARNINGS = 64;
let transientDiagnostic = null;
let nativeDiagnostics = {epoch: 0, revision: null, sha256: null, records: new Map(), truncated: false};
function renderDiagnostics() {
  const container = $("diagnostic"), content = document.createDocumentFragment();
  if (transientDiagnostic !== null) {
    const error = document.createElement("p"); error.textContent = transientDiagnostic; content.append(error);
  }
  for (const record of nativeDiagnostics.records.values()) {
    const warning = document.createElement("p");
    warning.dataset.code = record.code; warning.dataset.nodeId = record.nodeId;
    warning.textContent = "Warning " + record.code + " — " + record.nodeName + " (" + record.nodeId + "): " + record.message;
    content.append(warning);
  }
  if (nativeDiagnostics.truncated) {
    const warning = document.createElement("p"); warning.dataset.code = "EDITOR_VIEW_DIAGNOSTIC_LIMIT";
    warning.textContent = "Additional preview limitations are omitted. Inspect individual scene objects for their native configuration.";
    content.append(warning);
  }
  container.replaceChildren(content);
  container.hidden = transientDiagnostic === null && !nativeDiagnostics.records.size && !nativeDiagnostics.truncated;
  container.setAttribute("role", transientDiagnostic === null ? "status" : "alert");
  container.setAttribute("aria-live", transientDiagnostic === null ? "polite" : "assertive");
  container.dataset.revision = String(nativeDiagnostics.revision);
  container.dataset.sha256 = nativeDiagnostics.sha256 || "";
}
function beginNativeDiagnostics(snapshot, epoch) {
  nativeDiagnostics = {epoch, revision: snapshot.revision, sha256: snapshot.sha256, records: new Map(), truncated: false};
  renderDiagnostics();
}
function nativeWarning(code, node, message, epoch) {
  if (epoch !== loadEpoch || nativeDiagnostics.epoch !== epoch) return;
  const key = code + ":" + node.id;
  if (nativeDiagnostics.records.has(key)) return;
  const text = String(message);
  errors.push(text);
  if (nativeDiagnostics.records.size < MAX_NATIVE_WARNINGS) {
    nativeDiagnostics.records.set(key, {code, nodeId: node.id, nodeName: node.name.slice(0, 128),
      message: text.length > 2048 ? text.slice(0, 2048) + "…" : text});
  } else {
    if (nativeDiagnostics.truncated) return;
    nativeDiagnostics.truncated = true;
  }
  renderDiagnostics();
  if (transientDiagnostic === null) $("status").textContent = "Preview has limitations; saved state preserved.";
}
function diagnostic(error) {
  const text = error?.message || String(error);
  errors.push(text);
  transientDiagnostic = text;
  renderDiagnostics();
  $("status").textContent = "Operation rejected; saved state preserved.";
}
function clearDiagnostic() {
  transientDiagnostic = null;
  renderDiagnostics();
}
async function api(path, body) {
  const response = await fetch("/api/" + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: "Bearer " + token,
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  let result;
  try {
    result = await response.json();
  } catch {
    throw Error("Invalid local service response.");
  }
  if (!response.ok)
    throw Error(result.error?.message || result.message || "Request rejected.");
  return result;
}
async function upload(file) {
  if (file.size <= 0 || file.size > 64 * 1024 * 1024)
    throw Error("Choose a file between 1 byte and 64 MiB.");
  const response = await fetch("/api/uploads", {
    method: "POST",
    headers: {
      Authorization: "Bearer " + token,
      "Content-Type": "application/octet-stream",
      "X-Filename": encodeURIComponent(file.name),
    },
    body: file,
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error?.message || "Upload rejected.");
  return result;
}
function nodeLocked(id) { return evaluatedStates.get(id)?.locked !== false; }
function nodeSelectable(id) { return evaluatedStates.get(id)?.selectable === true; }
function transformReason(id) {
  if (nodeLocked(id)) return "Object is locked by native scene state.";
  if (!nodeSelectable(id)) return "Object is not selectable in native scene state.";
  return Object.hasOwn(nativeTransformReadonly, id) ? nativeTransformReadonly[id] : null;
}
function transformSelectionAllowed() { return roots().length > 0 && roots().every(id => !transformReason(id)); }
function applyNativeState(value) {
  const state = Array.isArray(value) ? new Map(value.map(node => [node.id, node])) : new Map(Object.entries(value));
  if (state.size !== nodes.size || [...nodes.keys()].some(id => !state.has(id)))
    throw Error("The evaluated native graph has no exact viewport node mapping.");
  const matrices = new Map();
  for (const [id, item] of state) {
    if (!Array.isArray(item.world_matrix) || item.world_matrix.length !== 4 ||
        item.world_matrix.some(row => !Array.isArray(row) || row.length !== 4 || row.some(number => !Number.isFinite(number))) ||
        ["editor_visible", "locked", "selectable", "cast_shadow", "receive_shadow"].some(field => typeof item[field] !== "boolean"))
      throw Error("The native viewport state is incomplete for " + id);
    matrices.set(id, new THREE.Matrix4().set(...item.world_matrix.flat()));
  }
  content.updateWorldMatrix(true, false);
  const local = new Map();
  for (const [id, object] of objects) {
    const parentId = object.parent?.[editorNode], parent = parentId ? matrices.get(parentId) : object.parent.matrixWorld;
    if (!parent || parent.determinant() === 0) throw Error("The native parent matrix cannot be represented in this viewport.");
    local.set(id, parent.clone().invert().multiply(matrices.get(id)));
  }
  evaluatedStates = state;
  for (const [id, object] of objects) {
    object.matrixAutoUpdate = false;
    object.matrix.copy(local.get(id));
    object.matrix.decompose(object.position, object.quaternion, object.scale);
    object.visible = state.get(id).editor_visible;
    function visit(child) {
      if (child !== object && child[editorNode]) return;
      if (child.isMesh) {
        child.castShadow = state.get(id).cast_shadow;
        child.receiveShadow = state.get(id).receive_shadow;
      }
      child.children.forEach(visit);
    }
    visit(object);
  }
  content.updateMatrixWorld(true);
}
function preview(scene) {
  playing = false;
  gizmo.detach();
  applyNativeState(scene.nodes);
  previewActive = true;
  refreshSelection();
}
function clearPreview() {
  applyNativeState(savedNativeState.nodes);
  previewActive = false;
  refreshSelection();
}
function roots(ids = selection) {
  return [...ids].filter((id) => {
    let parent = nodes.get(id)?.parent;
    while (parent) {
      if (ids.has(parent)) return false;
      parent = nodes.get(parent)?.parent;
    }
    return true;
  });
}
function localTransform(object) {
  return {
    translation: object.position.toArray(),
    rotation: object.quaternion.normalize().toArray(),
    scale: object.scale.toArray(),
  };
}
function setTransform(object, value) {
  object.position.fromArray(value.translation);
  object.quaternion.fromArray(value.rotation);
  object.scale.fromArray(value.scale);
  object.updateMatrix();
}
function nativeCommands(commands) {
  return commands.flatMap((command) => {
    if (command.operation) return [command];
    const identifier = command.id;
    if (command.op === "set-transform")
      return [
        {
          operation: "transform",
          parameters: {
            identifier,
            transform: command.transform,
            space: "local",
          },
        },
      ];
    if (command.op === "reparent")
      return [
        {
          operation: "reparent",
          parameters: {
            identifier,
            parent: command.parent,
            preserve_world: command.keep_world !== false,
          },
        },
      ];
    if (command.op === "set-node") {
      const patch = command.patch,
        result = [],
        flags = { identifier };
      for (const field of ["visible", "selectable", "locked"])
        if (field in patch) flags[field] = patch[field];
      if (patch.render)
        for (const [source, target] of [
          ["visible", "render_visible"],
          ["cast_shadow", "cast_shadow"],
          ["receive_shadow", "receive_shadow"],
        ])
          if (source in patch.render) flags[target] = patch.render[source];
      if (Object.keys(flags).length > 1)
        result.push({ operation: "node-flags", parameters: flags });
      if ("name" in patch)
        result.push({
          operation: "rename",
          parameters: { identifier, name: patch.name },
        });
      return result;
    }
    if (command.op === "set-collection")
      return [
        {
          operation: "collection",
          parameters: { identifier, visible: command.visible },
        },
      ];
    throw Error("Unsupported editor command.");
  });
}
function setBusy(value) {
  busy = value;
  inspect();
  attach();
  $("undo").disabled = busy || !currentSnapshot?.undo_available;
  $("redo").disabled = busy || !currentSnapshot?.redo_available;
  document.querySelectorAll("#group,#ungroup,#duplicate,#delete,#show-all,#collections input")
    .forEach((control) => { control.disabled = busy; });
  document.querySelectorAll("#outliner [data-id]")
    .forEach((control) => { control.draggable = !busy && !nodeLocked(control.dataset.id); });
}
function rejectPendingEdit() {
  if (!busy) return false;
  $("status").textContent = "Save in progress; retry this edit when saving finishes.";
  return true;
}
async function commit(commands) {
  if (rejectPendingEdit() || !commands.length) return;
  setBusy(true);
  gizmo?.detach();
  $("status").textContent = "Saving command…";
  try {
    const batch = nativeCommands(commands);
    for (const command of batch) {
      if (command.operation === "transform" && transformReason(command.parameters.identifier))
        throw Error(transformReason(command.parameters.identifier));
    }
    const result = await api("commands", {
      expected_revision: revision,
      expected_sha256: sha256,
      transaction_id: crypto.randomUUID(),
      commands: { schema_version: 1, commands: batch },
    });
    const displayed = await install(result);
    if (displayed === false) return;
    clearDiagnostic();
    $("status").textContent = "Saved revision " + revision;
  } catch (error) {
    diagnostic(error);
    await reload(false);
  } finally {
    setBusy(false);
  }
}
function select(id, add = false, toggle = false) {
  selectedBone = null;
  if (!add && !toggle) selection.clear();
  if (toggle && selection.has(id)) selection.delete(id);
  else if (objects.has(id) && nodeSelectable(id))
    selection.add(id);
  refreshSelection();
}
function selectBone(id) {
  const bone = boneObjects.get(id);
  if (!bone || !nodeSelectable(bone.userData.ownerId)) return;
  select(bone.userData.ownerId);
  selectedBone = id;
  refreshSelection();
}
function boneSelection() {
  const bone = boneObjects.get(selectedBone);
  return bone
    ? {
        owner_id: bone.userData.ownerId,
        bone_name: bone.name,
        transform: localTransform(bone),
      }
    : null;
}
function refreshSelection() {
  selection = new Set([...selection].filter(id => objects.has(id) && nodeSelectable(id)));
  for (const row of $("outliner").querySelectorAll("[data-bone]"))
    row.setAttribute(
      "aria-selected",
      String(row.dataset.bone === selectedBone),
    );
  for (const extension of extensions)
    extension.refresh?.(currentSnapshot, [...selection]);
  for (const button of $("outliner").querySelectorAll("[data-id]"))
    button.setAttribute(
      "aria-selected",
      String(selection.has(button.dataset.id)),
    );
  for (const h of selectionHelpers) {
    overlays.remove(h);
    h.dispose();
  }
  selectionHelpers.length = 0;
  for (const id of selection) {
    const obj = objects.get(id);
    if (!obj) continue;
    const h = new THREE.BoxHelper(obj, 0x66ddff);
    h.visible = overlayState.bounds;
    overlays.add(h);
    selectionHelpers.push(h);
  }
  attach();
  inspect();
}
function attach() {
  if (!gizmo || busy || lost || previewActive || selectedBone || !transformSelectionAllowed()) {
    gizmo?.detach();
    return;
  }
  const id = roots().find((id) => !transformReason(id)),
    object = objects.get(id);
  if (object) {
    for (const identifier of roots()) objects.get(identifier).matrixAutoUpdate = true;
    gizmo.attach(object);
    gizmo.enabled = true;
  } else {
    gizmo.detach();
    gizmo.enabled = false;
  }
}
function disposeContent() {
  gizmo?.detach();
  mixers.forEach(({ mixer }) => mixer.stopAllAction());
  mixers = [];
  nativeMaterials.clear();
  boneObjects.clear();
  objects.clear();
  nodes.clear();
  for (const helper of [...helpers, ...selectionHelpers]) {
    overlays.remove(helper);
    helper.dispose?.();
  }
  helpers.length = 0;
  selectionHelpers.length = 0;
  const geometries = new Set(),
    materials = new Set(),
    textures = new Set();
  content.traverse((object) => {
    if (object.geometry) geometries.add(object.geometry);
    for (const m of Array.isArray(object.material)
      ? object.material
      : [object.material])
      if (m) {
        materials.add(m);
        for (const value of Object.values(m))
          if (value?.isTexture) textures.add(value);
      }
  });
  content.clear();
  geometries.forEach((x) => x.dispose());
  materials.forEach((x) => x.dispose());
  textures.forEach((x) => x.dispose());
}
function materialFromNative(value, images) {
  const pbr = value.pbrMetallicRoughness || {},
    rgba = pbr.baseColorFactor || [0.7, 0.75, 0.8, 1],
    m = new THREE.MeshStandardMaterial({
      color: new THREE.Color(...rgba.slice(0, 3)),
      opacity: rgba[3],
      transparent: value.alphaMode === "BLEND",
      alphaTest: value.alphaMode === "MASK" ? (value.alphaCutoff ?? 0.5) : 0,
      side: value.doubleSided ? THREE.DoubleSide : THREE.FrontSide,
      metalness: pbr.metallicFactor ?? 0,
      roughness: pbr.roughnessFactor ?? 0.7,
    });
  const image = images?.[pbr.baseColorTexture?.index];
  if (image) {
    const texture = new THREE.TextureLoader().load(image);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.flipY = false;
    m.map = texture;
  }
  return m;
}
async function assetObject(asset) {
  if (asset.format === "glb") {
    const bytes = Uint8Array.from(atob(asset.data), (c) => c.charCodeAt(0));
    const manager = new THREE.LoadingManager();
    manager.setURLModifier((url) => {
      if (!url.startsWith("data:") && !url.startsWith("blob:"))
        throw Error("External GLB resource refused.");
      return url;
    });
    const gltf = await new GLTFLoader(manager).parseAsync(bytes.buffer, "");
    if (gltf.animations.length) {
      const mixer = new THREE.AnimationMixer(gltf.scene);
      mixers.push({ mixer, clips: gltf.animations, root: gltf.scene });
    }
    return gltf.scene;
  }
  if (asset.format === "geometry") {
    const group = new THREE.Group();
    for (const mesh of asset.meshes) {
      const geometry = new THREE.BufferGeometry();
      geometry.setAttribute(
        "position",
        new THREE.Float32BufferAttribute(mesh.positions.flat(), 3),
      );
      geometry.setIndex(mesh.triangles.flat());
      if (mesh.normals)
        geometry.setAttribute(
          "normal",
          new THREE.Float32BufferAttribute(mesh.normals.flat(), 3),
        );
      else geometry.computeVertexNormals();
      if (mesh.uv)
        geometry.setAttribute(
          "uv",
          new THREE.Float32BufferAttribute(mesh.uv.flat(), 2),
        );
      const object = new THREE.Mesh(
        geometry,
        materialFromNative(asset.materials[mesh.material] || {}, asset.images),
      );
      object.name = mesh.name || mesh.id;
      object.userData.nativeMeshId = mesh.id;
      group.add(object);
    }
    return group;
  }
  throw Error(asset.message || "Unsupported geometry format.");
}
async function install(snapshot) {
  previewActive = false;
  currentSnapshot = snapshot;
  $("undo").disabled = busy || !snapshot.undo_available;
  $("redo").disabled = busy || !snapshot.redo_available;
  const epoch = ++loadEpoch,
    assets = snapshot.viewport_assets || {};
  beginNativeDiagnostics(snapshot, epoch);
  doc = snapshot.scene;
  revision = snapshot.revision;
  sha256 = snapshot.sha256;
  savedNativeState = snapshot.viewport_state;
  if (!savedNativeState || savedNativeState.revision !== revision || savedNativeState.sha256 !== sha256)
    throw Error("The repository supplied no exact native viewport evaluation.");
  nativeTransformReadonly = savedNativeState.transform_readonly || {};
  if (savedNativeState.available === false) {
    disposeContent(); evaluatedStates.clear(); selection.clear(); selectedBone = null;
    $("outliner").replaceChildren(); $("collections").replaceChildren();
    $("revision").textContent = "Revision " + revision + " · " + String(sha256).slice(0, 10);
    diagnostic(savedNativeState.error?.message || "Native viewport evaluation is unavailable.");
    $("status").textContent = "Saved revision " + revision + "; viewport unavailable.";
    window.editorReady = false;
    return false;
  }
  if (!doc?.nodes)
    throw Error("The repository supplied no native Scene3D scene.");
  disposeContent();
  $("revision").textContent =
    "Revision " + revision + " · " + String(sha256).slice(0, 10);
  world.background = new THREE.Color(
    ...(doc.world?.background || [0.05, 0.08, 0.12]),
  );
  renderer.toneMappingExposure = 2 ** (doc.world?.exposure || 0);
  for (const node of doc.nodes) {
    nodes.set(node.id, node);
    const object = new THREE.Group();
    object.name = node.name;
    object[editorNode] = node.id;
    objects.set(node.id, object);
    setTransform(object, node.transform);
    object.visible = node.visible;
    content.add(object);
    if (node.asset) {
      const reference = node.asset;
      const component = node.metadata?.source_component;
      let asset = component !== undefined ? snapshot.viewport_nodes?.[node.id] :
        assets[JSON.stringify([reference.id, reference.version, reference.path, reference.sha256])];
      if (component !== undefined && asset?.format !== "unavailable" &&
          (asset?.source_component !== component || !asset.asset_reference ||
           JSON.stringify([asset.asset_reference.id, asset.asset_reference.version, asset.asset_reference.path, asset.asset_reference.sha256]) !==
           JSON.stringify([reference.id, reference.version, reference.path, reference.sha256])))
        asset = {format: "unavailable", message: "No exact native component projection."};
      if (asset && asset.format !== "unavailable") {
        try {
          const actual = await assetObject(asset);
          if (epoch !== loadEpoch) {
            actual.traverse((o) => {
              o.geometry?.dispose();
            });
            return;
          }
          object.add(actual);
        } catch (error) {
          nativeWarning("EDITOR_VIEW_ASSET_UNAVAILABLE", node, "Asset " + node.asset.id + ": " + error.message, epoch);
        }
      } else
        nativeWarning(
          "EDITOR_VIEW_ASSET_UNAVAILABLE", node, "Asset " +
            node.asset.id +
            ": " +
            (asset?.message || "No verified viewport adapter."), epoch,
        );
    }
    if (node.type === "camera") {
      const c = node.camera,
        actual =
          c.projection === "orthographic"
            ? new THREE.OrthographicCamera(
                -c.orthographic_height / 2,
                c.orthographic_height / 2,
                c.orthographic_height / 2,
                -c.orthographic_height / 2,
                c.near,
                c.far,
              )
            : new THREE.PerspectiveCamera(
                THREE.MathUtils.radToDeg(c.vertical_fov),
                1,
                c.near,
                c.far,
              );
      object.add(actual);
      const helper = new THREE.CameraHelper(actual);
      helper.userData.overlay = "cameras";
      overlays.add(helper);
      helpers.push(helper);
    }
    if (node.type === "light") {
      const l = node.light,
        ctor = {
          point: THREE.PointLight,
          directional: THREE.DirectionalLight,
          spot: THREE.SpotLight,
        }[l.type];
      if (!ctor) {
        nativeWarning(
          "EDITOR_VIEW_LIGHT_UNAVAILABLE", node, "Area-light preview is unavailable: the native scene light has no area dimensions. No point-light substitute was rendered.", epoch,
        );
        continue;
      }
      const actual = new ctor(new THREE.Color(...l.color), l.intensity);
      actual.position.set(0, 0, 0);
      if (actual.target) {
        actual.target.position.set(0, 0, -1);
        object.add(actual.target);
      }
      object.add(actual);
      const helper = new THREE.PointLightHelper(actual, 0.1);
      helper.userData.overlay = "lights";
      overlays.add(helper);
      helpers.push(helper);
    }
  }
  for (const node of doc.nodes) {
    const object = objects.get(node.id);
    if (node.parent) objects.get(node.parent).add(object);
    function visit(child) {
      if (child !== object && child[editorNode]) return;
      if (child.isMesh) {
        nativeMaterials.set(child, child.material);
        child.castShadow = node.render.cast_shadow;
        child.receiveShadow = node.render.receive_shadow;
      }
      if (child.isBone) {
        const id = node.id + "::" + child.name;
        boneObjects.set(id, child);
        child.userData.ownerId = node.id;
      }
      child.children.forEach(visit);
    }
    visit(object);
  }
  applyNativeState(savedNativeState.nodes);
  if ($("view").value === "camera") setView();
  for (const [id, object] of objects) {
    if ([...boneObjects.values()].some((b) => b.userData.ownerId === id)) {
      const helper = new THREE.SkeletonHelper(object);
      helper.userData.overlay = "bones";
      overlays.add(helper);
      helpers.push(helper);
    }
  }
  selectedBone = null;
  selection = new Set([...selection].filter((id) => objects.has(id) && nodeSelectable(id)));
  buildOutliner();
  buildCollections();
  buildClips();
  shading();
  applyOverlays();
  refreshSelection();
  $("stats").textContent =
    doc.nodes.length +
    " nodes · " +
    [...nativeMaterials.keys()].reduce(
      (n, m) =>
        n +
        (m.geometry.index?.count || m.geometry.attributes.position.count) / 3,
      0,
    ) +
    " triangles";
  window.editorReady = true;
  if ($("diagnostic").hidden)
    $("status").textContent = "Loaded saved revision " + revision;
}
async function reload(frame = false) {
  const snapshot = await api("snapshot");
  const displayed = await install(snapshot);
  if (frame && displayed !== false) frameScene();
  return displayed;
}
function buildOutliner() {
  const root = $("outliner"), hadFocus = root.contains(document.activeElement);
  root.replaceChildren();
  const query = $("filter").value.toLowerCase();
  function add(node, depth) {
    if (
      !query ||
      node.name.toLowerCase().includes(query) ||
      node.type.includes(query)
    ) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.id = node.id;
      button.tabIndex = -1;
      button.dataset.hidden = String(!evaluatedStates.get(node.id).editor_visible);
      button.setAttribute("role", "treeitem");
      button.setAttribute("aria-level", String(depth + 1));
      button.setAttribute("aria-selected", String(selection.has(node.id)));
      button.style.paddingLeft = 8 + depth * 14 + "px";
      button.textContent =
        (nodeLocked(node.id) ? "🔒 " : evaluatedStates.get(node.id).editor_visible ? "" : "◌ ") +
        node.name +
        " · " +
        node.type;
      button.title = node.id;
      button.onclick = (e) =>
        select(node.id, e.shiftKey, e.ctrlKey || e.metaKey);
      button.draggable = !busy && !nodeLocked(node.id);
      button.setAttribute("aria-disabled", String(!nodeSelectable(node.id)));
      button.ondragstart = (e) => e.dataTransfer.setData("text/plain", node.id);
      button.ondragover = (e) => e.preventDefault();
      button.ondrop = (e) => {
        e.preventDefault();
        const id = e.dataTransfer.getData("text/plain");
        if (nodes.has(id) && id !== node.id)
          commit([{ op: "reparent", id, parent: node.id, keep_world: true }]);
      };
      root.append(button);
      if (overlayState.bones)
        for (const [boneId, bone] of boneObjects) {
          if (bone.userData.ownerId !== node.id) continue;
          const row = document.createElement("button");
          row.textContent = "Bone · " + bone.name;
          row.style.paddingLeft = 22 + depth * 14 + "px";
          row.dataset.bone = boneId;
          row.tabIndex = -1;
          row.setAttribute("aria-disabled", String(!nodeSelectable(node.id)));
          row.setAttribute("role", "treeitem");
          row.setAttribute("aria-level", String(depth + 2));
          row.setAttribute("aria-selected", String(selectedBone === boneId));
          row.onclick = () => selectBone(boneId);
          root.append(row);
        }
    }
    for (const child of doc.nodes.filter((n) => n.parent === node.id))
      add(child, depth + 1);
  }
  for (const node of doc.nodes.filter((n) => !n.parent)) add(node, 0);
  const rows = [...root.querySelectorAll('[role="treeitem"]')];
  const key = row => row.dataset.bone || row.dataset.id;
  const tabstop = rows.find(row => key(row) === focusedOutlinerKey) || rows[0];
  if (tabstop) { tabstop.tabIndex = 0; if (hadFocus) tabstop.focus(); }
  root.onfocusin = event => {
    const row = event.target.closest('[role="treeitem"]');
    if (!row) return;
    focusedOutlinerKey = key(row);
    rows.forEach(item => { item.tabIndex = item === row ? 0 : -1; });
  };
  root.onkeydown = event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const row = event.target.closest('[role="treeitem"]');
    if (!row) return;
    const index = rows.indexOf(row);
    let target;
    if (event.key === "ArrowDown") target = rows[Math.min(index + 1, rows.length - 1)];
    else if (event.key === "ArrowUp") target = rows[Math.max(index - 1, 0)];
    else if (event.key === "Home") target = rows[0];
    else if (event.key === "End") target = rows.at(-1);
    else if (event.key === "ArrowLeft") {
      const parent = row.dataset.bone ? boneObjects.get(row.dataset.bone).userData.ownerId : nodes.get(row.dataset.id)?.parent;
      target = rows.find(item => item.dataset.id === parent);
    } else if (event.key === "ArrowRight") {
      target = rows.find(item => item.dataset.bone && boneObjects.get(item.dataset.bone).userData.ownerId === row.dataset.id ||
        item.dataset.id && nodes.get(item.dataset.id)?.parent === row.dataset.id);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); event.stopPropagation(); row.click(); return;
    } else return;
    event.preventDefault(); event.stopPropagation(); target?.focus();
  };
}
function buildCollections() {
  $("collections").replaceChildren();
  for (const collection of doc.collections || []) {
    const label = document.createElement("label"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = collection.visible;
    input.disabled = busy;
    input.onchange = () =>
      commit([
        { op: "set-collection", id: collection.id, visible: input.checked },
      ]);
    label.append(input, document.createTextNode(collection.name));
    $("collections").append(label);
  }
}
const fields = {};
for (const kind of ["translation", "rotation", "scale"]) {
  const row = document.createElement("div");
  row.className = "transform-row";
  const label = document.createElement("span");
  label.textContent = {
    translation: "Pos m",
    rotation: "Rot °",
    scale: "Scale",
  }[kind];
  row.append(label);
  fields[kind] = [];
  for (let i = 0; i < 3; i++) {
    const input = document.createElement("input");
    input.type = "number";
    input.step = kind === "rotation" ? "1" : ".01";
    input.setAttribute("aria-label", kind + " " + "XYZ"[i]);
    input.onchange = () => numeric(kind, i, input.value);
    fields[kind].push(input);
    row.append(input);
  }
  $("transform-fields").append(row);
}
function inspect() {
  const id = [...selection][0],
    node = nodes.get(id),
    object = objects.get(id);
  $("base-inspector")
    .querySelectorAll("input,select,button")
    .forEach((e) => {
      if (
        !e.closest("#overlay-controls") &&
        !["clip", "play", "time", "show-all"].includes(e.id)
      )
        e.disabled = !node || busy || nodeLocked(id) || previewActive ||
          ((e.closest("#transform-fields") || ["parent", "reset", "paste", "ground"].includes(e.id)) && !transformSelectionAllowed());
    });
  $("locked").disabled = !node || busy || previewActive;
  if (!node) {
    $("selected-name").textContent = "Select an object";
    $("selected-type").textContent = "";
    $("details").textContent = "";
    return;
  }
  $("selected-name").textContent =
    selection.size > 1 ? selection.size + " objects" : node.name;
  $("selected-type").textContent = node.type + (Object.hasOwn(nativeTransformReadonly, id) ? " · transform read-only" : "");
  $("name").value = node.name;
  $("visible").checked = node.visible;
  $("render-visible").checked = node.render.visible;
  $("locked").checked = nodeLocked(id);
  if (selectedBone) {
    const bone = boneObjects.get(selectedBone);
    $("selected-name").textContent = bone.name;
    $("selected-type").textContent = "Native GLB bone · inspectable pose";
    $("inspector")
      .querySelectorAll(
        "#base-inspector input,#base-inspector select,#base-inspector button",
      )
      .forEach((input) => (input.disabled = true));
    $("details").textContent = JSON.stringify(boneSelection(), null, 2);
    return;
  }
  const channels = drag && !transformReason(id) ? localTransform(object) : node.transform;
  const euler = new THREE.Euler().setFromQuaternion(new THREE.Quaternion().fromArray(channels.rotation), "XYZ");
  const values = {
    translation: channels.translation,
    rotation: [euler.x, euler.y, euler.z].map(THREE.MathUtils.radToDeg),
    scale: channels.scale,
  };
  for (const kind of Object.keys(fields))
    fields[kind].forEach(
      (input, i) => (input.value = Number(values[kind][i].toFixed(5))),
    );
  $("parent").replaceChildren(new Option("Scene root", ""));
  for (const other of doc.nodes)
    if (other.id !== id) $("parent").add(new Option(other.name, other.id));
  $("parent").value = node.parent || "";
  $("details").textContent = JSON.stringify(
    {
      id,
      asset: node.asset || null,
      bounds: node.bounds,
      collections: node.collections,
      animation_bindings: node.animation_bindings,
      constraints: node.constraints,
      authored_transform: node.transform,
      native_world_matrix: evaluatedStates.get(id).world_matrix,
      transform_readonly: transformReason(id),
    },
    null,
    2,
  );
}
function numeric(kind, index, value) {
  if (!transformSelectionAllowed()) { diagnostic("Selected transforms are read-only in the native viewport."); return; }
  const number = Number(value);
  if (
    !Number.isFinite(number) ||
    Math.abs(number) > 1e6 ||
    (kind === "scale" && number <= 0)
  ) {
    diagnostic(
      "Enter a finite value within ±1,000,000; scale must be positive.",
    );
    inspect();
    return;
  }
  const commands = [];
  for (const id of roots()) {
    if (nodeLocked(id)) continue;
    const object = objects.get(id),
      transform = localTransform(object);
    if (kind === "rotation") {
      const e = new THREE.Euler().setFromQuaternion(object.quaternion, "XYZ");
      e["xyz"[index]] = THREE.MathUtils.degToRad(number);
      transform.rotation = new THREE.Quaternion().setFromEuler(e).toArray();
    } else transform[kind][index] = number;
    commands.push({ op: "set-transform", id, transform });
  }
  commit(commands);
}
function selectedPatch(patch) {
  return roots()
    .filter((id) => !nodeLocked(id))
    .map((id) => ({ op: "set-node", id, patch }));
}
$("group").onclick = () => {
  const ids = roots();
  if (!ids.length) return;
  const parent = nodes.get(ids[0]).parent;
  const sameParent = ids.every((id) => nodes.get(id).parent === parent)
    ? parent
    : null;
  const id = "group-" + crypto.randomUUID().replaceAll("-", "").slice(0, 12);
  const node = {
    id,
    name: "Group",
    type: "group",
    parent: sameParent,
    transform: {
      translation: [0, 0, 0],
      rotation: [0, 0, 0, 1],
      scale: [1, 1, 1],
    },
    visible: true,
    collections: [],
    layers: [],
    material_overrides: [],
    animation_bindings: [],
    constraints: [],
    bounds: null,
    locked: false,
    selectable: true,
    render: { visible: true, cast_shadow: true, receive_shadow: true },
    source: null,
    metadata: {},
  };
  commit([
    { operation: "create", parameters: { node } },
    ...ids.map((identifier) => ({
      operation: "reparent",
      parameters: { identifier, parent: id, preserve_world: true },
    })),
  ]);
};
$("ungroup").onclick = () => {
  const commands = [];
  for (const id of roots()) {
    const node = nodes.get(id);
    if (node.type !== "group") continue;
    for (const child of doc.nodes.filter((n) => n.parent === id))
      commands.push({
        operation: "reparent",
        parameters: {
          identifier: child.id,
          parent: node.parent,
          preserve_world: true,
        },
      });
    commands.push({
      operation: "delete",
      parameters: { identifier: id, policy: "reject" },
    });
  }
  commit(commands);
};
$("duplicate").onclick = () =>
  commit(
    roots().map((identifier) => ({
      operation: "duplicate",
      parameters: {
        identifier,
        new_id: "copy-" + crypto.randomUUID().replaceAll("-", "").slice(0, 12),
      },
    })),
  );
$("delete").onclick = () =>
  commit(
    roots().map((identifier) => ({
      operation: "delete",
      parameters: { identifier, policy: "subtree" },
    })),
  );
$("name").onchange = () => commit(selectedPatch({ name: $("name").value }));
$("visible").onchange = () =>
  commit(selectedPatch({ visible: $("visible").checked }));
$("render-visible").onchange = () =>
  commit(
    roots().map((id) => ({
      op: "set-node",
      id,
      patch: {
        render: {
          ...nodes.get(id).render,
          visible: $("render-visible").checked,
        },
      },
    })),
  );
$("locked").onchange = () =>
  commit(
    roots().map((id) => ({
      op: "set-node",
      id,
      patch: { locked: $("locked").checked },
    })),
  );
$("parent").onchange = () =>
  commit(
    roots().map((id) => ({
      op: "reparent",
      id,
      parent: $("parent").value || null,
      keep_world: true,
    })),
  );
$("reset").onclick = () =>
  commit(
    roots().map((id) => ({
      op: "set-transform",
      id,
      transform: {
        translation: [0, 0, 0],
        rotation: [0, 0, 0, 1],
        scale: [1, 1, 1],
      },
    })),
  );
$("copy").onclick = () => {
  const id = [...selection][0];
  if (id) copied = structuredClone(nodes.get(id).transform);
};
$("paste").onclick = () => {
  if (copied)
    commit(
      roots().map((id) => ({
        op: "set-transform",
        id,
        transform: structuredClone(copied),
      })),
    );
};
$("ground").onclick = () => {
  const commands = [];
  content.updateMatrixWorld(true);
  for (const id of roots()) {
    const object = objects.get(id),
      box = new THREE.Box3().setFromObject(object),
      worldPosition = object.getWorldPosition(new THREE.Vector3());
    worldPosition.y -= box.min.y;
    const local = object.parent.worldToLocal(worldPosition),
      transform = localTransform(object);
    transform.translation = local.toArray();
    commands.push({ op: "set-transform", id, transform });
  }
  commit(commands);
};
$("hide").onclick = () => commit(selectedPatch({ visible: false }));
$("isolate").onclick = () => {
  const selectedRoots = roots(selection), ancestors = new Set();
  for (const id of selectedRoots) {
    let parent = nodes.get(id)?.parent;
    while (parent) { ancestors.add(parent); parent = nodes.get(parent)?.parent; }
  }
  for (const [id, obj] of objects)
    obj.visible = evaluatedStates.get(id).editor_visible && (
      ancestors.has(id) || selection.has(id) ||
      selectedRoots.some((root) => {
        let p = nodes.get(id)?.parent;
        while (p) {
          if (p === root) return true;
          p = nodes.get(p)?.parent;
        }
        return false;
      }));
  $("status").textContent = "Temporary isolation; saved visibility unchanged.";
};
$("show-all").onclick = () => {
  for (const [id, obj] of objects) obj.visible = evaluatedStates.get(id).editor_visible;
};
$("all").onclick = () => {
  selection = new Set(doc.nodes.filter((n) => nodeSelectable(n.id)).map((n) => n.id));
  refreshSelection();
};
$("none").onclick = () => {
  selection.clear();
  refreshSelection();
};
$("invert").onclick = () => {
  selection = new Set(
    doc.nodes
      .filter((n) => nodeSelectable(n.id) && !selection.has(n.id))
      .map((n) => n.id),
  );
  refreshSelection();
};
$("children").onclick = () => {
  for (const id of [...selection])
    for (const n of doc.nodes) if (n.parent === id && nodeSelectable(n.id)) selection.add(n.id);
  refreshSelection();
};
$("parent-select").onclick = () => {
  selection = new Set(
    [...selection].map((id) => nodes.get(id).parent).filter(id => id && nodeSelectable(id)),
  );
  refreshSelection();
};
$("same-type").onclick = () => {
  const types = new Set([...selection].map((id) => nodes.get(id).type));
  selection = new Set(
    doc.nodes.filter((n) => types.has(n.type) && nodeSelectable(n.id)).map((n) => n.id),
  );
  refreshSelection();
};
$("filter").oninput = buildOutliner;
function frameBox(box) {
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3()),
    span = Math.max(box.getSize(new THREE.Vector3()).length(), 0.5);
  if ($("view").value === "camera") {
    $("view").value = "perspective";
    setView();
  }
  const direction = camera.position.clone().sub(orbit.target).normalize();
  if (!direction.lengthSq()) direction.set(1, 0.7, 1).normalize();
  camera.position.copy(center).addScaledVector(direction, span * 1.8);
  orbit.target.copy(center);
  perspective.near = Math.max(0.001, span / 1000);
  perspective.far = Math.max(100, span * 100);
  perspective.updateProjectionMatrix();
  orthographic.userData.span = span;
  resize();
  orbit.update();
}
function frameScene() {
  frameBox(new THREE.Box3().setFromObject(content));
}
$("frame").onclick = () => {
  const box = new THREE.Box3();
  for (const id of selection) box.expandByObject(objects.get(id));
  frameBox(box);
};
$("frame-all").onclick = frameScene;
$("bookmark").onclick = () => {
  bookmark = {
    position: camera.position.toArray(),
    target: orbit.target.toArray(),
    view: $("view").value,
  };
  $("status").textContent = "View bookmark saved for this session.";
};
$("recall").onclick = () => {
  if (bookmark) {
    $("view").value = bookmark.view;
    setView();
    if ($("view").value !== "camera") {
      camera.position.fromArray(bookmark.position);
      orbit.target.fromArray(bookmark.target);
      orbit.update();
    }
  }
};
function setView() {
  let value = $("view").value;
  const center = orbit.target.clone(),
    distance = Math.max(camera.position.distanceTo(center), 1);
  camera = value === "perspective" ? perspective : orthographic;
  if (value === "camera") {
    const node = doc.nodes.find((n) => n.type === "camera" &&
      (activeNativeCameraId === null || n.id === activeNativeCameraId));
    if (!node) {
      diagnostic("No native camera is available.");
      $("view").value = value = "perspective";
      activeNativeCameraId = null;
      camera = perspective;
    } else {
      content.updateMatrixWorld(true);
      const actual = objects.get(node.id).children.find((o) => o.isCamera);
      camera = actual;
      activeNativeCameraId = node.id;
    }
  } else {
    activeNativeCameraId = null;
    const directions = {
      front: [0, 0, 1],
      back: [0, 0, -1],
      left: [-1, 0, 0],
      right: [1, 0, 0],
      top: [0, 1, 0],
      bottom: [0, -1, 0],
    };
    if (directions[value]) {
      camera.position
        .copy(center)
        .add(new THREE.Vector3(...directions[value]).multiplyScalar(distance));
      camera.up.set(
        0,
        value === "top" || value === "bottom" ? 0 : 1,
        value === "top" ? -1 : value === "bottom" ? 1 : 0,
      );
      camera.lookAt(center);
    } else camera.up.set(0, 1, 0);
  }
  orbit.object = camera;
  orbit.enabled = value !== "camera";
  gizmo.camera = camera;
  $("view-label").textContent = value;
  resize();
}
$("view").onchange = setView;
$("tool").onchange = () => gizmo.setMode($("tool").value);
$("space").onchange = () => gizmo.setSpace($("space").value);
$("snap").onchange = () => {
  const value = Number($("snap").value);
  if (!Number.isFinite(value) || value < 0 || value > 100) {
    $("snap").value = 0;
    return;
  }
  gizmo.setTranslationSnap(value || null);
  gizmo.setRotationSnap(value ? THREE.MathUtils.degToRad(15) : null);
  gizmo.setScaleSnap(value || null);
};
function shading() {
  const mode = $("shading").value;
  for (const [mesh, material] of nativeMaterials) {
    mesh.material =
      mode === "solid"
        ? solidMaterial
        : mode === "normals"
          ? normalMaterial
          : material;
    for (const m of Array.isArray(mesh.material)
      ? mesh.material
      : [mesh.material])
      m.wireframe = mode === "wireframe";
  }
}
$("shading").onchange = shading;
function applyOverlays() {
  grid.visible = overlayState.grid;
  axes.visible = overlayState.axes;
  for (const h of helpers)
    h.visible = Boolean(overlayState[h.userData.overlay]);
  selectionHelpers.forEach((h) => (h.visible = overlayState.bounds));
  $("guides").style.display =
    overlayState.safe || overlayState.thirds ? "block" : "none";
  $("guides").classList.toggle("thirds", overlayState.thirds);
}
for (const name of Object.keys(overlayState)) {
  const label = document.createElement("label"),
    input = document.createElement("input");
  input.type = "checkbox";
  input.checked = overlayState[name];
  input.setAttribute("aria-label", name + " overlay");
  input.onchange = () => {
    overlayState[name] = input.checked;
    applyOverlays();
  };
  label.append(input, document.createTextNode(name));
  $("overlay-controls").append(label);
}
function buildClips() {
  $("clip").replaceChildren(new Option("No clip", ""));
  for (let i = 0; i < mixers.length; i++)
    for (let j = 0; j < mixers[i].clips.length; j++)
      $("clip").add(new Option(mixers[i].clips[j].name, i + ":" + j));
  playing = false;
  animationTime = 0;
  $("play").textContent = "Play";
}
$("clip").onchange = () => {
  mixers.forEach((x) => x.mixer.stopAllAction());
  animationTime = 0;
  clipDuration = 0;
  if ($("clip").value) {
    const [i, j] = $("clip").value.split(":").map(Number),
      entry = mixers[i],
      clip = entry.clips[j];
    entry.mixer.clipAction(clip).play();
    clipDuration = clip.duration;
    $("time").max = String(clip.duration);
  } else $("time").max = "1";
};
$("play").onclick = () => {
  playing = !playing;
  $("play").textContent = playing ? "Pause" : "Play";
};
$("time").oninput = () => {
  playing = false;
  animationTime = Number($("time").value);
  mixers.forEach((x) => x.mixer.setTime(animationTime));
  $("clock").textContent = animationTime.toFixed(2) + " s";
};
async function historyCommand(name) {
  if (rejectPendingEdit()) return;
  setBusy(true);
  try {
    const displayed = await install(
      await api(name, {
        expected_revision: revision,
        expected_sha256: sha256,
        transaction_id: crypto.randomUUID(),
      }),
    );
    if (displayed !== false) clearDiagnostic();
  } catch (e) {
    diagnostic(e);
  } finally {
    setBusy(false);
  }
}
$("undo").onclick = () => historyCommand("undo");
$("redo").onclick = () => historyCommand("redo");
$("refresh").onclick = () => reload().catch(diagnostic);
function resize() {
  if (!renderer) return;
  const width = $("viewport").clientWidth,
    height = $("viewport").clientHeight;
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 2));
  renderer.setSize(Math.min(width, 2048), Math.min(height, 2048), false);
  if (camera.isPerspectiveCamera) camera.aspect = width / Math.max(height, 1);
  else if (camera === orthographic) {
    const span = orthographic.userData.span || 5;
    camera.left = (-span * width) / Math.max(height, 1) / 2;
    camera.right = -camera.left;
    camera.top = span / 2;
    camera.bottom = -span / 2;
  }
  camera.updateProjectionMatrix();
}
function cancelDrag() {
  if (!drag) return;
  for (const [id, record] of drag.records)
    setTransform(objects.get(id), record.local);
  drag = null;
  orbit.enabled = $("view").value !== "camera";
  gizmo.reset();
  inspect();
}
function initialize() {
  try {
    renderer = new THREE.WebGLRenderer({
      antialias: true,
      preserveDrawingBuffer: true,
    });
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.shadowMap.enabled = true;
    $("viewport").prepend(renderer.domElement);
    orbit = new OrbitControls(camera, renderer.domElement);
    orbit.enableDamping = true;
    orbit.dampingFactor = 0.15;
    orbit.maxDistance = 10000;
    orbit.minDistance = 0.01;
    gizmo = new TransformControls(camera, renderer.domElement);
    world.add(gizmo.getHelper());
    gizmo.addEventListener("dragging-changed", (e) => {
      orbit.enabled = $("view").value !== "camera" && !e.value;
      if (e.value) {
        content.updateMatrixWorld(true);
        drag = {
          primary: gizmo.object[editorNode],
          records: new Map(
            roots().map((id) => [
              id,
              {
                local: localTransform(objects.get(id)),
                world: objects.get(id).matrixWorld.clone(),
              },
            ]),
          ),
        };
      } else if (drag) {
        const current = drag;
        drag = null;
        if ([...current.records.keys()].some(id => transformReason(id))) {
          applyNativeState(savedNativeState.nodes);
          diagnostic("Selected transforms are read-only in the native viewport.");
          return;
        }
        commit(
          [...current.records.keys()]
            .filter((id) => !transformReason(id))
            .map((id) => ({
              op: "set-transform",
              id,
              transform: localTransform(objects.get(id)),
            })),
        );
      }
    });
    gizmo.addEventListener("objectChange", () => {
      if (drag) {
        const primary = objects.get(drag.primary);
        primary.updateMatrixWorld(true);
        const delta = primary.matrixWorld
          .clone()
          .multiply(drag.records.get(drag.primary).world.clone().invert());
        for (const [id, record] of drag.records) {
          if (id === drag.primary || transformReason(id)) continue;
          const object = objects.get(id),
            matrix = object.parent.matrixWorld
              .clone()
              .invert()
              .multiply(delta)
              .multiply(record.world);
          matrix.decompose(object.position, object.quaternion, object.scale);
          object.updateMatrixWorld(true);
        }
      }
      inspect();
      selectionHelpers.forEach((h) => h.update());
    });
    let pointer;
    renderer.domElement.addEventListener(
      "pointerdown",
      (e) => (pointer = [e.clientX, e.clientY]),
    );
    renderer.domElement.addEventListener("pointerup", (e) => {
      if (
        drag ||
        gizmo.dragging ||
        !pointer ||
        Math.hypot(e.clientX - pointer[0], e.clientY - pointer[1]) > 4
      )
        return;
      const rect = renderer.domElement.getBoundingClientRect(),
        ray = new THREE.Raycaster();
      ray.setFromCamera(
        new THREE.Vector2(
          ((e.clientX - rect.left) / rect.width) * 2 - 1,
          1 - ((e.clientY - rect.top) / rect.height) * 2,
        ),
        camera,
      );
      const hit = ray.intersectObjects(content.children, true).find((h) => {
        let object = h.object, identifier = null;
        while (object) {
          if (!object.visible) return false;
          if (!identifier && object[editorNode]) identifier = object[editorNode];
          object = object.parent;
        }
        return identifier !== null && nodeSelectable(identifier);
      });
      if (hit) {
        let object = hit.object;
        while (object && !object[editorNode]) object = object.parent;
        if (object && nodeSelectable(object[editorNode]))
          select(object[editorNode], e.shiftKey, e.ctrlKey || e.metaKey);
      } else if (!e.shiftKey) select(null);
    });
    renderer.domElement.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      lost = true;
      cancelDrag();
      diagnostic(
        "WebGL context lost. Saved scene is preserved; waiting for GPU recovery.",
      );
    });
    renderer.domElement.addEventListener("webglcontextrestored", () => {
      lost = false;
      reload()
        .then((displayed) => {
          if (displayed === false) return;
          clearDiagnostic();
          $("status").textContent = "WebGL restored; saved scene reloaded.";
        })
        .catch(diagnostic);
    });
    new ResizeObserver(resize).observe($("viewport"));
    resize();
    reload(true).catch(diagnostic);
  } catch (e) {
    diagnostic("WebGL initialization failed: " + e.message);
  }
}
const shortcuts = [
  ["?", "Open keyboard reference"],
  ["Ctrl/Cmd+Z", "Undo the last saved command"],
  ["Ctrl/Cmd+Shift+Z", "Redo a saved command"],
  ["G / R / S", "Choose move, rotate or scale tool"],
  ["F", "Frame the selected objects"],
  ["Escape", "Cancel a transform drag and clear selection"],
  ["Viewport/page arrows", "Move selected editable objects by 0.1 meter; Shift moves by 1 meter"],
  ["Outliner Up / Down / Home / End", "Focus previous, next, first or last object"],
  ["Outliner Left / Right", "Focus parent or first child"],
  ["Outliner Enter / Space", "Select the focused object; native visibility and locks still apply"],
  ["Tab / Shift+Tab", "Move between controls, then edit transforms through the numeric inspector"],
];
let shortcutReturnFocus = null, shortcutReturnKey = null;
function filterShortcuts() {
  const query = $("shortcut-search").value.toLocaleLowerCase().trim();
  const matches = shortcuts.filter(entry => entry.join(" ").toLocaleLowerCase().includes(query));
  $("shortcut-results").replaceChildren(...matches.map(([keys, action]) => {
    const item = document.createElement("li"), label = document.createElement("strong");
    label.textContent = keys; item.append(label, document.createTextNode(" — " + action)); return item;
  }));
  $("shortcut-count").textContent = matches.length + " matching shortcuts";
}
function showShortcutHelp() {
  if ($("shortcut-dialog").open) return;
  shortcutReturnFocus = document.activeElement;
  const row = shortcutReturnFocus?.closest('#outliner [role="treeitem"]');
  shortcutReturnKey = row?.dataset.bone ? {bone: row.dataset.bone} :
    row?.dataset.id ? {node: row.dataset.id} : null;
  $("shortcut-search").value = ""; filterShortcuts();
  $("shortcut-dialog").showModal(); $("shortcut-search").focus();
}
$("keyboard-help").onclick = showShortcutHelp;
$("shortcut-search").oninput = filterShortcuts;
$("shortcut-close").onclick = () => $("shortcut-dialog").close();
$("shortcut-dialog").onkeydown = event => {
  if (event.key !== "Tab") return;
  const first = $("shortcut-search"), last = $("shortcut-close");
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault(); last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault(); first.focus();
  }
};
$("shortcut-dialog").onclose = () => {
  let target = shortcutReturnFocus?.isConnected ? shortcutReturnFocus : null;
  if (!target && shortcutReturnKey) {
    target = [...$("outliner").querySelectorAll('[role="treeitem"]')].find(row =>
      shortcutReturnKey.bone ? row.dataset.bone === shortcutReturnKey.bone : row.dataset.id === shortcutReturnKey.node);
  }
  if (!target || target.disabled) target = $("keyboard-help");
  target.focus();
  shortcutReturnFocus = null; shortcutReturnKey = null;
};
window.addEventListener("keydown", (e) => {
  if (e.defaultPrevented || $("shortcut-dialog").open ||
      e.target.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return;
  if (e.key === "?" && !e.ctrlKey && !e.metaKey && !e.altKey) {
    e.preventDefault(); showShortcutHelp(); return;
  }
  if (e.altKey || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() !== "z")) return;
  if (e.key === "Escape") {
    cancelDrag();
    select(null);
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    historyCommand(e.shiftKey ? "redo" : "undo");
  } else if (e.key.toLowerCase() === "f") $("frame").click();
  else if ({ g: "translate", r: "rotate", s: "scale" }[e.key.toLowerCase()]) {
    $("tool").value = { g: "translate", r: "rotate", s: "scale" }[
      e.key.toLowerCase()
    ];
    gizmo.setMode($("tool").value);
  } else if (
    !e.target.closest("#diagnostic") && (e.target === document.body || e.target.closest("#viewport")) && !previewActive && transformSelectionAllowed() &&
    ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)
  ) {
    e.preventDefault();
    const axis = e.key.includes("Left") || e.key.includes("Right") ? 0 : 2,
      sign = e.key.includes("Left") || e.key.includes("Up") ? -1 : 1,
      step = e.shiftKey ? 1 : 0.1;
    commit(
      roots()
        .filter((id) => !nodeLocked(id))
        .map((id) => {
          const transform = localTransform(objects.get(id));
          transform.translation[axis] += step * sign;
          return { op: "set-transform", id, transform };
        }),
    );
  }
});
const clock = new THREE.Clock();
function frame() {
  if (disposed) return;
  requestAnimationFrame(frame);
  const delta = Math.min(clock.getDelta(), 0.1);
  if (!renderer || lost || document.hidden) return;
  if (playing && clipDuration) {
    animationTime = (animationTime + delta) % clipDuration;
    mixers.forEach((x) => x.mixer.setTime(animationTime));
    $("time").value = animationTime;
    $("clock").textContent = animationTime.toFixed(2) + " s";
  }
  if ($("view").value !== "camera") orbit.update();
  for (const h of helpers) h.update?.();
  selectionHelpers.forEach((h) => h.update());
  renderer.render(world, camera);
}
window.addEventListener("pagehide", () => {
  disposed = true;
  for (const extension of extensions) extension.dispose?.();
  disposeContent();
  gizmo?.dispose();
  orbit?.dispose();
  renderer?.dispose();
  normalMaterial.dispose();
  solidMaterial.dispose();
  grid.geometry.dispose();
  grid.material.dispose();
  axes.geometry.dispose();
  axes.material.dispose();
});
window.hatchEditor = {
  get snapshot() {
    return structuredClone(currentSnapshot);
  },
  get selection() {
    return [...selection];
  },
  get errors() {
    return [...errors];
  },
  select,
  selectBone,
  get boneSelection() {
    return boneSelection();
  },
  registerExtension(factory, container) {
    const extension = factory({
      container,
      api,
      upload,
      reload,
      preview,
      applyNativeState,
      clearPreview,
      getSnapshot: () => structuredClone(currentSnapshot),
      getSelection: () => [...selection],
      getBoneSelection: boneSelection,
      onBoneSelect: selectBone,
      commit,
      onSelect: select,
    });
    extensions.push(extension);
    if (currentSnapshot) extension.refresh?.(currentSnapshot, [...selection]);
    return extension;
  },
  commit,
  applyNativeState,
  preview,
  clearPreview,
  reload,
  objects,
  get renderer() {
    return renderer;
  },
  get camera() {
    return camera;
  },
  get gizmo() {
    return gizmo;
  },
};
initialize();
frame();
