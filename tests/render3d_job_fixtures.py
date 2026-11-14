"""Build declared synthetic v1 transport data from the retained old SQLite schema."""
import hashlib
import json
from pathlib import Path
import socket
import sqlite3


def create_legacy_store(root, *, workers=1, memory_bytes=4*1024**3,
                        storage_bytes=16*1024**3, gpu_workers=0):
    """Create real v1 tables directly; never downgrade a resource-aware marker.

    This empty compatibility fixture is not retained production history. The
    schema/provenance come from executing the original 369bd707 constructor;
    tests can add their own ordinary render jobs through the public v1 API.
    """
    root=Path(root);fixture=Path(__file__).parent/'fixtures/render3d-store-v1'
    schema=(fixture/'schema.sql').read_bytes()
    provenance=json.loads((fixture/'provenance.json').read_text())
    if hashlib.sha256(schema).hexdigest()!=provenance['schema_sha256']:
        raise AssertionError('Legacy store schema differs from its retained source evidence')
    root.mkdir();(root/'cache').mkdir();(root/'staging').mkdir()
    marker=dict(version=1,host=socket.gethostname(),capacity=dict(
        workers=workers,memory_bytes=memory_bytes,storage_bytes=storage_bytes,gpu_workers=gpu_workers))
    (root/'store.json').write_text(json.dumps(marker,indent=2)+'\n')
    with sqlite3.connect(root/'jobs.sqlite3') as db:
        db.executescript(schema.decode('utf-8'))
    return root
