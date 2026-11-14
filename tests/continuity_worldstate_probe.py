"""Retain real native closure + persistent world-state trace; synthetic/nonvisual."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from continuity_contracts import canonical_bytes, digest
from continuity_registry import IdentityRegistry
from continuity_worldstate import RICH_VERSION
from test_rig_continuity_temporal import fixture
from test_rig_continuity_worldstate import world_fixture, entity_event, rich_event


def main(destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=False)
    root=destination/'native';plan=fixture(root)
    def write(name,value):
        (destination/name).write_bytes(canonical_bytes(value))
    with IdentityRegistry(destination/'continuity.sqlite',asset_root=root) as registry:
        plan,world=world_fixture(root,registry,plan)
        panel=deepcopy(world['entities'][-1]);panel['environment']=dict(door='open',light_on=True)
        open_event=entity_event(world,'open-and-illuminate',1001,environment=panel['environment'])
        fact=dict(id='aster-knows-open-door',scope=dict(kind='character',subject='aster',start_tick=None,end_tick=None),
            category='knowledge',certainty='definitive',value=True,causes=['open-and-illuminate'],evidence=[])
        learn=rich_event('learn-open-door',1001,dict(event_type='world-fact',fact_id=fact['id'],before=None,after=fact),prerequisites=['open-and-illuminate']);learn['priority']=1
        incoming=deepcopy(world);incoming['entities'][-1]=panel;incoming['facts']=[fact]
        plan['scenes'][1]['incoming_world_state']=deepcopy(incoming)
        events=[open_event,learn];head=registry.create_timeline(plan,events=events)
        protected={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        queries=[('shot-0',0,'after'),('shot-0',1,'before'),('shot-0',1,'after'),('shot-2',0,'after'),('shot-0',4,'terminal')]
        snapshots=[registry.temporal_snapshot('main',shot,frame,boundary=boundary) for shot,frame,boundary in queries]
        for index in [3,0,4,1,2,3]:
            shot,frame,boundary=queries[index]
            assert registry.temporal_snapshot('main',shot,frame,boundary=boundary)==snapshots[index]
        assert snapshots[3]['snapshot']['world_state']==incoming
        assert snapshots[0]['snapshot']['world_state']==world
        after={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        assert protected==after
        write('plan.json',plan);write('events.json',events);write('snapshots.json',snapshots)
        write('history.json',registry.temporal_history('main'))
        write('verification.json',dict(schema_version=RICH_VERSION,fixture=True,visual_approval=False,
            head_sha256=head,native_files_unchanged=True,native_files=protected,
            snapshot_sha256=[s['sha256'] for s in snapshots],nonsequential_queries=[3,0,4,1,2,3],
            persisted=['door','light','character-knowledge'],
            limitations=['Structural state only; no door mesh animation or light render executed.',
                          'Costume/accessory transitions have separate executable regressions.',
                          'No exclusive prop ownership transaction, physics execution or production art approval.']))
    print(json.dumps(dict(destination=str(destination),head_sha256=head,fixture=True)))


if __name__=='__main__': main(sys.argv[1])
