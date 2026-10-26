"""Strict additive Animation3D v1 documents and local semantic admission.

Schema validity is not readiness: TargetRegistry resolves actual targets and
exact source bytes. Rendering, physics, camera/light/material evaluation,
contact solving and visual approval retain their existing owners.
"""
from copy import deepcopy
from functools import lru_cache
import math
import unicodedata

from rig_contracts import obj, arr, ref, mapping
from rig_errors import RigError, finite_json, portable_name, require
from animation3d_time import TICKS_PER_FRAME, frame_position, rate

ROOTS = ('project', 'clip', 'track', 'target', 'layer', 'state-machine', 'retarget',
         'constraint', 'interaction', 'scene', 'sequence', 'render', 'registry')
KINDS = {name: 'animation_' + name.replace('-', '_') + '_3d' for name in ROOTS}
SUPPORTED_INTERPOLATION = {'step', 'linear', 'hermite', 'quaternion-slerp'}
TARGET_PROPERTIES = {
    'node': {'translation':'vec3', 'rotation':'quaternion', 'scale':'vec3', 'visibility':'boolean'},
    'bone': {'translation':'vec3', 'rotation':'quaternion', 'scale':'vec3'},
    'morph': {'weight':'float'},
    'camera': {'translation':'vec3', 'rotation':'quaternion', **{k:'float' for k in ('focal_length','field_of_view','orthographic_size','focus_distance','aperture','exposure')}},
    'light': {'translation':'vec3', 'rotation':'quaternion', 'color':'vec3', **{k:'float' for k in ('intensity','range','cone_angle')}},
    'material': {'base_color':'vec4', 'emissive_color':'vec3', **{k:'float' for k in ('opacity','emissive_intensity','roughness','metallic')}},
    'environment': {k:'float' for k in ('sky_rotation','ambient_intensity','fog_density','wind_strength','sun_elevation','time_of_day')},
    'constraint': {'weight':'float','enabled':'boolean','target_position':'vec3'},
}


def _doc(name, fields, optional=()):
    fields = dict(schema_version={'const':1}, kind={'const':KINDS[name]}, **fields)
    return obj(fields, [key for key in fields if key not in optional])


