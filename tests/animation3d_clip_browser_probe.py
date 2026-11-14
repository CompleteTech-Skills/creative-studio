"""Display an actual Animation3D clip through the existing native Three.js owner.

This explicit adapter samples integer Animation3D frames into the legacy native
seconds-based clip format; it does not add a renderer or another evaluator.
"""
from pathlib import Path
import hashlib
import json
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'scripts'),str(ROOT/'tests')]
from animation3d_saved_clip_probe import run as saved_workflow
from animation3d_evaluator import RigClipEvaluator
from animation3d_clips import load_clip, source_time
from rig_models import load_rig
from rig_animation import clip_from_poses
from rig_jobs import save_rig
from rig_viewer import create_review_ui
from rig_browser import verify_browser
from rig_errors import timestamp


def visible_canvas_probe(viewer,destination):
    from functools import partial
    from http.server import ThreadingHTTPServer
    import threading
    import numpy as np
    from PIL import Image
    from playwright.sync_api import sync_playwright
    from rig_browser import QuietHandler
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(viewer)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as driver:
            browser=driver.chromium.launch(headless=True,args=['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport=dict(width=1400,height=1000),device_scale_factor=1)
            page.goto(f'http://127.0.0.1:{server.server_port}/',wait_until='networkidle')
            page.wait_for_function('window.hatchReady===true',timeout=30000)
            images=[]
            for name,time in [('frame0.png',0.),('frame650.png',.65)]:
                page.evaluate('time=>window.hatchReview.evaluate(time)',time)
                page.wait_for_timeout(100)
                path=destination/name
                page.locator('#viewport canvas').screenshot(path=str(path))
                images.append(np.asarray(Image.open(path).convert('RGBA')))
            assert images[0].shape==images[1].shape
            pixels=int(np.count_nonzero(np.any(images[0]!=images[1],axis=2)))
            assert pixels>200,('Actual fixed-camera WebGL canvas did not show sufficient rendered motion',pixels)
            browser.close()
            return dict(changed_canvas_pixels=pixels,source_times_seconds=[0.,.65],
                        scope='actual fixed-camera WebGL canvas comparison; UI labels excluded')
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)


def run(destination):
    destination=Path(destination).absolute();destination.mkdir(parents=True,exist_ok=False)
    proof=saved_workflow(destination/'workflow')
    original=destination/'workflow/source'
    working=destination/'preview-native'
    shutil.copytree(original/'native',working)
    rig=load_rig(working)
    # The engineering fixture is an oriented planar skinned strip. Use an
    # explicit two-sided debug material on the DERIVED preview copy so both
    # orbit directions display it; original source materials remain unchanged.
    for material in rig.geometry.materials:material['doubleSided']=True
    evaluator=RigClipEvaluator(original/'native','actor')
    body,face=load_clip(original/'body.json'),load_clip(original/'face.json')
    poses=[];clocks=[]
    for frame in range(25):
        body_time=dict(frame=frame)
        face_time=source_time(face,dict(frame=frame),dict(numerator=48,denominator=1))
        state=evaluator.evaluate_sources([(body,body_time),(face,face_time)])
        poses.append(state['pose']);clocks.append(dict(body=body_time,face=face_time,signature=state['dependency_signature']))
    provenance=dict(schema_version=1,tool_version='1.0.0',operation='Explicit Animation3D frame-to-native preview adapter',created_at=timestamp(),
        input_hashes={name:hashlib.sha256((original/name).read_bytes()).hexdigest() for name in ('body.json','face.json')},ancestry=[])
    clip=clip_from_poses(rig,'animation68-preview',[frame/24 for frame in range(25)],poses,
                         fps=24,loop=False,root_motion='embedded',provenance=provenance)
    rig.clips={clip['id']:clip}
    save_rig(rig,operation='Retained Animation3D browser preview fixture')
    viewer=destination/'viewer'
    create_review_ui(rig,viewer)
    report=verify_browser(rig,viewer,screenshot=destination/'preview.png')
    report['visible_canvas_motion']=visible_canvas_probe(viewer,destination)
    assert report['ok'] and report['ui_playback'] and report['ui_frame_step']
    assert report['compared_vertices']>0 and report['max_vertex_error_m']<.01
    after={str(p.relative_to(original)):hashlib.sha256(p.read_bytes()).hexdigest() for p in original.rglob('*') if p.is_file()}
    assert after==proof['source_hashes']
    report.update(fixture=True,source_workflow='Animation3D independent body/facial clips',
                  source_hashes_unchanged=True,frame_to_native_seconds_adapter=True,
                  preview_material_adapter='two-sided debug material on derived planar fixture only',
                  exact_source_clocks=clocks,visual_approval=False,
                  scope='actual compatible native rig/GLB loaded, played and frame-stepped in Three.js; not artistic approval')
    (destination/'browser-report.json').write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')
    (destination/'artifact-hashes.json').write_text(json.dumps({str(p.relative_to(destination)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in destination.rglob('*') if p.is_file()},indent=2,sort_keys=True)+'\n')
    return report


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path);args=parser.parse_args()
    report=run(args.destination)
    print(json.dumps(dict(ok=report['ok'],compared_vertices=report['compared_vertices'],maximum_vertex_error_m=report['max_vertex_error_m'],
                          actual_playback=report['ui_playback'],frame_step=report['ui_frame_step'],fixture=True,visual_approval=False)))
