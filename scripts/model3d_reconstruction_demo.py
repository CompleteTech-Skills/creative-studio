"""Execute reconstruction evidence C/D/E/F without providers or artistic approval.

Defaults to retained Milo canonical artwork; original PNG bytes are preserved.
The artwork method is explicitly a sampled silhouette extrusion. Synthetic
calibrated-view fixtures are kept separate from the actual source artwork.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import numpy as np
from PIL import Image
from atomic_publish import _rename_no_replace
from model3d_reconstruction import read_image,mask_from_image,publish,_seal_result
from model3d_reconstruction_solids import extrude_contour,extrude_mask,revolve_profile,heightfield
from model3d_reconstruction_views import calibrated_view,carve_visual_hull,rasterize_silhouette
from model3d_primitives import primitive
from rig_errors import RigError,require

ROOT=Path(__file__).resolve().parents[1]
DEFAULT_ARTWORK=ROOT/'examples/production-pilot/acquisition/milo/references/canonical-base.png'


def demonstrate(destination, *, artwork=DEFAULT_ARTWORK):
    destination=Path(destination).absolute();parent=destination.parent.resolve(strict=True);destination=parent/destination.name
    require(not destination.exists() and not destination.is_symlink(),'RECON_EXISTS','Use a new demonstration destination.')
    stage=Path(tempfile.mkdtemp(prefix='.reconstruction-demo-',dir=parent))
    try:
        records={}
        source=read_image(artwork);mask=mask_from_image(source,mode='alpha',threshold=127)
        # Explicit point subsampling is a demo acquisition decision, not a
        # modification of original artwork, cleanup or physical calibration.
        stride=16;sample=mask[::stride,::stride]
        artwork_result=extrude_mask(sample,pixel_size=.005*stride,thickness=.2,
            evidence=[dict(**source.descriptor(),interpretation='authored alpha>127',sampling=dict(method='point-grid',stride_px=stride,offset_px=[0,0]),scale='explicit hypothesis0.005m per original pixel; not measured')])
        artwork_result.report['fixture']=False
        artwork_result.report['source_origin']='retained canonical artwork; local extrusion does not authenticate original artwork approval'
        # Added source-origin labels are part of the method report before delivery.
        _seal_result(artwork_result)
        records['C-artwork-limited-extrusion']=publish(artwork_result,stage/'C-artwork',evidence=[source])
        contour=extrude_contour([[0,0],[3,0],[3,2],[2,2],[2,3],[0,3]],holes=[[[.5,.5],[1.5,.5],[1.5,1.5],[.5,1.5]]],thickness=.3,
                               evidence=[dict(origin='authored sketch profile fixture',dimensions='explicit meters')])
        records['D-contour-with-hole']=publish(contour,stage/'D-contour',format='obj')
        revolved=revolve_profile([[0,0],[.5,.1],[.8,.5],[.6,1],[0,1.1]],segments=32,
                                evidence=[dict(origin='authored rotational profile fixture',axis='+Y',symmetry='explicit hypothesis')])
        records['D-revolution']=publish(revolved,stage/'D-revolution')
        relief=heightfield([[0,.1,.1,0],[.1,.4,.4,.1],[.1,.4,.4,.1],[0,.1,.1,0]],spacing=[.2,.2],base_thickness=.1,
                           evidence=[dict(origin='authored metric height fixture',image_depth_inference=False)])
        records['D-relief']=publish(relief,stage/'D-relief')
        # Only these rendered observations enter reconstruction. The hidden
        # known source mesh is used for forward rasterization and comparison.
        known=primitive('box').geometry();placeholder=np.ones((48,48),dtype=bool)
        worlds=[np.eye(4),np.array([[0,0,1,3],[0,1,0,0],[-1,0,0,0],[0,0,0,1.]]),np.array([[1,0,0,0],[0,0,1,3],[0,-1,0,0],[0,0,0,1.]])]
        worlds[0][2,3]=3
        observations=[];view_evidence=[]
        for name,world in zip(['front','side','top'],worlds):
            camera=dict(name=name,calibration='known',camera_to_world=world,orthographic=(-1,1,-1,1),near=.1,far=10)
            provisional=calibrated_view(placeholder,evidence=dict(origin='known synthetic source, forward mesh render'),**camera)
            observed=rasterize_silhouette(known,provisional)
            path=stage/(name+'-observation.png');Image.fromarray(observed.astype('uint8')*255).save(path)
            captured=read_image(path,role='mask');view_evidence.append(captured)
            observations.append(calibrated_view(observed,evidence=dict(**captured.descriptor(),origin='forward-rendered known synthetic box; no source mesh supplied to carving'),**camera))
        hull=carve_visual_hull(observations,[[-1,-1,-1],[1,1,1]],[16,16,16])
        require(all(metric['iou']>.98 for metric in hull.report['reprojection']),'RECON_DEMO','Reconstructed mesh does not agree with calibrated observations.')
        require(hull.geometry.vertex_count!=known.vertex_count,'RECON_DEMO','Hull must be a distinct reconstruction, not the source mesh.')
        records['E-calibrated-visual-hull']=publish(hull,stage/'E-hull',evidence=view_evidence)
        incomplete=carve_visual_hull([observations[0],calibrated_view(observations[0].mask,name='front-again',calibration='known',evidence=dict(origin='redundant front observation'),camera_to_world=worlds[0],orthographic=(-1,1,-1,1),near=.1,far=10)],[[-1,-1,-1],[1,1,1]],[16,16,16])
        records['F-redundant-views']=publish(incomplete,stage/'F-incomplete',evidence=[view_evidence[0]])
        failures={}
        try:calibrated_view(placeholder,name='unknown',calibration='unknown',evidence=dict(origin='uncalibrated drawing'),camera_to_world=worlds[0],orthographic=(-1,1,-1,1))
        except RigError as exc:failures['unknown-calibration']=exc.report
        else:raise AssertionError('Unknown camera admitted')
        left=np.zeros((48,48),dtype=bool);left[12:36,4:12]=True
        right=np.zeros((48,48),dtype=bool);right[12:36,36:44]=True
        contradict=[calibrated_view(value,name=name,calibration='known',evidence=dict(origin='deliberately contradictory synthetic observation'),camera_to_world=worlds[0],orthographic=(-1,1,-1,1)) for name,value in [('left',left),('right',right)]]
        try:carve_visual_hull(contradict,[[-1,-1,-1],[1,1,1]],[16,16,16])
        except RigError as exc:failures['contradiction']=exc.report
        else:raise AssertionError('Contradictory evidence admitted')
        for p in stage.glob('*-observation.png'):p.unlink() # exact demo-owned staging copies; immutable originals are in published source inventories
        report=dict(schema_version=1,demonstrations=records,rejected_evidence=failures,provider_calls=0,review='pending',
                    distinctions=dict(C='actual retained artwork, limited sampled extrusion',D='authored sketch/height fixtures',E='synthetic calibrated forward rendered observations; new voxel mesh',F='incomplete/contradictory/unknown calibration evidence'),
                    limitations=['No complete anatomical/texture recovery or artwork approval.', 'No provider, Blender authoring, full parent10 acceptance, or automatic calibration is claimed.'])
        report['files']={p.relative_to(stage).as_posix():dict(bytes=p.stat().st_size,sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in stage.rglob('*') if p.is_file()}
        (stage/'acceptance.json').write_text(json.dumps(report,indent=2)+'\n')
        _rename_no_replace(stage,destination);return report
    finally:
        if stage.exists():shutil.rmtree(stage)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True);parser.add_argument('--artwork',default=str(DEFAULT_ARTWORK))
    args=parser.parse_args(argv)
    try:
        report=demonstrate(args.destination,artwork=args.artwork)
        print(json.dumps(dict(ok=True,files=len(report['files']),demonstrations=list(report['demonstrations']),provider_calls=0)))
        return 0
    except (RigError,OSError) as exc:
        print(json.dumps(dict(ok=False,error=exc.report if isinstance(exc,RigError) else str(exc))));return 2

if __name__=='__main__':raise SystemExit(main())