@lru_cache(maxsize=1)
def _definitions():
    identifier = {'type':'string','pattern':r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}(?![\s\S])'}
    text = {'type':'string','minLength':1,'maxLength':4096}
    semver = {'type':'string','pattern':r'^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?![\s\S])'}
    digest = {'type':'string','pattern':r'^[0-9a-f]{64}(?![\s\S])'}
    num = {'type':'number','minimum':-1e9,'maximum':1e9}
    pos = {'type':'number','exclusiveMinimum':0,'maximum':1e9}
    unit = {'type':'number','minimum':0,'maximum':1}
    frame = {'type':'integer','minimum':0,'maximum':10_000_000}
    natural = {'type':'integer','minimum':1,'maximum':10_000_000}
    boolean = {'type':'boolean'}
    ids = lambda low=0, high=4096: arr(identifier,low,high)
    optional_obj = lambda fields: obj(fields,[])
    vec3, quat = arr(num,3,3), arr(num,4,4)
    scalar = {'type':['string','number','boolean','null'],'maxLength':4096}
    metadata = mapping(scalar,128)
    fps = obj({k:{'type':'integer','minimum':1,'maximum':1_000_000} for k in ('numerator','denominator')})
    time = obj(dict(frame=frame,subframe_ticks={'type':'integer','minimum':0,'maximum':TICKS_PER_FRAME-1}),['frame'])
    asset = obj(dict(id=identifier,version=semver,path={'type':'string','minLength':1,'maxLength':1024},sha256=digest,kind=identifier))
    transform = obj(dict(position=vec3,rotation=quat,scale=arr(pos,3,3)))
    provenance = obj(dict(origin=text,kind={'enum':['generated','imported','authored','derived','fixture']},
        fixture=boolean,sources=arr(ref('asset'),0,4096),attribution=text,permissions=text,
        tool_versions=mapping(text,128),parameters=metadata,created_at=text,parent_version=semver),
        ['origin','kind','fixture','sources'])
    limits = obj({k:natural for k in ('max_frames','max_tracks','max_keyframes','max_actors','max_bones_per_rig',
        'max_vertices','max_morph_targets','max_scene_nodes','max_memory_bytes','max_work_bytes','max_render_pixels','max_parallel_jobs')})
    for k in ('max_memory_bytes','max_work_bytes','max_render_pixels'):
        limits['properties'][k] = {'type':'integer','minimum':1,'maximum':2**40}
    targets=[]
    for kind, properties in TARGET_PROPERTIES.items():
        fields = dict(kind={'const':kind},property={'enum':list(properties)})
        fields.update({k:identifier for k in ({'node':['node_id'],'bone':['actor_id','bone_id'],
            'morph':['actor_id','mesh_id','morph_id'],'camera':['camera_id'],'light':['light_id'],
            'material':['material_id'],'environment':[],'constraint':['constraint_id']}[kind])})
        targets.append(obj(fields))
    target={'oneOf':targets}
    value={'oneOf':[num,boolean,vec3,quat]}
    handle=obj(dict(time_delta_frames=num,value_delta={'oneOf':[num,vec3,quat]},weighted=boolean,weight=pos),['time_delta_frames','value_delta'])
    key=obj(dict(time=ref('time'),value=value,interpolation={'enum':['step','linear','ease','bezier','hermite','catmull-rom','tcb','quaternion-slerp','quaternion-squad']},
        ease_in=unit,ease_out=unit,in_tangent=value,out_tangent=value,in_handle=handle,out_handle=handle,
        tension=num,continuity=num,bias=num,metadata=metadata),['time','value'])
    track=obj(dict(id=identifier,target=ref('target'),value_type={'enum':['float','boolean','vec3','vec4','quaternion']},
        coordinate_space={'enum':['local','world','parent']},default_value=value,keys=arr(ref('key'),0,250000),
        extrapolation_before={'enum':['hold','linear','cycle']},extrapolation_after={'enum':['hold','linear','cycle']},enabled=boolean),
        ['id','target','value_type','default_value','keys','extrapolation_before','extrapolation_after','enabled'])
    event=obj(dict(id=identifier,name=text,frame=frame,type={'enum':['marker','animation_event','contact_start','contact_end','attach','release','handoff','state_transition']},payload=metadata,scope={'enum':['clip','actor','scene']}),['id','name','frame','type'])
    root=obj(dict(enabled=boolean,source_node=identifier,source_bone=identifier,translation_axes=arr(boolean,3,3),extract_rotation=boolean,
        mode={'enum':['apply_to_actor','in_place','extract_only']},cycle_displacement=vec3),['enabled','translation_axes','extract_rotation','mode'])
    contact=obj(dict(id=identifier,target_node=identifier,target_bone=identifier,target_anchor=identifier,
        kind={'enum':['foot','hand','grasp','support','custom']},start_frame=frame,end_frame=natural,
        coordinate_space={'enum':['world','actor','object']},target_object=identifier,position_tolerance_m=pos,
        orientation_tolerance_rad=pos,solver_weight=unit),['id','kind','start_frame','end_frame','coordinate_space','position_tolerance_m','solver_weight'])
    clip=_doc('clip',dict(id=identifier,version=semver,name=text,description=text,fps=fps,duration_frames=natural,
        rig_binding=ref('asset'),skeleton_signature=digest,tags=arr(text,0,128),categories=arr(text,0,128),
        loop_mode={'enum':['once','loop','ping_pong','hold']},completion_mode={'enum':['hold','reset','exit','transition']},default_speed=pos,
        tracks=arr(ref('track'),0,4096),events=arr(event,0,4096),contacts=arr(contact,0,4096),root_motion=root,
        entry_pose=identifier,exit_pose=identifier,interruption_policy={'enum':['any_frame','markers_only','on_completion','never']},
        interruption_markers=ids(),constraints=ids(),procedural_modifiers=ids(),provenance=ref('provenance'),dependencies=arr(ref('asset'),0,4096)),
        ['description','rig_binding','skeleton_signature','entry_pose','exit_pose','interruption_markers','constraints','procedural_modifiers'])
    mask=optional_obj(dict(included_bones=ids(),excluded_bones=ids(),include_descendants=boolean,per_bone_weights=mapping(unit,4096)))
    placement=obj(dict(id=identifier,clip=ref('asset'),actor_id=identifier,start_frame=frame,duration_frames=natural,
        source_in_frame=frame,source_out_frame=natural,playback_rate=fps,reverse=boolean,loop_override={'enum':['inherit','once','loop','ping_pong']},
        blend_in_frames=frame,blend_out_frames=frame,weight=unit,weight_track=ref('track'),layer_id=identifier,time_offset_frames=frame,enabled=boolean),
        ['id','clip','start_frame','duration_frames','source_in_frame','playback_rate','blend_in_frames','blend_out_frames','weight','layer_id','enabled'])
    layer=obj(dict(id=identifier,name=text,priority={'type':'integer','minimum':-10000,'maximum':10000},blend_mode={'enum':['override','additive','multiply']},weight=unit,
        weight_track=ref('track'),bone_mask=mask,affected_targets=arr(ref('target'),0,4096),reference_pose=identifier,muted=boolean,solo=boolean,placements=arr(ref('placement'),0,4096)),
        ['id','name','priority','blend_mode','weight','muted','solo','placements'])
    parameter={'oneOf':[obj(dict(name=identifier,type={'const':kind},default=default)) for kind,default in
        [('bool',boolean),('float',num),('int',{'type':'integer','minimum':-1e9,'maximum':1e9}),('trigger',{'const':False})]]}
    condition=obj(dict(parameter=identifier,operator={'enum':['eq','neq','lt','lte','gt','gte','triggered']},value={'type':['boolean','number']}),['parameter','operator'])
    state=obj(dict(id=identifier,name=text,clip=ref('asset'),blend_tree=identifier,playback_speed=pos,loop_override={'enum':['inherit','loop','once']},enter_events=ids(),exit_events=ids()),['id','name','playback_speed'])
    transition=obj(dict(id=identifier,from_state=identifier,to_state=identifier,conditions=arr(condition,1,128),condition_mode={'enum':['all','any']},priority={'type':'integer','minimum':-10000,'maximum':10000},
        duration_frames=frame,blend_curve={'enum':['linear','smoothstep','ease_in_out']},exit_time_normalized=unit,exit_marker=identifier,
        interruption_policy={'enum':['source','destination','higher_priority','none']},synchronize_phase=boolean),
        ['id','from_state','to_state','conditions','condition_mode','priority','duration_frames','blend_curve','interruption_policy'])
    transition['properties']['from_state']={'anyOf':[identifier,{'const':'*'}]}
    machine=_doc('state-machine',dict(id=identifier,version=semver,parameters=arr(parameter,0,128),states=arr(state,1,4096),
        transitions=arr(transition,0,4096),initial_state=identifier,transition_limit_per_frame={'type':'integer','minimum':1,'maximum':128},provenance=ref('provenance')))
    retarget=_doc('retarget',dict(id=identifier,version=semver,source_rig=ref('asset'),target_rig=ref('asset'),source_skeleton_signature=digest,target_skeleton_signature=digest,
        source_rest_pose=identifier,target_rest_pose=identifier,mappings=arr(obj(dict(source_bone=identifier,target_bone=identifier,rotation_offset=quat,translation_scale=vec3,
        mode={'enum':['rotation','translation','full_transform']},weight=unit),['source_bone','target_bone','rotation_offset','mode']),1,4096),
        root_policy={'enum':['preserve_world','scale_to_target','in_place']},scale_policy={'enum':['height_ratio','leg_length_ratio','explicit']},explicit_scale=pos,
        preserve_end_effectors=boolean,preserve_contacts=boolean,missing_bone_policy={'enum':['error','warn','ignore']},
        validation_thresholds=obj({k:pos for k in ('maximum_joint_error_rad','maximum_contact_error_m','maximum_root_drift_m')}),provenance=ref('provenance')),['explicit_scale'])
    bounds=obj(dict(minimum=vec3,maximum=vec3))
    constraint=obj(dict(id=identifier,type={'enum':['look_at','aim','parent','position','rotation','scale','distance','limit','two_bone_ik','multi_bone_ik','foot_plant','grasp','path_follow']},
        source=ref('target'),target=ref('target'),target_object_id=identifier,target_position=vec3,pole_target=vec3,chain_bones=ids(0,256),weight=unit,
        position_limits=bounds,rotation_limits=obj(dict(minimum_rad=vec3,maximum_rad=vec3)),start_frame=frame,end_frame=natural,enabled=boolean),['id','type','source','weight','enabled'])
    participant=obj(dict(actor_id=identifier,role={'enum':['giver','receiver','support','participant']},hand_bone=identifier,grip_anchor=identifier,interaction_marker=identifier),['actor_id','role'])
    transfer=obj(dict(frame=frame,previous_owner=identifier,next_owner=identifier,previous_anchor=identifier,next_anchor=identifier,
        world_pose_policy={'enum':['preserve','blend_to_receiver','explicit']},transfer_duration_frames=frame),['frame','world_pose_policy'])
    interaction=_doc('interaction',dict(id=identifier,participants=arr(participant,1,128),prop_id=identifier,start_frame=frame,end_frame=natural,
        synchronization_markers=ids(),ownership_changes=arr(transfer,0,4096),constraints=ids(),position_tolerance_m=pos,orientation_tolerance_rad=pos,
        failure_policy={'enum':['error','flag_for_review']}),['prop_id','ownership_changes'])
    actor=obj(dict(actor_id=identifier,model=ref('asset'),rig=ref('asset'),initial_transform=ref('transform'),animation_layers=arr(ref('layer'),0,128),
        state_machine=ref('asset'),retarget_profile=ref('asset'),constraints=arr(ref('constraint'),0,128),initial_state_parameters=mapping({'type':['boolean','number']},128)),
        ['actor_id','model','initial_transform','animation_layers','constraints'])
    scene=_doc('scene',dict(id=identifier,version=semver,scene_asset=ref('asset'),actors=arr(actor,0,4096),object_tracks=arr(ref('track'),0,4096),
        environment_tracks=arr(ref('track'),0,4096),camera_tracks=arr(ref('track'),0,4096),lighting_tracks=arr(ref('track'),0,4096),
        interactions=arr(ref('interaction'),0,4096),markers=arr(event,0,4096)))
    shot=obj(dict(id=identifier,scene_id=identifier,start_frame=frame,duration_frames=natural,active_camera_id=identifier,clip_placements=arr(ref('placement'),0,4096),
        actor_overrides=mapping(arr(ref('track'),0,4096),4096),transition_to_next={'oneOf':[obj(dict(type={'const':'cut'})),obj(dict(type={'const':'dissolve'},duration_frames=natural))]},continuity_requirements=ids()),
        ['id','scene_id','start_frame','duration_frames','active_camera_id','clip_placements'])
    sequence=_doc('sequence',dict(id=identifier,version=semver,fps=fps,shots=arr(shot,1,4096),markers=arr(event,0,4096),total_frames=natural))
    project=_doc('project',dict(id=identifier,version=semver,name=text,fixture=boolean,fps=fps,
        coordinate_system={'const':{'handedness':'right','up_axis':'Y','unit':'meters'}},
        **{k:arr(ref('asset'),0,4096) for k in ('scene_assets','character_assets','rig_assets','clips','state_machines','retarget_profiles','sequences')},
        character_state_bindings=mapping(ref('asset'),4096),default_render_profile=ref('asset'),budgets=limits,provenance=ref('provenance')),['character_state_bindings','default_render_profile'])
    render=_doc('render',dict(id=identifier,project=ref('asset'),sequence_id=identifier,start_frame=frame,end_frame_exclusive=natural,
        width={'type':'integer','minimum':1,'maximum':32768},height={'type':'integer','minimum':1,'maximum':32768},fps=fps,
        output_format={'enum':['png_sequence','exr_sequence','mp4','webm']},renderer={'enum':['blender','supported_plugin']},quality={'enum':['preview','standard','final']},
        transparent_background=boolean,samples={'type':'integer','minimum':1,'maximum':4096},seed={'type':'integer','minimum':0,'maximum':2**32-1},output_directory=text,include_audio={'const':False}),['samples'])
    entry=obj(dict(target=ref('target'),owner=identifier,source=ref('asset'),minimum=num,maximum=num),['target','owner','source'])
    registry=_doc('registry',dict(id=identifier,version=semver,entries=arr(entry,0,10000)))
    return dict(asset=asset,transform=transform,provenance=provenance,time=time,target=target,key=key,track=track,event=event,
        placement=placement,layer=layer,constraint=constraint,project=project,clip=clip,**{'state-machine':machine},retarget=retarget,
        interaction=interaction,scene=scene,sequence=sequence,render=render,registry=registry)


