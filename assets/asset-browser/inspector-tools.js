/** Additional inspection controls for the existing offline Three.js scene.
 * No source model, library record, material or animation authority is replaced.
 */
export function installInspectorTools({THREE,scene,camera,orbit,renderer,element}) {
  if (!element || !scene || !camera?.isPerspectiveCamera || !orbit?.target || !renderer) {
    throw new TypeError('Inspector controls require the existing scene, perspective camera, orbit controls and panel.');
  }
  const doc=element.ownerDocument;
  const root=doc.createElement('section');
  root.setAttribute('aria-label','3D inspection controls');
  const title=doc.createElement('h3');title.textContent='3D inspection';root.append(title);
  const note=doc.createElement('p');note.textContent='These controls change this inspection view. Source assets and review status stay unchanged.';root.append(note);
  const input=(tag,attributes={})=>{const node=doc.createElement(tag);for(const [name,value] of Object.entries(attributes))node.setAttribute(name,value);return node;};
  const label=(text,node)=>{const value=doc.createElement('label');value.append(doc.createTextNode(text+' '),node);root.append(value);return node;};
  const option=(name,value)=>{const node=doc.createElement('option');node.value=value;node.textContent=name;return node;};
  const preset=label('Camera preset',input('select',{'aria-label':'Camera preset'}));
  for(const [value,name] of [['three-quarter','Three-quarter'],['front','Front (+Z)'],['back','Back (−Z)'],['left','Left (−X)'],['right','Right (+X)'],['top','Top (+Y)']])preset.append(option(name,value));
  const lighting=label('Lighting',input('select',{'aria-label':'Inspection lighting'}));
  for(const [value,name] of [['neutral','Original lighting'],['studio','Warm studio'],['soft','Soft fill'],['flat','Flat inspection']])lighting.append(option(name,value));
  const bounds=label('Bounding box',input('input',{type:'checkbox','aria-label':'Bounding box'}));
  const axes=label('Coordinate axes',input('input',{type:'checkbox','aria-label':'Coordinate axes'}));
  const measurement=doc.createElement('pre');measurement.setAttribute('aria-label','Model bounds and coordinates');root.append(measurement);
  const meshTitle=doc.createElement('h4');meshTitle.textContent='Mesh visibility';root.append(meshTitle);
  const visibility=doc.createElement('div');visibility.setAttribute('aria-label','Mesh visibility');root.append(visibility);
  const hierarchy=doc.createElement('details');const summary=doc.createElement('summary');summary.textContent='Node hierarchy';hierarchy.append(summary);
  const tree=doc.createElement('pre');tree.setAttribute('aria-label','Loaded model hierarchy');hierarchy.append(tree);root.append(hierarchy);
  const materialDetails=doc.createElement('details');const materialSummary=doc.createElement('summary');materialSummary.textContent='Materials and skin bindings';materialDetails.append(materialSummary);
  const materials=doc.createElement('pre');materials.setAttribute('aria-label','Loaded model materials and skin bindings');materialDetails.append(materials);root.append(materialDetails);
  element.append(root);
  let model=null,meshes=[],boxHelper=null,axesHelper=null,lastBoundsUpdate=0;
  const visibilityBefore=new Map();
  const displayName=(value,fallback='(unnamed)')=>{const text=typeof value==='string' && value ? value : fallback;return text.length>512?text.slice(0,512)+'…':text;};
  const lights=[];
  scene.traverse(object=>{if(object.isLight)lights.push({object,intensity:object.intensity,color:object.color.clone(),ground:object.groundColor?.clone()});});
  function restoreLighting(){for(const value of lights){value.object.intensity=value.intensity;value.object.color.copy(value.color);if(value.ground)value.object.groundColor.copy(value.ground);}}
  function setLighting(){
    restoreLighting();
    if(lighting.value==='neutral')return;
    for(const {object} of lights){
      if(object.isHemisphereLight || object.isAmbientLight){object.intensity=lighting.value==='studio'?2.2:lighting.value==='soft'?3.2:3.6;}
      else {object.intensity=lighting.value==='studio'?4.5:lighting.value==='soft' ? .9 : 0;}
      if(lighting.value==='studio' && object.isDirectionalLight)object.color.setHex(0xffe4c4);
    }
  }
  function modelBox(){
    if(!model)return null;
    model.updateMatrixWorld(true);
    for(const mesh of meshes)if(mesh.isSkinnedMesh){mesh.skeleton.update();mesh.computeBoundingBox();}
    const box=new THREE.Box3().setFromObject(model);
    const size=box.getSize(new THREE.Vector3());
    return box.isEmpty() || ![...box.min,...box.max,...size].every(Number.isFinite)?null:box;
  }
  function fitCamera(){
    const box=modelBox();if(!box)return;
    const center=box.getCenter(new THREE.Vector3());
    const span=Math.max(...box.getSize(new THREE.Vector3()),.001);
    const directions={'three-quarter':[.5,.35,1],front:[0,0,1],back:[0,0,-1],left:[-1,0,0],right:[1,0,0],top:[0,1,1e-6]};
    const direction=new THREE.Vector3(...directions[preset.value]).normalize();
    const right=new THREE.Vector3().crossVectors(new THREE.Vector3(0,1,0),direction).normalize();
    const up=new THREE.Vector3().crossVectors(direction,right).normalize();
    const vertical=Math.tan(THREE.MathUtils.degToRad(camera.fov/2));
    const horizontal=vertical*Math.max(camera.aspect,1e-3);
    let distance=span*.01;
    for(const x of [box.min.x,box.max.x])for(const y of [box.min.y,box.max.y])for(const z of [box.min.z,box.max.z]){
      const corner=new THREE.Vector3(x,y,z).sub(center),depth=corner.dot(direction);
      distance=Math.max(distance,depth+Math.abs(corner.dot(right))/horizontal,depth+Math.abs(corner.dot(up))/vertical);
    }
    distance=distance*1.15+span*.01;
    camera.up.set(0,1,0);camera.position.copy(center).addScaledVector(direction,distance);
    camera.near=Math.max(span*1e-4,1e-6);camera.far=Math.max(span*100,distance+span*10);
    camera.updateProjectionMatrix();orbit.target.copy(center);orbit.update();
  }
  function removeHelper(helper){if(!helper)return;scene.remove(helper);helper.geometry?.dispose();for(const material of [].concat(helper.material||[]))material.dispose();}
  function updateMeasurement(box){
    const origin=model.getWorldPosition(new THREE.Vector3()).toArray();
    measurement.textContent=JSON.stringify({coordinates:'Right-handed, +Y up, +Z forward; meters',model_origin_m:origin,bounds_min_m:box.min.toArray(),bounds_max_m:box.max.toArray(),extent_m:box.getSize(new THREE.Vector3()).toArray()},null,2);
  }
  function clear(){
    for(const [mesh,visible] of visibilityBefore)mesh.visible=visible;
    visibilityBefore.clear();removeHelper(boxHelper);removeHelper(axesHelper);
    boxHelper=null;axesHelper=null;model=null;meshes=[];lastBoundsUpdate=0;
    restoreLighting();lighting.value='neutral';preset.value='three-quarter';bounds.checked=false;axes.checked=false;
    visibility.replaceChildren();tree.textContent='No loaded model.';materials.textContent='No loaded model.';measurement.textContent='No loaded model.';
    for(const node of [preset,lighting,bounds,axes])node.disabled=true;
  }
  function setModel(value,meshValues){
    clear();model=value;meshes=Array.from(meshValues||[]);
    if(!model)return;
    for(const node of [preset,lighting,bounds,axes])node.disabled=false;
    const box=modelBox();
    if(box){
      boxHelper=new THREE.BoxHelper(model,0xf4cd63);boxHelper.userData.hatchInspectorOverlay='bounds';boxHelper.visible=false;scene.add(boxHelper);
      axesHelper=new THREE.AxesHelper(Math.max(...box.getSize(new THREE.Vector3()),.001)*.35);
      axesHelper.userData.hatchInspectorOverlay='axes';axesHelper.position.copy(model.getWorldPosition(new THREE.Vector3()));axesHelper.visible=false;scene.add(axesHelper);
      updateMeasurement(box);
    }
    for(const [index,mesh] of meshes.entries()){
      visibilityBefore.set(mesh,mesh.visible);
      if(index>=256)continue;
      const row=doc.createElement('label'),checkbox=input('input',{type:'checkbox','aria-label':'Show mesh '+displayName(mesh.name,String(index+1))});
      checkbox.checked=mesh.visible;checkbox.addEventListener('change',()=>{mesh.visible=checkbox.checked;});
      row.append(checkbox,doc.createTextNode(' '+displayName(mesh.name,'Mesh '+String(index+1))));visibility.append(row);
    }
    if(meshes.length>256){const message=doc.createElement('p');message.textContent='Showing visibility controls for the first 256 of '+meshes.length+' meshes.';visibility.append(message);}
    const lines=[],pending=[{node:model,depth:0}];
    while(pending.length && lines.length<512){const {node,depth}=pending.pop();lines.push('  '.repeat(depth)+displayName(node.name)+' · '+node.type);const remaining=512-lines.length-pending.length;for(let i=Math.min(node.children.length,remaining)-1;i>=0;i--)pending.push({node:node.children[i],depth:depth+1});}
    if(lines.length===512)lines.push('Hierarchy display limited to 512 nodes; full native inventory remains authoritative.');
    tree.textContent=lines.join('\n');
    materials.textContent=JSON.stringify(meshes.slice(0,256).map(mesh=>({mesh:displayName(mesh.name),materials:[].concat(mesh.material||[]).slice(0,64).map(material=>({name:displayName(material.name),type:material.type,base_color:material.color?.getHexString(),metallic:material.metalness,roughness:material.roughness,textures:Object.keys(material).filter(key=>material[key]?.isTexture)})),skin:mesh.isSkinnedMesh?{joints:mesh.skeleton.bones.map(bone=>displayName(bone.name)),influences:mesh.geometry.attributes.skinWeight?.itemSize}:null})),null,2);
  }
  function update(){
    if(!model || !bounds.checked || performance.now()-lastBoundsUpdate<100)return;
    lastBoundsUpdate=performance.now();const box=modelBox();if(box){boxHelper?.update();updateMeasurement(box);}
  }
  preset.addEventListener('change',fitCamera);lighting.addEventListener('change',setLighting);
  bounds.addEventListener('change',()=>{if(boxHelper)boxHelper.visible=bounds.checked;update();});
  axes.addEventListener('change',()=>{if(axesHelper)axesHelper.visible=axes.checked;});
  clear();
  return {setModel,clear,update,fitCamera,dispose(){clear();root.remove();}};
}
