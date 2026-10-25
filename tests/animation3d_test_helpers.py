"""Synthetic contract fixtures; these are not rendered production evidence."""
from copy import deepcopy
from animation3d_contracts import KINDS

FPS=dict(numerator=24,denominator=1)
ASSET=dict(id='source',version='1.0.0',path='source.json',sha256='a'*64,kind='rig')
PROVENANCE=dict(origin='synthetic-contract-fixture',kind='fixture',fixture=True,sources=[])
TARGET=dict(kind='bone',actor_id='actor',bone_id='root',property='translation')
TRACK=dict(id='move',target=TARGET,value_type='vec3',default_value=[0,0,0],keys=[
    dict(time=dict(frame=0),value=[0,0,0],interpolation='linear'),
    dict(time=dict(frame=24),value=[1,0,0],interpolation='linear')],
    extrapolation_before='hold',extrapolation_after='hold',enabled=True)
LAYER=dict(id='body',name='Body',priority=0,blend_mode='override',weight=1,muted=False,solo=False,placements=[])
CONSTRAINT=dict(id='aim',type='position',source=TARGET,target_position=[1,0,0],weight=1,enabled=True)


def samples():
    out={name:dict(schema_version=1,kind=kind) for name,kind in KINDS.items()}
    for name in ('track','target','layer','constraint'):
        out[name]['value']=deepcopy(dict(track=TRACK,target=TARGET,layer=LAYER,constraint=CONSTRAINT)[name])
    out['clip'].update(id='clip',version='1.0.0',name='Clip',fps=FPS,duration_frames=24,tags=[],categories=[],loop_mode='once',completion_mode='hold',default_speed=1,
        tracks=[TRACK],events=[],contacts=[],root_motion=dict(enabled=False,translation_axes=[True]*3,extract_rotation=False,mode='in_place'),interruption_policy='any_frame',provenance=PROVENANCE,dependencies=[])
    out['state-machine'].update(id='machine',version='1.0.0',parameters=[],states=[dict(id='idle',name='Idle',clip=ASSET,playback_speed=1)],
        transitions=[],initial_state='idle',transition_limit_per_frame=4,provenance=PROVENANCE)
    out['retarget'].update(id='retarget',version='1.0.0',source_rig=ASSET,target_rig={**ASSET,'id':'other'},source_skeleton_signature='a'*64,target_skeleton_signature='b'*64,
        source_rest_pose='rest',target_rest_pose='rest',mappings=[dict(source_bone='a',target_bone='b',rotation_offset=[0,0,0,1],mode='rotation')],root_policy='in_place',scale_policy='explicit',explicit_scale=1,
        preserve_end_effectors=False,preserve_contacts=False,missing_bone_policy='error',validation_thresholds=dict(maximum_joint_error_rad=.1,maximum_contact_error_m=.01,maximum_root_drift_m=.01),provenance=PROVENANCE)
    out['interaction'].update(id='handoff',participants=[dict(actor_id='a',role='giver'),dict(actor_id='b',role='receiver')],start_frame=0,end_frame=24,
        synchronization_markers=[],constraints=[],position_tolerance_m=.01,orientation_tolerance_rad=.01,failure_policy='error')
    out['scene'].update(id='scene',version='1.0.0',scene_asset=ASSET,actors=[],object_tracks=[],environment_tracks=[],camera_tracks=[],lighting_tracks=[],interactions=[],markers=[])
    out['sequence'].update(id='sequence',version='1.0.0',fps=FPS,shots=[dict(id='shot',scene_id='scene',start_frame=0,duration_frames=24,active_camera_id='camera',clip_placements=[])],markers=[],total_frames=24)
    out['project'].update(id='project',version='1.0.0',name='Fixture',fixture=True,fps=FPS,coordinate_system=dict(handedness='right',up_axis='Y',unit='meters'),
        **{name:[] for name in ('scene_assets','character_assets','rig_assets','clips','state_machines','retarget_profiles','sequences')},
        budgets={name:100 for name in ('max_frames','max_tracks','max_keyframes','max_actors','max_bones_per_rig','max_vertices','max_morph_targets','max_scene_nodes','max_memory_bytes','max_work_bytes','max_render_pixels','max_parallel_jobs')},provenance=PROVENANCE)
    out['render'].update(id='job',project=ASSET,sequence_id='sequence',start_frame=0,end_frame_exclusive=24,width=64,height=64,fps=FPS,
        output_format='png_sequence',renderer='blender',quality='preview',transparent_background=False,seed=0,output_directory='output',include_audio=False)
    out['registry'].update(id='registry',version='1.0.0',entries=[])
    return deepcopy(out)