def schemas():
    defs=_definitions()
    result={}
    for name in ROOTS:
        root=ref(name)
        # Nested records have no kind/version; standalone documents do.
        if name in ('track','target','layer','constraint'):
            root=_doc(name,{'value':ref(name)})
        needed=set()
        def collect(value):
            if isinstance(value,dict):
                if '$ref' in value:
                    dependency=value['$ref'].removeprefix('#/$defs/')
                    if dependency not in needed:
                        needed.add(dependency); collect(defs[dependency])
                for child in value.values(): collect(child)
            elif isinstance(value,list):
                for child in value: collect(child)
        collect(root)
        result['animation3d-'+name+'.schema.json']={'$schema':'https://json-schema.org/draft/2020-12/schema',
            'title':'Animation3D '+name+' v1',**deepcopy(root),'$defs':{key:deepcopy(defs[key]) for key in sorted(needed)}}
    return result


def target_key(target):
    return tuple(sorted(target.items()))


def _unique(items,key='id'):
    values=[item[key] for item in items]
    require(len(values)==len(set(values)),'ANIMATION_DUPLICATE','Duplicate '+key+'.',stage='animation3d')
    return set(values)


def _check_value(value,kind,prop):
    expected=TARGET_PROPERTIES[kind][prop]
    if expected=='boolean': ok=type(value) is bool
    elif expected=='float': ok=type(value) in (float,int)
    else: ok=isinstance(value,list) and len(value)==(3 if expected=='vec3' else 4) and all(type(x) in (int,float) for x in value)
    require(ok,'ANIMATION_VALUE','Value does not match target property.',stage='animation3d')
    if expected=='quaternion':
        require(abs(sum(x*x for x in value)-1)<2e-5,'ANIMATION_QUATERNION','Rotation must be normalized.',stage='animation3d')
    if prop=='scale': require(all(x>1e-10 for x in value),'ANIMATION_SCALE','Scale must be positive.',stage='animation3d')
    if prop in ('opacity','roughness','metallic','weight'):
        require(0<=value<=1,'ANIMATION_RANGE','Normalized property must be in [0,1].',stage='animation3d')


