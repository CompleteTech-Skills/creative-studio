# Look-development contracts and compatibility audit

This is issue #83's executable contract foundation for [#15](https://github.com/CompleteTech-Skills/hatch-pet/issues/15), not implementation of that parent system. Shader translation, actual PBR lighting, texture decoding, native export, render quality enforcement and visual acceptance remain in #84–#90. A structurally valid material is not an executed shader. There are no Blender/browser capability claims in this module.

## Observed baseline and ownership

The implementation baseline is `a5dc729ebb2c31c4be0357afc3dd08e6fe15677e`.

| Existing authority | Actual behavior and preservation boundary |
| --- | --- |
| `style_registry.py`, `styles/`, `style-catalog.schema.json` | 205 selectable style entries, 14 separate themes, version/hash-bound style snapshots and explicit identity/user precedence. Descriptive generation instructions do not execute 3D shading. IDs and all existing files remain unchanged. |
| `asset_contracts.py`, `source_interface.py` | 2D reference roles, identity, original acquisition evidence. Look development refers to immutable identity/style snapshots; it does not create another identity catalog or replace provenance. |
| `production_contracts.py` | Closed version-1 2D cast/scene/shot/clip schemas; `lighting.direction` and `lighting.color_treatment` are planning metadata. The new independent schemas do not add keys to them. |
| `production_render.py` | Pillow layered raster compositor, encoded-sRGB source-over and exact production timing. It does not physically relight geometry. No change to its backend, color math, hashes or outputs is made here. |
| `production_workflow.py` | Scoped, hash-bound review dependency invalidation. No lookdev numerical result creates a visual receipt, rewrites historical approval or promotes fixtures to production artwork. |
| `model3d_contracts.py`, `model3d_validation.py` | Current Model3D asset/source/material and verified geometry inspection remain their own authority. Lookdev defines executable contract admission, not a replacement asset inspector or evidence that advanced shaders execute through Model3D. |
| `rig_contracts.py`, `rig_gltf.py`, `rig_blender_worker.py` | Existing imported material information, glTF and Blender rig evidence. This module neither replaces those formats nor asserts that its advanced PBR properties execute there. |
| Parallel scene/physics/asset foundations | Shared exact reference spelling `{id, version, path, sha256, kind}`. Scene owns transforms and hierarchy; physics owns friction/restitution rather than PBR. Render adapters must probe real backend features and consume lookdev definitions explicitly. |

Current delivery status reports recovered original Milo/Luma transport bytes and successful delivery gates. The old parent prompt's missing-payload statement is historical, not a new failure assertion. Native Krita exercise and human production-art approval remain separate limitations. The new examples are self-authored engineering fixtures and require no paid provider or historical pilot source.

## API and contracts

`lookdev_contracts.py` supplies nine Draft 2020-12 schemas from `definitions()`. The central `scripts/generate_schemas.py` preserves the existing Model3D and other schema families and includes all nine lookdev schemas:

| Kind | Scope |
| --- | --- |
| `material3d` | Metallic/roughness PBR surface, texture slots, optional graph, named variants and explicit render policy |
| `material-instance3d` | Exact base version plus typed whole-property surface overrides |
| `material-variant3d` | Reusable derived material with optional alternate graph |
| `texture3d` | Exact original file bytes, dimensions, encoding, role, wrap/filter and normal convention |
| `material-graph3d` | Bounded typed nodes, socket links, surface output and cycle detection; graph execution is deferred |
| `light3d` | Point/directional/spot/area/environment definitions with typed intensity and geometry |
| `lighting-rig3d` | Exact reusable light references |
| `look3d` | Exact material/light/quality references plus identity/style snapshot hashes and explicit color management |
| `render-quality3d` | Samples, bounces, resolution, seed, denoise and admission budget declarations; no scheduler claims |

