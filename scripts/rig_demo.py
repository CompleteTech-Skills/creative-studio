"""Reproducible real 3D fixtures, authored landmarks and executable motion demos.

These meshes are synthetic engineering fixtures, never accepted production art.
All screenshots and measurements derive from their actual weighted triangles.
"""
from __future__ import annotations
from copy import deepcopy
import math
from pathlib import Path
import numpy as np
from rig_animation import clip_from_poses
from rig_constraints import apply_constraints
from rig_corrections import replace_fit
from rig_errors import atomic_json, require, sha256
from rig_facial import apply_expressions, appendage_controls, finger_controls, gaze_at
from rig_geometry import vertex_normals
from rig_gltf import export_geometry, export_gltf
from rig_ik import solve_ik
from rig_jobs import create_run, save_rig
from rig_math import axis_angle, decompose, unit
from rig_models import Geometry, Mesh, load_rig
from rig_skeleton import evaluate_fk, neutral_pose, rotate_bone
from rig_skinning import generate_skin_weights
from rig_templates import generate_skeleton, load_rig_template
from rig_validation import validate_rig


def ellipsoid(name,center,radii,material,region='',*,latitudes=10,longitudes=16,blink=False):
    center=np.array(center);radii=np.array(radii);points=[[0.,1.,0.]]
    for i in range(1,latitudes):
        theta=math.pi*i/latitudes
        points.extend([[math.sin(theta)*math.cos(2*math.pi*j/longitudes),math.cos(theta),
                        math.sin(theta)*math.sin(2*math.pi*j/longitudes)] for j in range(longitudes)])
    points.append([0.,-1.,0.]);faces=[];bottom=len(points)-1
    for j in range(longitudes):
        nxt=(j+1)%longitudes;faces.append([0,1+nxt,1+j])
        for i in range(latitudes-2):
            a=1+i*longitudes+j;b=1+i*longitudes+nxt;c=a+longitudes;d=b+longitudes
            faces.extend([[a,b,c],[b,d,c]])
        a=1+(latitudes-2)*longitudes+j;b=1+(latitudes-2)*longitudes+nxt
        faces.append([a,b,bottom])
    points=center+np.array(points)*radii;faces=np.array(faces,dtype=np.uint32)
    mesh=Mesh(name,points,faces,material,{'NORMAL':vertex_normals(points,faces)},name=name,region=region)
    if blink:
        delta=np.zeros_like(points);delta[:,1]=-(points[:,1]-center[1])*.92
        mesh.morphs['blink']={'POSITION':delta}
    return mesh


def tube(name,points,radii,material,region='',*,sides=12,subdivisions=3,flatten=1.):
    points=np.asarray(points,float);radii=np.asarray(radii if np.ndim(radii) else [radii]*len(points),float)
    centers=[];sizes=[]
    for i in range(len(points)-1):
        for t in np.linspace(0,1,subdivisions,endpoint=False):centers.append(points[i]*(1-t)+points[i+1]*t);sizes.append(radii[i]*(1-t)+radii[i+1]*t)
    centers.append(points[-1]);sizes.append(radii[-1]);centers=np.array(centers);vertices=[]
    previous_axis=None
    for i,center in enumerate(centers):
        tangent=unit(centers[min(i+1,len(centers)-1)]-centers[max(i-1,0)])
        if previous_axis is None:
            anchor=np.eye(3)[int(np.argmin(np.abs(tangent)))];a=unit(np.cross(tangent,anchor))
        else:a=unit(previous_axis-tangent*np.dot(previous_axis,tangent))
        b=np.cross(tangent,a);previous_axis=a
        for j in range(sides):
            angle=2*math.pi*j/sides;vertices.append(center+sizes[i]*(a*math.cos(angle)+b*math.sin(angle)*flatten))
    vertices.extend([centers[0],centers[-1]]);faces=[]
    for i in range(len(centers)-1):
        for j in range(sides):
            a=i*sides+j;b=i*sides+(j+1)%sides;c=a+sides;d=b+sides
            faces.extend([[a,b,c],[b,d,c]])
    for j in range(sides):
        faces.append([len(vertices)-2,(j+1)%sides,j])
        faces.append([len(vertices)-1,(len(centers)-1)*sides+j,(len(centers)-1)*sides+(j+1)%sides])
    vertices=np.array(vertices);faces=np.array(faces,dtype=np.uint32)
    # The tube rings are oriented by their tangents; reverse if the closed volume is negative.
    tri=vertices[faces]
    if np.einsum('ij,ij->i',tri[:,0],np.cross(tri[:,1],tri[:,2])).sum()<0:faces=faces[:,[0,2,1]]
    return Mesh(name,vertices,faces,material,{'NORMAL':vertex_normals(vertices,faces)},name=name,region=region)


