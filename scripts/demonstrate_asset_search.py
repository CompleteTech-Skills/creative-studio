"""Build and query an offline index of real registered procedural mesh bytes."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from asset_library import AssetLibrary
from asset_library_contracts import seal
from asset_library_search import SearchIndex
from rig_errors import RigError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True,type=Path)
    args=parser.parse_args()
    args.destination.mkdir(mode=0o700,parents=False,exist_ok=False)
    example=Path(__file__).resolve().parents[1]/'examples/asset-library-foundation'
    base=json.loads((example/'manifest.json').read_bytes())
    library=AssetLibrary(args.destination/'library',create=True)
    locations=[]
    for identifier,words in [('cartoon-robot',['small','cartoon','robot']),
                             ('stone-tower',['medieval','stone','village']),
                             ('oak-tree',['outdoor','tree'])]:
        manifest=deepcopy(base)
        manifest['identity'].update(id=identifier,slug=identifier,name=identifier.replace('-',' ').title())
        manifest['metadata']['keywords']=words
        manifest['metadata']['capabilities']=['walk'] if identifier=='cartoon-robot' else []
        manifest['files'][0]['technical_metadata']={'triangles':1}
        manifest['provenance']['sources']=[manifest['files'][0]]
        reference=library.register(seal(manifest),{f['path']:example/f['path'] for f in manifest['files']})
        locations.append(dict(library='demo',reference=reference))
    index=SearchIndex(args.destination/'index',{'demo':library},create=True)
    build=index.rebuild(locations)
    robot=index.search('Find a small cartoon robot that can walk.')
    assert [r['location']['reference']['id'] for r in robot['results']]==['cartoon-robot']
    page=index.search(limit=2,sort='name')
    final=index.search(limit=2,sort='name',cursor=page['next_cursor'])
    assert len(page['results'])+len(final['results'])==3
    library.update_annotations(locations[0]['reference'],expected_revision=0,tags=['changed'],favorite=True,note='')
    try:
        index.search('robot')
    except RigError as exc:
        stale=exc.report['code']
    else:
        raise AssertionError('Changed annotations must invalidate the snapshot')
    assert stale=='LIBRARY_SEARCH_STALE'
    report=dict(schema_version=1,fixture=True,build=build,query=robot,
                pagination_count=3,stale_annotation_result=stale,
                original_payload_sha256=base['files'][0]['sha256'],embedding_executed=False)
    (args.destination/'evidence.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))


if __name__=='__main__':main()
