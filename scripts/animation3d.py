#!/usr/bin/env python3
"""Validate additive animation documents or sample a native rig target channel."""
import argparse
import json
from pathlib import Path
from animation3d_contracts import validate_contract
from animation3d_registry import TargetRegistry, sample_track
from rig_errors import RigError, read_json


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('capabilities')
    validate=commands.add_parser('validate');validate.add_argument('--file',type=Path,required=True)
    sample=commands.add_parser('sample');sample.add_argument('--file',type=Path,required=True)
    sample.add_argument('--registry',type=Path,required=True);sample.add_argument('--root',type=Path,required=True)
    sample.add_argument('--frame',type=int,required=True);sample.add_argument('--subframe-ticks',type=int,default=0)
    args=parser.parse_args(argv)
    try:
        if args.command=='capabilities':
            result=dict(schema_version=1,contracts=True,exact_frame_time=True,
                target_adapters=['native-rig-bone','native-rig-morph'],
                interpolation=['step','linear','hermite','quaternion-slerp'],
                constraints_executed=False,scene_evaluation=False,rendering=False,visual_approval=False)
        else:
            document=read_json(args.file);validate_contract(document)
            if args.command=='validate':
                result=dict(valid=True,kind=document['kind'],source_ready=False,
                    evidence_scope='document shape and local relationships only')
            else:
                registry=TargetRegistry(args.root,read_json(args.registry))
                value=sample_track(document['value'],dict(frame=args.frame,subframe_ticks=args.subframe_ticks),registry)
                result=dict(value=value,evidence_scope='one registered native rig channel; not a scene or render')
        print(json.dumps(result,sort_keys=True,allow_nan=False))
        return 0
    except (RigError,OSError) as exc:
        report=exc.report if isinstance(exc,RigError) else dict(code='ANIMATION_IO',message=str(exc))
        print(json.dumps(dict(error=report),sort_keys=True,allow_nan=False))
        return 2


if __name__=='__main__': raise SystemExit(main())
