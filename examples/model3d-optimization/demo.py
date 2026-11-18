#!/usr/bin/env python3
"""Execute actual LOD generation/export/reimport; no generated-art claim."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from model3d_geometry import sphere
from model3d_optimization import generate_lods,export_lods
from rig_models import Geometry
from rig_import import load_character_model
p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',type=Path,required=True);a=p.parse_args()
geometry=Geometry([sphere(.5,segments=24,rings=12,name='fixture-head')],[dict(name='clay',pbrMetallicRoughness=dict(baseColorFactor=[.7,.2,.1,1.],metallicFactor=0.,roughnessFactor=.65))])
result=generate_lods(geometry,cell_sizes=[.08,.16],source_version='procedural-engineering-fixture-v1',regenerate_normals=True,max_distance=.15,min_silhouette_iou=.85,budgets={'triangles':500,'vertices':260})
export_lods(result,a.destination)
counts=[]
for i,level in enumerate(result.levels):
    imported=load_character_model(a.destination/f'lod{i}'/'model.glb')
    assert imported.geometry.triangle_count==level.triangle_count
    counts.append(dict(level=i,vertices=imported.geometry.vertex_count,triangles=imported.geometry.triangle_count))
receipt=dict(fixture=True,artistic_approval=False,levels=counts,files={str(p.relative_to(a.destination)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(a.destination.rglob('*')) if p.is_file()})
(a.destination/'acceptance.json').write_text(json.dumps(receipt,indent=2)+'\n');print(json.dumps(receipt))
