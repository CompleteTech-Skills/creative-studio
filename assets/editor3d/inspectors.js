// Native inspector controls compile to acknowledged Scene3D transactions.
// No control edits imported geometry/materials or only changes browser state.
function element(tag, text, attrs = {}) {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = text;
  for (const [name, item] of Object.entries(attrs)) value.setAttribute(name, item);
  return value;
}
function number(value, min, max, step = "any") {
  const input = element("input", undefined, {type: "number", min, max, step});
  input.value = String(value);
  return input;
}
function choice(value, values) {
  const input = element("select");
  for (const item of values) input.append(element("option", item, {value: item}));
  input.value = value;
  return input;
}
function checkbox(value) {
  const input = element("input", undefined, {type: "checkbox"});
  input.checked = value;
  return input;
}
function label(parent, title, input) {
  const row = element("label", title + " ");
  row.append(input); parent.append(row); return input;
}
function values(inputs) {
  return inputs.map((input) => {
    const value = input.valueAsNumber;
    if (!Number.isFinite(value)) throw Error("Every numeric inspector value must be finite.");
    return value;
  });
}
function vector(parent, title, initial, min, max) {
  const field = element("fieldset"), inputs = [];
  field.append(element("legend", title));
  initial.forEach((value, index) => inputs.push(label(field, ["R", "G", "B"][index], number(value, min, max))));
  parent.append(field); return inputs;
}

