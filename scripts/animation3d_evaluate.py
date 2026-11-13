#!/usr/bin/env python3
"""Evaluate real persisted native skeletal/morph clips at an arbitrary frame."""
import argparse
import json
from pathlib import Path
from animation3d_clips import load_clip
from animation3d_evaluator import RigClipEvaluator
from rig_errors import RigError,atomic_json,require


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--actor',required=True)
    parser.add_argument('--clip',type=Path,action='append',required=True)
    parser.add_argument('--frame',type=int,required=True)
    parser.add_argument('--subframe-ticks',type=int,default=0)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        require(not args.output.resolve().is_relative_to(args.root.resolve()) and
                args.output.resolve() not in {p.resolve() for p in args.clip},
                'ANIMATION_PATH','Evaluated output must not replace source clips or native rig files.')
        require(len(args.clip)<=64,'ANIMATION_LIMIT','At most 64 independent clips may be composed.')
        evaluator=RigClipEvaluator(args.root,args.actor)
        result=evaluator.evaluate([load_clip(path) for path in args.clip],
                                  dict(frame=args.frame,subframe_ticks=args.subframe_ticks))
        atomic_json(args.output,result)
        print(json.dumps(dict(output=str(args.output),dependency_signature=result['dependency_signature'],
                              meshes=len(result['geometry']),scope=result['scope']),sort_keys=True))
        return 0
    except (RigError,OSError) as exc:
        error=exc.report if isinstance(exc,RigError) else dict(code='ANIMATION_IO',message=str(exc))
        print(json.dumps(dict(error=error),sort_keys=True)); return 2


if __name__=='__main__': raise SystemExit(main())
