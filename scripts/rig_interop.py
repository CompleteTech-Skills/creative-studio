"""Independent glTF validator adapter and measured interchange comparisons."""
from pathlib import Path
import os
import shutil
import subprocess
import numpy as np
from rig_errors import RigError, decode_json, require, sha256


def validate_gltf(path,*,timeout=120):
    node=shutil.which('node');require(node is not None,'RIG_NODE_MISSING','Install Node for the independent Khronos validator.')
    command=[node,str(Path(__file__).with_name('rig_validate_gltf.cjs')),str(Path(path).absolute())]
    try:result=subprocess.run(command,capture_output=True,text=True,timeout=timeout,check=False)
    except subprocess.TimeoutExpired as exc:raise RigError('RIG_VALIDATOR_TIMEOUT','Independent glTF validation exceeded its timeout.',retryable=True) from exc
    require(bool(result.stdout.strip()),'RIG_VALIDATOR_MISSING','Install optional tools/rig dependencies before independent validation.',observed=result.stderr[-4096:])
    report=decode_json(result.stdout);report['asset_sha256']=sha256(path)
    report['ok']=result.returncode==0 and report['issues']['numErrors']==0
    return report


def compare_imported(rig,imported,*,sample_times=(.17,.53,.87),memory_bytes=None,maximum_work=5000000):
    """Bounded native identity/deformation diagnostic, not independent acceptance.

    Preserves oriented face multiplicity, corner fields and resolved material
    bindings across vertex/face/material-resource reindexing. Format changes may
    alter image encoding only when decoded RGBA pixels remain exactly equal.
    Rules and limitations: references/rig-roundtrip-identity.md.
    """
    from rig_models import RigAsset
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_interop_identity import compare_identity,corner_records,loose_records
    from rig_surface_compare import match_rows
    from copy import deepcopy
    require(type(maximum_work) is int and maximum_work>0,'RIG_ROUNDTRIP_COMPARE_LIMIT','Comparison work budget must be a positive integer.')
    memory_bytes=rig.manifest.get('limits',{}).get('memory_bytes',2*1024**3) if memory_bytes is None else memory_bytes
    require(type(memory_bytes) is int and memory_bytes>0,'RIG_ROUNDTRIP_COMPARE_LIMIT','Comparison memory budget must be a positive integer.')
    require(isinstance(sample_times,(tuple,list)) and len(sample_times)<=64
            and all(type(value) in (int,float) and np.isfinite(value) and 0<=value<=1 for value in sample_times),
            'RIG_ROUNDTRIP_TIME','Sample fractions must be a bounded finite list in0..1.')
    identity=compare_identity(rig.geometry,imported.geometry,memory_bytes=memory_bytes,maximum_work=maximum_work)
    manifest=deepcopy(rig.manifest);manifest['skeleton']=imported.skeleton
    manifest['controllers']=dict(ik=[],constraints=[])
    other=RigAsset(manifest,imported.geometry,imported.weights,imported.clips)
    require(rig.clips.keys()==other.clips.keys(),'RIG_ROUNDTRIP_CLIP','Round trip changed the named animation inventory.')
    maximum=0.;samples=0;remaining_work=maximum_work-identity['work']
    def compare_pose(a,b):
        nonlocal maximum,samples,remaining_work
        for mesh in rig.geometry.meshes:
            other_mesh=other.geometry.by_id()[mesh.id]
            expected,width=corner_records(mesh,positions=a[mesh.id]['positions'],memory_bytes=memory_bytes,attributes=False)
            actual,_=corner_records(other_mesh,positions=b[mesh.id]['positions'],memory_bytes=memory_bytes,attributes=False)
            report=match_rows(expected,actual,tolerance=.01,position_tolerance=.01,cyclic_width=width,
                              memory_bytes=memory_bytes,maximum_work=remaining_work)
            remaining_work-=report['work'];maximum=max(maximum,report['max_position_error']);samples+=1
            loose=match_rows(loose_records(mesh,positions=a[mesh.id]['positions'],memory_bytes=memory_bytes,attributes=False),
                             loose_records(other_mesh,positions=b[mesh.id]['positions'],memory_bytes=memory_bytes,attributes=False),
                             tolerance=.01,position_tolerance=.01,memory_bytes=memory_bytes,maximum_work=remaining_work)
            remaining_work-=loose['work'];maximum=max(maximum,loose['max_position_error'])
    compare_pose(evaluate_deformation(rig),evaluate_deformation(other))
    for name,clip in rig.clips.items():
        for fraction in sample_times:
            time=fraction*clip['duration']
            compare_pose(evaluate_deformation(rig,evaluate_animation(rig,clip,time)['pose']),
                         evaluate_deformation(other,evaluate_animation(other,other.clips[name],time)['pose']))
    return dict(independent=False,reader='hatch-native',max_vertex_error_m=maximum,mesh_samples=samples,
                tolerance_m=.01,identity=identity,static_rest_sample=True,
                comparison_work=maximum_work-remaining_work,ok=maximum<=.01,
                limitations=['Native decoder diagnostic; independent Blender/Three.js and visual identity review remain separate.',
                             'Geometrically degenerate faces have no measurable winding; their multiplicity is retained and counts are explicit.',
                             'Unsampled animation times/events and authoring/bone metadata are not structurally certified by this diagnostic.'])