def validate_track(track, duration_frames=None):
    target=track['target']; kind=target['kind']; prop=target['property']
    require(track['value_type']==TARGET_PROPERTIES[kind][prop],'ANIMATION_VALUE','Declared value type differs from target.',stage='animation3d')
    _check_value(track['default_value'],kind,prop)
    previous=None
    for index,key in enumerate(track['keys']):
        position=frame_position(key['time'])
        require(previous is None or previous<position,'ANIMATION_TIME','Keys must be strictly increasing; duplicate policy is reject.',stage='animation3d')
        require(duration_frames is None or position<=duration_frames,'ANIMATION_TIME','Key exceeds terminal clip boundary.',stage='animation3d')
        previous=position; _check_value(key['value'],kind,prop)
        interpolation=key.get('interpolation','step' if track['value_type']=='boolean' else 'quaternion-slerp' if track['value_type']=='quaternion' else 'linear')
        require(interpolation in SUPPORTED_INTERPOLATION,'ANIMATION_INTERPOLATION','Interpolation is not executed by this foundation.',stage='animation3d')
        require((track['value_type']!='boolean' or interpolation=='step') and
            (track['value_type']!='quaternion' or interpolation in ('step','quaternion-slerp')) and
            (interpolation!='quaternion-slerp' or track['value_type']=='quaternion'),
            'ANIMATION_INTERPOLATION','Interpolation/value combination unsupported.',stage='animation3d')
        curve_fields=set(key)&{'ease_in','ease_out','in_handle','out_handle','tension','continuity','bias'}
        require(not curve_fields,'ANIMATION_INTERPOLATION','Unused curve controls cannot be silently ignored.',stage='animation3d')
        tangents=set(key)&{'in_tangent','out_tangent'}
        expected_tangents=set()
        if index and track['keys'][index-1].get('interpolation')=='hermite': expected_tangents.add('in_tangent')
        if index<len(track['keys'])-1 and interpolation=='hermite': expected_tangents.add('out_tangent')
        require(tangents==expected_tangents,'ANIMATION_TANGENT','Tangents must match the incoming/outgoing Hermite segments.',stage='animation3d')
        for name in tangents:
            tangent=key[name]; value=key['value']
            require(type(tangent) in (int,float) if type(value) in (int,float) else isinstance(tangent,list) and len(tangent)==len(value) and all(type(x) in (int,float) for x in tangent),
                    'ANIMATION_TANGENT','Tangent dimensions must match value; units are value/frame.',stage='animation3d')
    require(track['extrapolation_before']=='hold' and track['extrapolation_after']=='hold',
        'ANIMATION_EXTRAPOLATION','This foundation executes only hold extrapolation.',stage='animation3d')
    return track


