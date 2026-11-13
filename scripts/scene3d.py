#!/usr/bin/env python3
"""Scene3D inspection and offline language edits using native scene services."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write a JSON report to a new file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("capabilities")
    sub.add_parser("doctor")
    for name in ("validate", "inspect", "snapshot", "plan", "describe", "revise", "apply-plan", "edit", "scene-state", "generate"):
        command = sub.add_parser(name)
        command.add_argument("--project", required=True, type=Path)
        if name in ("plan", "describe", "revise", "apply-plan", "edit", "scene-state", "generate"):
            command.add_argument("--scene", required=True, help="Exact project-relative scene manifest")
        if name in ("plan", "describe", "revise"):
            command.add_argument("--text", required=True, help="Semicolon-separated supported offline statements")
            command.add_argument("--environment", action="store_true", help="Compile supported geometry-template prose")
        if name in ("apply-plan", "edit", "generate"):
            command.add_argument("--file", required=True, type=Path)
        if name in ("snapshot", "revise", "apply-plan", "edit", "generate"):
            command.add_argument("--destination", required=True, type=Path, help="New immutable snapshot directory")
            command.add_argument("--dry-run", action="store_true")
        if name == "edit":
            command.add_argument("--expected-revision", required=True, type=int)
            command.add_argument("--expected-sha256", required=True)
    from scene3d_cli_commands import add_parsers
    add_parsers(sub)
    from scene3d_cli_lifecycle import add_parsers as add_lifecycle_parsers
    add_lifecycle_parsers(sub)
    evaluate = sub.add_parser("evaluate", help="Sample actual scene animation at an exact frame")
    evaluate.add_argument("--project", required=True, type=Path)
    evaluate.add_argument("--scene", required=True)
    evaluate.add_argument("--shot", required=True)
    evaluate.add_argument("--frame", required=True, help="Exact integer or rational frame, for example 5/2")
    evaluate.add_argument("--sample-geometry", action="store_true")
    return parser


def capabilities():
    return {"schema_version": 1, "kind": "scene3d-cli-capabilities", "language_backend": "offline-grammar-v1",
            "supported": ["validated-closure-inspection", "immutable-snapshot", "language-plan", "bounded-environment-template-prose",
                          "exact-rational-scene-evaluation", "named-native-command-adapters", "atomic-typed-batch", "exact-target-transform", "visibility-lock", "subtree-duplicate", "reparent", "safe-delete"],
            "language_statements": ["move TARGET 2 meters east", "position TARGET at 1, 0, 3",
                                    "scale TARGET by 2", "rotate TARGET 30 degrees around y", "hide TARGET",
                                    "show TARGET", "lock TARGET", "unlock TARGET", "duplicate TARGET as new-id",
                                    "reparent TARGET under PARENT", "delete TARGET"],
            "coordinates": {"east": "+X", "up": "+Y", "north": "+Z", "unit": "meter"},
            "limits": {"text_utf8_bytes": 16384, "statements": 128, "commands": 256},
            "unavailable": ["free-form-general-language-generation", "remote-language-provider",
                            "automatic-asset-substitution", "browser-server", "renderer"],
            "notes": "Native scene operations preserve original source bytes. Snapshots require a new destination; no visual approval is implied."}


def execute(args):
    if args.command in ("capabilities", "doctor"):
        result = capabilities()
        if args.command == "doctor":
            import importlib.util
            result["dependencies"] = {name: importlib.util.find_spec(name) is not None for name in ("numpy", "PIL", "pygltflib")}
        return result
    if args.command in ("init", "archive", "restore", "impact"):
        from scene3d_cli_lifecycle import execute as execute_lifecycle
        return execute_lifecycle(args)
    if args.command in ("scene", "asset", "object", "prefab", "camera", "shot", "lighting", "environment", "terrain", "room", "building", "layout"):
        from scene3d_cli_commands import execute as execute_native
        return execute_native(args)
    if args.command == "evaluate":
        from fractions import Fraction
        from rig_errors import RigError
        from scene3d_evaluate import evaluate_scene
        try:
            frame = Fraction(args.frame)
        except (ValueError, ZeroDivisionError) as exc:
            raise RigError("SCENE3D_FRAME", "Frame must be an exact integer or rational.", stage="scene3d-cli") from exc
        return evaluate_scene(args.project, args.scene, args.shot, frame, sample_geometry=args.sample_geometry)
    from scene3d_contracts import load_project
    from scene3d_graph import SceneGraph
    from scene3d_prefabs import PrefabLibrary
    from scene3d_snapshot import snapshot_project
    bundle = load_project(args.project)
    if args.command == "validate":
        return {"status": bundle["status"], "files": len(bundle["inventory"]), "project": bundle["project"]["id"]}
    if args.command == "inspect":
        return {"status": bundle["status"], "project": bundle["project"], "inventory": bundle["inventory"]}
    if args.command == "snapshot":
        if args.dry_run:
            return {"dry_run": True, "status": bundle["status"], "files": len(bundle["inventory"]), "destination": str(args.destination)}
        return snapshot_project(args.project, args.destination)
    from scene3d_language import compile_description, apply_plan
    graph = SceneGraph.from_bundle(bundle, args.scene, source_root=args.project)
    if args.command == "scene-state":
        return {"scene": args.scene, "revision": graph.revision, "scene_sha256": graph.digest,
                "document": graph.document(), "base_pose": graph.evaluate_base()}
    if args.command == "edit":
        from asset_library import read_source_bytes
        from rig_errors import decode_json
        from scene3d_edit import apply_commands
        from scene3d_graph import require
        require(type(args.expected_revision) is int and args.expected_revision >= 0 and
                graph.revision == args.expected_revision and graph.digest == args.expected_sha256,
                "Scene changed after the source revision was inspected.", "SCENE3D_CONFLICT")
        commands = decode_json(read_source_bytes(args.file, 1024 * 1024))
        library = PrefabLibrary.from_files(args.project, bundle)
        applied = apply_commands(graph, library, commands)
        if args.dry_run:
            return {"dry_run": True, "revision": graph.revision, "scene_sha256": graph.digest,
                    "operations": len(applied["results"]), "destination": str(args.destination)}
        result = snapshot_project(args.project, args.destination, graph, scene_path=args.scene,
                                  derived_documents=applied["derived_documents"])
        result["operations"] = len(applied["results"])
        return result
    if args.command in ("apply-plan", "generate"):
        from asset_library import read_source_bytes
        from rig_errors import decode_json
        plan = decode_json(read_source_bytes(args.file, 1024 * 1024))
    else:
        if args.environment:
            from scene3d_language_environment import compile_environment_description
            plan = compile_environment_description(graph, args.text)
        else:
            plan = compile_description(graph, args.text)
    if args.command in ("plan", "describe"):
        return plan
    if args.command == "generate":
        from scene3d_graph import require
        require(isinstance(plan, dict) and plan.get("kind") == "scene3d-environment-language-plan",
                "Generate requires a reviewed environment geometry plan.", "SCENE3D_LANGUAGE_KIND")
    if isinstance(plan, dict) and plan.get("kind") == "scene3d-environment-language-plan":
        from scene3d_language_environment import apply_environment_plan
        return apply_environment_plan(args.project, args.destination, args.scene, plan, dry_run=args.dry_run)
    library = PrefabLibrary.from_files(args.project, bundle)
    applied = apply_plan(graph, library, plan)
    if args.dry_run:
        return {"dry_run": True, "plan": plan, "revision": graph.revision,
                "scene_sha256": graph.digest, "destination": str(args.destination)}
    result = snapshot_project(args.project, args.destination, graph, scene_path=args.scene,
                              derived_documents=applied["derived_documents"])
    result["language_plan"] = plan
    return result


def main(argv=None):
    args = _parser().parse_args(argv)
    from rig_errors import RigError
    stream, report_identity = None, None
    try:
        if args.output:
            # Reserve report admission before any immutable scene is published.
            # Exclusive creation also rejects existing files and symlinks.
            stream = args.output.open("x", encoding="utf-8")
            info = os.fstat(stream.fileno())
            report_identity = (info.st_dev, info.st_ino)
        result = execute(args)
        text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        if stream is not None:
            stream.write(text)
            stream.flush()
        else:
            print(text, end="")
        return 2 if result.get("status") == "unresolved" else 0
    except (RigError, OSError, UnicodeError) as exc:
        if stream is not None:
            stream.close()
            stream = None
            # Remove only the report reserved by this attempt, never a path a
            # different writer replaced while the command was running.
            try:
                current = args.output.lstat()
                if report_identity == (current.st_dev, current.st_ino):
                    args.output.unlink()
            except OSError:
                pass
        report = exc.report if isinstance(exc, RigError) else {"code": "SCENE3D_IO", "message": str(exc)}
        print(json.dumps({"ok": False, "error": report}, ensure_ascii=True), file=sys.stderr)
        return 2
    finally:
        if stream is not None:
            stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
