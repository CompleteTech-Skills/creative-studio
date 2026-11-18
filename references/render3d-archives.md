# Portable Render3D deliveries

`render3d_archive.archive_render(delivery_root, archive_path, request=None,
snapshot_root=None)` verifies and stages a complete delivery before publishing
a new ZIP exclusively. Supply the request and snapshot root when packaging a
JobStore materialization; existing `request.json` must agree and an existing
`snapshot/` cannot be replaced. Payloads include the exact request, result,
source closure, images, logs, manifest and historical verification evidence.

`restore_render(archive_path, new_destination)` checks every inventory byte,
rejects unexpected entries and verifies the restored source and decoded images
before exclusive publication. Existing and racing destinations survive. The
ZIP uses stored entries; compressed, encrypted, symlink and directory entries
are rejected. Limits are 500,000 entries, 1 GiB per file, 8 GiB aggregate payload,
and 128 MiB inventory. Oversized jobs require a separately versioned package
policy; limits are not silently bypassed.

Restoration verifies integrity against the archived backend identity. It does
not execute that backend, authenticate the original machine, grant visual
approval, or establish GPU availability. The original verification file remains
unchanged; the function returns a new verification report. New executable
rendering depends on installed optional rendering dependencies.

Tests exercise actual decoded PNG fixtures, exact source restoration,
unlisted/traversal/tampered entries and a racing destination. These tests do
not establish full film or real Blender rendering acceptance.
