# Bounded triangle contact diagnostics

`model3d_optimization_intersections.triangle_contacts(mesh)` and
`geometry_contacts(geometry)` execute exact binary-rational predicates after a
bounded AABB sweep. They report actual nonadjacent triangle contacts, including
coplanar overlap, containment, tangency and noncoplanar crossing. Mesh data and
source evidence remain unchanged. Geometry reports retain mesh IDs and local
triangle indices, including contacts across independently indexed parts.

Input triangles must be nondegenerate; repair remains a separate explicit
operation. Native admission and working-memory checks execute before exact work.
Defaults allow 4096 triangles and 100000 sweep pair comparisons. Hard API ceilings
are 20000 triangles and 200000 comparisons. Exhaustion produces a structured
failure; it never becomes a partial passing report. Exact predicates use each
finite float16/32/64 value's exact binary-rational representation, with no geometric epsilon.
Higher-precision float arrays reject. Mixed integer/floating parts reject integer
coordinates outside the conservative exact float64 range ±2**53 before aggregation;
homogeneous integer coordinates remain exact. Validated triangle indices widen to
int64 before applying part offsets. Coplanar
projection and all segment/triangle predicates use rational arithmetic.

Pairs sharing any indexed vertex are excluded. Adjacent foldover remains
unmeasured. Coincident positions with separate vertex indices are contacts,
including deliberate attachments, unwelded seams and duplicate surfaces. The
report does not infer whether a contact is unwanted, whether two solids penetrate,
inside/outside, minimum clearance or artistic quality. A zero result proves only
that the executed nonadjacent contact checks found no contact under that scope.
Native topology diagnostics still cover manifold/winding/degenerate/boundary
properties; this module does not rewrite those authorities or repair surfaces.

Run against an immutable captured local GLB/glTF/OBJ source:

```bash
python scripts/model3d_optimization_intersections.py --input model.glb \
  --max-triangles 4096 --max-pairs 100000
```

Contacts return success with a measured JSON report; callers choose rejection or
review policy. Invalid inputs and exceeded budgets return nonzero structured JSON.
This additive module does not complete #48 or parent #10. Central quality/repair
CLI and lifecycle integration remain owned by their existing programs.