def make_geometry(archetype,*,proportions=(1.,1.,1.)):
    template=load_rig_template(archetype);bones={b['id']:b for b in template['bones']};meshes=[];regions={}
    palette={'humanoid':[[.07,.48,.52,1],[1.,.46,.16,1],[.035,.10,.17,1]],
             'quadruped':[[.83,.43,.13,1],[1.,.79,.39,1],[.16,.08,.05,1]],
             'winged-creature':[[.32,.20,.61,1],[.13,.76,.73,1],[.075,.05,.17,1]],
             'robotic':[[.30,.40,.52,1],[1.,.59,.13,1],[.07,.10,.15,1]]}[archetype]
    materials=[dict(name=name,pbrMetallicRoughness=dict(baseColorFactor=color,metallicFactor=.12 if archetype=='robotic' else 0.,roughnessFactor=.55),doubleSided=False)
               for name,color in zip(('body','accent','dark','eye-white'),palette+[[.94,.97,1.,1]])]
    def add(mesh,allowed=None):
        meshes.append(mesh)
        if allowed:regions[mesh.id]=allowed
    def limb(name,ids,radii=.07,mat=0):
        points=[bones[x]['head'] for x in ids]+[bones[ids[-1]]['tail']]
        add(tube(name,points,radii,mat),ids)
    if archetype=='humanoid':
        add(ellipsoid('torso',[0,1.24,0],[.25,.29,.16],0),['pelvis','spine','chest'])
        add(ellipsoid('hips',[0,.98,0],[.23,.16,.15],2),['pelvis'])
        add(tube('neck-mesh',[[0,1.46,0],[0,1.66,0]],.085,1,'neck'))
        add(ellipsoid('head-mesh',[0,1.79,.015],[.25,.27,.225],0,'head'))
        add(ellipsoid('jaw-mesh',[0,1.66,.18],[.15,.07,.08],1,'jaw'))
        add(ellipsoid('nose',[0,1.77,.25],[.045,.035,.035],2,'head'))
        add(ellipsoid('chest-badge',[0,1.32,.157],[.07,.075,.02],1,'chest'))
        for side,sign in [('left',-1),('right',1)]:
            limb('arm-'+side,['upper-arm.'+side,'forearm.'+side],[.09,.068,.052],0)
            limb('leg-'+side,['thigh.'+side,'shin.'+side],[.11,.085,.065],0)
            add(ellipsoid('boot-'+side,[sign*.15,.085,.10],[.105,.08,.19],2,'foot.'+side))
            add(ellipsoid('hand-'+side,bones['hand.'+side]['head'],[.07,.07,.055],1,'hand.'+side))
            for finger in ('thumb','index','middle','ring','pinky'):
                limb('finger-'+finger+'-'+side,[f'{finger}.{i}.{side}' for i in (1,2,3)],.013,1)
            add(ellipsoid('ear-'+side,[sign*.25,1.79,0],[.07,.095,.065],1,'ear.'+side))
    elif archetype in ('quadruped','winged-creature'):
        add(ellipsoid('body-mesh',[0,.72,-.04],[.31,.25,.58],0),['pelvis','spine','chest'])
        add(tube('neck-mesh',[[0,.79,.33],[0,.96,.54]],[.18,.16],0),['neck','chest'])
        add(ellipsoid('head-mesh',[0,.99,.64],[.235,.19,.25],0,'head'))
        add(ellipsoid('muzzle',[0,.91,.82],[.17,.10,.15],1,'jaw'))
        add(ellipsoid('nose',[0,.95,.945],[.075,.05,.045],2,'head'))
        positions=['front','rear']+(['middle'] if archetype=='winged-creature' else [])
        for position in positions:
            for side in ('left','right'):
                suffix=position+'.'+side;limb('leg-'+suffix,['upper-leg.'+suffix,'lower-leg.'+suffix],[.085,.065,.05])
                center=np.array(bones['paw.'+suffix]['head'])+[0,-.035,.055]
                add(ellipsoid('paw-'+suffix,center,[.085,.065,.13],1,'paw.'+suffix))
        limb('tail-mesh',['tail.'+str(i) for i in (1,2,3,4)],[.075,.06,.045,.032,.012],0)
        for side in ('left','right'):limb('ear-'+side,['ear.'+side],[.075,.035],2)
        if archetype=='winged-creature':
            for side in ('left','right'):
                ids=['wing-root.'+side,'wing-tip.'+side]
                add(tube('wing-membrane-'+side,[bones[x]['head'] for x in ids]+[bones[ids[-1]]['tail']],
                         [.18,.31,.035],1,flatten=.12,subdivisions=5,sides=16),ids)
                limb('wing-strut-'+side,ids,[.035,.025,.012],2)
    else:
        add(ellipsoid('base-shell',[0,.14,0],[.28,.14,.25],2,'base'))
        for name in ('shoulder','elbow','gripper','jaw.left','jaw.right'):
            limb(name+'-shell',[name],.07 if name in ('shoulder','elbow') else .035,0 if name in ('shoulder','elbow') else 1)
            add(ellipsoid(name+'-joint',bones[name]['head'],[.09]*3,1,name,latitudes=6,longitudes=12))
    if archetype!='robotic':
        for side in ('left','right'):
            eye='eye.'+side;center=np.array(bones[eye]['head'])
            add(ellipsoid('eye-white-'+side,center,[.071,.073,.045],3,eye,blink=True))
            add(ellipsoid('pupil-'+side,center+[0,0,.04],[.031,.041,.013],2,eye,latitudes=8,longitudes=12,blink=True))
    factor=np.array(proportions,dtype=float)
    for mesh in meshes:
        mesh.positions*=factor
        for target in mesh.morphs.values():target['POSITION']*=factor
        mesh.attributes['NORMAL']=vertex_normals(mesh.positions,mesh.triangles)
    landmarks={b['id']:dict(head=(np.array(b['head'])*factor).tolist(),tail=(np.array(b['tail'])*factor).tolist(),
                approved=True,source='procedural-fixture',confidence=1.,reference='Authored fixture mesh and skeleton share these explicit construction landmarks.') for b in template['bones']}
    return Geometry(meshes,materials),template,landmarks,regions