def _check_condition_intersection(conditions, parameters):
    """Reject impossible conjunctions without evaluating user expressions."""
    grouped={}
    for condition in conditions: grouped.setdefault(condition['parameter'],[]).append(condition)
    for name,items in grouped.items():
        kind=parameters[name]['type']
        if kind=='trigger': continue
        if kind=='bool':
            possible={False,True}
            for item in items:
                possible &= {item['value']} if item['operator']=='eq' else {not item['value']}
            require(bool(possible),'ANIMATION_CONDITION','Contradictory Boolean conditions.',stage='animation3d')
            continue
        low,high=-1e9,1e9; low_open=high_open=False; excluded=set()
        for item in items:
            op,value=item['operator'],item['value']
            if op=='neq': excluded.add(value)
            if op in ('eq','gt','gte'):
                if value>low: low,low_open=value,op=='gt'
                elif value==low: low_open=low_open or op=='gt'
            if op in ('eq','lt','lte'):
                if value<high: high,high_open=value,op=='lt'
                elif value==high: high_open=high_open or op=='lt'
        if kind=='int':
            low=math.floor(low)+1 if low_open else math.ceil(low)
            high=math.ceil(high)-1 if high_open else math.floor(high)
            valid=low<=high and (high-low+1)>sum(low<=x<=high for x in excluded)
        else:
            valid=low<high or low==high and not low_open and not high_open and low not in excluded
        require(valid,'ANIMATION_CONDITION','Contradictory numeric conditions.',stage='animation3d')


