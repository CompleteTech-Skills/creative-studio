#!/usr/bin/env python3
"""Retain real CPU-rendered asset preview evidence from procedural fixtures."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path

import numpy as np
from PIL import Image

from asset_library import AssetLibrary, file_record
from asset_library_contracts import seal
from asset_library_preview import create_preview, verify_preview
from rig_demo import create_demo, ellipsoid
from rig_errors import RigError, atomic_json
from rig_gltf import export_geometry, export_gltf
from rig_models import Geometry, load_rig

ROOT=Path(__file__).resolve().parents[1]


def demonstrate(output, executable):
    output=Path(output).absolute()
    output.mkdir(parents=True,exist_ok=False)
    library=AssetLibrary(output/'library',create=True)
    license=output/'license.txt'
    license.write_text('Procedural engineering fixtures; CC0-1.0. Not production artwork.\n')

    def register(source, version):
        manifest=json.loads((ROOT/'examples/asset-library-foundation/manifest.json').read_text())
        files=[file_record(source,'runtime/asset.glb','runtime'),file_record(license,'source/license.txt','license')]
        manifest.update(version=version,files=files)
        manifest['provenance']['sources']=[files[0]]
        manifest['provenance']['license_evidence']=[files[1]]
        manifest['publication']['inventory']=[item['path'] for item in files]
        return library.register(seal(manifest),{'runtime/asset.glb':source,'source/license.txt':license})

    mesh=ellipsoid('sphere',[0,0,0],[1,.8,1],0,latitudes=6,longitudes=10)
    mesh.attributes['TEXCOORD_0']=np.column_stack(((mesh.positions[:,0]+1)/2,(mesh.positions[:,1]+1)/2))
    geometry=Geometry([mesh],[dict(pbrMetallicRoughness=dict(baseColorFactor=[.7,.1,.05,1],metallicFactor=.1,roughnessFactor=.4))])
    source=output/'sphere.glb';export_geometry(geometry,source,provenance=dict(fixture=True))
    reference=register(source,'1.0.0')
    cache=output/'previews';records={}

    def preview(name, ref, **config):
        records[name]=create_preview(library,ref,cache,config=dict(width=64,height=64,samples=2,**config),executable=executable)
        verify_preview(cache/records[name]['key'])
        return records[name]

    preview('source',reference)
    preview('material',reference,mode='material',material=dict(base_color=[0,1,0,1],metallic=0,roughness=.5))
    changed=deepcopy(geometry);changed.meshes[0].positions[:,0]*=.3
    changed_source=output/'changed.glb';export_geometry(changed,changed_source,provenance=dict(fixture=True))
    preview('geometry',register(changed_source,'1.0.1'))
    create_demo(output/'robot','robotic',fps=4)
    robot_source=output/'robot.glb';export_gltf(load_rig(output/'robot'),robot_source)
    robot=register(robot_source,'1.0.2')
    preview('motion',robot,mode='motion',clip='hinge-sweep',times=['0','1/2'])
    preview('skeleton',robot,mode='skeleton')

    def pixels(name,index=0):
        record=records[name]
        with Image.open(cache/record['key']/record['images'][index]['path']) as image:
            return np.asarray(image).astype(float)

    metrics=dict(material_mean_pixel_delta=float(np.abs(pixels('source')-pixels('material')).mean()),
                 geometry_mean_pixel_delta=float(np.abs(pixels('source')-pixels('geometry')).mean()),
                 motion_mean_pixel_delta=float(np.abs(pixels('motion')-pixels('motion',1)).mean()))
    if metrics['material_mean_pixel_delta']<=1 or metrics['geometry_mean_pixel_delta']<=1 or metrics['motion_mean_pixel_delta']<=.01:
        raise RigError('LIBRARY_PREVIEW_DEMO','Actual preview differences did not meet fixture thresholds.')
    result=dict(schema_version=1,fixture=True,visual_review='not-reviewed',records=records,metrics=metrics,
                limitations=['CPU Cycles only','Procedural engineering fixtures; no artistic acceptance'])
    atomic_json(output/'acceptance.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--blender',required=True)
    args=parser.parse_args()
    result=demonstrate(args.output,args.blender)
    print(json.dumps(dict(path=str(args.output/'acceptance.json'),metrics=result['metrics']),sort_keys=True))


if __name__=='__main__':main()