def author_motions(rig,archetype,*,fps=12):
    skeleton=rig.skeleton;world=evaluate_fk(skeleton);bones={b['id']:b for b in skeleton['bones']}
    chains={c['id']:c for c in rig.manifest['controllers']['ik']};duration=2.;times=np.linspace(0,duration,int(fps*duration)+1).tolist()
    clips={};root=skeleton['root'];constraints=rig.manifest['controllers']['constraints']
    def authored(name,fn,*,loop=False,contacts=None,events=None):
        poses=[fn(t) for t in times]
        clip=clip_from_poses(rig,name,times,poses,fps=fps,loop=loop,contacts=contacts,events=events)
        clips[name]=clip;return clip
    def rotate(pose,name,axis,value):return rotate_bone(skeleton,pose,name,axis_angle(axis,value))
    def foot_target(pose,chain,target):
        return solve_ik(skeleton,pose,chain,target,orientation=decompose(world[chain['bones'][-1]])['rotation'],constraints=constraints).pose
    authored('neutral',lambda t:neutral_pose(skeleton),loop=True)
    if archetype=='robotic':
        def hinge(t):
            pose=neutral_pose(skeleton);pose=rotate(pose,'shoulder',[0,0,1],math.sin(math.pi*t)*.8)
            pose=rotate(pose,'elbow',[0,0,1],math.sin(math.pi*t+.7)*.6)
            return apply_constraints(skeleton,pose,constraints)[0]
        authored('hinge-sweep',hinge,loop=True)
        def slide(t):
            p=hinge(t);p['bones'].setdefault('gripper',{})['translation']=(np.array(bones['gripper']['rest']['translation'])+[0,.06*(1-math.cos(math.pi*t)),0]).tolist()
            return apply_constraints(skeleton,p,constraints)[0]
        authored('gripper-slide',slide,loop=True);return clips
    leg_chains=[c for c in chains.values() if c['id'].startswith('leg.')]
    speed=.16 if archetype=='humanoid' else .22
    def gait(name,cycle,speed_,lift):
        stance=.62;contacts=[];events=[]
        offsets={c['id']:(0. if c['id'].endswith('left') else .5) for c in leg_chains}
        if archetype!='humanoid':
            for c in leg_chains:
                if '.rear.' in c['id']:offsets[c['id']]=(offsets[c['id']]+.5)%1
                if '.middle.' in c['id']:offsets[c['id']]=(offsets[c['id']]+.5)%1
        # Targets are world-space plants. Each next stance advances one stride.
        for c in leg_chains:
            offset=offsets[c['id']];rest=world[c['bones'][-1]][:3,3]
            for k in range(-1,int(duration/cycle)+2):
                start=(k-offset)*cycle;end=start+stance*cycle
                a=max(0.,start);b=min(duration,end)
                if b<=a:continue
                target=rest+[0,0,speed_*cycle*(k-offset+stance*.5)]
                ident=f'{c["id"]}-{k+2}'
                contacts.append(dict(schema_version=1,id=ident,chain=c['id'],start=a,end=b,target=target.tolist(),
                                     orientation=decompose(world[c['bones'][-1]])['rotation'],kind='foot' if archetype=='humanoid' else 'paw'))
                events.extend([dict(schema_version=1,time=a,type='foot_contact',target=c['bones'][-1]),
                               dict(schema_version=1,time=b,type='foot_release',target=c['bones'][-1])])
        def pose_at(t):
            p=neutral_pose(skeleton);p['bones'][root]=dict(translation=[0.,-.045,speed_*t])
            for c in leg_chains:
                phase=t/cycle+offsets[c['id']];k=math.floor(phase);phase-=k
                rest=world[c['bones'][-1]][:3,3];plant=speed_*cycle*(k-offsets[c['id']]+stance*.5)
                if phase < stance:target=rest+[0,0,plant]
                else:
                    u=(phase-stance)/(1-stance);smooth=u*u*(3-2*u)
                    target=rest+[0,lift*math.sin(math.pi*u),plant+speed_*cycle*smooth]
                p=foot_target(p,c,target)
            if archetype=='humanoid':
                for side,sign in [('left',1),('right',-1)]:p=rotate(p,'upper-arm.'+side,[1,0,0],sign*.22*math.sin(2*math.pi*t/cycle))
            else:p=appendage_controls(rig,p,time=t,tail_wag=.25)
            return p
        return authored(name,pose_at,loop=True,contacts=contacts,events=events)
    gait('walk',1.,speed,.09)
    if archetype=='humanoid':
        def wave(t):
            envelope=math.sin(math.pi*t/duration)**2;p=neutral_pose(skeleton)
            target=np.array([.45,1.7,.12])+[.09*math.sin(3*math.pi*t)*envelope,.04*math.cos(3*math.pi*t)*envelope,0]
            rest=world['hand.right'][:3,3];target=rest*(1-envelope)+target*envelope
            p=solve_ik(skeleton,p,chains['arm.right'],target,constraints=constraints).pose
            return finger_controls(rig,p,side='right',curl=.18,spread=.5)
        authored('wave',wave)
        def reach(t):
            u=math.sin(math.pi*t/duration)**2;p=neutral_pose(skeleton)
            target=world['hand.right'][:3,3]*(1-u)+np.array([.30,1.32,.50])*u
            p=solve_ik(skeleton,p,chains['arm.right'],target,constraints=constraints).pose
            return finger_controls(rig,p,side='right',curl=.4*u)
        authored('reach',reach)
        def crouch(t):
            u=math.sin(math.pi*t/duration)**2;p=neutral_pose(skeleton);p['bones'][root]=dict(translation=[0.,-.30*u,0.])
            p=rotate(p,'spine',[1,0,0],.25*u)
            for c in leg_chains:p=foot_target(p,c,world[c['bones'][-1]][:3,3])
            return p
        contacts=[dict(schema_version=1,id=c['id']+'-plant',chain=c['id'],start=0.,end=duration,
                       target=world[c['bones'][-1]][:3,3].tolist(),orientation=decompose(world[c['bones'][-1]])['rotation'],kind='foot') for c in leg_chains]
        authored('crouch',crouch,contacts=contacts)
        def blink(t):
            p=gaze_at(rig,neutral_pose(skeleton),[.7*math.sin(math.pi*t),1.8,3.],head_follow=.2)
            amount=max(0.,1-abs(t-.75)/.2)
            return apply_expressions(rig,p,{'blink':amount})
        authored('blink-expression',blink,loop=True)
    elif archetype=='quadruped':
        gait('trot',.5,.36,.13)
        def sit(t):
            u=math.sin(math.pi*t/duration)**2;p=neutral_pose(skeleton);p['bones'][root]=dict(translation=[0.,-.32*u,-.10*u])
            p=rotate(p,'pelvis',[1,0,0],-.75*u);p=rotate(p,'spine',[1,0,0],.18*u)
            for c in leg_chains:
                target=world[c['bones'][-1]][:3,3].copy()
                if '.rear.' in c['id']:target[2]+=.10*u
                p=foot_target(p,c,target)
            return p
        authored('sit',sit)
        authored('tail-wag',lambda t:appendage_controls(rig,neutral_pose(skeleton),time=t,tail_wag=.9),loop=True)
        authored('head-turn',lambda t:gaze_at(rig,neutral_pose(skeleton),[1.2*math.sin(math.pi*t),1.2,2.],head_follow=.55),loop=True)
    else:
        authored('wing-flap',lambda t:appendage_controls(rig,neutral_pose(skeleton),time=t,wing_flap=.9),loop=True)
        authored('tail-curl',lambda t:appendage_controls(rig,neutral_pose(skeleton),time=t,tail_curl=math.sin(math.pi*t/duration)**2,tail_wag=.25),loop=True)
        authored('gaze',lambda t:gaze_at(rig,neutral_pose(skeleton),[1.4*math.sin(math.pi*t),1.4,2.],head_follow=.5),loop=True)
        def combined(t):
            p=appendage_controls(rig,neutral_pose(skeleton),time=t,wing_flap=.65,wing_fold=.15,tail_wag=.7,tail_curl=.4)
            p=gaze_at(rig,p,[.8,1.25,2.],head_follow=.3)
            c=chains['leg.middle.right'];target=world[c['bones'][-1]][:3,3]+[.08,.15*math.sin(math.pi*t/duration)**2,.08]
            return foot_target(p,c,target)
        authored('combined',combined,loop=True)
    return clips