def _check_constraint(node):
    if 'target' in node:
        source={k:v for k,v in node['source'].items() if k!='property'}
        target={k:v for k,v in node['target'].items() if k!='property'}
        require(source!=target,'ANIMATION_CONSTRAINT','Constraint cannot target its source.',stage='animation3d')
    chain=node.get('chain_bones',[])
    require(len(chain)==len(set(chain)),'ANIMATION_CONSTRAINT','Constraint repeats chain bone.',stage='animation3d')
    if node['type'] in ('two_bone_ik','multi_bone_ik','foot_plant'):
        require(node['source']['kind']=='bone' and len(chain)>=3 and
            (node['type']!='two_bone_ik' or len(chain)==3) and
            ('target' in node or 'target_position' in node),
            'ANIMATION_CONSTRAINT','IK requires a bone source, valid chain and explicit target.',stage='animation3d')
    for key,low,high in [('position_limits','minimum','maximum'),('rotation_limits','minimum_rad','maximum_rad')]:
        if key in node:
            require(all(a<=b for a,b in zip(node[key][low],node[key][high])),
                'ANIMATION_CONSTRAINT','Constraint limits are reversed.',stage='animation3d')


def _check_layer(node):
    _unique(node['placements'])
    for placement in node['placements']:
        require(placement['layer_id']==node['id'],'ANIMATION_REFERENCE','Placement references a different layer.',stage='animation3d')
        require(placement['blend_in_frames']+placement['blend_out_frames']<=placement['duration_frames'],
            'ANIMATION_INTERVAL','Placement blends exceed its duration.',stage='animation3d')
        require('source_out_frame' not in placement or placement['source_in_frame']<placement['source_out_frame'],
            'ANIMATION_INTERVAL','Source trim is empty/reversed.',stage='animation3d')
    mask=node.get('bone_mask',{})
    included=mask.get('included_bones',[]); excluded=mask.get('excluded_bones',[])
    require(len(included)==len(set(included)) and len(excluded)==len(set(excluded)) and not set(included)&set(excluded),
        'ANIMATION_MASK','Bone mask repeats or contradicts a bone.',stage='animation3d')


def _walk(value, *, container=False):
    if isinstance(value,dict):
        if not container: yield value
        for key,child in value.items():
            # These schema-declared scalar maps are opaque annotations, not
            # embedded clocks, constraints or executable animation records.
            if key in {'metadata','payload','tool_versions','per_bone_weights','initial_state_parameters'}:
                continue
            if key=='parameters' and isinstance(child,dict): continue
            # These maps hold named typed records; inspect their values only.
            yield from _walk(child,container=key in {'character_state_bindings','actor_overrides'})
    elif isinstance(value,list):
        for child in value: yield from _walk(child)


