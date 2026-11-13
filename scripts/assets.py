"""Explicit local asset CLI; JSON results and stable diagnostics on every path."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from rig_errors import RigError, decode_json
from asset_library import AssetLibrary, read_source_bytes
from asset_library_operations import AssetOperations, MAX_REQUEST_BYTES, fail

GROUPS={
    'library':('create','list','inspect','verify','stats'),
    'version':('create','list','compare','promote','rollback','state'),
    'metadata':('update',),'tags':('add','remove'),'collections':('create',),
    'project':('bind','unbind','verify','impact','lock'),
    'bundle':('create','verify','restore'),
    'storage':('stats','audit','gc','upgrade','recover'),
    'jobs':('list','status','cancel','resume','run'),
}
COMMANDS=('capabilities','doctor','import','import-batch','generate','list','show','search',
    'semantic-search','inspect','validate','verify','files','dependencies','dependents',
    'compatibility','preview','thumbnail','turntable','browser','replace','repair','migrate',
    'export','index','register')


def _options(parser):
    # Flags may appear before or after subcommands, with suppressed child
    # defaults preserving values supplied to the parent parser.
    parser.add_argument('--index',default=argparse.SUPPRESS,help='Explicit canonical search index directory')
    parser.add_argument('--port',type=int,default=argparse.SUPPRESS,help='Browser loopback port')
    parser.add_argument('--intake-root',default=argparse.SUPPRESS,help='Explicit HTTP intake root')
    parser.add_argument('--output-root',default=argparse.SUPPRESS,help='Explicit HTTP output root')
    parser.add_argument('--library',default=argparse.SUPPRESS,help='Explicit private library directory')
    parser.add_argument('--project',default=argparse.SUPPRESS,help='Explicit project identifier')
    parser.add_argument('--asset',default=argparse.SUPPRESS,help='Asset identity')
    parser.add_argument('--version',default=argparse.SUPPRESS,help='Exact semantic version')
    parser.add_argument('--sha256',default=argparse.SUPPRESS,help='Exact manifest digest')
    parser.add_argument('--request',default=argparse.SUPPRESS,help='Bounded JSON request file')
    parser.add_argument('--output',default=argparse.SUPPRESS,help='New report path; existing files are preserved')
    parser.add_argument('--jobs-root',default=argparse.SUPPRESS,help='Explicit managed journal directory')
    parser.add_argument('--limits',default=argparse.SUPPRESS,help='Bounded JSON managed resource ceilings for a new store')
    parser.add_argument('--job',default=argparse.SUPPRESS,help='Managed job identifier')
    parser.add_argument('--apply',action='store_true',default=argparse.SUPPRESS,help='Explicitly execute the mutation')
    parser.add_argument('--dry-run',action='store_true',default=argparse.SUPPRESS,help='Report a mutation plan without execution')
    parser.add_argument('--json',action='store_true',default=argparse.SUPPRESS,help='JSON is always emitted')


class Parser(argparse.ArgumentParser):
    def error(self,message):
        raise RigError('LIBRARY_OPERATIONS_REQUEST',message,stage='asset-cli')


def parser():
    root=Parser(description=__doc__);_options(root)
    commands=root.add_subparsers(dest='command',required=True)
    for name in COMMANDS:
        p=commands.add_parser(name,help='Execute '+name+' through its canonical owner');_options(p)
    for name,actions in GROUPS.items():
        p=commands.add_parser(name,help='Local '+name+' operations');_options(p)
        children=p.add_subparsers(dest='action',required=True)
        for action in actions:
            child=children.add_parser(action,help=name+' '+action);_options(child)
    return root


def run(argv=None):
    args=parser().parse_args(argv);p={}
    if hasattr(args,'request'):
        p=decode_json(read_source_bytes(args.request,MAX_REQUEST_BYTES))
        if not isinstance(p,dict):fail('REQUEST','Request file must contain an object.')
    operation=args.command+('.'+args.action if hasattr(args,'action') else '')
    if hasattr(args,'asset'):
        if operation in ('list','version.list','version.state'):
            p.setdefault('asset_id',args.asset)
        else:
            if not hasattr(args,'version') or not hasattr(args,'sha256'):
                fail('REQUEST','Exact asset access requires --asset, --version and --sha256.')
            reference=dict(id=args.asset,version=args.version,sha256=args.sha256)
            if 'reference' in p and p['reference']!=reference:fail('REQUEST','Conflicting exact references.')
            p['reference']=reference
    if hasattr(args,'project'):p.setdefault('project_id',args.project)
    apply=getattr(args,'apply',False);dry_run=getattr(args,'dry_run',False)
    if operation=='capabilities':
        if p:fail('REQUEST','Capability discovery has no request parameters.')
        result=AssetOperations.capabilities()
    elif operation=='library.create':
        if not hasattr(args,'library'):fail('REQUEST','Supply --library.')
        if p:fail('REQUEST','Library creation has no request parameters.')
        if dry_run:result=dict(status='plan',destination=args.library,executed=False)
        elif not apply:fail('APPLY','Library creation requires --apply.')
        else:result=AssetLibrary(args.library,create=True).storage_status()
    else:
        if not hasattr(args,'library'):fail('REQUEST','Supply --library.')
        limits=decode_json(read_source_bytes(args.limits,MAX_REQUEST_BYTES)) if hasattr(args,'limits') else None
        owner=AssetOperations(args.library,jobs_root=getattr(args,'jobs_root',None),job_limits=limits)
        if operation=='browser':
            if p:fail('REQUEST','Browser startup uses explicit flags, not an operation request.')
            if not hasattr(args,'index'):fail('REQUEST','Browser startup requires --index.')
            from asset_library_operations import module
            forwarded=['--library',args.library,'--index',args.index]
            for name in ('port','jobs_root','intake_root','output_root'):
                if hasattr(args,name):forwarded.extend(['--'+name.replace('_','-'),str(getattr(args,name))])
            code=module('browser').main(forwarded)
            return dict(ok=code in (None,0),result=dict(server_exit_code=code))
        if operation.startswith('jobs.'):
            action=args.action
            if p:fail('REQUEST','Job commands use --job and no request file.')
            if action=='list':result=owner.list_jobs()
            else:
                identifier=getattr(args,'job',None)
                if identifier is None:fail('REQUEST','Supply --job.')
                if action in ('cancel','resume','run') and not apply:fail('APPLY','Job mutation requires --apply.')
                function={'status':owner.job_status,'cancel':owner.cancel_job,
                    'resume':owner.resume_job,'run':owner.run_job}[action]
                result=dict(status='plan',job=identifier,operation=action,executed=False) if dry_run and action in ('cancel','resume','run') else function(identifier)
        else:
            if operation=='storage.gc' and apply:
                operation='storage.gc.apply'
            result=owner.execute(operation,p,apply=apply,dry_run=dry_run)
    document=dict(ok=True,result=result)
    if hasattr(args,'output'):
        from asset_library_operations import _path
        path=_path(args.output)
        # Reports are explicitly exclusive; no approved artifact can be replaced.
        with path.open('xb') as stream:
            stream.write(json.dumps(document,ensure_ascii=False,allow_nan=False,indent=2).encode('utf-8')+b'\n')
    return document


def main(argv=None):
    try:
        print(json.dumps(run(argv),ensure_ascii=False,allow_nan=False,indent=2))
        return 0
    except RigError as exc:
        print(json.dumps(dict(ok=False,error=exc.report),ensure_ascii=False),file=sys.stderr)
        return 2 if exc.report['code'] in ('LIBRARY_OPERATIONS_REQUEST','LIBRARY_OPERATIONS_APPLY','LIBRARY_OPERATIONS_PATH') else 1
    except (OSError,TypeError,ValueError,KeyError) as exc:
        error=RigError('LIBRARY_OPERATIONS_IO' if isinstance(exc,OSError) else 'LIBRARY_OPERATIONS_REQUEST',str(exc),stage='asset-cli')
        print(json.dumps(dict(ok=False,error=error.report),ensure_ascii=False),file=sys.stderr)
        return 1 if isinstance(exc,OSError) else 2


if __name__=='__main__':
    raise SystemExit(main())
