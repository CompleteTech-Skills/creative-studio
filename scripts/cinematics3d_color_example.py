#!/usr/bin/env python3
"""Real OCIO numeric patch proof, explicitly synthetic and not a 3D render."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np
from PIL import Image
import cinematics3d_color as color


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path, value):
    path.write_text(json.dumps(value, indent=2, allow_nan=False)+'\n')


def run(profile, output, *, retain_profile=False):
    inventory = color.capture_config(profile)
    capabilities = color.inspect_config(profile, inventory)
    probe = color.probe_scene_linear(profile, inventory)
    output = Path(output).absolute()
    if any(path.is_symlink() for path in [output, *output.parents]):
        raise ValueError('example output cannot traverse symlinks')
    output.mkdir()
    if retain_profile:
        with color.snapshot_config(profile, inventory) as snapshot:
            shutil.copytree(snapshot, output/'profile')
    _json(output/'config-inventory.json', inventory)
    _json(output/'capabilities.json', capabilities)
    _json(output/'scene-linear-probe.json', probe)
    values = np.array([[0., 0., 0., 1.], [.0031308]*3+[1.], [.18]*3+[1.], [1., 1., 1., 1.],
                       [4., 2., .5, 1.], [16., 8., 2., 1.], [1., 0., 0., 1.], [0., 1., 0., 1.],
                       [0., 0., 1., 1.], [.18, .4, 2., .25], [0., 0., 0., 0.]], dtype=np.float32)
    np.save(output/'input.npy', values)
    source_manifest = {'kind': 'synthetic-color-patch-source', 'schema_version': 1,
                       'origin': 'synthetic-engineering-fixture', 'image': 'input.npy',
                       'image_sha256': _sha(output/'input.npy'), 'primaries': probe['primaries'],
                       'working_space': probe['working_space'], 'alpha_mode': 'straight',
                       'transfer': 'linear', 'view_applied': False, 'exposure_applied': False}
    _json(output/'source-manifest.json', source_manifest)
    source = {'kind': 'cinematics-color-source', 'schema_version': 1,
              'config_closure_sha256': inventory['closure_sha256'], 'working_space': probe['working_space'],
              'primaries': probe['primaries'], 'transfer': 'linear', 'alpha_mode': 'straight',
              'view_applied': False, 'exposure_applied': False,
              'source_manifest_sha256': _sha(output/'source-manifest.json'), 'source_artifact_sha256': _sha(output/'input.npy')}
    _json(output/'source-context.json', source)
    actual, executions = {}, {}
    selections = [('linear-master', {'target': 'scene-linear', 'display': '', 'view': ''}),
                  ('standard', {'view': 'Standard'}), ('agx', {'view': 'AgX'}),
                  ('filmic', {'view': 'Filmic'}), ('exposure-plus-one', {'view': 'Standard', 'exposure_stops': 1.})]
    views = {item['name'] for display in capabilities['displays'] if display['name'] == 'sRGB' for item in display['views']}
    for name, settings in selections:
        if settings.get('view') and settings['view'] not in views:
            executions[name] = {'status': 'unsupported', 'reason': 'view not present in actual config'}
            continue
        plan = color.make_plan(profile, inventory, source, **settings)
        pixels, execution = color.transform_pixels(profile, plan, values)
        np.save(output/(name+'.npy'), pixels)
        _json(output/(name+'-plan.json'), plan)
        _json(output/(name+'-execution.json'), execution)
        # This small engineering preview deliberately clamps and quantizes;
        # original float pixels/masters above are retained unchanged.
        preview = np.rint(np.clip(pixels, 0., 1.)*255).astype(np.uint8)
        preview = np.repeat(np.repeat(preview[None, :, :], 32, axis=0), 32, axis=1)
        Image.fromarray(preview).save(output/(name+'-patches.png'))
        actual[name] = pixels
        executions[name] = {'status': 'executed', 'clamped_rgb_values_for_preview': execution['rgb_below_zero']+execution['rgb_above_one'],
                            'float_artifact': name+'.npy', 'execution': name+'-execution.json'}
    if not np.array_equal(actual['linear-master'], values):
        raise ValueError('zero-exposure linear master changed source pixels')
    report = {'kind': 'cinematics-color-numeric-proof', 'schema_version': 1,
              'origin': 'synthetic-engineering-fixture', 'config_closure_sha256': inventory['closure_sha256'],
              'source_context_sha256': color.object_hash(source), 'ocio_version': color.OCIO_VERSION,
              'numpy_version': np.__version__, 'selections': executions,
              'input_hdr_max': float(values[:, :3].max()), 'linear_master_hdr_max': float(actual['linear-master'][:, :3].max()),
              'standard_gray': float(actual['standard'][2, 0]),
              'gray_srgb_reference': color.linear_to_srgb_reference(.18),
              'standard_gray_reference_error': abs(float(actual['standard'][2, 0])-color.linear_to_srgb_reference(.18)),
              'exposure_plus_one_gray': float(actual['exposure-plus-one'][2, 0]),
              'view_mean_absolute_differences': {name: float(np.abs(pixels[:, :3]-actual['standard'][:, :3]).mean())
                                                 for name, pixels in actual.items() if name in ('agx', 'filmic')},
              'native_render_executed': False, 'native_source_alpha_or_baked_state_proven': False,
              'visual_approval': 'not-performed', 'hdr_video_supported': False,
              'profile_retained': retain_profile}
    _json(output/'proof.json', report)
    _json(output/'artifact-hashes.json', {path.relative_to(output).as_posix(): _sha(path)
                                        for path in sorted(output.rglob('*')) if path.is_file()})
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--retain-profile', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.profile, args.output, retain_profile=args.retain_profile), indent=2, allow_nan=False))
