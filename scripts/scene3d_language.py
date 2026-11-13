"""Bounded offline language plans over the canonical Scene3D command service.

The grammar is intentionally explicit: every statement either produces a typed
command or an unresolved diagnostic. Applying a plan rechecks its exact authored
scene and never treats missing assets as generated geometry.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re

from rig_errors import RigError
from scene3d_contracts import ID

MAX_TEXT = 16384
MAX_STATEMENTS = 128
MAX_COMMANDS = 256
WORD_NUMBERS = {name: value for value, name in enumerate((
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
    "eighteen", "nineteen", "twenty"))}
NUMBER = r"(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)|" + "|".join(WORD_NUMBERS) + ")"
LABEL = r'(?P<target>"[^"\n]{1,256}"|[a-zA-Z][a-zA-Z0-9 _-]{0,255}?)'


def _require(condition, message, code="SCENE3D_LANGUAGE"):
    if not condition:
        raise RigError(code, message, stage="scene3d-language")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _text(value):
    _require(isinstance(value, str) and 0 < len(value) <= MAX_TEXT,
             "Description must contain 1–16384 UTF-8 bytes.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise RigError("SCENE3D_LANGUAGE", "Description is not valid UTF-8.", stage="scene3d-language") from exc
    _require(size <= MAX_TEXT,
             "Description must contain 1–16384 UTF-8 bytes.")
    _require(not any(ord(c) < 32 and c not in "\n\r\t" for c in value), "Control characters are not supported.")
    statements = [s.strip().rstrip(".").strip() for s in re.split(r"[;\n]+", value) if s.strip()]
    _require(0 < len(statements) <= MAX_STATEMENTS, "Description must contain 1–128 statements.")
    return statements


def _target(scene, label):
    label = label.strip()
    quoted = label.startswith('"') and label.endswith('"')
    label = label.strip('"')
    nodes = scene["nodes"]
    # Exact IDs take precedence; display names must be uniquely matched. Articles
    # are optional grammar only when the complete unquoted name did not match.
    def matches_for(candidate):
        exact = [n for n in nodes if n["id"] == candidate]
        return exact or [n for n in nodes if n["name"].casefold() == candidate.casefold()]
    matches = matches_for(label)
    if not matches and not quoted and label.lower().startswith("the "):
        matches = matches_for(label[4:])
    _require(len(matches) == 1,
             "Target is missing or ambiguous: " + label, "SCENE3D_LANGUAGE_TARGET")
    return matches[0]


def _number(text, *, positive=False):
    value = float(WORD_NUMBERS[text.lower()]) if text.lower() in WORD_NUMBERS else float(text)
    _require(math.isfinite(value) and abs(value) <= 1e6 and (not positive or value > 1e-8),
             "Numeric parameter exceeds supported bounds.")
    return value


def _transform(node, **changes):
    transform = deepcopy(node["transform"])
    transform.update(changes)
    return {"operation": "transform", "parameters": {"identifier": node["id"], "transform": transform}}


def _compile_statement(statement, scene):
    flags = re.IGNORECASE
    match = re.fullmatch(r"move\s+" + LABEL + r"\s+(?P<distance>" + NUMBER +
                         r")\s*(?:meters?|m)\s+(?P<direction>east|west|north|south|up|down)", statement, flags)
    if match:
        node = _target(scene, match["target"])
        distance = _number(match["distance"])
        axis, sign = {"east": (0, 1), "west": (0, -1), "up": (1, 1),
                      "down": (1, -1), "north": (2, 1), "south": (2, -1)}[match["direction"].lower()]
        # Language directions are canonical world directions, even under rotated parents.
        return {"intent": "world-displacement", "target": node["id"], "axis": axis, "distance": sign * distance}
    match = re.fullmatch(r"(?:position|place|move)\s+" + LABEL + r"\s+at\s*\(?\s*(?P<x>" + NUMBER +
                         r")\s*,\s*(?P<y>" + NUMBER + r")\s*,\s*(?P<z>" + NUMBER + r")\s*\)?(?:\s*(?:meters?|m))?", statement, flags)
    if match:
        node = _target(scene, match["target"])
        return {"intent": "world-position", "target": node["id"],
                "position": [_number(match[a]) for a in ("x", "y", "z")]}
    match = re.fullmatch(r"scale\s+" + LABEL + r"\s+by\s+(?P<factor>" + NUMBER + r")", statement, flags)
    if match:
        node = _target(scene, match["target"])
        factor = _number(match["factor"], positive=True)
        return _transform(node, scale=[v * factor for v in node["transform"]["scale"]])
    match = re.fullmatch(r"rotate\s+" + LABEL + r"\s+(?P<angle>" + NUMBER +
                         r")\s+degrees?\s+(?:around|about)\s+(?P<axis>x|y|z)", statement, flags)
    if match:
        from rig_math import axis_angle, qmul
        node = _target(scene, match["target"])
        axis = [0., 0., 0.]
        axis["xyz".index(match["axis"].lower())] = 1.
        rotation = qmul(node["transform"]["rotation"], axis_angle(axis, math.radians(_number(match["angle"]))))
        return _transform(node, rotation=rotation.tolist())
    match = re.fullmatch(r"(?P<verb>hide|show|lock|unlock)\s+" + LABEL, statement, flags)
    if match:
        node = _target(scene, match["target"])
        verb = match["verb"].lower()
        key, value = ("locked", verb == "lock") if verb in ("lock", "unlock") else ("visible", verb == "show")
        return {"operation": "node-flags", "parameters": {"identifier": node["id"], key: value}}
    match = re.fullmatch(r"(?:delete|remove)\s+" + LABEL, statement, flags)
    if match:
        node = _target(scene, match["target"])
        return {"operation": "delete", "parameters": {"identifier": node["id"], "policy": "reject"}}
    match = re.fullmatch(r"duplicate\s+" + LABEL + r"\s+as\s+(?P<new>[a-z][a-z0-9_-]{0,63})", statement, flags)
    if match:
        node = _target(scene, match["target"])
        _require(re.fullmatch(ID["pattern"], match["new"]) is not None, "New ID must be a canonical node identifier.")
        return {"operation": "duplicate", "parameters": {"identifier": node["id"], "new_id": match["new"]}}
    match = re.fullmatch(r"reparent\s+" + LABEL + r"\s+under\s+(?P<parent>\"[^\"\n]{1,256}\"|[a-zA-Z][a-zA-Z0-9 _-]{0,255})", statement, flags)
    if match:
        node, parent = _target(scene, match["target"]), _target(scene, match["parent"])
        return {"operation": "reparent", "parameters": {"identifier": node["id"], "parent": parent["id"], "preserve_world": True}}
    _require(False, "Unsupported statement; no words were ignored: " + statement, "SCENE3D_LANGUAGE_UNSUPPORTED")


def compile_description(graph, text):
    """Compile all statements in order against private canonical scene state.

    Examples: ``Move the camera 2 meters north; hide mesh``; ``duplicate mesh as
    another-mesh; position another-mesh at 1, 0, 3``. Duplication reuses actual
    source geometry and provenance. No external planner is invoked.
    """
    from scene3d_graph import SceneGraph
    from scene3d_prefabs import PrefabLibrary
    from scene3d_edit import apply_commands
    statements = _text(text)
    working = SceneGraph(graph.document(), external_references=graph._references,
                         asset_bounds=graph._asset_bounds, animation_documents=graph._animation_documents,
                         animation_payloads=graph._animation_payloads)
    commands, diagnostics = [], []
    for index, statement in enumerate(statements):
        try:
            command = _compile_statement(statement, working.document())
            if "intent" in command:
                matrix = working.transform(command["target"], space="world")
                if command["intent"] == "world-displacement":
                    matrix[command["axis"]][3] += command["distance"]
                else:
                    for axis, value in enumerate(command["position"]):
                        matrix[axis][3] = value
                command = {"operation": "transform", "parameters": {"identifier": command["target"], "space": "world", "transform": matrix}}
            # The canonical service decides whether a command is legal. This also
            # ensures later statements observe previous planned transforms.
            apply_commands(working, PrefabLibrary(), {"schema_version": 1, "commands": [command]})
            commands.append(command)
        except RigError as exc:
            diagnostics.append({"statement": index, "text": statement, "code": exc.report["code"], "message": exc.report["message"]})
            # Stop simulation after failure: later targets may depend on it.
            break
    verification_scope = 'scene-only'
    if not diagnostics:
        try:
            from scene3d_language_environment import _preflight_edit
            verification_scope = _preflight_edit(graph, working)
        except RigError as exc:
            diagnostics.append({"statement": len(statements) - 1, "text": statements[-1],
                                "code": exc.report["code"], "message": exc.report["message"]})
    plan = {"schema_version": 1, "kind": "scene3d-language-plan", "planner": "offline-grammar-v1",
            "original_text": text, "base_revision": graph.revision, "base_sha256": graph.digest,
            "commands": commands, "diagnostics": diagnostics, "substitutions": [],
            "verification_scope": verification_scope,
            "status": "ready" if not diagnostics else "unresolved"}
    plan["sha256"] = _digest(plan)
    return plan


compile_revision = compile_description


def apply_plan(graph, library, plan):
    """Apply only an intact ready plan against its exact source scene."""
    keys = {"schema_version", "kind", "planner", "original_text", "base_revision", "base_sha256",
            "commands", "diagnostics", "substitutions", "status", "sha256", "verification_scope"}
    _require(isinstance(plan, dict) and set(plan) == keys and type(plan["schema_version"]) is int and
             plan["schema_version"] == 1 and plan["kind"] == "scene3d-language-plan" and
             plan["planner"] == "offline-grammar-v1", "Unsupported language plan.")
    _require(plan["status"] == "ready" and plan["diagnostics"] == [] and plan["substitutions"] == [],
             "Resolve every diagnostic/substitution before applying the plan.")
    _require(type(plan["base_revision"]) is int and graph.revision == plan["base_revision"] and
             graph.digest == plan["base_sha256"], "Scene changed after language planning.", "SCENE3D_LANGUAGE_STALE")
    _require(isinstance(plan["commands"], list) and 0 < len(plan["commands"]) <= MAX_COMMANDS,
             "Language plan has no bounded executable commands.")
    # Recompile the preserved text; a caller cannot authorize different commands
    # by rewriting both the plan and its unkeyed integrity hash.
    expected = compile_description(graph, plan["original_text"])
    _require(expected == plan, "Plan is not the deterministic compilation of its original description.",
             "SCENE3D_LANGUAGE_HASH")
    from scene3d_edit import apply_commands
    return apply_commands(graph, library, {"schema_version": 1, "commands": deepcopy(plan["commands"])})
