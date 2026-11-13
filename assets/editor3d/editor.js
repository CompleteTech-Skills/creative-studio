import * as THREE from "three";
import { OrbitControls } from "/vendor/examples/jsm/controls/OrbitControls.js";
import { GLTFLoader } from "/vendor/examples/jsm/loaders/GLTFLoader.js";
import { TransformControls } from "./TransformControls.js";
const $ = (id) => document.getElementById(id);
const tokenKey = "hatch-editor-bearer-v1";
let token = location.hash.slice(1);
try {
  if (token.length >= 24 && token.length <= 1024) sessionStorage.setItem(tokenKey, token);
  else if (!token) token = sessionStorage.getItem(tokenKey) || "";
} catch { /* A launch URL still works if tab storage is unavailable. */ }
// The bearer token stays in this origin/tab session, never in a query, asset URL or saved document.
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
const commitInterceptors = new Set(), installSubscribers = new Map(), unavailableNodes = new Map(), previewEditAuthorizers = new Set(), timelineProviders = new Set();
let lastEvaluation = null;
function clearInstallSubscriptions() {
  for (const record of installSubscribers.values()) {
    record.cleanup?.(); record.cleanup = null;
  }
}
async function notifyInstalled(epoch) {
  const snapshot = structuredClone(currentSnapshot);
  await Promise.all([...installSubscribers].map(async ([callback, record]) => {
    const cleanup = await callback({snapshot: structuredClone(snapshot), epoch,
      isCurrent: () => !disposed && !lost && loadEpoch === epoch && installSubscribers.get(callback) === record});
    if (typeof cleanup !== "function") return;
    if (disposed || lost || loadEpoch !== epoch || installSubscribers.get(callback) !== record) cleanup();
    else record.cleanup = cleanup;
  }));
}
function canEditPreview() {
  if (!previewActive || !lastEvaluation || lastEvaluation.revision !== revision ||
      lastEvaluation.snapshot_sha256 !== sha256 || lastEvaluation.scene?.path !== currentSnapshot.scene_path ||
      !selection.size || selectedBone || $("clip").value) return false;
  const shot = currentSnapshot.documents[lastEvaluation.shot?.path];
  if (!shot || !currentSnapshot.project.shots.some(ref => ref.path === lastEvaluation.shot.path)) return false;
  const frame = lastEvaluation.frame;
  if (!frame || !Number.isSafeInteger(frame.numerator) || frame.denominator !== 1) return false;
  for (const id of roots()) {
    const node = nodes.get(id), state = lastEvaluation.nodes?.[id];
    if (!node || node.locked || !state || state.locked || state.selectable === false ||
        node.transform.pivot?.some(value => value !== 0) ||
        (id === shot.camera && shot.camera_overrides?.transform) ||
        node.constraints?.length || node.attachment || node.path_binding ||
        state.native_clip || shot.path_bindings?.length || shot.temporal_constraints?.some(item =>
          item.target === id && item.start_frame <= frame.numerator && frame.numerator < item.end_frame)) return false;
  }
  let allowed = 0;
  for (const callback of previewEditAuthorizers) {
    try { if (callback({snapshot: structuredClone(currentSnapshot), evaluation: structuredClone(lastEvaluation), selection: [...selection]}) === true) allowed++; }
    catch { return false; }
  }
  return allowed === 1;
}
function registerTimelineProvider(callback) {
  if (typeof callback !== "function") throw Error("A timeline provider must be a function.");
  timelineProviders.add(callback);
  return () => timelineProviders.delete(callback);
}
function getTimelineContext() {
  if (timelineProviders.size === 0) return null;
  if (timelineProviders.size !== 1) throw Error("Exactly one timeline provider is required.");
  const value = [...timelineProviders][0]();
  if (value == null) return null;
  const expected = ["revision", "sha256", "scene_path", "shot_path", "frame"];
  if (Object.keys(value).sort().join() !== expected.sort().join() ||
      value.revision !== revision || value.sha256 !== sha256 || value.scene_path !== currentSnapshot.scene_path ||
      !currentSnapshot.project.shots.some(ref => ref.path === value.shot_path)) throw Error("Timeline context is stale or unregistered.");
  const shot = currentSnapshot.documents[value.shot_path], f = value.frame;
  if (shot.scene.path !== value.scene_path || !f || Object.keys(f).sort().join() !== "denominator,numerator" ||
      !Number.isSafeInteger(f.numerator) || !Number.isSafeInteger(f.denominator) || f.denominator <= 0 ||
      f.numerator < 0 || BigInt(f.numerator) >= BigInt(shot.duration_frames) * BigInt(f.denominator))
    throw Error("Timeline context does not name an exact frame in the active saved shot.");
  return structuredClone(value);
}
function refreshPreviewEditState() { inspect(); attach(); }
function registerPreviewEditAuthorizer(callback) {
  if (typeof callback !== "function") throw Error("A preview edit authorizer must be a function.");
  previewEditAuthorizers.add(callback);
  return () => { previewEditAuthorizers.delete(callback); refreshPreviewEditState(); };
}
function registerCommitInterceptor(callback) {
  if (typeof callback !== "function") throw Error("A command interceptor must be a function.");
  commitInterceptors.add(callback);
  return () => commitInterceptors.delete(callback);
}
function subscribeInstalled(callback) {
  if (typeof callback !== "function") throw Error("An install subscriber must be a function.");
  if (installSubscribers.has(callback)) throw Error("Install subscriber is already registered.");
  const record = {cleanup: null}; installSubscribers.set(callback, record);
  if (window.editorReady) {
    const epoch = loadEpoch;
    Promise.resolve(callback({snapshot: structuredClone(currentSnapshot), epoch,
      isCurrent: () => !disposed && !lost && loadEpoch === epoch && installSubscribers.get(callback) === record}))
      .then(cleanup => { if (typeof cleanup !== "function") return;
        if (disposed || lost || loadEpoch !== epoch || installSubscribers.get(callback) !== record) cleanup();
        else record.cleanup = cleanup;
      }).catch(diagnostic);
  }
  return () => { record.cleanup?.(); installSubscribers.delete(callback); };
}
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
function diagnostic(error) {
  const text = error?.message || String(error);
  errors.push(text);
  $("diagnostic").hidden = false;
  $("diagnostic").textContent = text;
  $("status").textContent = "Operation rejected; saved state preserved.";
}
function clearDiagnostic() {
  $("diagnostic").hidden = true;
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
async function downloadArtifact(body) {
  const response = await fetch("/api/render/artifact", {method: "POST",
    headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"}, body: JSON.stringify(body)});
  if (!response.ok) { const result = await response.json(); throw Error(result.error?.message || "Artifact download rejected."); }
  const media = response.headers.get("Content-Type");
  if (!["image/png", "image/x-exr", "video/mp4", "video/webm"].includes(media)) throw Error("Unsupported artifact response.");
  const disposition = response.headers.get("Content-Disposition") || "";
  const filename = /^attachment; filename="([A-Za-z0-9][A-Za-z0-9_.-]{0,127})"$/.exec(disposition)?.[1];
  if (!filename) throw Error("Artifact has no safe download name.");
  const blob = await response.blob(), url = URL.createObjectURL(blob), anchor = document.createElement("a");
  try { anchor.href = url; anchor.download = filename; anchor.click(); }
  finally { setTimeout(() => URL.revokeObjectURL(url), 1000); }
  return {bytes: blob.size, content_type: media, filename};
}
function applyNativeProperties(object, node) {
  if (node.light) {
    const light = object.children.find(child => child.isLight);
    if (light) { light.intensity = node.light.intensity; light.color.setRGB(...node.light.color); }
  }
  if (node.camera) {
    const actual = object.children.find(child => child.isCamera), c = node.camera;
    if (actual) {
      actual.near = c.near; actual.far = c.far;
      if (actual.isPerspectiveCamera) actual.fov = THREE.MathUtils.radToDeg(c.vertical_fov);
      else { actual.top = c.orthographic_height/2; actual.bottom = -actual.top;
        actual.right = actual.top * (actual.aspect || 1); actual.left = -actual.right; }
      actual.updateProjectionMatrix();
    }
  }
}
async function capturePreview(metadata) {
  if (busy || lost || disposed || !window.editorReady) throw Error("The saved viewport is not ready for capture.");
  if (drag || $("clip").value || $("shading").value !== "material")
    throw Error("Return to saved material shading without a local clip or active drag before capture.");
  const required = ["revision", "sha256", "shot", "camera", "frame", "source_time", "color_space", "view_transform", "resolution"];
  if (Object.keys(metadata).sort().join() !== required.sort().join() || metadata.revision !== revision || metadata.sha256 !== sha256)
    throw Error("Capture requires the exact current saved revision and complete metadata.");
  if (metadata.color_space !== "srgb" || metadata.view_transform !== "standard")
    throw Error("Browser PNG capture supports srgb with the standard view transform only.");
  const [width, height] = metadata.resolution || [];
  if (metadata.resolution.length !== 2 || ![width, height].every(n => Number.isSafeInteger(n) && n > 0 && n <= 4096) || width*height > 8388608)
    throw Error("Capture resolution exceeds the bounded viewport budget.");
  const shotRefs = currentSnapshot.project.shots.filter(ref => ref.id === metadata.shot?.id && ref.version === metadata.shot?.version);
  if (shotRefs.length !== 1) throw Error("Capture shot identity is absent or ambiguous.");
  const shot = currentSnapshot.documents[shotRefs[0].path];
  if (shot.scene.path !== currentSnapshot.scene_path || shot.camera !== metadata.camera || !Number.isSafeInteger(metadata.frame) || metadata.frame < 0 || metadata.frame >= shot.duration_frames)
    throw Error("Capture camera/frame does not belong to the active saved shot.");
  const time = metadata.source_time;
  if (!time || !Number.isSafeInteger(time.numerator) || !Number.isSafeInteger(time.denominator) || time.denominator <= 0 ||
      BigInt(time.numerator)*BigInt(shot.fps.numerator) !== BigInt(metadata.frame)*BigInt(shot.fps.denominator)*BigInt(time.denominator))
    throw Error("Capture source time differs from the exact saved shot frame.");
  busy = true;
  gizmo.detach();
  const epoch = loadEpoch, previous = lastEvaluation && structuredClone(lastEvaluation);
  const size = renderer.getSize(new THREE.Vector2()), ratio = renderer.getPixelRatio();
  const oldTone = renderer.toneMapping, oldColor = renderer.outputColorSpace;
  const oldOverlay = overlays.visible, oldGizmo = gizmo.getHelper().visible;
  try {
    const evaluated = await api("timeline/evaluate", {revision, sha256, shot: shotRefs[0].path, frame: metadata.frame});
    if (epoch !== loadEpoch || metadata.revision !== revision || metadata.sha256 !== sha256) throw Error("Saved viewport changed during capture.");
    if (evaluated.camera !== metadata.camera || evaluated.shot?.id !== metadata.shot.id || evaluated.shot?.version !== metadata.shot.version ||
        evaluated.frame?.numerator !== metadata.frame || evaluated.frame?.denominator !== 1 ||
        JSON.stringify(evaluated.time) !== JSON.stringify(time)) throw Error("Native evaluation differs from capture metadata.");
    for (const [id, node] of Object.entries(evaluated.nodes)) {
      if (!objects.has(id) || node.native_clip || JSON.stringify(node.material_overrides || []) !== JSON.stringify(nodes.get(id)?.material_overrides || []))
        throw Error("This evaluated geometry/material binding has no exact browser capture adapter.");
      if (node.visible !== false && unavailableNodes.has(id)) throw Error(unavailableNodes.get(id));
      if (node.camera) {
        const actual = objects.get(id).children.find(child => child.isCamera);
        if (!actual || Boolean(actual.isPerspectiveCamera) !== (node.camera.projection === "perspective"))
          throw Error("The evaluated camera projection has no matching browser capture adapter.");
      }
    }
    preview(evaluated);
    const captureCamera = objects.get(metadata.camera)?.children.find(child => child.isCamera);
    if (!captureCamera) throw Error("The exact native capture camera is unavailable.");
    const projected = captureCamera.clone();
    content.updateMatrixWorld(true); captureCamera.updateWorldMatrix(true, false);
    projected.matrixAutoUpdate = false; projected.matrix.copy(captureCamera.matrixWorld);
    projected.matrixWorld.copy(captureCamera.matrixWorld); projected.matrixWorldInverse.copy(projected.matrixWorld).invert();
    if (projected.isPerspectiveCamera) projected.aspect = width / height;
    else { const halfHeight = (projected.top - projected.bottom)/2; projected.left = -halfHeight*width/height; projected.right = halfHeight*width/height; }
    projected.updateProjectionMatrix();
    renderer.setPixelRatio(1); renderer.setSize(width, height, false);
    renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.LinearToneMapping;
    overlays.visible = false; gizmo.getHelper().visible = false;
    renderer.render(world, projected);
    const image = renderer.domElement.toDataURL("image/png");
    if (epoch !== loadEpoch || lost) throw Error("Viewport became unavailable during capture.");
    return {...structuredClone(metadata), image, backend: "three-0.180.0", offline_render: false};
  } finally {
    renderer.setPixelRatio(ratio); renderer.setSize(size.x, size.y, false);
    renderer.toneMapping = oldTone; renderer.outputColorSpace = oldColor;
    overlays.visible = oldOverlay; gizmo.getHelper().visible = oldGizmo;
    if (epoch === loadEpoch) { if (previous) preview(previous); else clearPreview(); }
    busy = false; attach();
  }
}
function preview(scene) {
  if (scene.world && (!Number.isFinite(scene.world.exposure) || scene.world.exposure < -20 || scene.world.exposure > 20))
    throw Error("Evaluated preview exposure is supported from -20 to +20 stops.");
  if (scene.revision !== undefined && (scene.revision !== revision || scene.snapshot_sha256 !== sha256 ||
      scene.scene?.path !== currentSnapshot.scene_path)) throw Error("Evaluation belongs to another saved scene revision.");
  lastEvaluation = structuredClone(scene);
  playing = false;
  previewActive = true;
  gizmo.detach();
  const evaluated = Array.isArray(scene.nodes)
    ? new Map(scene.nodes.map((node) => [node.id, node]))
    : new Map(Object.entries(scene.nodes));
  content.updateMatrixWorld(true);
  for (const [id, node] of evaluated) {
    const object = objects.get(id);
    if (!object) continue;
    if (node.world_matrix) {
      const worldMatrix = new THREE.Matrix4().set(...node.world_matrix.flat());
      const parentId = nodes.get(id)?.parent,
        parent = evaluated.get(parentId);
      const parentWorld = parent?.world_matrix
        ? new THREE.Matrix4().set(...parent.world_matrix.flat())
        : object.parent.matrixWorld;
      object.matrix.copy(parentWorld.clone().invert().multiply(worldMatrix));
      object.matrixAutoUpdate = false;
      object.matrix.decompose(object.position, object.quaternion, object.scale);
    } else if (node.transform) {
      object.matrixAutoUpdate = true;
      setTransform(object, node.transform);
    }
    if (typeof node.visible === "boolean") object.visible = node.visible;
    applyNativeProperties(object, node);
  }
  if (scene.world) renderer.toneMappingExposure = 2 ** scene.world.exposure;
  content.updateMatrixWorld(true);
  refreshPreviewEditState();
}
function clearPreview() {
  lastEvaluation = null;
  previewActive = false;
  for (const node of doc.nodes) {
    const object = objects.get(node.id);
    object.matrixAutoUpdate = true;
    setTransform(object, node.transform);
    object.visible = node.visible;
    applyNativeProperties(object, node);
  }
  renderer.toneMappingExposure = 2 ** doc.world.exposure;
  content.updateMatrixWorld(true);
  attach();
  inspect();
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
async function commit(commands) {
  if (busy || !commands.length) return;
  busy = true;
  gizmo?.detach();
  $("status").textContent = "Saving command…";
  try {
    const envelope = {expected_revision: revision, expected_sha256: sha256,
      transaction_id: crypto.randomUUID(), commands: nativeCommands(commands), snapshot: structuredClone(currentSnapshot)};
    const previewEdit = previewActive;
    if (previewEdit && (!canEditPreview() || envelope.commands.some(command => command.operation !== "transform" || !roots().includes(command.parameters?.identifier))))
      throw Error("Evaluated preview edits require authorized atomic transform autokey.");
    const intercepted = [];
    for (const callback of commitInterceptors) {
      const handled = await callback(structuredClone(envelope));
      if (handled != null) intercepted.push(handled);
    }
    if (previewEdit && intercepted.length !== 1) throw Error("Preview edits require one atomic native command interceptor.");
    if (intercepted.length > 1) throw Error("More than one extension claimed this command transaction.");
    let result;
    if (intercepted.length) {
      const handled = intercepted[0];
      if (!/^[a-z][a-z0-9-]*\/[a-z][a-z0-9-]*$/.test(handled.route) ||
          handled.body?.expected_revision !== envelope.expected_revision ||
          handled.body?.expected_sha256 !== envelope.expected_sha256 ||
          handled.body?.transaction_id !== envelope.transaction_id)
        throw Error("Extension command must retain the exact transaction identity.");
      const acknowledged = await api(handled.route, handled.body);
      result = await api("snapshot");
      if (acknowledged.revision !== result.revision || acknowledged.sha256 !== result.sha256)
        throw Error("The acknowledged extension revision changed before reload.");
    } else result = await api("commands", {...envelope, snapshot: undefined,
      commands: {schema_version: 1, commands: envelope.commands}});
    await install(result);
    clearDiagnostic();
    $("status").textContent = "Saved revision " + revision;
  } catch (error) {
    diagnostic(error);
    await reload(false);
  } finally {
    busy = false;
    attach();
  }
}
function select(id, add = false, toggle = false) {
  selectedBone = null;
  if (!add && !toggle) selection.clear();
  if (toggle && selection.has(id)) selection.delete(id);
  else if (objects.has(id) && nodes.get(id)?.selectable !== false)
    selection.add(id);
  refreshSelection();
}
function selectBone(id) {
  const bone = boneObjects.get(id);
  if (!bone) return;
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
  if (!gizmo || busy || lost || (previewActive && !canEditPreview()) || selectedBone) {
    gizmo?.detach();
    return;
  }
  const id = roots().find((id) => !nodes.get(id).locked),
    object = objects.get(id);
  if (object) {
    if (previewActive) for (const identifier of roots()) objects.get(identifier).matrixAutoUpdate = true;
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
      group.add(object);
    }
    return group;
  }
  throw Error(asset.message || "Unsupported geometry format.");
}
async function install(snapshot) {
  window.editorReady = false;
  clearInstallSubscriptions();
  lastEvaluation = null;
  previewActive = false;
  currentSnapshot = snapshot;
  $("undo").disabled = !snapshot.undo_available;
  $("redo").disabled = !snapshot.redo_available;
  const epoch = ++loadEpoch,
    assets = snapshot.viewport_assets || {};
  doc = snapshot.scene;
  revision = snapshot.revision;
  sha256 = snapshot.sha256;
  if (!doc?.nodes)
    throw Error("The repository supplied no native Scene3D scene.");
  disposeContent();
  unavailableNodes.clear();
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
    if (["prefab_instance", "billboard_2d"].includes(node.type)) unavailableNodes.set(node.id, "No exact viewport adapter for " + node.type);
    if (node.asset) {
      const reference = node.asset;
      const asset = assets[JSON.stringify([reference.id, reference.version, reference.path, reference.sha256])];
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
          unavailableNodes.set(node.id, error.message);
          diagnostic("Asset " + node.asset.id + ": " + error.message);
        }
      } else {
        unavailableNodes.set(node.id, asset?.message || "No verified viewport adapter.");
        diagnostic(
          "Asset " +
            node.asset.id +
            ": " +
            (asset?.message || "No verified viewport adapter."),
        );
      }
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
        unavailableNodes.set(node.id, "The native area light has no exact viewport adapter.");
        diagnostic(
          "Area-light preview is unavailable: the native scene light has no area dimensions. No point-light substitute was rendered.",
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
  content.updateMatrixWorld(true);
  for (const [id, object] of objects) {
    if ([...boneObjects.values()].some((b) => b.userData.ownerId === id)) {
      const helper = new THREE.SkeletonHelper(object);
      helper.userData.overlay = "bones";
      overlays.add(helper);
      helpers.push(helper);
    }
  }
  selectedBone = null;
  selection = new Set([...selection].filter((id) => objects.has(id)));
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
  await notifyInstalled(epoch);
  if (epoch !== loadEpoch || disposed || lost) return;
  window.editorReady = true;
  if ($("diagnostic").hidden)
    $("status").textContent = "Loaded saved revision " + revision;
}
async function reload(frame = false) {
  const snapshot = await api("snapshot");
  await install(snapshot);
  if (frame) frameScene();
}
function buildOutliner() {
  const root = $("outliner");
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
      button.dataset.hidden = String(!node.visible);
      button.setAttribute("role", "treeitem");
      button.setAttribute("aria-level", String(depth + 1));
      button.setAttribute("aria-selected", String(selection.has(node.id)));
      button.style.paddingLeft = 8 + depth * 14 + "px";
      button.textContent =
        (node.locked ? "🔒 " : node.visible ? "" : "◌ ") +
        node.name +
        " · " +
        node.type;
      button.title = node.id;
      button.onclick = (e) =>
        select(node.id, e.shiftKey, e.ctrlKey || e.metaKey);
      button.draggable = !node.locked;
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
}
function buildCollections() {
  $("collections").replaceChildren();
  for (const collection of doc.collections || []) {
    const label = document.createElement("label"),
      input = document.createElement("input");
    input.type = "checkbox";
    input.checked = collection.visible;
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
  const previewAllowed = previewActive && canEditPreview();
  $("base-inspector")
    .querySelectorAll("input,select,button")
    .forEach((e) => {
      if (
        !e.closest("#overlay-controls") &&
        !["clip", "play", "time", "show-all"].includes(e.id)
      )
        e.disabled = !node || busy || node.locked || (previewActive && (!e.closest("#transform-fields") || !previewAllowed));
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
  $("selected-type").textContent = node.type;
  $("name").value = node.name;
  $("visible").checked = node.visible;
  $("render-visible").checked = node.render.visible;
  $("locked").checked = node.locked;
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
  const euler = new THREE.Euler().setFromQuaternion(object.quaternion, "XYZ");
  const values = {
    translation: object.position.toArray(),
    rotation: [euler.x, euler.y, euler.z].map(THREE.MathUtils.radToDeg),
    scale: object.scale.toArray(),
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
    },
    null,
    2,
  );
}
function numeric(kind, index, value) {
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
    if (nodes.get(id).locked) continue;
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
    .filter((id) => !nodes.get(id).locked)
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
  if (id) copied = localTransform(objects.get(id));
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
  for (const [id, obj] of objects)
    obj.visible =
      selection.has(id) ||
      roots(selection).some((root) => {
        let p = nodes.get(id)?.parent;
        while (p) {
          if (p === root) return true;
          p = nodes.get(p)?.parent;
        }
        return false;
      });
  $("status").textContent = "Temporary isolation; saved visibility unchanged.";
};
$("show-all").onclick = () => {
  for (const [id, obj] of objects) obj.visible = nodes.get(id).visible;
};
$("all").onclick = () => {
  selection = new Set(doc.nodes.filter((n) => n.selectable).map((n) => n.id));
  refreshSelection();
};
$("none").onclick = () => {
  selection.clear();
  refreshSelection();
};
$("invert").onclick = () => {
  selection = new Set(
    doc.nodes
      .filter((n) => n.selectable && !selection.has(n.id))
      .map((n) => n.id),
  );
  refreshSelection();
};
$("children").onclick = () => {
  for (const id of [...selection])
    for (const n of doc.nodes) if (n.parent === id) selection.add(n.id);
  refreshSelection();
};
$("parent-select").onclick = () => {
  selection = new Set(
    [...selection].map((id) => nodes.get(id).parent).filter(Boolean),
  );
  refreshSelection();
};
$("same-type").onclick = () => {
  const types = new Set([...selection].map((id) => nodes.get(id).type));
  selection = new Set(
    doc.nodes.filter((n) => types.has(n.type)).map((n) => n.id),
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
    camera.position.fromArray(bookmark.position);
    orbit.target.fromArray(bookmark.target);
    orbit.update();
  }
};
function setView() {
  const value = $("view").value;
  const center = orbit.target.clone(),
    distance = Math.max(camera.position.distanceTo(center), 1);
  camera = value === "perspective" ? perspective : orthographic;
  if (value === "camera") {
    const node = doc.nodes.find((n) => n.type === "camera");
    if (!node) {
      diagnostic("No native camera is available.");
      $("view").value = "perspective";
      camera = perspective;
    } else {
      content.updateMatrixWorld(true);
      const actual = objects.get(node.id).children.find((o) => o.isCamera);
      camera = actual;
    }
  } else {
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
  if (busy) return;
  busy = true;
  try {
    await install(
      await api(name, {
        expected_revision: revision,
        expected_sha256: sha256,
        transaction_id: crypto.randomUUID(),
      }),
    );
    clearDiagnostic();
  } catch (e) {
    diagnostic(e);
  } finally {
    busy = false;
    attach();
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
  orbit.enabled = true;
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
      orbit.enabled = !e.value;
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
        commit(
          [...current.records.keys()]
            .filter((id) => !nodes.get(id).locked)
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
          if (id === drag.primary || nodes.get(id).locked) continue;
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
        let object = h.object;
        while (object) {
          if (!object.visible) return false;
          object = object.parent;
        }
        return true;
      });
      if (hit) {
        let object = hit.object;
        while (object && !object[editorNode]) object = object.parent;
        if (object && nodes.get(object[editorNode]).selectable)
          select(object[editorNode], e.shiftKey, e.ctrlKey || e.metaKey);
      } else if (!e.shiftKey) select(null);
    });
    renderer.domElement.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      lost = true;
      clearInstallSubscriptions();
      cancelDrag();
      diagnostic(
        "WebGL context lost. Saved scene is preserved; waiting for GPU recovery.",
      );
    });
    renderer.domElement.addEventListener("webglcontextrestored", () => {
      lost = false;
      reload()
        .then(() => {
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
window.addEventListener("keydown", (e) => {
  if (e.target.matches("input,textarea,select")) return;
  if (e.key === "Escape") {
    cancelDrag();
    select(null);
  } else if ((e.ctrlKey || e.metaKey) && e.key === "z") {
    e.preventDefault();
    historyCommand(e.shiftKey ? "redo" : "undo");
  } else if (e.key.toLowerCase() === "f") $("frame").click();
  else if ({ g: "translate", r: "rotate", s: "scale" }[e.key.toLowerCase()]) {
    $("tool").value = { g: "translate", r: "rotate", s: "scale" }[
      e.key.toLowerCase()
    ];
    gizmo.setMode($("tool").value);
  } else if (
    (!previewActive || canEditPreview()) &&
    ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)
  ) {
    e.preventDefault();
    const axis = e.key.includes("Left") || e.key.includes("Right") ? 0 : 2,
      sign = e.key.includes("Left") || e.key.includes("Up") ? -1 : 1,
      step = e.shiftKey ? 1 : 0.1;
    commit(
      roots()
        .filter((id) => !nodes.get(id).locked)
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
  orbit.update();
  for (const h of helpers) h.update?.();
  selectionHelpers.forEach((h) => h.update());
  renderer.render(world, camera);
}
window.addEventListener("pagehide", () => {
  disposed = true;
  clearInstallSubscriptions();
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
      registerCommitInterceptor,
      registerPreviewEditAuthorizer,
      registerTimelineProvider,
      getTimelineContext,
      refreshPreviewEditState,
      capturePreview,
      downloadArtifact,
      subscribeInstalled,
      getRenderer: () => renderer,
      getScene: () => world,
      getCamera: () => camera,
      reload,
      preview,
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
  registerCommitInterceptor,
  registerPreviewEditAuthorizer,
  registerTimelineProvider,
  getTimelineContext,
  refreshPreviewEditState,
  capturePreview,
  downloadArtifact,
  subscribeInstalled,
  commit,
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
