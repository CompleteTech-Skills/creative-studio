import assert from 'node:assert/strict';
import fs from 'node:fs';
import * as THREE from '../assets/rig-viewer/vendor/build/three.module.min.js';

const source = fs.readFileSync(new URL('../assets/editor3d/editor.js', import.meta.url), 'utf8');
const body = source.slice(source.indexOf('function resize() {'), source.indexOf('function cancelDrag()'));
const viewport = {clientWidth: 1000, clientHeight: 500};
const renderer = {setPixelRatio() {}, setSize() {}};
const native = new THREE.OrthographicCamera(-2, 2, 2, -2, 0.1, 100);
const free = new THREE.OrthographicCamera(-5, 5, 5, -5, 0.1, 100);
free.userData.span = 6;
function check(camera, span) {
  const resize = new Function('renderer', '$', 'devicePixelRatio', 'camera', 'orthographic', body + ';return resize;')(renderer, () => viewport, 1, camera, free);
  for (const [width, height] of [[1000, 500], [500, 1000], [1000, 500]]) {
    viewport.clientWidth = width; viewport.clientHeight = height; resize();
    assert.equal(camera.top - camera.bottom, span);
    const origin = new THREE.Vector3(0, 0, -1).project(camera);
    const x = new THREE.Vector3(1, 0, -1).project(camera);
    const y = new THREE.Vector3(0, 1, -1).project(camera);
    assert.ok(Math.abs((x.x - origin.x) * width - (y.y - origin.y) * height) < 1e-9);
  }
}
check(native, 4);
check(free, 6);
