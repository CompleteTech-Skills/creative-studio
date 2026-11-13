"""Bounded continuity adapters to existing native rig and review authorities.

No new acquisition, review, rendering or job lifecycle. Optional imports occur
only at explicit probe/execution. Every source-dependent probe is input-bound.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import hashlib
import io
import platform
from pathlib import Path
import uuid

from continuity_capabilities import OwnerAdapter, MATRIX, RESOURCES
from continuity_contracts import VERSION, canonical_bytes, portable_path, _read_regular_bytes, verify_references


def pointer(root, path, *, kind='evidence'):
    relative = Path(path).relative_to(root).as_posix()
    portable_path(relative)
    data = _read_regular_bytes(path, limit=128*1024*1024)
    return dict(id='evidence-' + hashlib.sha256(data).hexdigest()[:24], version=VERSION,
                path=relative, sha256=hashlib.sha256(data).hexdigest(), kind=kind)


def _write_new(root, folder, payload, extension='json'):
    """New evidence only. Parent directory is caller-owned, not a sandbox."""
    target = root / folder / (uuid.uuid4().hex + '.' + extension)
    current = root
    for part in Path(folder).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('CONTINUITY_CAPABILITY_PATH: evidence directory symlink is forbidden')
    if target.parent.is_symlink() or not target.parent.is_dir():
        raise ValueError('CONTINUITY_CAPABILITY_PATH: use a caller-owned existing evidence directory')
    with target.open('xb') as stream:
        stream.write(payload)
    return pointer(root, target)


class RigOwner(OwnerAdapter):
    """Source-bound real rig inspection/evaluation/CPU reference rendering.

    Reuses native rig job lock/limits. This adapter does not implement scene
    constraints, calibrated camera mapping, retargeting or PBR rendering.
    """
    SUPPORTED = {
        'geometry-inspection': ('independent-geometry-parser', ('geometry', 'materials', 'skins', 'morphs', 'extensions-and-channels')),
        'scene-animation-evaluation': ('bounded-native-evaluator', ('rig-pose', 'deformed-vertices')),
        'rig-compatibility': ('exact-rig-data', ('hierarchy', 'rest-pose', 'bind-matrices', 'skin-weights')),
        'visual-reference': ('tested-geometry-renderer', ('reference-render',)),
        'gpu-rendering': ('tested-cpu-renderer', ('reference-render',)),
    }

    def __init__(self, root, rig_path, evidence_folder, capability, *, expiry_seconds=300):
        if capability not in self.SUPPORTED:
            raise ValueError('CONTINUITY_CAPABILITY_OWNER: unsupported rig capability')
        if type(expiry_seconds) is not int or not 1 <= expiry_seconds <= 3600:
            raise ValueError('CONTINUITY_CAPABILITY_EXPIRY: use 1..3600 seconds')
        self.root = Path(root).resolve(strict=True)
        self.rig_path = portable_path(rig_path)
        if Path(self.rig_path).name != 'rig.json':
            raise ValueError('CONTINUITY_CAPABILITY_SOURCE: native rig manifest must be rig.json')
        self.folder = portable_path(evidence_folder)
        self.capability = capability
        self.bound = pointer(self.root, self.root / self.rig_path, kind='hatch-3d-rig')
        self._disk_remaining = 64*1024*1024
        self._evidence = []
        # A real probe is required before advertising availability. It produces
        # the exact same semantic operation as execute, not --version output.
        with self.reserve(None):
            result = self._execute()
        self._evidence = result['evidence']
        from rig_errors import VERSION as native_version
        self._snap = dict(schema_version=VERSION, kind='continuity-capability-snapshot',
            capability_id=capability, implementation_id=self.SUPPORTED[capability][0],
            owner='hatch-rig', backend='native-rig-cpu', backend_version=native_version, contract_version=VERSION,
            input_types=['hatch-3d-rig'], output_types=['native-inspection'], operations=[capability],
            status='available', discovered=True, tested_operations=[capability], evidence=self._evidence,
            limits=dict(memory_bytes=512*1024*1024, disk_bytes=64*1024*1024,
                        cpu_seconds=60, gpu_bytes=0, concurrency=1),
            offline=True, asset_rights_required=True,
            expires_at=(datetime.now(timezone.utc)+timedelta(seconds=expiry_seconds)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            limitations=['Native rig adapter only; no general scene or PBR renderer.',
                         'CPU reference is 64x64, three-quarter view, fixed native base-color lighting.',
                         'No aesthetic approval, retarget compatibility, exact physics continuation or GUI claim.'])

    def snapshot(self): return deepcopy(self._snap)

    @contextmanager
    def reserve(self, check):
        from rig_errors import job_lock
        from rig_models import load_rig
        with job_lock((self.root/self.rig_path).parent):
            verify_references(self.root, dict(source=self.bound))
            rig = load_rig((self.root/self.rig_path).parent)
            # Native limits remain authoritative. No token or lease is forged;
            # the native owner's actual exclusive OS lock is held throughout.
            if rig.geometry.vertex_count > 100000 or rig.geometry.triangle_count > 100000:
                raise ValueError('CONTINUITY_CAPABILITY_LIMIT: bounded adapter maximum is 100000 vertices/triangles')
            if check is not None and self.bound not in check['inputs']:
                raise ValueError('CONTINUITY_CAPABILITY_SOURCE: request does not bind probed rig bytes')
            if check is not None:
                estimated_memory = 32*1024*1024 + rig.geometry.vertex_count*256 + rig.geometry.triangle_count*64
                if check['resources']['memory_bytes'] < estimated_memory:
                    raise ValueError('CONTINUITY_CAPABILITY_LIMIT: native estimated working memory exceeds reservation')
                self._disk_remaining = check['resources']['disk_bytes']
            else:
                self._disk_remaining = 64*1024*1024
            self._rig = rig
            try:
                yield
                verify_references(self.root, dict(source=self.bound))
                load_rig((self.root/self.rig_path).parent)
            finally:
                self._rig = None

    def execute(self, check): return self._execute()

    def _emit(self, payload, extension='json'):
        if len(payload) > self._disk_remaining:
            raise ValueError('CONTINUITY_CAPABILITY_LIMIT: evidence exceeds disk reservation')
        self._disk_remaining -= len(payload)
        return _write_new(self.root, self.folder, payload, extension)

    def _execute(self):
        rig = self._rig
        from rig_geometry import analyze_geometry
        from rig_skeleton import evaluate_fk, neutral_pose
        from rig_skinning import evaluate_deformation
        from rig_render import render_rig_preview
        from rig_models import rig_signature
        cap = self.capability
        import numpy as np
        import PIL
        from rig_errors import VERSION as native_version
        measurements = dict(software=dict(python=platform.python_version(),numpy=np.__version__,Pillow=PIL.__version__,native_rig=native_version),rig_sha256=rig_signature(rig.manifest), source_sha256=rig.manifest['source']['sha256'],
                            fixture=rig.manifest['source']['fixture'], visual_approval=False)
        evidence = []
        if cap == 'geometry-inspection':
            measurements['native_extensions_used'] = list(rig.geometry.extensions_used)
            measurements['source_hierarchy_status'] = 'unavailable-in-native-rig-adapter'
            measurements['supported_input'] = 'hatch-3d-rig; not a direct GLB/glTF parser'
            measurements['unsupported_channels'] = ['original-source-node-hierarchy','general-scene-constraints']
            measurements['geometry'] = analyze_geometry(rig.geometry, rig.manifest['limits'])
            measurements['materials'] = deepcopy(rig.geometry.materials)
            measurements['skins'] = {key:dict(vertices=len(value.indices), influences=value.indices.shape[1])
                                       for key,value in rig.weights.items()}
            measurements['morphs'] = {m.id:list(m.morphs) for m in rig.geometry.meshes}
        elif cap in ('scene-animation-evaluation', 'rig-compatibility'):
            if not rig.skeleton:
                raise ValueError('CONTINUITY_CAPABILITY_RIG: actual rig/skeleton is missing')
            pose = neutral_pose(rig.skeleton)
            world = evaluate_fk(rig.skeleton, pose)
            measurements['world_matrices'] = {k:v.tolist() for k,v in world.items()}
            measurements['skeleton'] = deepcopy(rig.skeleton)
            measurements['deformation'] = {k:dict(positions=v['positions'].tolist())
                for k,v in evaluate_deformation(rig, pose).items()}
            measurements['skin_bindings'] = {k:dict(joint_ids=v.joints,
                inverse_bind_matrices=v.inverse_bind.tolist(),
                joints=v.indices.tolist(), weights=v.weights.tolist()) for k,v in rig.weights.items()}
        else:
            if not rig.skeleton:
                raise ValueError('CONTINUITY_CAPABILITY_RIG: renderer needs actual rig data')
            image = render_rig_preview(rig, width=64, height=64, camera='three-quarter')
            stream = io.BytesIO(); image.save(stream, format='PNG')
            evidence.append(self._emit(stream.getvalue(), 'png'))
            measurements.update(width=64, height=64, device='cpu', renderer='native-rig-z-buffer',
                                texture_shading='base-color-only', camera='three-quarter')
        artifact = dict(schema_version=VERSION, kind='continuity-native-capability-evidence',
                        capability=cap, source=self.bound, measurements=measurements)
        evidence.append(self._emit(canonical_bytes(artifact)))
        return dict(outcome='executed', evidence=evidence, measurements=measurements,
                    verified_fields=list(self.SUPPORTED[cap][1]), limitations=[])

class ImageMastersOwner(OwnerAdapter):
    """Actual verified image decoding/master retention; never fake video export.

    The native encoder is unavailable; validated image masters remain useful but
    the requested video gate remains partial. No audio or video is fabricated.
    """
    def __init__(self, root, images, evidence_folder, *, expiry_seconds=300):
        if type(expiry_seconds) is not int or not 1 <= expiry_seconds <= 3600:
            raise ValueError('CONTINUITY_CAPABILITY_EXPIRY: use 1..3600 seconds')
        self.root = Path(root).resolve(strict=True)
        self.folder = portable_path(evidence_folder)
        self.images = [pointer(self.root, self.root/portable_path(p), kind='image-master') for p in images]
        if not 1 <= len(self.images) <= 128:
            raise ValueError('CONTINUITY_CAPABILITY_LIMIT: use 1..128 image masters')
        self._lock = __import__('threading').Lock()
        self._resources = dict(memory_bytes=128*1024*1024, disk_bytes=8*1024*1024)
        with self.reserve(None): result = self._execute()
        import PIL
        self._snap = dict(schema_version=VERSION, kind='continuity-capability-snapshot',
            capability_id='video-encoding', implementation_id='preserve-image-masters',
            owner='hatch-image-masters', backend='Pillow', backend_version=PIL.__version__,
            contract_version=VERSION, input_types=['image-master'], output_types=['verified-image-masters'],
            operations=['video-encoding'], status='available', discovered=True,
            tested_operations=['video-encoding'], evidence=result['evidence'],
            limits=dict(memory_bytes=128*1024*1024, disk_bytes=8*1024*1024,
                        cpu_seconds=30, gpu_bytes=0, concurrency=1), offline=True, asset_rights_required=True,
            expires_at=(datetime.now(timezone.utc)+timedelta(seconds=expiry_seconds)).strftime('%Y-%m-%dT%H:%M:%SZ'),
            limitations=['No encoder executed. Video export and silent-stream verification remain unavailable.'])

    def snapshot(self): return deepcopy(self._snap)

    @contextmanager
    def reserve(self, check):
        if not self._lock.acquire(blocking=False):
            raise ValueError('CONTINUITY_CAPABILITY_OWNERSHIP: image inspection already active')
        try:
            if check is not None and any(p not in check['inputs'] for p in self.images):
                raise ValueError('CONTINUITY_CAPABILITY_SOURCE: every original master must be bound')
            self._resources = check['resources'] if check is not None else dict(memory_bytes=128*1024*1024, disk_bytes=8*1024*1024)
            verify_references(self.root, dict(images=self.images))
            yield
            verify_references(self.root, dict(images=self.images))
        finally: self._lock.release()

    def _execute(self):
        from PIL import Image
        records = []
        total_pixels = 0
        for item in self.images:
            payload = _read_regular_bytes(self.root/item['path'], limit=8*1024*1024,
                                          expected_sha256=item['sha256'])
            with Image.open(io.BytesIO(payload)) as image:
                pixels = image.width*image.height
                if (pixels > 4_000_000 or total_pixels+pixels > 16_000_000
                        or pixels*8 + len(payload) > self._resources['memory_bytes']):
                    raise ValueError('CONTINUITY_CAPABILITY_LIMIT: image decode exceeds pixel budget')
                image.load()
                total_pixels += pixels
                records.append(dict(source=item, width=image.width, height=image.height,
                                    mode=image.mode, format=image.format))
        import PIL
        measurements = dict(software=dict(python=platform.python_version(),Pillow=PIL.__version__),image_masters=records, decoded_pixels=total_pixels,
                            video_exported=False, audio_inspected=False, originals_preserved=True)
        payload = canonical_bytes(measurements)
        if len(payload) > self._resources['disk_bytes']:
            raise ValueError('CONTINUITY_CAPABILITY_LIMIT: evidence exceeds disk reservation')
        evidence = _write_new(self.root, self.folder, payload)
        return dict(outcome='partial', evidence=[evidence], measurements=measurements,
                    verified_fields=['image-masters'],
                    limitations=['Silent video export is incomplete; retain exact image masters.'])

    def execute(self, check): return self._execute()
