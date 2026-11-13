#!/usr/bin/env python3
"""Construct, reopen and query real persisted room geometry; retain hash receipt."""
import argparse
import hashlib
import json
from pathlib import Path

from scene3d_contracts import load_project
from scene3d_environment import create_environment, load_meshes
from scene3d_graph import SceneGraph
from scene3d_spatial import SpatialIndex


def demonstrate(destination):
    root = Path(destination)
    create_environment(root,dict(id='rooms',version=1,seed=1,generator='rooms',parameters=dict(rooms=2),budget=30))
    bundle = load_project(root)
    scene_path = bundle['project']['scenes'][-1]['path']
    graph = SceneGraph.from_bundle(bundle,scene_path)
    index = SpatialIndex(graph,load_meshes(root,scene_path))
    report = dict(scene=scene_path,scene_sha256=hashlib.sha256((root/scene_path).read_bytes()).hexdigest(),
                  graph_sha256=graph.digest,geometry={p:r for p,r in bundle['inventory'].items() if p.endswith('.obj')},
                  door_ray=index.raycast([0,1,0],[1,0,0]),wall_ray=index.raycast([0,2.6,0],[1,0,0]),
                  ground=index.ground([0,2.8,0]),
                  contract='canonical Y-up world metres; exact base-pose triangles; no native render claim')
    if (report['door_ray']['node'],report['wall_ray']['node'],report['ground']['node']) != (
            'rooms-partition-2','rooms-partition-1','rooms-floor-0'):
        raise RuntimeError('Actual persisted geometry query mismatch.')
    receipt = root.parent/(root.name+'-spatial-receipt.json')
    receipt.write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    return report


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True,type=Path)
    print(json.dumps(demonstrate(parser.parse_args().destination),sort_keys=True))
