/* Exact saved-revision timeline. All persistent edits receive backend acknowledgement. */
export function mountTimeline({container,api,getSnapshot,getSelection,preview,clearPreview,reload,onSelect,registerCommitInterceptor}) {
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
  const unregister=registerCommitInterceptor?.(({commands,expected_revision,expected_sha256,transaction_id,snapshot:current})=>{
    if(!autokey.checked||!commands.some(c=>c.operation==='transform'))return null;
    if(commands.some(c=>c.operation!=='transform'))throw Error('Autokey requires a transform-only transaction.');
    if(!current.project.shots.some(ref=>ref.path===shotPath))throw Error('Reload and select the current shot before autokey.');
    const at=Number(frame.value);if(!Number.isSafeInteger(at)||at<0||at>=current.documents[shotPath].duration_frames)throw Error('Autokey frame must be inside the selected shot.');
    const keys=[];for(const cmd of commands)for(const channel of ['translation','rotation','scale'])keys.push({operation:'key-upsert',document:shotPath,target:cmd.parameters.identifier,property:channel,frame:at,value:structuredClone(cmd.parameters.transform[channel])});
    return {route:'timeline/edit',body:{expected_revision,expected_sha256,transaction_id,commands:keys}};
  });
  const tracks=document.createElement('div');tracks.setAttribute('aria-label','Saved tracks and keys');
  function message(text){if(!disposed)status.textContent=text;}
  function button(label,fn){const b=document.createElement('button');b.textContent=label;b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>message(e.message)));return b;}
  function target(){const selected=getSelection();if(property.value==='world_exposure')return 'world';if(selected.length!==1)throw Error('Select one object for this channel.');return selected[0];}
  async function edit(commands){stop();const s=getSnapshot();await api('timeline/edit',{expected_revision:s.revision,expected_sha256:s.sha256,transaction_id:crypto.randomUUID(),commands});await reload();message('Timeline edit saved.');}
  function command(operation){return {operation,document:shotPath,target:target(),property:property.value};}
  async function scrub(at=Number(frame.value)){
    if(!snapshot||!shotPath)return;const serial=++request;const s=getSnapshot();
    const result=await api('timeline/evaluate',{revision:s.revision,sha256:s.sha256,shot:shotPath,frame:at});
    if(disposed||serial!==request)return;preview(result);message(`Saved revision ${result.revision}, frame ${result.frame.numerator}/${result.frame.denominator}`);
  }
  function stop(){playing=false;cancelAnimationFrame(raf);play.textContent='Play';}
  async function tick(now){if(!playing||disposed)return;const doc=snapshot.documents[shotPath],fps=doc.fps.numerator/doc.fps.denominator;
    const current=firstFrame+Math.floor((now-clock)*fps/1000);
    if(current>=doc.duration_frames){stop();return;}frame.value=String(current);
    try{await scrub(current);}catch(e){stop();message(e.message);return;}
    if(playing)raf=requestAnimationFrame(tick);
  }
  const play=button('Play',()=>{if(playing){stop();return;}playing=true;play.textContent='Pause';clock=performance.now();firstFrame=Number(frame.value);raf=requestAnimationFrame(tick);});
  const move=document.createElement('input');move.type='number';move.min='0';move.step='1';move.value='1';move.setAttribute('aria-label','Move selected key to frame');
  const duration=document.createElement('input');duration.type='number';duration.min='1';duration.step='1';duration.setAttribute('aria-label','Shot duration in frames');
  const camera=document.createElement('select');camera.setAttribute('aria-label','Shot camera');
  root.append(autoLabel,shot,frame,button('Scrub',()=>scrub()),play,button('Return to authored scene',()=>{stop();++request;clearPreview();}),property,value,interpolation,
    button('Insert or replace key',()=>edit([{...command('key-upsert'),frame:Number(frame.value),value:JSON.parse(value.value),interpolation:interpolation.value}])),
    button('Delete key',()=>edit([{...command('key-delete'),frame:Number(frame.value)}])),move,
    button('Move key',()=>edit([{...command('key-move'),frame:Number(frame.value),to_frame:Number(move.value)}])),
    button('Set interpolation',()=>edit([{...command('track-interpolation'),interpolation:interpolation.value}])),duration,camera,
    button('Save shot settings',()=>edit([{operation:'shot-settings',document:shotPath,duration_frames:Number(duration.value),camera:camera.value}])),
    button('Move shot earlier',()=>reorder(-1)),button('Move shot later',()=>reorder(1)),tracks,status);
  const resourceID=document.createElement('input');resourceID.setAttribute('aria-label','New resource ID');
  const resourceName=document.createElement('input');resourceName.setAttribute('aria-label','New scene or shot name');
  const clip=document.createElement('select');clip.setAttribute('aria-label','Reusable clip');
  const speed=document.createElement('input');speed.type='number';speed.min='0.001';speed.step='any';speed.value='1';speed.setAttribute('aria-label','Clip playback speed');
  const loop=document.createElement('input');loop.type='checkbox';loop.setAttribute('aria-label','Loop clip');
  root.insertBefore(resourceID,status);root.insertBefore(resourceName,status);
  for(const [label,operation] of [['Duplicate shot','shot-duplicate'],['Create empty shot','shot-create'],['Create reusable clip','clip-create']])root.insertBefore(button(label,()=>edit([{operation,source:shotPath,id:resourceID.value,...(operation==='clip-create'?{}:{name:resourceName.value})}])),status);
  root.insertBefore(clip,status);root.insertBefore(speed,status);root.insertBefore(loop,status);
  root.insertBefore(button('Bind clip to selected object',()=>edit([{operation:'clip-bind',document:shotPath,clip:clip.value,target:target(),start_frame:Number(frame.value),end_frame:snapshot.documents[shotPath].duration_frames,speed:Number(speed.value),loop:loop.checked}])),status);
  container.append(root);
  function reorder(delta){const paths=snapshot.project.shots.map(r=>r.path),index=paths.indexOf(shotPath),other=index+delta;if(other<0||other>=paths.length)return;
    [paths[index],paths[other]]=[paths[other],paths[index]];return edit([{operation:'project-order',field:'shots',paths}]);}
  function renderShot(){const doc=snapshot?.documents[shotPath];if(!doc)return;frame.max=String(doc.duration_frames-1);frame.value=String(Math.min(Number(frame.value),doc.duration_frames-1));duration.value=String(doc.duration_frames);
    const scene=snapshot.documents[doc.scene.path];camera.replaceChildren();for(const id of scene.cameras){const o=document.createElement('option');o.value=id;o.textContent=id;camera.append(o);}camera.value=doc.camera;
    tracks.replaceChildren();for(const track of doc.tracks){const line=document.createElement('p');line.textContent=`${track.target} · ${track.property} · ${track.interpolation}: `;
      for(const key of track.keys){line.append(button(String(key.frame),()=>{frame.value=String(key.frame);if(track.target!=='world')onSelect?.(track.target);property.value=track.property;interpolation.value=track.interpolation;value.value=JSON.stringify(key.value);return scrub();}));}tracks.append(line);}
  }
  shot.addEventListener('change',()=>{stop();++request;clearPreview();shotPath=shot.value;renderShot();});
  return {refresh(next){stop();++request;snapshot=next;clip.replaceChildren();for(const ref of next.project.animations){const option=document.createElement('option');option.value=ref.path;option.textContent=ref.id+' '+ref.version;clip.append(option);}shot.replaceChildren();for(const ref of next.project.shots){const o=document.createElement('option');o.value=ref.path;o.textContent=next.documents[ref.path].name;shot.append(o);}if(!next.documents[shotPath]||!next.project.shots.some(r=>r.path===shotPath))shotPath=next.project.shots[0]?.path||'';shot.value=shotPath;renderShot();},dispose(){disposed=true;stop();++request;unregister?.();root.remove();}};
}
