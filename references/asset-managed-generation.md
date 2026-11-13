# Managed bounded native generation

`assets generate --library LIBRARY --request REQUEST.json --apply` queues the
existing `ImportPipeline.generate` implementation through the managed job owner.
It does not call a model, install a provider or choose a remote service.
The only generation profile is the native Blender box/sphere implementation.
Requests require exact `identity`, `version`, `metadata`, `license_file`, and
`parameters`. Optional native controls are `fixture`, `blender`, `timeout` and
`memory_bytes`; HTTP scopes reject executable overrides.

The canonical generation profile requires exactly these parameters:

```json
{"primitive":"box","dimensions":[1,1,1],"color":[0.2,0.4,0.6,1],"animated":true}
```

The primitive is `box` or `sphere`, dimensions are three finite values from
0.001 through 1000, color is four finite values from zero through one, and
animation is an explicit boolean. A sphere requires equal dimensions. The
same canonical preflight helper is used by the direct generator and managed
admission; unsupported providers and ignored options fail before job creation.
The existing native adapter owns actual geometry, animation, conversion,
Blender save/reopen, independent validation and provenance.

Generation reserves the shared CPU, Blender, I/O and hashing pools and retains
native timeout/memory limits plus managed staging/runtime/attempt ceilings.
License bytes are pinned before queueing and rechecked against the actual
retained generated import. The generated evidence must match the exact requested
primitive profile, source GLB hash, identity, metadata, version and fixture flag.
A verified result publishes unchanged through the canonical registry.

The existing private staged import library and exact-reference recovery handle
a process exit after publication. Resume reads the existing reference and does
not publish another version. If a process exits before a verified private
version exists, a bounded retry may repeat deterministic local Blender work;
there is no paid generation call. Originals, license evidence and prior versions
are preserved. A generated engineering primitive is not reconstruction, artistic
approval, production approval, or a general model-generation capability.
