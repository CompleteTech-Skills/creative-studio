"""Verify committed authoring examples, exact native scene bytes and relocation.

No Blender is started and no image, movie, effect execution or visual approval
is manufactured. The retained Scene3D triangle is an engineering fixture.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import json
import tempfile
from cinematics3d_contracts import Registry, admission, canonical_bytes
from scene3d_contracts import load_project

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path('examples/cinematics3d')


def verify(root=ROOT):
    root=Path(root)
    native=load_project(root/'examples/scene3d-contracts')
    paths=sorted((root/DIRECTORY).glob('*.json'))
    documents=[(path.relative_to(root).as_posix(),path.read_bytes()) for path in paths]
    external={('examples/scene3d-contracts/'+name):(root/'examples/scene3d-contracts'/name).read_bytes()
              for name in ('example-shot.json','example-scene.json')}
    registry=Registry(documents,external=external)
    effects=registry.effective_stack(registry.reference('shot-stack','1.0.0'))
    before={path:hashlib.sha256(data).hexdigest() for path,data in registry.bytes().items()}
    # Relocate and read the actual original bytes instead of rebuilding metadata.
    with tempfile.TemporaryDirectory(prefix='hatch-cinematic-contract-') as temporary:
        destination=Path(temporary)
        for path,data in registry.bytes().items():
            target=destination/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        restored=Registry([(path,(destination/path).read_bytes()) for path in before],external=external)
        after={path:hashlib.sha256(data).hexdigest() for path,data in restored.bytes().items()}
    if before != after: raise ValueError('Relocated bytes differ')
    return dict(schema_version=1,fixture=True,native_scene_status=native['status'],
                config_hashes=before,relocation='exact-byte-verified',
                effective_effect_ids=[e['id'] for e in effects],admission=admission(effects),
                rendered=False,visual_approval=False)


if __name__=='__main__': print(json.dumps(verify(),indent=2))
