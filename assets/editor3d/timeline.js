/* Exact saved-revision timeline. All persistent edits receive backend acknowledgement. */
export function mountTimeline({container,api,getSnapshot,getSelection,preview,clearPreview,reload,onSelect,registerCommitInterceptor,registerPreviewEditAuthorizer,refreshPreviewEditState,registerTimelineProvider}) {
  let snapshot=null,shotPath='',request=0,disposed=false,playing=false,clock=0,firstFrame=0,raf=0;
  const root=document.createElement('section');root.setAttribute('aria-label','Animation timeline');
  const status=document.createElement('p');status.setAttribute('role','status');
  const shot=document.createElement('select');shot.setAttribute('aria-label','Timeline shot');
  const frame=document.createElement('input');frame.type='number';frame.min='0';frame.step='1';frame.value='0';frame.setAttribute('aria-label','Exact frame');
  const property=document.createElement('select');property.setAttribute('aria-label','Animated property');
  for(const p of ['translation','rotation','scale','visible','light_intensity','world_exposure']){const o=document.createElement('option');o.value=p;o.textContent=p;property.append(o);}
  const value=document.createElement('input');value.value='[0,0,0]';value.setAttribute('aria-label','Key value as JSON');
  const interpolation=document.createElement('select');interpolation.setAttribute('aria-label','Track interpolation');
  for(const mode of ['linear','step','slerp']){const o=document.createElement('option');o.value=mode;o.textContent=mode;interpolation.append(o);}
  const autokey=document.createElement('input');autokey.type='checkbox';autokey.setAttribute('aria-label','Autokey transforms at selected frame');autokey.disabled=typeof registerCommitInterceptor!=='function';
  const autoLabel=document.createElement('label');autoLabel.append(autokey,document.createTextNode(' Autokey transforms'));
  const unregisterTimeline=registerTimelineProvider?.(()=>{
    const s=getSnapshot(),at=Number(frame.value),chosen=s.documents[shotPath];
    if(!s.project.shots.some(ref=>ref.path===shotPath)||chosen?.scene.path!==s.scene_path||!Number.isSafeInteger(at)||at<0||at>=chosen.duration_frames)return null;
    return {revision:s.revision,sha256:s.sha256,scene_path:s.scene_path,shot_path:shotPath,frame:{numerator:at,denominator:1}};
  });
  const unregisterPreview=registerPreviewEditAuthorizer?.(({snapshot:current,evaluation,selection=[]})=>{
    const chosen=current.documents[shotPath],overrides=chosen?.camera_overrides;
    if(selection.includes(chosen?.camera)&&(overrides?.transform||overrides?.[chosen?.camera]?.transform))return false;
    const at=Number(frame.value);
    return !playing&&autokey.checked&&Number.isSafeInteger(at)&&current.project.shots.some(ref=>ref.path===shotPath)&&
      current.documents[shotPath].scene.path===current.scene_path&&evaluation?.shot?.path===shotPath&&
      evaluation.revision===current.revision&&evaluation.snapshot_sha256===current.sha256&&
      evaluation.scene?.path===current.scene_path&&evaluation.frame?.numerator===at*evaluation.frame?.denominator;
  });
  autokey.addEventListener('change',()=>refreshPreviewEditState?.());
  frame.addEventListener('input',()=>{stop();++request;refreshPreviewEditState?.();});
  const unregister=registerCommitInterceptor?.(({commands,expected_revision,expected_sha256,transaction_id,snapshot:current})=>{
    if(!autokey.checked||!commands.some(c=>c.operation==='transform'))return null;
    if(playing)throw Error('Pause timeline playback before autokey editing.');
    if(commands.some(c=>c.operation!=='transform'))throw Error('Autokey requires a transform-only transaction.');
    if(!current.project.shots.some(ref=>ref.path===shotPath))throw Error('Reload and select the current shot before autokey.');
    if(current.documents[shotPath].scene.path!==current.scene_path)throw Error('Switch to the shot scene before autokey.');
    const chosen=current.documents[shotPath],overrides=chosen.camera_overrides;
    if(commands.some(cmd=>cmd.parameters.identifier===chosen.camera)&&(overrides?.transform||overrides?.[chosen.camera]?.transform))throw Error('Remove the shot camera transform override before autokey.');
    const at=Number(frame.value);if(!Number.isSafeInteger(at)||at<0||at>=current.documents[shotPath].duration_frames)throw Error('Autokey frame must be inside the selected shot.');
    const keys=[];for(const cmd of commands)for(const channel of ['translation','rotation','scale'])keys.push({operation:'key-upsert',document:shotPath,target:cmd.parameters.identifier,property:channel,frame:at,value:structuredClone(cmd.parameters.transform[channel])});
    return {route:'timeline/edit',body:{expected_revision,expected_sha256,transaction_id,commands:keys}};
  });
  const curve=document.createElementNS('http://www.w3.org/2000/svg','svg');curve.setAttribute('viewBox','0 0 500 180');curve.setAttribute('role','img');curve.setAttribute('aria-label','Evaluated channel curve');curve.style.width='100%';curve.style.maxHeight='180px';
  const curveLegend=document.createElement('p');curveLegend.textContent='Curve samples use native local channels before constraints.';
  async function drawCurve(){const s=getSnapshot(),serial=++request;const selectedTarget=target(),selectedProperty=property.value,selectedShot=shotPath;
    const result=await api('timeline/curve',{revision:s.revision,sha256:s.sha256,shot:selectedShot,target:selectedTarget,property:selectedProperty,sample_count:17});
    const currentTarget=property.value==='world_exposure'?'world':getSelection().length===1?getSelection()[0]:null;
    if(disposed||serial!==request||selectedProperty!==property.value||selectedShot!==shotPath||selectedTarget!==currentTarget||result.revision!==getSnapshot().revision||result.snapshot_sha256!==getSnapshot().sha256)return;
    curve.replaceChildren();const samples=result.samples,values=samples.map(sample=>Array.isArray(sample.value)?sample.value:[Number(sample.value)]),all=values.flat(),minimum=Math.min(...all),maximum=Math.max(...all),span=maximum-minimum||1;
    const colors=['#55ccff','#ffcc55','#ff77aa','#99dd66'];
    for(let component=0;component<values[0].length;component++){const line=document.createElementNS('http://www.w3.org/2000/svg','polyline');line.setAttribute('fill','none');line.setAttribute('stroke',colors[component]);line.setAttribute('stroke-width','2');line.setAttribute('points',values.map((value,index)=>`${20+460*index/(values.length-1)},${160-140*(value[component]-minimum)/span}`).join(' '));curve.append(line);}
    curveLegend.textContent=`${result.target} · ${result.property}: ${minimum} to ${maximum}. ${samples.length} exact native samples (${result.semantics.replaceAll('-',' ')}); edit saved keys below.`;
    message('Curve sampled from saved revision '+result.revision+'.');
  }
  const tracks=document.createElement('div');tracks.setAttribute('aria-label','Saved tracks and keys');
  function message(text){if(!disposed)status.textContent=text;}
  function button(label,fn){const b=document.createElement('button');b.textContent=label;b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>message(e.message)));return b;}
  function target(){const selected=getSelection();if(property.value==='world_exposure')return 'world';if(selected.length!==1)throw Error('Select one object for this channel.');return selected[0];}
  async function edit(commands){stop();const s=getSnapshot();await api('timeline/edit',{expected_revision:s.revision,expected_sha256:s.sha256,transaction_id:crypto.randomUUID(),commands});await reload();message('Timeline edit saved.');}
  function command(operation){return {operation,document:shotPath,target:target(),property:property.value};}
  async function scrub(at=Number(frame.value)){
    if(!snapshot||!shotPath)return;const serial=++request;const s=getSnapshot();
    if(s.documents[shotPath]?.scene.path!==s.scene_path)throw Error('Switch to the shot scene before preview.');
    const result=await api('timeline/evaluate',{revision:s.revision,sha256:s.sha256,shot:shotPath,frame:at});
    if(disposed||serial!==request)return;if(result.scene.path!==getSnapshot().scene_path)throw Error('Preview scene changed; reload before scrubbing.');preview(result);refreshPreviewEditState?.();message(`Saved revision ${result.revision}, frame ${result.frame.numerator}/${result.frame.denominator}`);
  }
  function stop(){playing=false;cancelAnimationFrame(raf);play.textContent='Play';refreshPreviewEditState?.();}
  async function tick(now){if(!playing||disposed)return;const doc=snapshot.documents[shotPath],fps=doc.fps.numerator/doc.fps.denominator;
    const current=firstFrame+Math.floor((now-clock)*fps/1000);
    if(current>=doc.duration_frames){stop();return;}frame.value=String(current);
    try{await scrub(current);}catch(e){stop();message(e.message);return;}
    if(playing)raf=requestAnimationFrame(tick);
  }
  const play=button('Play',()=>{if(playing){stop();return;}playing=true;play.textContent='Pause';refreshPreviewEditState?.();clock=performance.now();firstFrame=Number(frame.value);raf=requestAnimationFrame(tick);});
  const move=document.createElement('input');move.type='number';move.min='0';move.step='1';move.value='1';move.setAttribute('aria-label','Move selected key to frame');
  const duration=document.createElement('input');duration.type='number';duration.min='1';duration.step='1';duration.setAttribute('aria-label','Shot duration in frames');
  const camera=document.createElement('select');camera.setAttribute('aria-label','Shot camera');
  root.append(autoLabel,shot,frame,button('Scrub',()=>scrub()),play,button('Return to authored scene',()=>{stop();++request;clearPreview();}),property,value,interpolation,
    button('Insert or replace key',()=>edit([{...command('key-upsert'),frame:Number(frame.value),value:JSON.parse(value.value),interpolation:interpolation.value}])),
    button('Delete key',()=>edit([{...command('key-delete'),frame:Number(frame.value)}])),move,
    button('Move key',()=>edit([{...command('key-move'),frame:Number(frame.value),to_frame:Number(move.value)}])),
    button('Set interpolation',()=>edit([{...command('track-interpolation'),interpolation:interpolation.value}])),duration,camera,
    button('Save shot settings',()=>edit([{operation:'shot-settings',document:shotPath,duration_frames:Number(duration.value),camera:camera.value}])),
    button('Move shot earlier',()=>reorder(-1)),button('Move shot later',()=>reorder(1)),button('Evaluate channel curve',()=>drawCurve()),curve,curveLegend,tracks,status);
  const resourceID=document.createElement('input');resourceID.setAttribute('aria-label','New resource ID');
  const resourceName=document.createElement('input');resourceName.setAttribute('aria-label','New scene or shot name');
  const clip=document.createElement('select');clip.setAttribute('aria-label','Reusable clip');
  const speed=document.createElement('input');speed.type='number';speed.min='0.001';speed.step='any';speed.value='1';speed.setAttribute('aria-label','Clip playback speed');
  const loop=document.createElement('input');loop.type='checkbox';loop.setAttribute('aria-label','Loop clip');
  const sceneSelect=document.createElement('select');sceneSelect.setAttribute('aria-label','Project scene');
  root.insertBefore(sceneSelect,status);root.insertBefore(button('Open scene',async()=>{stop();const s=getSnapshot();await api('timeline/select-scene',{expected_revision:s.revision,expected_sha256:s.sha256,transaction_id:crypto.randomUUID(),scene:sceneSelect.value});await reload();message('Scene opened from saved native project.');}),status);
  for(const [label,operation] of [['Duplicate scene','scene-duplicate'],['Create empty scene','scene-create']])root.insertBefore(button(label,()=>edit([{operation,source:snapshot.scene_path,id:resourceID.value,name:resourceName.value}])),status);
  root.insertBefore(button('Rename scene',()=>edit([{operation:'scene-rename',document:snapshot.scene_path,name:resourceName.value}])),status);
  root.insertBefore(button('Delete scene',()=>edit([{operation:'scene-delete',document:snapshot.scene_path}])),status);
  root.insertBefore(resourceID,status);root.insertBefore(resourceName,status);
  for(const [label,operation] of [['Duplicate shot','shot-duplicate'],['Create empty shot','shot-create'],['Create reusable clip','clip-create']])root.insertBefore(button(label,()=>edit([{operation,source:shotPath,id:resourceID.value,...(operation==='clip-create'?{}:{name:resourceName.value})}])),status);
  root.insertBefore(clip,status);root.insertBefore(speed,status);root.insertBefore(loop,status);
  root.insertBefore(button('Bind clip to selected object',()=>edit([{operation:'clip-bind',document:shotPath,clip:clip.value,target:target(),start_frame:Number(frame.value),end_frame:snapshot.documents[shotPath].duration_frames,speed:Number(speed.value),loop:loop.checked}])),status);
  container.append(root);
  function reorder(delta){const paths=snapshot.project.shots.map(r=>r.path),index=paths.indexOf(shotPath),other=index+delta;if(other<0||other>=paths.length)return;
    [paths[index],paths[other]]=[paths[other],paths[index]];return edit([{operation:'project-order',field:'shots',paths}]);}
  function renderShot(){curve.replaceChildren();const doc=snapshot?.documents[shotPath];if(!doc)return;frame.max=String(doc.duration_frames-1);frame.value=String(Math.min(Number(frame.value),doc.duration_frames-1));duration.value=String(doc.duration_frames);
    const scene=snapshot.documents[doc.scene.path];camera.replaceChildren();for(const id of scene.cameras){const o=document.createElement('option');o.value=id;o.textContent=id;camera.append(o);}camera.value=doc.camera;
    tracks.replaceChildren();for(const track of doc.tracks){const line=document.createElement('p');line.textContent=`${track.target} · ${track.property} · ${track.interpolation}: `;
      for(const key of track.keys){line.append(button(String(key.frame),()=>{frame.value=String(key.frame);if(track.target!=='world')onSelect?.(track.target);property.value=track.property;interpolation.value=track.interpolation;value.value=JSON.stringify(key.value);return scrub();}));}tracks.append(line);}
  }
  property.addEventListener('change',()=>{++request;curve.replaceChildren();curveLegend.textContent='Choose Evaluate channel curve to sample this channel.';});
  shot.addEventListener('change',()=>{stop();++request;clearPreview();shotPath=shot.value;renderShot();});
  return {refresh(next){stop();++request;const previous=snapshot?.documents[shotPath],previousActive=snapshot?.scene_path;snapshot=next;sceneSelect.replaceChildren();for(const ref of next.project.scenes){const option=document.createElement('option');option.value=ref.path;option.textContent=next.documents[ref.path].name;sceneSelect.append(option);}sceneSelect.value=next.scene_path;clip.replaceChildren();for(const ref of next.project.animations){const option=document.createElement('option');option.value=ref.path;option.textContent=ref.id+' '+ref.version;clip.append(option);}shot.replaceChildren();for(const ref of next.project.shots){const o=document.createElement('option');o.value=ref.path;o.textContent=next.documents[ref.path].name;shot.append(o);}if(!next.documents[shotPath]||!next.project.shots.some(r=>r.path===shotPath))shotPath=next.project.shots.find(ref=>ref.id===previous?.id&&next.documents[ref.path].scene.id===previous?.scene.id)?.path||next.project.shots[0]?.path||'';if(previousActive!==next.scene_path&&next.documents[shotPath]?.scene.path!==next.scene_path)shotPath=next.project.shots.find(ref=>next.documents[ref.path].scene.path===next.scene_path)?.path||'';shot.value=shotPath;renderShot();if(autokey.checked&&shotPath)queueMicrotask(()=>scrub().catch(e=>message(e.message)));},dispose(){disposed=true;stop();++request;unregisterTimeline?.();unregisterPreview?.();unregister?.();root.remove();}};
}
