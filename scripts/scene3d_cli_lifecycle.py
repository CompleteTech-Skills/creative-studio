"""CLI delegation to the canonical durable repository and dependency owner."""
from pathlib import Path


def add_parsers(sub):
    init = sub.add_parser('init', help='Capture a validated native project as a durable editor repository')
    init.add_argument('--project', required=True, type=Path)
    init.add_argument('--scene', required=True)
    init.add_argument('--destination', required=True, type=Path)
    archive = sub.add_parser('archive', help='Archive every durable repository revision')
    archive.add_argument('--repository', required=True, type=Path)
    archive.add_argument('--destination', required=True, type=Path)
    restore = sub.add_parser('restore', help='Verify and restore a repository archive to a new destination')
    restore.add_argument('--file', required=True, type=Path)
    restore.add_argument('--destination', required=True, type=Path)
    impact = sub.add_parser('impact', help='Hash exact transitive dependencies for selected document paths')
    impact.add_argument('--project', required=True, type=Path)
    impact.add_argument('--file', required=True, type=Path, help='JSON list of exact document paths')
    impact.add_argument('--purpose', default='authoring-impact')


def execute(args):
    from editor3d_repository import EditorRepository
    if args.command == 'init':
        return EditorRepository.create(args.project, args.destination, scene_path=args.scene).snapshot()
    if args.command == 'archive':
        return EditorRepository(args.repository).archive(args.destination)
    if args.command == 'restore':
        return EditorRepository.restore_archive(args.file, args.destination).snapshot()
    from asset_library import read_source_bytes
    from rig_errors import decode_json
    from scene3d_lifecycle import dependency_receipt
    return dependency_receipt(args.project, decode_json(read_source_bytes(args.file, 1024 * 1024)), purpose=args.purpose)
