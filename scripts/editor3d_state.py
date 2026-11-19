"""Saved native graph state for the browser; no client constraint/pivot solver."""
from scene3d_graph import SceneGraph
from rig_errors import require


def project_editor_state(snapshot, root):
    scene = snapshot['scene']
    path = snapshot.get('scene_path')
    if path is None:
        matches = [name for name, document in snapshot['documents'].items()
                   if document.get('kind') == 'scene3d-scene' and document['id'] == scene['id'] and document['version'] == scene['version']]
        require(len(matches) == 1, 'EDITOR3D_VIEW_STATE', 'Select one exact saved native scene.')
        path = matches[0]
    graph = SceneGraph.from_bundle(snapshot, path, source_root=root)
    evaluated = graph.evaluate_base()
    readonly = {}
    for node in scene['nodes']:
        if node['constraints']:
            readonly[node['id']] = 'Transform is controlled by native constraints.'
        elif 'attachment' in node:
            readonly[node['id']] = 'Transform is controlled by a native attachment.'
        elif any(value != 0 for value in node['transform'].get('pivot', [])):
            readonly[node['id']] = 'Pivot transform editing is not available in this viewport.'
    return dict(schema_version=1, available=True, revision=snapshot['revision'], sha256=snapshot['sha256'],
                nodes=evaluated, transform_readonly=readonly, base_pose_only=True, animation_evaluated=False)
