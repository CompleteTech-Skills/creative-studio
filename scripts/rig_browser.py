"""Independent Three.js loading, playback and actual skinned-vertex verification."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import numpy as np
from rig_errors import require, sha256


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self,*args):pass


def verify_browser(rig,viewer,*,screenshot=None,headless=True):
    from playwright.sync_api import sync_playwright
    from rig_animation import evaluate_animation
    from rig_skinning import evaluate_deformation
    from rig_models import rig_signature
    from rig_errors import read_json
    viewer_config=read_json(Path(viewer)/'review.json')
    require(viewer_config['rig_sha256']==rig_signature(rig.manifest),
            'RIG_BROWSER_STALE','Regenerate the viewer for the current rig revision.')
    aliases=viewer_config.get('clip_aliases',{})
    require(isinstance(aliases,dict) and all(isinstance(k,str) and isinstance(v,str) and v in rig.clips
            for k,v in aliases.items()),'RIG_BROWSER_CLIP_ALIAS','Viewer aliases must bind clips in the current rig.')
    reduced=viewer_config.get('reduced_motion_clip',aliases.get('neutral','neutral' if 'neutral' in rig.clips else None))
    require(reduced is None or isinstance(reduced,str) and reduced in rig.clips,
            'RIG_BROWSER_REDUCED','Reduced-motion target is absent from this rig version.')
    server=ThreadingHTTPServer(('127.0.0.1',0),partial(QuietHandler,directory=str(viewer)))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(headless=headless,args=['--no-sandbox','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            page=browser.new_page(viewport=dict(width=1400,height=1000),device_scale_factor=1)
            errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{server.server_port}/',wait_until='networkidle')
            try:page.wait_for_function('window.hatchReady === true',timeout=30000)
            except Exception:
                require(False,'RIG_BROWSER_LOAD','The offline viewer did not finish loading.',observed=errors+[page.locator('#errors').inner_text()])
            indices={m.id:np.linspace(0,len(m.positions)-1,min(16,len(m.positions)),dtype=int).tolist() for m in rig.geometry.meshes}
            maximum=0.;compared=0;clip_reports=[]
            for name,clip in rig.clips.items():
                for fraction in (0.,.37,.73):
                    t=clip['duration']*fraction
                    sample=page.evaluate('args=>window.hatchReview.sample(...args)',[name,t,indices])
                    native=evaluate_deformation(rig,evaluate_animation(rig,clip,t)['pose'])
                    local=0.
                    for mesh in rig.geometry.meshes:
                        require(mesh.id in sample['meshes'],'RIG_BROWSER_MESH','Three.js did not load a required mesh.',asset=mesh.id)
                        error=float(np.linalg.norm(native[mesh.id]['positions'][indices[mesh.id]]-np.array(sample['meshes'][mesh.id]),axis=1).max())
                        local=max(local,error);compared+=len(indices[mesh.id])
                    maximum=max(maximum,local);clip_reports.append(dict(clip=name,time=t,max_vertex_error_m=local))
            # Exercise controls through the DOM, not only internal sampling helpers.
            selected='walk' if 'walk' in rig.clips else list(rig.clips)[-1]
            page.select_option('#clip',selected);page.click('#play');page.wait_for_timeout(200);page.click('#play')
            advanced=page.evaluate('window.hatchReview.time');require(advanced>0,'RIG_BROWSER_PLAYBACK','Play did not advance animation time.')
            page.check('#skeleton');page.check('#wireframe');page.check('#weights');page.select_option('#bone',rig.skeleton['bones'][1]['id'])
            page.click('#step');step_time=page.evaluate('window.hatchReview.time');require(step_time>advanced,'RIG_BROWSER_STEP','Frame stepping did not advance time.')
            page.uncheck('#weights');page.uncheck('#wireframe');page.uncheck('#skeleton')
            expressions=[name for name,value in rig.manifest['facial']['expressions'].items() if value['morphs'] or value['bones']]
            expression_edit=False
            if expressions:
                page.click('#reset-pose')
                snapshot="()=>{const values=[];window.hatchReview.scene.traverse(o=>{if(o.isBone)values.push(...o.quaternion.toArray());if(o.morphTargetInfluences)values.push(...o.morphTargetInfluences);});return values;}"
                rest=np.array(page.evaluate(snapshot));page.select_option('#expression',expressions[0])
                changed=np.array(page.evaluate(snapshot));require(np.max(np.abs(changed-rest))>1e-5,'RIG_BROWSER_EXPRESSION','An authored expression did not change the actual scene.')
                page.select_option('#expression','');released=np.array(page.evaluate(snapshot))
                require(np.max(np.abs(released-rest))<1e-6,'RIG_BROWSER_EXPRESSION_RELEASE','Neutral face did not release the authored expression.')
                expression_edit=True
            ik_edit=False
            if rig.manifest['controllers']['ik']:
                chain=rig.manifest['controllers']['ik'][0]
                page.select_option('#control-mode','ik');page.select_option('#ik-chain',chain['id'])
                target=page.evaluate('window.hatchReview.ikTarget()');target[1]+=.02
                solved=page.evaluate('args=>window.hatchReview.solveIK(...args)',[chain['id'],target])
                require(np.isfinite(solved['residual_m']),'RIG_BROWSER_IK','Interactive IK returned a nonfinite residual.')
                ik_edit=True;page.select_option('#control-mode','fk');page.click('#reset-pose')
            page.check('#reduced');require(page.input_value('#clip')==(reduced or ''),'RIG_BROWSER_REDUCED','Reduced motion did not select the declared clip or actual rest pose.')
            stopped=page.evaluate('window.hatchReview.time');page.wait_for_timeout(120)
            require(page.evaluate('window.hatchReview.time')==stopped,'RIG_BROWSER_REDUCED','Reduced motion did not stop playback.')
            page.uncheck('#reduced');page.select_option('#clip',selected)
            page.evaluate('window.hatchReview.evaluate(.65)')
            if screenshot:
                Path(screenshot).parent.mkdir(parents=True,exist_ok=True);page.screenshot(path=str(screenshot),full_page=True)
            version=browser.version
            require(not errors,'RIG_BROWSER_ERRORS','Browser execution errors occurred.',observed=errors)
            report=dict(reader='Three.js',version='0.180.0',browser=version,independent=True,ok=maximum<=.01,rig_sha256=rig_signature(rig.manifest),
                        max_vertex_error_m=maximum,tolerance_m=.01,compared_vertices=compared,clips=clip_reports,
                        ui_playback=True,ui_frame_step=True,ui_skeleton=True,ui_weights=True,ui_ik=ik_edit,ui_expressions=expression_edit,ui_reduced_motion=True,
                        errors=errors,asset_sha256=sha256(Path(viewer)/'character.glb'))
            if screenshot:report['screenshot_sha256']=sha256(screenshot)
            browser.close();return report
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
