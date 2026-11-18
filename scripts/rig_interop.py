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
    report = decode_json(result.stdout)
    require(isinstance(report, dict), 'RIG_VALIDATOR_REPORT', 'Independent glTF validation returned a non-object report.')
    issues = report.get('issues')
    require(isinstance(issues, dict), 'RIG_VALIDATOR_REPORT', 'Independent glTF validation returned a missing or invalid issues object.')
    report['asset_sha256'] = sha256(path)
    errors = issues.get('numErrors')
    warnings = issues.get('numWarnings')
    report['ok'] = (
        result.returncode == 0
        and issues.get('truncated') is False
        and type(errors) is int and errors == 0
        and type(warnings) is int and warnings >= 0
    )
    return report


def compare_imported(rig,imported,*,sample_times=(.17,.53,.87)):
    """Native diagnostic comparison; explicitly not an independent decoder receipt."""
    from rig_models import RigAsset
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from copy import deepcopy
    manifest=deepcopy(rig.manifest);manifest['skeleton']=imported.skeleton
    manifest['controllers']=dict(ik=[],constraints=[])
    other=RigAsset(manifest,imported.geometry,imported.weights,imported.clips)
    maximum=0.;samples=0
    for name,clip in rig.clips.items():
        require(name in other.clips,'RIG_ROUNDTRIP_CLIP','Round trip lost a named animation.',asset=name)
        for fraction in sample_times:
            time=fraction*clip['duration']
            a=evaluate_deformation(rig,evaluate_animation(rig,clip,time)['pose'])
            b=evaluate_deformation(other,evaluate_animation(other,other.clips[name],time)['pose'])
            for mesh in rig.geometry.meshes:
                require(mesh.id in b and a[mesh.id]['positions'].shape==b[mesh.id]['positions'].shape,
                        'RIG_ROUNDTRIP_TOPOLOGY','Round trip changed mesh topology.')
                maximum=max(maximum,float(np.linalg.norm(a[mesh.id]['positions']-b[mesh.id]['positions'],axis=1).max()));samples+=1
    return dict(independent=False,reader='hatch-native',max_vertex_error_m=maximum,mesh_samples=samples,
                tolerance_m=.01,ok=maximum<=.01)
