"""Bounded local mesh foundation: real primitives, import, measurements and exports."""
from __future__ import annotations
import argparse
import json
import sys
from rig_errors import RigError, decode_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('capabilities')
    primitive=commands.add_parser('primitive')
    primitive.add_argument('--kind',required=True,choices=['box','plane','sphere','cylinder','cone','torus'])
    primitive.add_argument('--parameters',default='{}',help='JSON numeric primitive parameters.')
    primitive.add_argument('--normal-mode',choices=['flat','smooth'],default='smooth')
    primitive.add_argument('--name',default='mesh')
    imported=commands.add_parser('import')
    imported.add_argument('--file',required=True)
    imported.add_argument('--scale',type=float,default=1.)
    for command in [primitive,imported]:
        command.add_argument('--destination',required=True,help='New export directory; never replaces a destination.')
        command.add_argument('--format',choices=['glb','gltf','obj'],default='glb')
    args=parser.parse_args(argv)
    try:
        if args.command=='capabilities':
            result=dict(schema_version=1,implemented=['bounded-primitives','editable-polygon-loops','native-import-snapshot','topology-measurement','explicit-coordinates','standalone-GLB-glTF-OBJ-export'],
                geometry_authority='rig_models.Geometry',import_authority='rig_import.load_character_model',
                unavailable=['general-remeshing','robust-self-intersections','provider-inference','human-production-approval'],
                source_preservation='hash-bound snapshot; original bytes never rewritten',network_calls=False)
        else:
            from model3d_interchange import export_mesh
            from model3d_topology import topology_report
            if args.command=='primitive':
                from model3d_primitives import primitive
                item=primitive(args.kind,decode_json(args.parameters),name=args.name,normal_mode=args.normal_mode)
                geometry=item.geometry()
                result=dict(export=export_mesh(geometry,args.destination,format=args.format,editable=item),
                    editable_source=dict(positions=item.source_positions.tolist(),faces=item.faces),
                    topology=[topology_report(mesh) for mesh in geometry.meshes])
            else:
                from model3d_mesh_import import import_mesh
                from rig_errors import require
                with import_mesh(args.file,scale=args.scale) as imported:
                    require(imported.model.skeleton is None and not imported.model.weights and not imported.model.clips,
                        'MODEL3D_EXPORT_LOSS','Standalone mesh CLI cannot export skins or animation; use their native owning exporter.')
                    result=dict(export=export_mesh(imported.model.geometry,args.destination,format=args.format,source_snapshot=imported),
                        sources=imported.source_records,provenance=imported.provenance,
                        topology=[topology_report(mesh) for mesh in imported.model.geometry.meshes])
        print(json.dumps(result,indent=2,allow_nan=False));return 0
    except RigError as exc:
        print(json.dumps(dict(ok=False,error=exc.report)),file=sys.stderr);return 2
    except OSError as exc:
        print(json.dumps(dict(ok=False,error=dict(code='MODEL3D_IO',message=str(exc)))),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())
