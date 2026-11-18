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
from continuity_test_helpers import sample, pointer


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
        accessory=dict(id='badge',asset=pointer(),status='equipped',attachment=None,material=None)
        equip=entity_event(world,'equip-badge',1001,entity_id='aster',reason='accessory-change',accessories=[accessory]);equip['priority']=2
        costume=sample('costume');costume.update(id='coat',identity_id='aster')
        costume['variant']=pointer();costume['accessories']=[pointer()];costume['material_overrides']=[]
        data=canonical_bytes(costume);(root/'coat.json').write_bytes(data)
        selected=dict(costume=pointer('coat.json',data,kind='continuity-costume',identity='coat'),
            compatible_revisions=[world['entities'][0]['revision']],clothing=[pointer()],materials=[],textures=[],
            attachments=[],layers=[dict(id='coat',visible=True)],
            approved_combination=pointer('reference.ppm',b'P6\n1 1\n255\n\xff\x00\x00',kind='evidence',identity='reference'),
            appearance='clean',physics_state=None,reference_renders=[])
        before=deepcopy(world['entities'][0]);before['accessories']=[accessory]
        after=deepcopy(before);after['costume']=selected
        wear=rich_event('wear-coat',1001,dict(event_type='world-entity',entity_id='aster',reason='costume-change',before=before,after=after),prerequisites=['equip-badge']);wear['priority']=3
        incoming['entities'][0]=after
        plan['scenes'][1]['incoming_world_state']=deepcopy(incoming)
        events=[open_event,learn,equip,wear];head=registry.create_timeline(plan,events=events)
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
            persisted=['costume','accessory','door','light','character-knowledge'],
            limitations=['Structural state only; no door mesh animation or light render executed.',
                          'Clothing/accessories are labeled structural mesh transport fixtures, not production artwork.',
                          'No exclusive prop ownership transaction, physics execution or production art approval.']))
    with IdentityRegistry(destination/'continuity.sqlite',asset_root=root) as reopened:
        assert reopened.temporal_history('main')['sha256']==head
        assert reopened.temporal_snapshot('main','shot-2',0)==snapshots[3]
    verification=json.loads((destination/'verification.json').read_bytes())
    verification['restart_verified']=True
    write('verification.json',verification)
    print(json.dumps(dict(destination=str(destination),head_sha256=head,fixture=True,restart_verified=True)))


if __name__=='__main__': main(sys.argv[1])