Every resource has schema version 1, stable ID, semantic version, kind, declared dependencies, provenance/license/attribution, validation status, capabilities, and optional name/description/tags. No unknown fields are accepted. `validation_status` is structural metadata only; no schema has an approval status. Exact resource references bind original serialized bytes, not reformatted JSON. Canonical serialization is sorted-key, UTF-8, compact JSON with one terminal newline; `canonical_bytes()` and `content_hash()` expose it. Importing the module and generating schemas need only the standard library; runtime structural validation uses the existing optional pinned `jsonschema` dependency.

`validate_resource(value)` validates a single document and its local semantic relationships. It rejects nonfinite numbers, invalid dates, versions/fields/enums/ranges, inappropriate light fields/units, nonunit rotations/directions, unsafe paths (including Windows device superscripts), hidden undeclared references, wrong resource kinds, contradictory policies and malformed graph links. `decode_json()` from the existing rig authority rejects duplicate keys before validation. JSON Schema encodes portable path spelling/traversal/device rules and true-end identifier/hash/timestamp patterns from the same definitions; NFC and cross-resource aliases remain runtime checks. JSON Schema handles structural checks from the same definitions; runtime additionally checks cross-field invariants, graph typing/cycles and collection/reference identity. Schema success alone cannot prove those semantic invariants or dependency availability.

`Registry([(path, original_bytes), ...], payloads={path: original_bytes})` validates the complete resource closure, original content hashes, exact versions/kinds/paths, alias conflicts and cycles. Referenced texture source bytes are mandatory and hash/size-verified. These checks do not decode/inspect texture content; that belongs to #85. Bounds are 1024 resources, 2 MiB per JSON resource, 32 MiB total JSON, 64 MiB supplied payloads, 256 graph nodes, and 64 material-inheritance levels. Texture declarations additionally bound decoded dimensions/memory. File I/O is the caller's authority: the registry accepts bytes and never follows host filesystem paths or publishes anything.

`registry.get(ref)` and `registry.reference(id, version)` return copies. `effective_material(ref)` resolves instance/variant inheritance without mutating any base, checks effective alpha/physical conflicts and returns the surface, graph/texture references, render policy and deterministic effective hash. Overrides replace complete typed surface properties; they are not arbitrary JSON patch operations. Base and derived render requirements are unioned; strictness cannot be weakened, and conflicting fallback modes conservatively reject fallback. Embedded normal/height/displacement/occlusion maps cannot simultaneously use the corresponding texture slot. Define material variants as separately registered derived resources; do not create reciprocal base↔variant dependencies, which are correctly rejected as cycles. A base may name independent alternate variants whose dependency closure remains acyclic.

`check_compatibility(resource, adapter_feature_set)` checks self-contained material, graph, light or quality policy; unresolved graph materials and lighting rigs cannot receive a positive single-document verdict. Use `registry.check_material_compatibility(ref, adapter_feature_set)` for inherited materials and transitive graph policies. These gates reject unsupported strict features, including active advanced surface requirements inferred from actual values. Inferred requirements cover nondefault specular/IOR, active volume, advanced surface settings, shadows, denoise and indirect lighting. Active texture slots require the same feature as their embedded surface equivalent: normal/height/occlusion/emission/opacity/transmission, true displacement, volume thickness, clearcoat, sheen, anisotropy or iridescence. A generic texture-slots capability cannot bypass those requirements. Inherited instances must use the registry method; graph policies and node requirements cannot be bypassed by an enclosing material or permissive fallback. Lighting rigs require checking their resolved lights; no whole-rig renderer compatibility API is implemented here. Non-strict fallback requires explicit permission and returns `requires-explicit-fallback`; all results say `executed: false`. The caller must supply a real adapter's probed capabilities. The contract layer does not invent a backend support matrix.

## Units, color and defaults

The world convention remains right-handed +Y up, +Z actor forward, meters, XYZW unit quaternions and radians. Canonical base-color factors and other surface colors are linear-sRGB. Color light/constant values carry an explicit encoding. Color textures (including sheen color) are sRGB or linear-sRGB; normal/roughness/metallic/occlusion/height/displacement/opacity maps use `data`. Tangent normals use OpenGL positive-Y. Adapter conversion, texture decoding, tone mapping and linear-light rendering remain future execution work.

