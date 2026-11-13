"""Reproducible rendered LookDev experiments; never an aesthetic approval."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from lookdev_blender import material_plan
from lookdev_contracts import Registry, canonical_bytes
from rig_errors import require

ROOT = Path(__file__).resolve().parents[1]


def sweep_documents():
    """Nine independent canonical resources; all factors except M/R are fixed."""
    template = json.loads((ROOT / 'examples/lookdev-contracts/ceramic.json').read_text())
    result = []
    for metallic in (0.0, 0.5, 1.0):
        for roughness in (0.1, 0.5, 0.9):
            value = json.loads(json.dumps(template))
            value['id'] = f'm{metallic:g}-r{roughness:g}'
            value['surface'].update(metallic=metallic, roughness=roughness,
                base_color=[0.12, 0.06, 0.025, 1], double_sided=True)
            value['provenance']['tool'] = 'lookdev-laboratory-sweep-v1'
            result.append(value)
    return result


def highlight_metrics(image_path):
    """Display-referred luminance statistics on the alpha-covered sphere.

    Baseline-subtracted half-height area and weighted radius measure the shape
    of a reflected emitter; peak and mean are separate and never imply approval.
    """
    import numpy as np
    from PIL import Image
    with Image.open(image_path) as image:
        data = np.asarray(image.convert('RGBA'), dtype=np.float64) / 255
    mask = data[:, :, 3] > .99
    require(int(mask.sum()) > 100, 'LOOKDEV_LAB_IMAGE', 'Insufficient opaque object pixels.')
    rgb = data[:, :, :3]
    luminance = rgb @ np.array([.2126, .7152, .0722])
    values = luminance[mask]
    baseline = float(np.percentile(values, 25))
    peak = float(np.percentile(values, 99.9))
    require(peak > baseline + 1e-4, 'LOOKDEV_LAB_HIGHLIGHT', 'No measurable highlight contrast.')
    excess = np.maximum(luminance - baseline, 0) * mask
    y, x = np.indices(mask.shape)
    weight = excess.sum()
    cx, cy = float((x * excess).sum() / weight), float((y * excess).sum() / weight)
    radius = float((((x - cx) ** 2 + (y - cy) ** 2) * excess).sum() / weight) ** .5
    return {'opaque_pixels': int(mask.sum()), 'luminance_mean': float(values.mean()),
            'luminance_peak_99_9': peak, 'luminance_baseline_25': baseline,
            'half_height_pixels': int(((luminance > baseline + (peak-baseline)*.5) & mask).sum()),
            'excess_rms_radius_pixels': radius}


def measure_sweep(destination):
    """Verify mapping, controlled inputs and actual pixel differences independently."""
    from PIL import Image, ImageChops, ImageStat
    root = Path(destination)
    report = json.loads((root / 'render-report.json').read_text())
    documents = sweep_documents()
    require(set(report['materials']) == {x['id'] for x in documents},
            'LOOKDEV_LAB_REPORT', 'Expected every nine-cell material.')
    measurements, comparisons = {}, {}
    for value in documents:
        identifier = value['id']
        sockets = report['materials'][identifier]
        for field in ('metallic', 'roughness'):
            require(abs(sockets[field] - value['surface'][field]) < 1e-6,
                    'LOOKDEV_LAB_MAPPING', 'Rendered material socket mismatch.')
        measurements[identifier] = highlight_metrics(root / (identifier + '.png'))
    for metallic in (0, .5, 1):
        for first, second in ((.1, .5), (.5, .9)):
            a, b = f'm{metallic:g}-r{first:g}', f'm{metallic:g}-r{second:g}'
            with Image.open(root / (a+'.png')) as ai, Image.open(root / (b+'.png')) as bi:
                mean = sum(ImageStat.Stat(ImageChops.difference(ai.convert('RGB'), bi.convert('RGB'))).mean) / 3
            require(mean > .5, 'LOOKDEV_LAB_EFFECT', 'Roughness did not measurably alter rendered pixels.')
            comparisons[a+'/'+b] = {'display_rgb_mae_255': mean}
    # End-point controls have the strongest contrast and avoid claiming every
    # noisy intermediate percentile is monotonic. With fixed geometry and area
    # emitter, a .1 surface must reflect a narrower/brighter lobe than .9.
    for metallic in (0, .5, 1):
        sharp = measurements[f'm{metallic:g}-r0.1']
        broad = measurements[f'm{metallic:g}-r0.9']
        require(sharp['luminance_peak_99_9'] > broad['luminance_peak_99_9'] * 1.05,
                'LOOKDEV_LAB_HIGHLIGHT', 'Lower roughness did not strengthen the highlight by 5%.')
        require(broad['half_height_pixels'] > sharp['half_height_pixels'] * 1.1,
                'LOOKDEV_LAB_HIGHLIGHT', 'Higher roughness did not broaden the half-height area by 10%.')
    for roughness in (.1, .5, .9):
        a,b=f'm0-r{roughness:g}',f'm1-r{roughness:g}'
        with Image.open(root/(a+'.png')) as ai, Image.open(root/(b+'.png')) as bi:
            mean=sum(ImageStat.Stat(ImageChops.difference(ai.convert('RGB'),bi.convert('RGB'))).mean)/3
        require(mean>.5,'LOOKDEV_LAB_EFFECT','Metallic did not measurably alter rendered pixels.')
        comparisons[a+'/'+b]={'display_rgb_mae_255':mean}
    return {'measurements': measurements, 'comparisons': comparisons,
            'interpretation': 'Display-referred sphere statistics; no cross-engine or aesthetic approval.',
            'unverified': ['full demonstrations A,C,D,E,F,G,H,I,J', 'browser comparison', 'production art approval']}


def run_sweep(destination, blender):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=False)
    # Retain the exact trusted Python/schema inputs and execute their fixed
    # fixture from this snapshot. Detect mutation across the host planning step.
    source_files = sorted((ROOT/'scripts').glob('*.py')) + sorted((ROOT/'profiles').glob('*.json')) + [
        ROOT/'examples/lookdev-contracts/ceramic.json', ROOT/'tests/fixtures/lookdev_laboratory_sweep.py']
    source_payloads = {str(p.relative_to(ROOT)): p.read_bytes() for p in source_files}
    source_hashes = {name: hashlib.sha256(payload).hexdigest() for name, payload in source_payloads.items()}
    snapshot = destination/'source'
    for name, payload in source_payloads.items():
        target=snapshot/name; target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(payload)
    values = sweep_documents()
    registry = Registry([(x['id']+'.json', canonical_bytes(x)) for x in values])
    for value in values:
        (destination/(value['id']+'.material.json')).write_bytes(canonical_bytes(value))
    plans = [material_plan(registry, registry.reference(x['id'], x['version'])) for x in values]
    job = {'scripts': str(snapshot/'scripts'), 'destination': str(destination), 'plans': plans}
    (destination/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    command=[str(Path(blender).resolve()),'--background','--factory-startup','--disable-autoexec',
             '--threads','1','--python-exit-code','1','--python',str(snapshot/'tests/fixtures/lookdev_laboratory_sweep.py'),
             '--',str(destination/'job.json')]
    start=time.monotonic()
    environment = dict(os.environ)
    temporary_root = Path(environment.get('HATCH_LOOKDEV_LAB_TMPDIR', str(destination.parent/'.lookdev-lab-tmp')))
    temporary = temporary_root/destination.name
    temporary.mkdir(parents=True,exist_ok=False)
    environment['TMPDIR'] = str(temporary)
    result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900,env=environment)
    (destination/'render.log').write_bytes(result.stdout)
    require(result.returncode==0,'LOOKDEV_LAB_RENDER','Trusted Blender sweep failed; inspect retained render.log.')
    require(all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest for name,digest in source_hashes.items()),
            'LOOKDEV_LAB_SOURCE_CHANGED', 'Source changed during planning/rendering; retained snapshot is diagnostic evidence only.')
    measures=measure_sweep(destination)
    measures['render_elapsed_seconds']=time.monotonic()-start
    measures['command']=command
    measures['source_sha256']=source_hashes
    measures['temporary_directory']=str(temporary)
    (destination/'measurements.json').write_text(json.dumps(measures,indent=2)+'\n')
    manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(destination.iterdir()) if p.is_file()}
    (destination/'sha256.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return measures


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--blender',required=True,type=Path)
    args=parser.parse_args()
    run_sweep(args.output,args.blender)
    print(args.output/'measurements.json')

if __name__=='__main__': main()
