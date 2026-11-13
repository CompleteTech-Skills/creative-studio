"""Retain a reproducible synthetic native replay trace (no visual approval)."""
from copy import deepcopy
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from continuity_contracts import canonical_bytes
from continuity_registry import IdentityRegistry
from test_rig_continuity_temporal import fixture, event
from scene3d_example import TRANSFORM


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    plan=fixture(args.output/'native')
    before=deepcopy(TRANSFORM);before['translation']=[2,0,0]
    after=deepcopy(TRANSFORM);after['translation']=[7,0,0]
    events=[event('discover',1001),event('move',4004,kind='relocate',target='aster',
        before=before,after=after,prerequisites=['discover'])]
    def write(name,value):
        (args.output/name).write_bytes(canonical_bytes(value)+b'\n')
    with IdentityRegistry(args.output/'continuity.sqlite',asset_root=args.output/'native') as registry:
        initial=registry.create_timeline(plan)
        head=registry.append_events('main',events,expected_sha256=initial)
        queries=[('terminal','shot-0',4,'terminal'),('before','shot-0',1,'before'),
                 ('after','shot-0',1,'after'),('last-displayed','shot-0',3,'after'),
                 ('next-shot','shot-1',0,'after'),('flashback','shot-2',0,'before')]
        snapshots={}
        for name,shot,frame,boundary in queries:
            snapshots[name]=registry.temporal_snapshot('main',shot,frame,boundary=boundary)
            write(name+'.json',snapshots[name])
        for name,shot,frame,boundary in reversed(queries):
            assert snapshots[name]==registry.temporal_snapshot('main',shot,frame,boundary=boundary)
        assert snapshots['terminal']['snapshot']['entities']==snapshots['next-shot']['snapshot']['entities']
        assert snapshots['terminal']['snapshot']['entities']!=snapshots['last-displayed']['snapshot']['entities']
        write('plan.json',plan);write('events.json',events)
        write('history.json',registry.temporal_history('main'))
    # A distinct incoming native scene requires its relocation chronology to
    # be authored atomically, rather than storing an invalid empty timeline.
    arrival_root=args.output/'arrival-native'
    arrival_plan=fixture(arrival_root,incoming_translation=[7,0,0])
    arrival_plan['shots'][2]['story_start_tick']=4004
    protected=lambda:{p.relative_to(arrival_root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in arrival_root.rglob('*') if p.is_file()}
    arrival_before=protected()
    with IdentityRegistry(args.output/'arrival.sqlite',asset_root=arrival_root) as registry:
        arrival_head=registry.create_timeline(arrival_plan,events=events)
        incoming=registry.temporal_snapshot('main','shot-2',0)
        prior=registry.temporal_snapshot('main','shot-0',3)
        assert incoming['snapshot']['entities'][0]['transform']['translation']==[7.,0.,0.]
        assert prior['snapshot']['entities'][0]['transform']['translation']==[2.,0.,0.]
        assert incoming['snapshot']['applied_events']==['discover','move']
        assert registry.temporal_history('main')['events']==events
        write('arrival-plan.json',arrival_plan);write('arrival-snapshot.json',incoming)
    assert arrival_before==protected()
    source_root=Path(__file__).resolve().parents[1]
    sources=['scripts/continuity_temporal.py','scripts/continuity_registry.py','scripts/continuity_contracts.py',
             'scripts/scene3d_contracts.py','scripts/scene3d_math.py','scripts/rig_math.py',
             'tests/test_rig_continuity_temporal.py','tests/continuity_temporal_probe.py']
    report=dict(status='passed-synthetic-structural-replay',production_artwork_approved=False,
        versions=dict(python=platform.python_version(),numpy=importlib.metadata.version('numpy'),
                      jsonschema=importlib.metadata.version('jsonschema'),sqlite=__import__('sqlite3').sqlite_version),
        atomic_initial_events=dict(status='passed-changed-native-incoming-scene',head_sha256=arrival_head,
            native_directory='arrival-native',snapshot_sha256=incoming['sha256'],
            protected_native_before=arrival_before,protected_native_after=protected()),
        initial_sha256=initial,head_sha256=head,queries=[dict(name=n,shot=s,frame=f,boundary=b) for n,s,f,b in queries],
        snapshot_hashes={k:v['sha256'] for k,v in snapshots.items()},
        source_sha256={p:hashlib.sha256((source_root/p).read_bytes()).hexdigest() for p in sources},
        assertions=['out-of-order replay hashes equal','terminal N differs from displayed N-1',
                    'terminal equals next shot initial entities','before/after exact story event'],
        limitations=['static native hierarchy and explicit discrete events only','no animation, physics or visual approval'])
    write('report.json',report)
    print(json.dumps(report,sort_keys=True))


if __name__=='__main__':main()