def _semantic(value):
    for node in _walk(value):
        if set(node)=={'numerator','denominator'}: rate(node)
        if 'source' in node and 'weight' in node and 'type' in node and 'enabled' in node: _check_constraint(node)
        if 'placements' in node and 'blend_mode' in node: _check_layer(node)
        if 'animation_layers' in node: _unique(node['animation_layers'])
        if 'sha256' in node and 'path' in node:
            path=portable_name(node['path'])
            require(path==unicodedata.normalize('NFC',path),'ANIMATION_PATH','Paths must use NFC.',stage='animation3d')
        if set(node)=={'position','rotation','scale'}:
            _check_value(node['rotation'],'node','rotation'); _check_value(node['scale'],'node','scale')
        if 'keys' in node and 'target' in node: validate_track(node,value.get('duration_frames'))
        if 'start_frame' in node and 'end_frame' in node:
            require(node['start_frame']<node['end_frame'],'ANIMATION_INTERVAL','Empty/reversed interval.',stage='animation3d')
        if 'rotation_offset' in node: _check_value(node['rotation_offset'],'node','rotation')
    kind=value['kind']
    if kind==KINDS['clip']:
        _unique(value['tracks']); _unique(value['events']); _unique(value['contacts'])
        channels=[target_key(t['target']) for t in value['tracks']]
        require(len(set(channels))==len(channels),'ANIMATION_DUPLICATE','Duplicate clip target channel.',stage='animation3d')
        duration=value['duration_frames']
        require(all(e['frame']<=duration for e in value['events']) and all(c['end_frame']<=duration for c in value['contacts']),
            'ANIMATION_TIME','Event/contact exceeds clip duration.',stage='animation3d')
        for contact in value['contacts']:
            require(sum(key in contact for key in ('target_node','target_bone','target_anchor'))==1,
                'ANIMATION_CONTACT','Contact requires exactly one target.',stage='animation3d')
        markers={e['name'] for e in value['events'] if e['type']=='marker'}
        require(set(value.get('interruption_markers',[]))<=markers,'ANIMATION_REFERENCE','Missing interruption marker.',stage='animation3d')
        root=value['root_motion']; sources=set(root)&{'source_node','source_bone'}
        require(len(sources)==int(root['enabled']),'ANIMATION_ROOT','Enabled root motion requires exactly one source; disabled motion has none.',stage='animation3d')
    elif kind==KINDS['state-machine']:
        states=_unique(value['states']); _unique(value['transitions']); _unique(value['parameters'],'name')
        require(value['initial_state'] in states,'ANIMATION_REFERENCE','Missing initial state.',stage='animation3d')
        params={p['name']:p for p in value['parameters']}; priorities=set(); edges={s:set() for s in states}
        for state in value['states']:
            require(('clip' in state)^('blend_tree' in state),'ANIMATION_STATE','State needs exactly one clip or blend tree.',stage='animation3d')
        for transition in value['transitions']:
            source=transition['from_state']; dest=transition['to_state']
            require(source in states|{'*'} and dest in states,'ANIMATION_REFERENCE','Missing transition state.',stage='animation3d')
            for origin in states if source=='*' else [source]:
                priority=(origin,transition['priority'])
                require(priority not in priorities,'ANIMATION_PRIORITY','Ambiguous transition priority.',stage='animation3d')
                priorities.add(priority)
            equalities={}
            for condition in transition['conditions']:
                if transition['condition_mode']=='all' and condition['operator']=='eq':
                    name=condition['parameter']
                    require(name not in equalities or equalities[name]==condition.get('value'),
                        'ANIMATION_CONDITION','Contradictory equality conditions.',stage='animation3d')
                    equalities[name]=condition.get('value')
                require(condition['parameter'] in params,'ANIMATION_REFERENCE','Missing condition parameter.',stage='animation3d')
                parameter=params[condition['parameter']]; ptype=parameter['type']; op=condition['operator']
                require((ptype=='trigger' and op=='triggered' and 'value' not in condition) or
                    (ptype!='trigger' and op!='triggered' and 'value' in condition and
                     ((ptype=='bool' and type(condition['value']) is bool and op in ('eq','neq')) or
                      (ptype in ('float','int') and type(condition['value']) in (int,float) and (ptype!='int' or type(condition['value']) is int)))),
                    'ANIMATION_CONDITION','Condition operator/value mismatches parameter.',stage='animation3d')
            if transition['condition_mode']=='all': _check_condition_intersection(transition['conditions'],params)
            for origin in states if source=='*' else [source]: edges[origin].add(dest)
        visited=set(); pending=[value['initial_state']]
        while pending:
            current=pending.pop()
            if current not in visited: visited.add(current); pending.extend(edges[current]-visited)
        require(visited==states,'ANIMATION_STATE','Unreachable state.',stage='animation3d')
        # Zero-duration unconditional cycles cannot advance time. Conditions may
        # legitimately form animation loops; the runtime transition limit stays explicit.
        zero={s:set() for s in states}
        for t in value['transitions']:
            if t['duration_frames']==0 and 'exit_marker' not in t and 'exit_time_normalized' not in t:
                for s in states if t['from_state']=='*' else [t['from_state']]: zero[s].add(t['to_state'])
        # Kahn's algorithm is bounded by graph size, not Python recursion depth.
        incoming={s:0 for s in states}
        for destinations in zero.values():
            for destination in destinations: incoming[destination]+=1
        ready=[s for s,count in incoming.items() if count==0]; processed=0
        while ready:
            source=ready.pop(); processed+=1
            for destination in zero[source]:
                incoming[destination]-=1
                if incoming[destination]==0: ready.append(destination)
        require(processed==len(states),'ANIMATION_CYCLE','Potential zero-time transition cycle.',stage='animation3d')
    elif kind==KINDS['retarget']:
        _unique(value['mappings'],'source_bone'); _unique(value['mappings'],'target_bone')
        require(('explicit_scale' in value)==(value['scale_policy']=='explicit'),'ANIMATION_RETARGET','Explicit scale policy needs exactly one value.',stage='animation3d')
    elif kind==KINDS['interaction']:
        actors=_unique(value['participants'],'actor_id'); previous=-1; holder=None; initialized=False
        for transfer in value.get('ownership_changes',[]):
            require(value['start_frame']<=transfer['frame']<value['end_frame'] and transfer['frame']>previous,
                'ANIMATION_INTERVAL','Ownership events must be unique, ordered and inside interaction.',stage='animation3d')
            previous=transfer['frame']
            for key in ('previous_owner','next_owner'):
                require(key not in transfer or transfer[key] in actors,'ANIMATION_REFERENCE','Ownership actor is not a participant.',stage='animation3d')
            require(not initialized or transfer.get('previous_owner')==holder,'ANIMATION_OWNERSHIP','Broken ownership chain.',stage='animation3d')
            holder=transfer.get('next_owner'); initialized=True
    elif kind==KINDS['sequence']:
        _unique(value['shots']); _unique(value['markers'])
        end=0
        for index,shot in enumerate(value['shots']):
            require(shot['start_frame']==end,'ANIMATION_SEQUENCE','Shots must form a contiguous non-overlapping partition.',stage='animation3d')
            require(shot.get('transition_to_next',{'type':'cut'})['type']=='cut','ANIMATION_TRANSITION','Dissolve sampling requires downstream sequencing support.',stage='animation3d')
            end+=shot['duration_frames']
        require(end==value['total_frames'] and all(e['frame']<=end for e in value['markers']),
            'ANIMATION_SEQUENCE','Sequence duration/marker mismatch.',stage='animation3d')
    elif kind==KINDS['scene']:
        _unique(value['actors'],'actor_id'); _unique(value['interactions']); _unique(value['markers'])
        for field,targetkind in [('object_tracks','node'),('environment_tracks','environment'),('camera_tracks','camera'),('lighting_tracks','light')]:
            _unique(value[field]); require(all(t['target']['kind']==targetkind for t in value[field]),'ANIMATION_TARGET','Track in wrong owner domain.',stage='animation3d')
    elif kind==KINDS['render']:
        require(value['start_frame']<value['end_frame_exclusive'],'ANIMATION_INTERVAL','Render interval must be nonempty.',stage='animation3d')
        portable_name(value['output_directory'])
    elif kind==KINDS['registry']:
        channels=[target_key(e['target']) for e in value['entries']]
        require(len(set(channels))==len(channels),'ANIMATION_DUPLICATE','Duplicate registry target.',stage='animation3d')
        for entry in value['entries']:
            require(('minimum' in entry)==('maximum' in entry) and entry.get('minimum',0)<=entry.get('maximum',0),
                'ANIMATION_RANGE','Registry bounds need ordered minimum/maximum.',stage='animation3d')
    elif kind==KINDS['project']:
        for key in ('scene_assets','character_assets','rig_assets','clips','state_machines','retarget_profiles','sequences'):
            _unique(value[key])
        require(value['fixture']==value['provenance']['fixture'],'ANIMATION_PROVENANCE','Fixture provenance mismatch.',stage='animation3d')


def validate_contract(value):
    finite_json(value)
    require(isinstance(value,dict) and value.get('kind') in KINDS.values(),'ANIMATION_KIND','Unknown animation document kind.',stage='animation3d')
    try:
        from jsonschema import Draft202012Validator
    except ImportError as exc:
        raise RigError('ANIMATION_DEPENDENCY',
            'Install requirements-rig.txt to validate animation contracts.',stage='animation3d') from exc
    name=next(name for name,kind in KINDS.items() if kind==value['kind'])
    error=next(Draft202012Validator(schemas()['animation3d-'+name+'.schema.json']).iter_errors(value),None)
    if error:
        raise RigError('ANIMATION_SCHEMA',error.message[:2048],stage='animation3d',asset='/'+'/'.join(map(str,error.absolute_path)))
    _semantic(value)
    return value
