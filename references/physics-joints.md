# Native mechanical joints (issue 172, in development)

The joint adapter maps admitted rigid body pairs to Blender Bullet fixed, hinge,
slider (prismatic), point/ball and generic six-DOF constraints. Anchors are local
rigid transforms. Both world anchor frames must coincide at preparation. The
axis is expressed in anchor A coordinates; the adapter converts the canonical
Y-up frame to Blender's Z-up frame. Hinge limits use radians; slider limits use
meters. Connected body collision exclusion maps to the native constraint flag.

Motor and break-force parameters are rejected until their physical units and
solver behavior have independent acceptance evidence. PyBullet joints are not
implemented. The existing native kinematic body authority remains unchanged.

Native settings are read back into the simulation report and compared with the
admitted request. Samples and the baked native scene use the existing cache and
independent reopen path. `physics_joints.measurements` calculates anchor error,
axis displacement and relative rotation from solver body samples, using shared
rig mathematics. It does not generate motion.

Spring travel uses the declared axis and scalar linear limits; all perpendicular and angular axes remain locked. Generic six-DOF accepts explicit per-axis limits. Positive damping requires positive stiffness; zero-stiffness damping is rejected. Native reports verify anchor position, axis direction, spring activation, limits and stiffness/damping.

Verification is in progress. The real solver gate is
`HATCH_PHYSICS_BACKEND_TEST=1 python -m unittest discover -s tests -p test_rig_physics_joints.py`.
The current fixture exercises seven constraint mappings and measures anchor
error; broader chain/door demonstrations, active spring behavior, kinematic
joint drivers and motor/break acceptance remain unfinished. Issue 172 and parent
stage 157 must remain open.
