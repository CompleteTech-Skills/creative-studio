"""Capability admission for continuity; native owners retain jobs and approvals.

Import is stdlib-only. Trusted Python owners register bounded adapters; JSON
requests cannot name a command, install a provider, or grant policy authority.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata
import json
import math
import os
import sys
import shutil
import re
from pathlib import Path
import threading
import time

from continuity_contracts import (ID, SHA, SEMVER, TEXT, PATH, STAMP, VERSION,
    arr, obj, enum, nullable, ref, integer, canonical_bytes, digest,
    portable_path, verify_references, _read_regular_bytes, MAX_DEPENDENCY_BYTES)
from rig_errors import decode_json

OUTCOMES = ('executed', 'supported_fallback', 'partial', 'blocked_missing_capability',
            'needs_visual_review', 'failed', 'canceled')
# Requested semantic capability -> (permitted bounded fallbacks, unavailable fields).
# These do not assert that an implementation is installed or tested.
MATRIX = {
 'geometry-inspection': (('independent-geometry-parser',), ('geometry', 'materials', 'skins', 'morphs')),
 'blender-native-inspection': (('validated-data-export',), ('native-asset-inspection',)),
 'scene-animation-evaluation': (('bounded-native-evaluator',), ('evaluated-world-state', 'contacts', 'phase')),
 'rig-compatibility': (('exact-rig-data',), ('rig-compatibility', 'retargeting')),
 'physics-continuation': (('verified-native-cache',), ('physics-state', 'exact-continuation')),
 'spatial-screen-checks': (('world-camera-math',), ('screen-direction', 'camera-mapping')),
 'visual-reference': (('tested-geometry-renderer',), ('reference-render',)),
 'visual-comparison': (('measured-image-comparison',), ('visual-metrics', 'visual-approval')),
 'gpu-rendering': (('tested-cpu-renderer',), ('gpu-render',)),
 'video-encoding': (('preserve-image-masters',), ('silent-video-export',)),
 'graphical-editor': (('python-cli-service',), ('gui-integration',)),
 'ai-interpretation': (('deterministic-rules',), ('ai-interpretation',)),
 'historical-media': (('labeled-test-inputs',), ('original-media',)),
}
# Hard blockers cannot be relabeled as a missing optional dependency.
HARD = ('integrity', 'ownership', 'permission', 'canonical-reference', 'network-policy')
MANDATORY_FIELDS = {
 'geometry-inspection': ('geometry', 'materials', 'skins', 'morphs', 'source-hierarchy', 'extensions-and-channels'),
 'blender-native-inspection': ('native-asset-inspection',),
 'scene-animation-evaluation': ('evaluated-world-state', 'contacts', 'phase'),
 'rig-compatibility': ('rig-compatibility',),
 'physics-continuation': ('physics-state', 'exact-continuation'),
 'spatial-screen-checks': ('screen-direction', 'camera-mapping'),
 'visual-reference': ('reference-render',),
 'visual-comparison': ('visual-metrics', 'visual-approval'),
 'gpu-rendering': ('reference-render',),
 'video-encoding': ('silent-video-export', 'video-timing', 'audio-absence'),
 'graphical-editor': ('gui-integration',),
 'ai-interpretation': ('interpretation',),
 'historical-media': ('original-media',),
}
RESOURCES = ('memory_bytes', 'disk_bytes', 'cpu_seconds', 'gpu_bytes', 'concurrency')


def definitions():
    pointer = obj(dict(id=ID, version=SEMVER, path=PATH, sha256=SHA, kind=ID))
    limits = obj({k: integer(0, 2**53-1) for k in RESOURCES})
    check = obj(dict(id=ID, capability=enum(*MATRIX), schema_version=SEMVER,
        inputs=arr(ref('pointer'), maximum=128), input_types=arr(ID, maximum=32, unique=True),
        output_types=arr(ID, maximum=32, unique=True), resources=ref('limits'),
        required_fields=arr(ID, maximum=128, unique=True), allow_fallback=dict(type='boolean'),
        gpu_required=dict(type='boolean'), needs_visual_approval=dict(type='boolean')))
    request = obj(dict(schema_version=dict(const=VERSION), kind=dict(const='continuity-capability-request'),
        request_id=ID, project_revision=ref('pointer'), checks=arr(ref('check'), minimum=1, maximum=128)))
    snapshot = obj(dict(schema_version=dict(const=VERSION), kind=dict(const='continuity-capability-snapshot'),
        capability_id=enum(*MATRIX), implementation_id=ID, owner=ID, backend=ID,
        backend_version=nullable(SEMVER), contract_version=SEMVER, input_types=arr(ID, unique=True),
        output_types=arr(ID, unique=True), operations=arr(ID, unique=True),
        status=enum('available', 'unavailable', 'untested'), discovered=dict(type='boolean'),
        tested_operations=arr(ID, unique=True), evidence=arr(ref('pointer'), maximum=128),
        limits=ref('limits'), offline=dict(type='boolean'), asset_rights_required=dict(type='boolean'),
        expires_at=STAMP, limitations=arr(TEXT, maximum=128)))
    result = obj(dict(schema_version=dict(const=VERSION), kind=dict(const='continuity-capability-result'),
        request_sha256=SHA, request_id=ID, outcome=enum(*OUTCOMES), checks=arr(ref('check-result'), maximum=128),
        integrity=enum('verified', 'failed'), visual_approval=dict(const=False), host_policy=ref('host-policy'),
        admission_resources=ref('admission-resources'),
        report_sha256=SHA))
    check_result = obj(dict(id=ID, capability=enum(*MATRIX), outcome=enum(*OUTCOMES),
        implementation_id=nullable(ID), owner=nullable(ID), backend=nullable(ID),
        backend_version=nullable(SEMVER), snapshot_sha256=nullable(SHA), snapshot=nullable(ref('snapshot')),
        missing_fields=arr(ID, unique=True), unsatisfied_gates=arr(ID, unique=True),
        evidence=arr(ref('pointer'), maximum=128), measurements=nullable(dict(type='object')),
        limitations=arr(TEXT, maximum=128), elapsed_seconds=dict(type='number', minimum=0),
        error=nullable(TEXT)))
    host_policy = obj(dict(offline=dict(type='boolean'), asset_rights=dict(type='boolean'),
        allowed_owners=arr(ID, maximum=128, unique=True), limits=ref('limits')))
    admission_resources = obj(dict(wall_seconds=dict(type='number',minimum=0),
        process_cpu_seconds=dict(type='number',minimum=0),reserved_cpu_seconds=integer(1),
        scope=dict(const='Complete admission including reference/schema/report work; conservative wall deadline equals CPU reservation.')))
    execution = obj(dict(evidence=arr(ref('pointer'), maximum=128), measurements=nullable(dict(type='object')),
        verified_fields=arr(ID, maximum=128, unique=True), limitations=arr(TEXT, maximum=128),
        outcome=enum('executed','partial','needs_visual_review','failed','canceled')))
    return dict(pointer=pointer, limits=limits, check=check, request=request,
                **{'host-policy':host_policy, 'execution-result':execution, 'admission-resources':admission_resources},
                snapshot=snapshot, result=result, **{'check-result': check_result})


def schemas():
    d = definitions()
    return {kind: {'$schema': 'https://json-schema.org/draft/2020-12/schema',
                   '$defs': d, '$ref': '#/$defs/' + kind}
            for kind in ('request', 'snapshot', 'result')}


def validate(value, kind):
    canonical_bytes(value)
    if kind not in ('request', 'snapshot', 'result', 'check-result', 'execution-result'):
        raise ValueError('CONTINUITY_CAPABILITY_SCHEMA: unknown kind')
    from jsonschema import Draft202012Validator
    value = deepcopy(value)
    schema = {'$defs':definitions(), '$ref':'#/$defs/'+kind}
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise ValueError('CONTINUITY_CAPABILITY_SCHEMA: ' + errors[0].message)
    # Traverse schema types, not arbitrary data-key names, and normalize only
    # declared integer positions. Metadata measurements remain opaque JSON.
    def normalize(item, schema):
        if 'anyOf' in schema:
            for branch in schema['anyOf']:
                if Draft202012Validator({'$defs':definitions(), **branch}).is_valid(item):
                    return normalize(item,branch)
        if '$ref' in schema:
            name = schema['$ref'].split('/')[-1]
            if name == 'pointer': portable_path(item['path'])
            schema = definitions()[name]
        if schema.get('type') == 'integer':
            return int(item)
        if isinstance(item, dict) and schema.get('type') == 'object':
            return {k: normalize(v, schema.get('properties', {}).get(k, {})) for k, v in item.items()}
        if isinstance(item, list) and schema.get('type') == 'array':
            return [normalize(v, schema['items']) for v in item]
        return item
    value = normalize(value, definitions()[kind])
    if kind == 'request':
        ids = [c['id'] for c in value['checks']]
        if any(c['resources']['concurrency'] != 1 or c['resources']['cpu_seconds'] < 1
               or c['resources']['memory_bytes'] < 1 for c in value['checks']):
            raise ValueError('CONTINUITY_CAPABILITY_LIMIT: serial checks require positive CPU/memory and concurrency one')
        if len(ids) != len(set(ids)):
            raise ValueError('CONTINUITY_CAPABILITY_SCHEMA: repeated check ID')
    if kind == 'snapshot':
        datetime.strptime(value['expires_at'], '%Y-%m-%dT%H:%M:%SZ')
        if value['implementation_id'] not in (value['capability_id'], *MATRIX.get(value['capability_id'], ((), ()))[0]):
            raise ValueError('CONTINUITY_CAPABILITY_FALLBACK: implementation outside explicit matrix')
        if not set(value['tested_operations']) <= set(value['operations']):
            raise ValueError('CONTINUITY_CAPABILITY_SCHEMA: tested operation not advertised')
        if value['status'] == 'available' and (value['backend_version'] is None or not value['evidence'] or not value['tested_operations']):
            raise ValueError('CONTINUITY_CAPABILITY_EVIDENCE: available requires retained execution evidence')
    return value


@dataclass(frozen=True)
class Policy:
    """Trusted host policy, never supplied or weakened by a request document."""
    offline: bool = True
    asset_rights: bool = False
    allowed_owners: frozenset = frozenset()
    max_memory_bytes: int = 512 * 1024 * 1024
    max_disk_bytes: int = 64 * 1024 * 1024
    max_cpu_seconds: int = 60
    max_gpu_bytes: int = 0
    max_concurrency: int = 1

    def __post_init__(self):
        if type(self.offline) is not bool or type(self.asset_rights) is not bool:
            raise ValueError('CONTINUITY_CAPABILITY_POLICY: policy flags must be booleans')
        if (not isinstance(self.allowed_owners, frozenset) or len(self.allowed_owners)>128
                or not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}',x) for x in self.allowed_owners)):
            raise ValueError('CONTINUITY_CAPABILITY_POLICY: explicit immutable owner allowlist required')
        for key in RESOURCES:
            value = getattr(self, 'max_' + key)
            if type(value) is not int or not 0 <= value <= 2**53-1:
                raise ValueError('CONTINUITY_CAPABILITY_POLICY: invalid resource limit')


class OwnerAdapter:
    """Trusted owner interface, not a plugin loader or a new job controller.

    reserve(check) MUST acquire the native owner's resource/exclusivity authority
    and yield; execute(check) returns a result bound to exact inputs. Snapshots
    bind discovery/probe evidence, but every admitted check still executes.
    """
    def snapshot(self): raise NotImplementedError
    def reserve(self, check): raise NotImplementedError
    def execute(self, check): raise NotImplementedError


def _message(exc, maximum=3000):
    return str(exc).encode('utf-8',errors='backslashreplace').decode('utf-8')[:maximum]


def _failure(check, message=None):
    return dict(id=check['id'], capability=check['capability'], outcome='blocked_missing_capability',
        implementation_id=None, owner=None, backend=None, backend_version=None, snapshot_sha256=None, snapshot=None,
        missing_fields=sorted(set(check['required_fields']) | set(MANDATORY_FIELDS[check['capability']])), unsatisfied_gates=[], evidence=[], measurements=None,
        limitations=[], elapsed_seconds=0., error=message)


class ReferencePass:
    """Per-admission verified references, with cheap observed-change guards.

    New/observably changed refs are hashed once. A single final fresh hash pass
    verifies the entire flattened closure, including earlier evidence. A forged
    unchanged metadata fingerprint cannot manufacture final integrity success.
    """
    def __init__(self, root):
        self.root=root
        self.refs={}
        self.fingerprints={}
        self.sizes={}
        self.hash_passes=0

    def _fingerprint(self, pointer):
        path=self.root/portable_path(pointer['path'])
        current=self.root
        for part in pointer['path'].split('/'):
            current/=part
            if current.is_symlink():
                raise ValueError('CONTINUITY_CAPABILITY_PATH: reference symlink is forbidden')
        value=path.stat(follow_symlinks=False)
        return (value.st_dev,value.st_ino,value.st_size,value.st_mtime_ns,value.st_ctime_ns)

    def verify(self, pointers, *, force=False):
        selected={}
        for pointer in pointers:
            key=portable_path(pointer['path']).casefold()
            prior=self.refs.get(key)
            if prior is not None and prior!=pointer:
                raise ValueError('CONTINUITY_CAPABILITY_REFERENCE: conflicting exact reference')
            if key in selected and selected[key]!=pointer:
                raise ValueError('CONTINUITY_CAPABILITY_REFERENCE: conflicting exact reference')
            selected[key]=pointer
        changed={key:p for key,p in selected.items() if force or key not in self.refs
                 or self._fingerprint(p)!=self.fingerprints[key]}
        if changed:
            retained=sum(v for k,v in self.sizes.items() if k not in changed)
            available=MAX_DEPENDENCY_BYTES-retained
            if available<1:
                raise ValueError('CONTINUITY_CAPABILITY_LIMIT: reference closure exceeds byte budget')
            # Explicit pointer list; no arbitrary metadata is traversed. This
            # compatibility boundary switches to owner113's final typed-list API.
            observed=verify_references(self.root,dict(refs=list(changed.values())),maximum_bytes=available)
            self.hash_passes+=1
            sizes={f['path'].casefold():f['byte_size'] for f in observed['files']}
            for key,pointer in changed.items():
                self.refs[key]=deepcopy(pointer)
                self.fingerprints[key]=self._fingerprint(pointer)
                self.sizes[key]=sizes[key]
        return dict(files=len(self.refs),byte_size=sum(self.sizes.values()),hash_passes=self.hash_passes)

    def final(self):
        return self.verify(list(self.refs.values()),force=True)


def _peak_memory_bytes():
    """Native lifetime process peak, conservatively including prior work.

    Owners execute in this process, so a reservation covers its whole resident
    working set. This is an admission/result gate, not an OS allocation sandbox.
    """
    try:
        import resource
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(rss if sys.platform == 'darwin' else rss * 1024)
    except ImportError:
        if sys.platform != 'win32':
            return None
        try:
            import ctypes
            from ctypes import wintypes
            class Counters(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                    'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                    'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
            counters = Counters(); counters.cb = ctypes.sizeof(counters)
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            query = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
            query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
            query.restype = wintypes.BOOL
            if query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return int(counters.PeakWorkingSetSize)
        except (OSError, AttributeError):
            pass
        return None


class Admission:
    def __init__(self, root, *, policy, owners=(), clock=None):
        self.root = Path(root).resolve(strict=True)
        if not isinstance(policy, Policy):
            raise ValueError('CONTINUITY_CAPABILITY_POLICY: trusted Policy required')
        self.policy = policy
        self.owners = tuple(owners)
        if len(self.owners)>128 or not all(isinstance(x, OwnerAdapter) for x in self.owners):
            raise ValueError('CONTINUITY_CAPABILITY_OWNER: trusted owner adapters required')
        self._busy = threading.Lock()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self, request, *, canceled=lambda: False, now=None):
        start_clock = time.monotonic()
        start_cpu = time.process_time()
        request = validate(request, 'request')
        initial = now or self.clock()
        clock = (lambda: initial + timedelta(seconds=time.monotonic()-start_clock)) if now is not None else self.clock
        if not isinstance(initial, datetime) or initial.tzinfo is None:
            raise ValueError('CONTINUITY_CAPABILITY_TIME: timezone-aware admission time required')
        totals = {key: sum(c['resources'][key] for c in request['checks']) for key in ('cpu_seconds', 'disk_bytes')}
        if any(totals[key] > getattr(self.policy, 'max_' + key) for key in totals):
            raise ValueError('CONTINUITY_CAPABILITY_LIMIT: aggregate CPU/disk reservation exceeds host budget')
        reserved = sum(c['resources']['cpu_seconds'] for c in request['checks'])
        meter=dict(wall_start=start_clock,cpu_start=start_cpu,reserved_cpu_seconds=reserved)
        references=ReferencePass(self.root)
        output = dict(schema_version=VERSION, kind='continuity-capability-result', request_sha256=digest(request),
            request_id=request['request_id'], outcome='blocked_missing_capability', checks=[],
            integrity='verified', visual_approval=False, host_policy=dict(offline=self.policy.offline,
                asset_rights=self.policy.asset_rights, allowed_owners=sorted(self.policy.allowed_owners),
                limits={key:getattr(self.policy,'max_'+key) for key in RESOURCES}),
            admission_resources=dict(wall_seconds=0.,process_cpu_seconds=0.,reserved_cpu_seconds=reserved,
                scope='Complete admission including reference/schema/report work; conservative wall deadline equals CPU reservation.'),report_sha256='0'*64)
        # Integrity is global, never a missing optional capability. Verify ALL
        # refs even when a requested capability is missing/canceled.
        try:
            references.verify([request['project_revision'],*[p for c in request['checks'] for p in c['inputs']]])
        except (ValueError, OSError) as exc:
            output['integrity'] = 'failed'
            for check in request['checks']:
                item = _failure(check, 'CONTINUITY_CAPABILITY_INTEGRITY: ' + _message(exc))
                item.update(outcome='failed', unsatisfied_gates=['integrity'])
                output['checks'].append(item)
            return self._finish(output,meter)
        if not self._busy.acquire(blocking=False):
            for check in request['checks']:
                item = _failure(check, 'CONTINUITY_CAPABILITY_BUSY: admission already active')
                item.update(outcome='failed', unsatisfied_gates=['ownership'])
                output['checks'].append(item)
            return self._finish(output,meter)
        try:
            report_bytes=0
            for check in request['checks']:
                if (time.monotonic()-meter['wall_start']>reserved or time.process_time()-meter['cpu_start']>reserved):
                    item=_failure(check,'CONTINUITY_CAPABILITY_LIMIT: complete admission budget exhausted')
                    item.update(outcome='failed',unsatisfied_gates=['resource-admission'])
                elif output['integrity'] == 'failed':
                    item = _failure(check, 'CONTINUITY_CAPABILITY_INTEGRITY: earlier reference changed')
                    item.update(outcome='failed', unsatisfied_gates=['integrity'])
                elif canceled():
                    item = _failure(check)
                    item.update(outcome='canceled', unsatisfied_gates=['cancellation'])
                else:
                    try:
                        references.verify([request['project_revision'],*check['inputs']])
                        item = self._check(check, clock, canceled,references,meter)
                    except Exception as exc:
                        item = _failure(check, 'CONTINUITY_CAPABILITY_OWNER: ' + _message(exc))
                        item.update(outcome='failed', unsatisfied_gates=['owner-contract'])
                try:
                    item = validate(item, 'check-result')
                    encoded_bytes=len(canonical_bytes(item))
                    if encoded_bytes > 1024*1024:
                        raise ValueError('CONTINUITY_CAPABILITY_REPORT_LIMIT: check exceeds 1 MiB')
                    if report_bytes + encoded_bytes > 4*1024*1024:
                        raise ValueError('CONTINUITY_CAPABILITY_REPORT_LIMIT: cumulative checks exceed 4 MiB')
                except Exception as exc:
                    item = _failure(check, _message(exc))
                    item.update(outcome='failed', unsatisfied_gates=['owner-contract'])
                report_bytes+=len(canonical_bytes(item))
                output['checks'].append(item)
                try:
                    references.verify([request['project_revision'],*check['inputs']])
                    references.verify(item['evidence'])
                    if item['snapshot'] is not None:references.verify(item['snapshot']['evidence'])
                except (ValueError,OSError) as exc:
                    self._invalidate(output,exc)
            try:
                references.final()
            except (ValueError,OSError) as exc:
                self._invalidate(output,exc)
        finally:
            self._busy.release()
        return self._finish(output,meter)

    @staticmethod
    def _invalidate(output,exc):
        output['integrity']='failed'
        for previous in output['checks']:
            previous.update(outcome='failed',error='CONTINUITY_CAPABILITY_INTEGRITY: '+_message(exc),
                unsatisfied_gates=sorted(set(previous['unsatisfied_gates'])|{'integrity'}))

    def _finish(self, output,meter):
        statuses = [c['outcome'] for c in output['checks']]
        if 'failed' in statuses: result = 'failed'
        elif 'canceled' in statuses: result = 'canceled'
        elif all(s in ('executed', 'supported_fallback') for s in statuses):
            result = 'supported_fallback' if 'supported_fallback' in statuses else 'executed'
        elif 'needs_visual_review' in statuses: result = 'needs_visual_review'
        elif any(s in ('executed', 'supported_fallback', 'partial') for s in statuses): result = 'partial'
        else: result = 'blocked_missing_capability'
        output['outcome'] = result
        output['report_sha256'] = digest({k:v for k,v in output.items() if k != 'report_sha256'})
        output=validate(output,'result')
        wall=time.monotonic()-meter['wall_start'];cpu=time.process_time()-meter['cpu_start']
        output['admission_resources'].update(wall_seconds=wall,process_cpu_seconds=cpu)
        if wall>meter['reserved_cpu_seconds'] or cpu>meter['reserved_cpu_seconds']:
            output['outcome']='failed'
            for check in output['checks']:
                check.update(outcome='failed',error='CONTINUITY_CAPABILITY_LIMIT: complete admission budget exhausted',
                    unsatisfied_gates=sorted(set(check['unsatisfied_gates'])|{'resource-admission'}))
        output['report_sha256']=digest({k:v for k,v in output.items() if k!='report_sha256'})
        # Enforce once more after the bounded final digest. Measurements are
        # taken before digest emission to avoid self-referential timing hashes.
        if (time.monotonic()-meter['wall_start']>meter['reserved_cpu_seconds']
                or time.process_time()-meter['cpu_start']>meter['reserved_cpu_seconds']):
            output['outcome']='failed'
            for check in output['checks']:
                check.update(outcome='failed',error='CONTINUITY_CAPABILITY_LIMIT: complete admission budget exhausted',
                    unsatisfied_gates=sorted(set(check['unsatisfied_gates'])|{'resource-admission'}))
            output['report_sha256']=digest({k:v for k,v in output.items() if k!='report_sha256'})
        return output

    def _check(self, check, clock, canceled,references,meter):
        item = _failure(check)
        def exhausted():
            if (time.monotonic()-meter['wall_start']>meter['reserved_cpu_seconds']
                    or time.process_time()-meter['cpu_start']>meter['reserved_cpu_seconds']):
                item.update(outcome='failed',error='CONTINUITY_CAPABILITY_LIMIT: complete admission budget exhausted',
                    unsatisfied_gates=['resource-admission'])
                return True
            return False
        candidates = []
        rejected = []
        owner_errors = []
        for adapter in self.owners:
            if exhausted():return item
            try:
                snapshot = validate(adapter.snapshot(), 'snapshot')
            except Exception as exc:
                owner_errors.append('Invalid owner snapshot: ' + _message(exc,1000))
                continue
            if snapshot['capability_id'] == check['capability']:
                candidates.append((adapter, snapshot))
        # Exact implementation before declared fallbacks, deterministic ties.
        order = (check['capability'], *MATRIX[check['capability']][0])
        candidates.sort(key=lambda pair: (order.index(pair[1]['implementation_id']), pair[1]['owner']))
        for adapter, snap in candidates:
            if exhausted():return item
            fallback = snap['implementation_id'] != check['capability']
            label = snap['owner'] + '/' + snap['backend'] + '@' + (snap['backend_version'] or 'unknown')
            reason = None
            if fallback and not check['allow_fallback']:
                rejected.append(label + ': fallback disabled'); continue
            if snap['status'] != 'available' or check['capability'] not in snap['tested_operations']:
                rejected.append(label + ': ' + snap['status'] + '/operation untested'); continue
            if datetime.strptime(snap['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) <= clock():
                rejected.append(label + ': expired ' + snap['expires_at']); continue
            if snap['contract_version'] != check['schema_version']:
                rejected.append(label + ': incompatible schema ' + snap['contract_version']); continue
            if not set(check['input_types']) <= set(snap['input_types']):
                rejected.append(label + ': unsupported input types'); continue
            if not set(check['output_types']) <= set(snap['output_types']):
                rejected.append(label + ': unsupported output types'); continue
            item.update(implementation_id=snap['implementation_id'], owner=snap['owner'], backend=snap['backend'],
                backend_version=snap['backend_version'], snapshot_sha256=digest(snap), snapshot=deepcopy(snap), limitations=snap['limitations'])
            gates = []
            if snap['owner'] not in self.policy.allowed_owners: gates.append('permission')
            if self.policy.offline and not snap['offline']: gates.append('network-policy')
            if snap['asset_rights_required'] and not self.policy.asset_rights: gates.append('permission')
            three_d = {'geometry-inspection','blender-native-inspection','scene-animation-evaluation',
                'rig-compatibility','physics-continuation','spatial-screen-checks','visual-reference','gpu-rendering'}
            native_types = {'hatch-3d-rig','model3d-asset','mesh','model/gltf-binary','model/gltf+json','application/x-blender'}
            if check['capability'] in three_d and not any(p['kind'] in native_types for p in check['inputs']):
                gates.append('canonical-reference')
            if check['gpu_required'] and snap['implementation_id'] == 'tested-cpu-renderer': gates.append('gpu-required')
            for key, value in check['resources'].items():
                if value > snap['limits'][key] or value > getattr(self.policy, 'max_' + key): gates.append('resource-' + key)
            if gates:
                item.update(outcome='failed', unsatisfied_gates=sorted(set(gates)))
                return item  # A fallback never waives a hard gate.
            peak = _peak_memory_bytes()
            if peak is None or peak > check['resources']['memory_bytes']:
                item.update(outcome='failed', error=('CONTINUITY_CAPABILITY_LIMIT: process peak memory unavailable' if peak is None else
                        'CONTINUITY_CAPABILITY_LIMIT: process peak memory '+str(peak)+' bytes exceeds reservation '+str(check['resources']['memory_bytes'])+' bytes'),
                    unsatisfied_gates=['resource-memory_bytes'])
                return item
            try:
                references.verify(snap['evidence'])
                if exhausted():return item
                start = time.monotonic()
                cpu_start = time.process_time()
                with adapter.reserve(deepcopy(check)):
                    if exhausted():return item
                    if datetime.strptime(snap['expires_at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) <= clock():
                        item.update(outcome='blocked_missing_capability', error='CONTINUITY_CAPABILITY_EXPIRED: probe expired before execution',
                            unsatisfied_gates=['capability-expiry'])
                        return item
                    if canceled():
                        item.update(outcome='canceled', unsatisfied_gates=['cancellation'])
                        return item
                    peak = _peak_memory_bytes()
                    if peak is None or peak > check['resources']['memory_bytes']:
                        item.update(outcome='failed', error='CONTINUITY_CAPABILITY_LIMIT: reservation exceeded process memory budget',
                            unsatisfied_gates=['resource-memory_bytes'])
                        return item
                    result = adapter.execute(deepcopy(check))
                    # Exact source hashes are checked again before declaring any
                    # result usable. Native adapters must snapshot parsed inputs.
                    references.verify(check['inputs'])
                elapsed = time.monotonic()-start
                cpu_used = time.process_time()-cpu_start
                result = validate(result, 'execution-result')
                if len(canonical_bytes(result)) > 1024*1024:
                    raise ValueError('CONTINUITY_CAPABILITY_REPORT_LIMIT: native result exceeds 1 MiB')
                if set(result) != {'evidence', 'measurements', 'verified_fields', 'limitations', 'outcome'}:
                    raise ValueError('CONTINUITY_CAPABILITY_OWNER: malformed execution result')
                if result['outcome'] not in ('executed', 'partial', 'needs_visual_review', 'failed', 'canceled'):
                    raise ValueError('CONTINUITY_CAPABILITY_OWNER: illegal native outcome')
                references.verify(result['evidence'])
                fields = result['verified_fields']
                if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
                    raise ValueError('CONTINUITY_CAPABILITY_OWNER: verified fields must be typed names')
                mandatory = set(MANDATORY_FIELDS[check['capability']]) | set(check['required_fields'])
                if snap['implementation_id'] == 'validated-data-export':
                    mandatory.add('source-conversion-binding')
                missing = sorted(mandatory - set(fields))
                status = result['outcome']
                if status == 'executed' and not result['evidence']:
                    raise ValueError('CONTINUITY_CAPABILITY_EVIDENCE: execution has no retained artifacts')
                if check['capability'] == 'gpu-rendering' and (check['gpu_required'] or not fallback):
                    if not isinstance(result['measurements'], dict) or result['measurements'].get('device') != 'gpu':
                        raise ValueError('CONTINUITY_CAPABILITY_DEVICE: actual GPU execution is required')
                if snap['implementation_id'] == 'deterministic-rules':
                    if not isinstance(result['measurements'],dict) or result['measurements'].get('model_invoked') is not False:
                        raise ValueError('CONTINUITY_CAPABILITY_FALLBACK: deterministic fallback must identify that no model executed')
                categorical = {
                    'labeled-test-inputs':('original-media',),
                    'python-cli-service':('gui-integration',),
                    'preserve-image-masters':('silent-video-export','video-timing','audio-absence'),
                }.get(snap['implementation_id'], ())
                missing = sorted(set(missing)|set(categorical))
                if elapsed > check['resources']['cpu_seconds'] or cpu_used > check['resources']['cpu_seconds']:
                    status = 'failed'
                    item['unsatisfied_gates'].append('resource-cpu_seconds')
                if canceled(): status = 'canceled'
                if status == 'executed' and missing: status = 'partial'
                if status == 'executed' and fallback: status = 'supported_fallback'
                if status == 'needs_visual_review' or ((check['needs_visual_approval'] or check['capability'] == 'visual-comparison') and status in ('executed','supported_fallback','partial')):
                    status = 'needs_visual_review'
                    if 'visual-approval' not in missing: missing.append('visual-approval')
                    item['unsatisfied_gates'].append('visual-approval')
                measurements = deepcopy(result['measurements']) or {}
                peak = _peak_memory_bytes()
                if peak is None or peak > check['resources']['memory_bytes']:
                    status = 'failed'
                    item['unsatisfied_gates'].append('resource-memory_bytes')
                measurements['admission_resources'] = dict(wall_seconds=elapsed, process_cpu_seconds=cpu_used,
                    process_peak_rss_bytes=peak, rss_scope='whole-process lifetime peak; enforced conservatively against reservation; not an OS allocation sandbox',
                    rss_status='unavailable' if peak is None else 'measured')
                item.update(outcome=status, evidence=result['evidence'], measurements=measurements,
                    missing_fields=missing, limitations=[*item['limitations'], *result['limitations']], elapsed_seconds=elapsed)
                return validate(item, 'check-result')
            except Exception as exc:
                # An executing backend failure must never silently select another
                # implementation or be renamed a successful fallback.
                item.update(outcome='failed', error='CONTINUITY_CAPABILITY_EXECUTION: '+_message(exc),
                    unsatisfied_gates=['execution'])
                return item
        if owner_errors:
            item.update(outcome='failed', unsatisfied_gates=['owner-contract'])
        item['limitations'] = owner_errors + rejected + ['No compatible, current, independently executed owner snapshot is available.',
                               'Unavailable fields remain unsatisfied; discovery is not execution.']
        if check['needs_visual_approval']:
            item['unsatisfied_gates'].append('visual-approval')
        return item


def discover():
    """Named owner discovery without subprocess/network/credential inspection."""
    packages = {}
    for name in ('numpy', 'jsonschema', 'Pillow'):
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    from rig_errors import VERSION as native_version
    tools = {'blender':shutil.which('blender'), 'ffmpeg':shutil.which('ffmpeg')}
    owners = {
        'geometry-inspection':('hatch-rig','native-rig-cpu',native_version),
        'blender-native-inspection':('hatch-rig','blender',None),
        'scene-animation-evaluation':('hatch-animation','native-evaluator',None),
        'rig-compatibility':('hatch-rig','native-rig-cpu',native_version),
        'physics-continuation':('hatch-physics','native-cache',None),
        'spatial-screen-checks':('hatch-camera','camera-math',None),
        'visual-reference':('hatch-rig','native-rig-cpu',native_version),
        'visual-comparison':('hatch-review','native-review',None),
        'gpu-rendering':('hatch-render','gpu-backend',None),
        'video-encoding':('hatch-render','ffmpeg',None),
        'graphical-editor':('hatch-editor','native-editor',None),
        'ai-interpretation':('hatch-director','native-director',None),
        'historical-media':('hatch-acquisition','retained-originals',None),
    }
    rows = []
    for cap,(fallbacks,fields) in MATRIX.items():
        owner,backend,version = owners[cap]
        rows.append(dict(capability_id=cap, owner=owner, backend=backend, backend_version=version,
            contract_version=VERSION, operations=[cap], limits=None, status='untested',
            probe_executed=False, tool_path=tools.get(backend), permitted_fallbacks=list(fallbacks),
            unavailable_fields=list(fields)))
    return dict(schema_version=VERSION, kind='continuity-capability-discovery', packages=packages,
        capabilities=rows, note='Metadata/path discovery only. Unknown versions/limits remain null; no operation or visual approval executed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['discover', 'schemas', 'admit'])
    parser.add_argument('--request', type=Path)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    if args.command == 'discover': result = discover()
    elif args.command == 'schemas': result = schemas()
    else:
        if args.request is None: parser.error('admit requires --request')
        request = decode_json(_read_regular_bytes(args.request, limit=8*1024*1024))
        # CLI has no implicit authority to reserve native resources or invoke
        # tools. Use the documented trusted Python/native service adapter API.
        result = Admission(args.root, policy=Policy()).run(request)
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError) as exc:
        print(json.dumps(dict(error=str(exc))))
        raise SystemExit(2)
