// Canonical prefab projection metadata only. Geometry remains with the existing
// Editor native asset/component consumers and saved edits target authored nodes.
function context(snapshot) {
  const source = snapshot.scene;
  const projection = snapshot.viewport_prefabs;
  if (!projection) {
    if (source.nodes.some(node => node.type === "prefab_instance"))
      throw Error("No canonical read-only prefab projection is available.");
    return {scene: source, authored: new Map(source.nodes.map(node => [node.id, node])), targets: new Map()};
  }
  if (projection.schema_version !== 1 || projection.revision !== snapshot.revision ||
      projection.sha256 !== snapshot.sha256 || projection.authored_scene_path !== snapshot.scene_path ||
      projection.authored_scene_sha256 !== snapshot.inventory?.[snapshot.scene_path]?.sha256 ||
      projection.scene.id !== source.id || projection.scene.version !== source.version ||
      projection.scene.revision !== source.revision || !Array.isArray(projection.scene.nodes) ||
      projection.scene.nodes.length > 1000)
    throw Error("Prefab runtime projection does not match the saved source identity.");
  const authored = new Map(source.nodes.map(node => [node.id, node]));
  const runtime = new Map(projection.scene.nodes.map(node => [node.id, node]));
  if (runtime.size !== projection.scene.nodes.length || [...authored.keys()].some(id => !runtime.has(id)))
    throw Error("Prefab runtime projection lost or duplicated native node identities.");
  const targets = new Map(Object.entries(projection.edit_targets));
  const derived = [...runtime.keys()].filter(id => !authored.has(id));
  const readonly = new Set(projection.readonly_nodes);
  if (readonly.size !== projection.readonly_nodes.length || readonly.size !== derived.length || targets.size !== derived.length ||
      derived.some(id => !readonly.has(id) || !targets.has(id) || authored.get(targets.get(id))?.type !== "prefab_instance"))
    throw Error("Derived prefab content has no exact authored anchor mapping.");
  for (const id of derived) {
    let current = id;
    const seen = new Set();
    while (!authored.has(current)) {
      if (!runtime.has(current) || seen.has(current) || seen.size >= 128)
        throw Error("Invalid derived prefab ancestry.");
      seen.add(current);
      current = runtime.get(current).parent;
    }
    if (targets.get(id) !== current)
      throw Error("Derived prefab selection does not name its authored ancestor.");
  }
  return {scene: projection.scene, authored, targets};
}

export function createPrefabProjection(snapshot) {
  const {scene, authored, targets} = context(snapshot);
  function editTarget(identifier) {
    const target = targets.has(identifier) ? targets.get(identifier) : identifier;
    if (!authored.has(target)) throw Error("Unknown authored prefab edit target.");
    return target;
  }
  return Object.freeze({
    scene,
    readonly: identifier => targets.has(identifier),
    editTarget,
    authoredNode: identifier => authored.get(editTarget(identifier)),
  });
}
export function prefabRuntimeScene(snapshot) { return createPrefabProjection(snapshot).scene; }
export function prefabReadonly(snapshot, identifier) { return createPrefabProjection(snapshot).readonly(identifier); }
export function prefabEditTarget(snapshot, identifier) { return createPrefabProjection(snapshot).editTarget(identifier); }
export function prefabAuthoredNode(snapshot, identifier) { return createPrefabProjection(snapshot).authoredNode(identifier); }