export function mountInspectors({container, getSnapshot, getSelection, api, reload}) {
  let current = null, selected = [], disposed = false, busy = false;
  const status = element("p", "", {role: "status", "aria-live": "polite"});
  async function submit(path, body) {
    if (busy || disposed) return;
    busy = true;
    container.querySelectorAll("fieldset,button").forEach(control => { control.disabled = true; });
    status.textContent = "Saving native properties…";
    try {
      const snapshot = getSnapshot();
      await api(path, {
        expected_revision: snapshot.revision, expected_sha256: snapshot.sha256,
        transaction_id: crypto.randomUUID(), ...body,
      });
      await reload();
      status.textContent = "Native properties saved and reloaded.";
    } catch (error) { status.textContent = error.message || String(error); }
    finally { busy = false; if (!disposed) render(); }
  }
  const apply = commands => submit("commands", {commands: {schema_version: 1, commands}});
  function button(parent, title, makeCommands) {
    const control = element("button", title, {type: "button"});
    control.disabled = busy;
    control.addEventListener("click", () => {
      try { apply(makeCommands(), control); }
      catch (error) { status.textContent = error.message || String(error); }
    });
    parent.append(control); return control;
  }
  function render() {
    if (disposed) return;
    container.replaceChildren();
    container.append(element("h2", "Native inspectors"), status);
    const snapshot = current || getSnapshot(), scene = snapshot?.scene;
    if (!scene) { status.textContent = "Open a saved native scene to inspect it."; return; }
    const ids = selected.length ? selected : getSelection();
    const node = ids.length === 1 ? scene.nodes.find((item) => item.id === ids[0]) : null;
    if (ids.length > 1) container.append(element("p", "Select one object for specialized properties."));
    if (node) {
      container.append(element("h3", node.name || node.id));
      if (node.locked) container.append(element("p", "This object is locked. Unlock it before editing native properties."));
      if (node.type === "camera") cameraInspector(node);
      else if (node.type === "light") lightInspector(node);
      else {
        materialInspector(node);
        if (node.type === "skinned_character") container.append(element("p", "Bone and morph preview may be inspected in the viewport. Persistent pose, constraints and mirroring require the native rig transaction adapter; these controls are unavailable."));
      }
    }
    worldInspector(scene);
  }
  async function materialInspector(node) {
    const section=element("section"), loading=element("p","Loading exact material slots…");
    section.append(element("h3","Materials"),loading);container.append(section);
    try {
      const descriptor=await api("inspectors/describe",{node_id:node.id});
      if (!section.isConnected || disposed) return;
      loading.remove();
      if (!descriptor.material_projection?.length) {
        section.append(element("p",descriptor.material_mapping_unavailable || "No editable native geometry slots."));return;
      }
      const projection=descriptor.material_projection;
      for (const slot of [...new Set(projection.map(item=>item.slot))]) {
        const native=descriptor.materials.find(item=>item.slot===slot);
        const initial=native?.effective?.surface || {base_color:[.8,.8,.8,1],metallic:0,roughness:.5};
        const field=element("fieldset");field.disabled=busy || node.locked;
        field.append(element("legend",slot));
        if (!native) field.append(element("p","Imported surface is preserved. Assigning a new authored material replaces this slot only."));
        const color=vector(field,"Linear RGB base color",initial.base_color.slice(0,3),0,1);
        const metallic=label(field,"Metallic",number(initial.metallic,0,1));
        const roughness=label(field,"Roughness",number(initial.roughness,0,1));
        const control=element("button",native?"Save native material":"Assign new authored material",{type:"button"});
        const unsupported=native && (Object.keys(native.effective.texture_slots).length || native.effective.graph ||
          Object.keys(initial).some(key=>!["base_color","base_color_space","metallic","roughness","opacity","alpha_mode","alpha_cutoff","double_sided","ior","normal_orientation"].includes(key)));
        if (unsupported) {field.disabled=true;field.append(element("p","This native material needs texture or advanced surface adapters; scalar replacement is unavailable."));}
        control.onclick=()=>{
          try {
            const surface={base_color:[...values(color),initial.base_color[3]],metallic:values([metallic])[0],roughness:values([roughness])[0]};
            submit(native?"inspectors/material":"inspectors/assign-material",{node_id:node.id,slot,surface});
          } catch(error) {status.textContent=error.message || String(error);}
        };
        field.append(control);section.append(field);
        if (!native) {
          const source=element("details");source.append(element("summary","Imported source surface (read-only)"));
          const snapshot=getSnapshot(),identity=JSON.stringify([node.asset.id,node.asset.version,node.asset.path,node.asset.sha256]);
          const asset=snapshot.viewport_assets?.[identity];
          const index=projection.find(item=>item.slot===slot)?.material_index;
          const original=(asset?.source_materials || asset?.materials)?.[index];
          source.append(element("pre",original?JSON.stringify(original,null,2):"The original source has no named material for this surface."));section.append(source);
        }
      }
    } catch(error) {if(section.isConnected)loading.textContent=error.message || String(error);}
  }
  function cameraInspector(node) {
    const field = element("fieldset");field.disabled = node.locked || busy;
    field.append(element("legend", "Camera optics"));
    const camera = node.camera;
    const projection = label(field, "Projection", choice(camera.projection, ["perspective", "orthographic"]));
    const near = label(field, "Near plane (m)", number(camera.near, 0.000001, 1000000));
    const far = label(field, "Far plane (m)", number(camera.far, 0.000001, 1000000));
    const fov = label(field, "Vertical field of view (radians)", number(camera.vertical_fov, 0.000001, 3.141592653589793));
    const height = label(field, "Orthographic height (m)", number(camera.orthographic_height, 0.000001, 1000000));
    button(field, "Save camera optics", () => {
      const [n, f, angle, h] = values([near, far, fov, height]);
      return [{operation: "configure-node", parameters: {identifier: node.id, properties: {camera: {
        projection: projection.value, near: n, far: f, vertical_fov: angle, orthographic_height: h,
      }}}}];
    });
    field.append(element("p", "Depth of field, focal length/sensor authoring, targets and paths are unavailable here. Saved projection and clipping values are native scene properties."));
    container.append(field);
  }
  function lightInspector(node) {
    const field = element("fieldset");field.disabled = node.locked || busy;
    field.append(element("legend", "Light"));
    const type = label(field, "Light type", choice(node.light.type, ["point", "directional", "spot"]));
    if (node.light.type === "area") {
      type.append(element("option", "area (read-only)", {value: "area"}));type.value="area";type.disabled=true;
      field.disabled=true;
      field.append(element("p", "Area dimensions and area-light preview are not supported by this scene contract."));
    }
    const color = vector(field, "Linear RGB color", node.light.color, 0, 1);
    const intensity = label(field, "Native intensity", number(node.light.intensity, 0, 1000000000));
    button(field, "Save light", () => [{operation: "configure-node", parameters: {identifier: node.id, properties: {light: {
      type: type.value, color: values(color), intensity: values([intensity])[0],
    }}}}]);
    field.append(element("p", "Beam shape, area dimensions, range and shadow quality are unavailable. Browser illumination is a preview; offline rendering remains authoritative."));
    container.append(field);
  }
  function worldInspector(scene) {
    const field = element("fieldset");field.disabled = busy;field.append(element("legend", "World"));
    const background = vector(field, "Linear background RGB", scene.world.background, 0, 1000000000);
    const exposure = label(field, "Exposure (stops)", number(scene.world.exposure, -20, 20));
    button(field, "Save world", () => [{operation: "configure-world", parameters: {properties: {
      background: values(background), exposure: values([exposure])[0],
    }}}]);
    field.append(element("p", "HDRI import and world animation are unavailable in this revision."));
    container.append(field);
  }
  return {
    refresh(snapshot, selectedIds) { current = snapshot; selected = [...selectedIds];render(); },
    dispose() { disposed = true;container.replaceChildren(); },
  };
}
