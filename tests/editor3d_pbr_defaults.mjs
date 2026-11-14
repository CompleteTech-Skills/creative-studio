import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from '../tools/rig/node_modules/three/build/three.module.js';
import { GLTFLoader } from '../tools/rig/node_modules/three/examples/jsm/loaders/GLTFLoader.js';

// Exercise both actual editor assetObject branches with Three.js and GLTFLoader.
const source = fs.readFileSync(new URL('../assets/editor3d/editor.js', import.meta.url), 'utf8');
const body = source.slice(source.indexOf('function materialFromNative('), source.indexOf('async function install('));
const assetObject = new Function('THREE', 'GLTFLoader', 'mixers', body + ';return assetObject;')(THREE, GLTFLoader, []);
const positions = new Float32Array([0,0,0, 1,0,0, 0,1,0]);
function glb(material) {
  const primitive = {attributes:{POSITION:0}};
  const doc = {asset:{version:'2.0'}, buffers:[{byteLength:positions.byteLength}],
    bufferViews:[{buffer:0,byteLength:positions.byteLength}],
    accessors:[{bufferView:0,componentType:5126,count:3,type:'VEC3',min:[0,0,0],max:[1,1,0]}],
    meshes:[{primitives:[primitive]}], nodes:[{mesh:0}], scenes:[{nodes:[0]}], scene:0};
  if (material !== null) { doc.materials = [material]; primitive.material = 0; }
  const json = Buffer.from(JSON.stringify(doc));
  const jsonLength = (json.length + 3) & ~3;
  const bytes = Buffer.alloc(12 + 8 + jsonLength + 8 + positions.byteLength, 0x20);
  bytes.writeUInt32LE(0x46546c67,0); bytes.writeUInt32LE(2,4); bytes.writeUInt32LE(bytes.length,8);
  bytes.writeUInt32LE(jsonLength,12); bytes.writeUInt32LE(0x4e4f534a,16); json.copy(bytes,20);
  const offset = 20 + jsonLength;
  bytes.writeUInt32LE(positions.byteLength,offset); bytes.writeUInt32LE(0x004e4942,offset+4);
  Buffer.from(positions.buffer).copy(bytes,offset+8);
  return bytes.toString('base64');
}
function parameters(material) {
  return {color:material.color.toArray(), opacity:material.opacity, metalness:material.metalness,
    roughness:material.roughness, emissive:material.emissive.toArray(), transparent:material.transparent, alphaTest:material.alphaTest, side:material.side};
}
const cases = [
  [{emissiveFactor:[1,0.25,0]}, {color:[1,1,1],opacity:1,metalness:1,roughness:1,emissive:[1,0.25,0],transparent:false,alphaTest:0,side:THREE.FrontSide}],
  [{emissiveFactor:[0,0,0]}, {color:[1,1,1],opacity:1,metalness:1,roughness:1,emissive:[0,0,0],transparent:false,alphaTest:0,side:THREE.FrontSide}],
  [null, {color:[1,1,1],opacity:1,metalness:1,roughness:1,emissive:[0,0,0],transparent:false,alphaTest:0,side:THREE.FrontSide}],
  [{}, {color:[1,1,1],opacity:1,metalness:1,roughness:1,emissive:[0,0,0],transparent:false,alphaTest:0,side:THREE.FrontSide}],
  [{pbrMetallicRoughness:{baseColorFactor:[0.2,0.3,0.4,0.5],metallicFactor:0}},
    {color:[0.2,0.3,0.4],opacity:0.5,metalness:0,roughness:1,emissive:[0,0,0],transparent:false,alphaTest:0,side:THREE.FrontSide}],
  [{pbrMetallicRoughness:{roughnessFactor:0},alphaMode:'BLEND',doubleSided:true},
    {color:[1,1,1],opacity:1,metalness:1,roughness:0,emissive:[0,0,0],transparent:true,alphaTest:0,side:THREE.DoubleSide}],
];
for (const [material, expected] of cases) {
  const projected = await assetObject({format:'geometry', meshes:[{id:'triangle',positions:[[0,0,0],[1,0,0],[0,1,0]],triangles:[[0,1,2]],material:0}], materials:[material ?? {name:'Default material'}], images:[]});
  const loaded = await assetObject({format:'glb',data:glb(material)});
  let glbMesh; loaded.traverse(node => { if (node.isMesh) glbMesh = node; });
  assert.deepEqual(parameters(projected.children[0].material), expected);
  assert.deepEqual(parameters(glbMesh.material), expected);
}
console.log('Projected geometry and GLB PBR parameters match for absent, omitted, explicit, and emissive factors.');
