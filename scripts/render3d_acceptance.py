"""Reinspect real render outputs and build bounded, source-bound preview evidence.

This module does not grant scene or artistic approval. It reuses the independent
pixel verifier and reports measured image changes without inferring their cause.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io
from pathlib import Path
import shutil
import tempfile

from atomic_publish import _rename_no_replace
from render3d_contracts import Render3DError, require
from rig_errors import atomic_json, safe_path, sha256


def inspect_render(request, result, render_root, snapshot_root, destination, *, expected_backend):
    """Independently decode the full sequence and retain actual sampled previews.

    The caller supplies the independently observed executed backend identity.
    Image verification cannot prove backend execution; retained worker evidence
    and execution tests remain necessary.
    """
    from PIL import Image, ImageDraw
    from render3d_verify import verify_result
    render_root, destination = Path(render_root), Path(destination).absolute()
    require(not destination.exists() and not destination.is_symlink(), 'ownership',
            'Use a new evidence destination.')
    require(not destination.parent.resolve().joinpath(destination.name).is_relative_to(render_root.resolve()),
            'ownership', 'Evidence must be outside the immutable render output.')
    verification = verify_result(request, result, render_root,
        snapshot_root=snapshot_root, expected_backend=expected_backend)
    combined = sorted((item for item in result['images'] if item['pass_name'] == 'combined'),
                      key=lambda item:item['frame'])
    selected = sorted({0, len(combined)//2, len(combined)-1})
    require(combined, 'incomplete-output', 'No verified combined images available.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.render-evidence-', dir=destination.parent))
    try:
        previews, sampled = [], []
        for index in selected:
            record = combined[index]
            if record['format'] != 'png':
                continue
            reference = record['file']
            require(reference['bytes'] <= 64*1024**2, 'system-memory',
                    'Preview source exceeds the 64 MiB encoded-image budget.')
            require(record['resolution'][0]*record['resolution'][1] <= 16*1024**2,
                    'system-memory', 'Preview source exceeds the 16-megapixel decoding budget.')
            path = safe_path(render_root, reference['path'], exists=True)
            # Rebind preview decoding to exactly the independently verified bytes.
            with path.open('rb') as stream:
                payload = stream.read(reference['bytes'] + 1)
            require(len(payload) == reference['bytes']
                    and hashlib.sha256(payload).hexdigest() == reference['sha256'],
                    'stale-asset', 'Preview source changed after independent verification.')
            with Image.open(io.BytesIO(payload)) as image:
                image.load()
                rgba = image.convert('RGBA')
                extrema = rgba.getextrema()
                pixel_sha = hashlib.sha256(rgba.tobytes()).hexdigest()
                rgba.thumbnail((384, 384))
                preview = Image.new('RGB', (384, 408), '#30343b')
                preview.paste(rgba, ((384-rgba.width)//2, (384-rgba.height)//2), rgba)
                ImageDraw.Draw(preview).text((8,389), 'Frame ' + str(record['frame']), fill='white')
                name = f'frame-{record["frame"]:06d}.png'
                preview.save(stage/name)
                previews.append(preview)
                sampled.append(dict(frame=record['frame'], source=deepcopy(reference),
                    decoded_rgba_sha256=pixel_sha, rgba_extrema=extrema,
                    preview=dict(path=name, sha256=sha256(stage/name))))
        if previews:
            sheet=Image.new('RGB',(384*len(previews),408),'#30343b')
            for index,preview in enumerate(previews):sheet.paste(preview,(384*index,0))
            sheet.save(stage/'contact-sheet.png')
        report=dict(schema_version=1,kind='render3d-acceptance-inspection',
            verification=verification,sampled=sampled,
            sampled_pixel_change=len({item['decoded_rgba_sha256'] for item in sampled})>1,
            limitation='Pixel change does not alone prove camera, skeletal, material or lighting correctness. No visual approval is granted.')
        if previews:report['contact_sheet']=dict(path='contact-sheet.png',sha256=sha256(stage/'contact-sheet.png'))
        atomic_json(stage/'inspection.json',report)
        _rename_no_replace(stage,destination)
        return report
    finally:
        if stage.exists():shutil.rmtree(stage)