def create_demo(destination,archetype,*,identifier=None,proportions=(1.,1.,1.),fps=12,animate=True):
    destination=Path(destination).absolute();identifier=identifier or 'demo-'+archetype
    require(not destination.exists(),'RIG_DEMO_EXISTS','Generate a demonstration in a new directory.')
    geometry,template,landmarks,regions=make_geometry(archetype,proportions=proportions)
    destination.parent.mkdir(parents=True,exist_ok=True)
    source=destination.parent/(identifier+'-input.glb')
    export_geometry(geometry,source,provenance=dict(fixture=True,origin='procedural-fixture',generator='rig_demo.py',archetype=archetype))
    rig=create_run(source,destination,identifier=identifier,fixture=True,attribution='Hatch Pet procedural engineering fixture; generated by rig_demo.py.')
    fitted=generate_skeleton(rig.geometry,template,landmarks=landmarks,character_id=identifier)
    replace_fit(rig.manifest, fitted)
    rig.manifest['archetype']=archetype
    factor=np.array(proportions,float)
    for chain,original in zip(rig.manifest['controllers']['ik'],template['ik']):chain['pole']=(np.array(original['pole'])*factor).tolist()
    for socket,original in zip(rig.manifest['attachments'],template['sockets']):socket['transform']['translation']=(np.array(original['transform']['translation'])*factor).tolist()
    rig.manifest['facial']['eyes']=[b['id'] for b in rig.skeleton['bones'] if b['role'].startswith('eye.')]
    if any(m.morphs for m in rig.geometry.meshes):
        rig.manifest['facial']['expressions']['blink']=dict(schema_version=1,morphs={'blink':1.},bones={},incompatible=[])
    rig.weights=generate_skin_weights(rig.geometry,rig.skeleton,method='rigid' if archetype=='robotic' else 'diffusion',regions=regions,iterations=8)
    save_rig(rig,operation='fixture-skeleton-and-binding')
    if animate:rig.clips=author_motions(rig,archetype,fps=fps);save_rig(rig,operation='fixture-animation')
    atomic_json(destination/'source/landmarks.json',dict(source_sha256=sha256(source),landmarks=landmarks))
    atomic_json(destination/'source/weight-regions.json',regions)
    report=validate_rig(rig,require_animation=animate);atomic_json(destination/'validation/numeric.json',report)
    export=export_gltf(rig,destination/'exports/character.glb');atomic_json(destination/'validation/export.json',export)
    return rig,report


def main():
    import argparse,json
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',required=True)
    p.add_argument('--archetype',choices=['humanoid','quadruped','winged-creature','robotic'],required=True)
    p.add_argument('--fps',type=int,default=12);a=p.parse_args()
    rig,report=create_demo(a.destination,a.archetype,fps=a.fps)
    print(json.dumps(dict(path=str(rig.root),ok=report['ok'],metrics=report['metrics']),indent=2))
    if not report['ok']:raise SystemExit(1)


if __name__=='__main__':main()