Point/spot intensities use candela, directional lights lux, area lights watt and environment illumination watt per square meter. Area size/radius, volume thickness, attenuation distance, height/displacement scales and subsurface radii use meters. Temperature uses kelvin. Iridescent thickness uses nanometers. Spot angles/anisotropy/UV rotation use radians. Texture-slot scale and offset use UV coordinates (three components retained for future mapping modes), while the slot's `texcoord` selects a bounded UV set.

Texture slots have explicit channel types: scalar properties select one of R/G/B/A; color slots select RGB, with RGBA additionally allowed for base color. Normal maps require at least RGB. Slot strength is dimensionless except height/displacement strength, which is measured in meters. Embedded scalar maps use R and embedded normals use RGB. A graph texture node requires an explicit `channels` selection: scalar selections expose the `value` socket with canonical normalized/clamped [0,1] sampling; RGB/RGBA selections expose `color` and require a declared color texture. Data maps cannot silently become colors. Additional swizzle/conversion nodes are unsupported in this version.

Supported scalar graph nodes have a memoized static range analysis: constants, multiply and explicit `clamp-float` (clamp to [0,1]). Every intermediate must remain finite within +/-1e9; physical roughness/metallic and mix factors must be provably [0,1], so out-of-range arithmetic requires an explicit clamp. Each multiplication is bounded before a downstream operation, preventing repeated-squaring big-integer growth. This is executable contract analysis, not shader execution or texture decoding. Future backends must implement these exact semantics or reject the corresponding capability.

`default_surface()` returns explicit physically plausible dielectric defaults: linear base color 0.8, roughness 0.5, metallic/transmission/emission/coat/sheen/anisotropy zero, IOR 1.5, full opaque coverage and unit dielectric specular strength. It returns fresh nested objects. Validation never silently injects defaults. True displacement, transmission and opacity are separate properties; declaring them does not enable a renderer feature.

## Reproduce the contract workflow

```bash
python -m pip install -r requirements-rig.txt
python scripts/generate_schemas.py --check
python scripts/lookdev_contracts.py --check
python scripts/lookdev_contracts.py --validate examples/lookdev-contracts/ceramic.json
PYTHONPATH=scripts python -m unittest discover -s tests -p test_lookdev_contracts.py -v
```

Load the complete retained example from repository root:

```python
import sys
from pathlib import Path
sys.path.insert(0, 'scripts')
from lookdev_contracts import Registry
root = Path('examples/lookdev-contracts')
names = ['ceramic', 'blue-coat', 'wet-coat', 'albedo', 'graph', 'key', 'studio', 'ceramic-look', 'draft']
library = Registry([(name + '.json', (root / (name + '.json')).read_bytes()) for name in names],
                   payloads={'textures/albedo.png': (root / 'textures/albedo.png').read_bytes()})
ref = library.reference('wet-coat', '1.0.0')
print(library.effective_material(ref))
```

`examples/lookdev-contracts/evidence.json` pins all exact resource hashes and the effective wet-coat result. The tiny PNG is an actual self-authored 2×2 RGBA source. No rendered laboratory, production art or approval is represented by this example.

## Requirement-to-test traceability

`tests/test_lookdev_contracts.py` exercises every kind through generated schema and runtime roundtrip; exact-byte dependency closure; missing content and wrong hashes/versions/kinds/paths; immutable nested inheritance and effective hashing; typed overrides/conflicts; valid and malformed socket graphs/cycles; physical range/unit/angle/rotation/color constraints; portable paths and case/Unicode aliases; numeric/JSON/allocation bounds; explicit unsupported-feature behavior; import isolation; schema drift; and the real CLI workflow. Legacy style, production-contract, schema and compatibility suites verify the unchanged owners. Actual command outputs are summarized in the PR and retained validation record; renderer acceptance is deliberately not inferred from contract tests.
