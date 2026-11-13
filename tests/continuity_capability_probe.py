#!/usr/bin/env python3
"""Retain real bounded capability-admission evidence; no artistic approval.

Use a new destination. Copies a labeled retained robotic engineering fixture,
executes existing native inspection/FK/skin/CPU rendering, and records all
thirteen requested capability outcomes including genuinely unavailable owners.
No Blender, FFmpeg, GUI, model provider or child process is invoked.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from continuity_capabilities import Admission, MATRIX, Policy, discover
from continuity_capability_owners import RigOwner, ImageMastersOwner, pointer
from continuity_contracts import VERSION, canonical_bytes


def hashes(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file()}


def run(destination):
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Use a new destination; existing evidence is never overwritten.')
    destination.mkdir(parents=True,exist_ok=False)
    shutil.copytree(ROOT/'examples/rig-3d/robotic',destination/'rig')
    (destination/'evidence').mkdir()
    (destination/'masters').mkdir()
    (destination/'project.json').write_bytes(canonical_bytes(dict(kind='capability-probe-project',
        schema_version=VERSION,fixture=True,production_artwork=False,visual_approval=False,
        source='Retained robotic engineering fixture; no historical artwork restoration claim.')))
    before=hashes(destination/'rig')
    owners=[RigOwner(destination,'rig/rig.json','evidence',cap) for cap in RigOwner.SUPPORTED]
    reference=next(o for o in owners if o.capability=='visual-reference')
    png=next(p for p in reference.snapshot()['evidence'] if p['path'].endswith('.png'))
    shutil.copyfile(destination/png['path'],destination/'masters/frame-000000.png')
    masters=ImageMastersOwner(destination,['masters/frame-000000.png'],'evidence')
    owners.append(masters)
    project=pointer(destination,destination/'project.json',kind='project-revision')
    rig=owners[0].bound
    request=dict(schema_version=VERSION,kind='continuity-capability-request',request_id='native-fixture-probe',
        project_revision=project,checks=[])
    for i,cap in enumerate(MATRIX):
        image=cap=='video-encoding'
        request['checks'].append(dict(id='check-'+str(i),capability=cap,schema_version=VERSION,
            inputs=masters.images if image else [rig],
            input_types=['image-master'] if image else ['hatch-3d-rig'],
            output_types=['verified-image-masters'] if image else ['native-inspection'],
            resources=dict(memory_bytes=128*1024*1024,disk_bytes=8*1024*1024,cpu_seconds=5,gpu_bytes=0,concurrency=1),
            required_fields=[],allow_fallback=True,gpu_required=False,
            needs_visual_approval=cap in ('visual-reference','visual-comparison')))
    policy=Policy(asset_rights=True,allowed_owners=frozenset(['hatch-rig','hatch-image-masters']),
        max_cpu_seconds=120,max_disk_bytes=128*1024*1024)
    result=Admission(destination,policy=policy,owners=owners).run(request)
    after=hashes(destination/'rig')
    if before!=after:raise AssertionError('Native fixture bytes changed during read-only admission.')
    if result['visual_approval'] or result['integrity']!='verified':
        raise AssertionError('Admission integrity/approval contract failed.')
    if any(c['outcome']=='failed' for c in result['checks']):
        raise AssertionError('Unexpected native owner failure: '+json.dumps(result['checks']))
    rows={c['capability']:c['outcome'] for c in result['checks']}
    expected={'geometry-inspection':'partial','scene-animation-evaluation':'partial',
        'rig-compatibility':'partial','visual-reference':'needs_visual_review',
        'gpu-rendering':'supported_fallback','video-encoding':'partial'}
    for cap in MATRIX:
        if rows[cap]!=expected.get(cap,'blocked_missing_capability'):
            raise AssertionError('Incorrect outcome for '+cap+': '+rows[cap])
    from importlib.metadata import version
    receipt=dict(schema_version=VERSION,kind='continuity-capability-probe-receipt',fixture=True,
        production_artwork_approval=False,python=platform.python_version(),
        packages={n:version(n) for n in ('numpy','Pillow','jsonschema')},
        executed_backend='native-rig-cpu / native-image-masters',child_backend_processes=0,
        request=request,result=result,discovery=discover(),protected_native_before=before,
        protected_native_after=after,source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT/'scripts').glob('*.py'))},limitations=[
            'Native rig adapter is not direct GLB/glTF parsing and cannot recover original node hierarchy.',
            'No Blender/physics/evaluated-scene/GUI/LLM or video encoder execution.',
            'CPU image is an engineering reference, not PBR or human aesthetic acceptance.',
            'Missing native owners and incomplete fields remain explicitly unsatisfied.'])
    (destination/'report.json').write_bytes(canonical_bytes(receipt))
    print(json.dumps(dict(destination=str(destination),outcomes=rows,source_files_unchanged=len(before),
                         visual_approval=False,report_sha256=hashlib.sha256((destination/'report.json').read_bytes()).hexdigest()),indent=2))
    return receipt


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    run(args.destination)
