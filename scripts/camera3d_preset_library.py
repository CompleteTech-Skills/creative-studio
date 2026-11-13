"""Bounded immutable project/global cinematography preset snapshots.

These library templates snapshot meaningful camera/movement/shot parameters.
Scene reference resolution and instantiation remain with the native owners.
"""
from copy import deepcopy
from pathlib import Path
import json

from camera3d_contracts import ID, VERSION, MAX_BYTES, canonical_bytes, definitions, validate
from camera3d_presets import (CAMERA_PRESETS, MOVEMENT_PRESETS, SHOT_PRESETS,
                             camera_preset, movement_preset, shot_preset)

ROOT = Path(__file__).resolve().parents[1]


def schema():
    """Strict portable shape; numerical/source semantics also run on import."""
    def obj(properties):
        return dict(type='object',additionalProperties=False,properties=properties,required=list(properties))
    def enum(values):return dict(type='string',enum=list(values))
    camera = obj(dict(optics={'$ref':'#/$defs/optics'}, transform={'$ref':'#/$defs/trs'},
                      preset=enum(CAMERA_PRESETS),preset_version=VERSION))
    movement_variants=[]
    for name,(motion,defaults) in MOVEMENT_PRESETS.items():
        params={}
        for key,value in defaults.items():
            params[key]=({'type':'array','items':{'type':'number','minimum':-1e6,'maximum':1e6},'minItems':3,'maxItems':3}
                         if isinstance(value,list) else
                         {'type':'integer','minimum':0,'maximum':2**32-1} if key=='seed' else
                         {'type':'number','minimum':0 if key in ('amplitude_m','frequency_hz') else -1e6,'maximum':1e6})
        movement_variants.append(obj(dict(preset={'const':name},preset_version=VERSION,
                                          motion={'const':motion},parameters=obj(params))))
    movement={'oneOf':movement_variants}
    shot=obj(dict(preset=enum(SHOT_PRESETS),preset_version=VERSION,camera=camera,movement=movement,
                  subject_frame_fraction={'type':'number','exclusiveMinimum':0,'maximum':1}))
    variants=[]
    for family,content in [('camera',camera),('movement',movement),('shot',shot)]:
        variants.append(obj(dict(schema_version={'const':1},kind={'const':'camera3d-library-preset'},
            id=ID,version=VERSION,family={'const':family},content=content)))
    return {'$schema':'https://json-schema.org/draft/2020-12/schema',
            '$id':'https://creative-studio.local/schemas/camera3d-library-preset.schema.json',
            '$defs':definitions(),'oneOf':variants}


def _camera(content):
    validate(content['optics'],'optics')
    from camera3d_math import world_transform
    world_transform(content['transform'])


def _movement(content):
    expected=movement_preset(content['preset'],overrides=content['parameters'])
    if content['motion']!=expected['motion'] or content['preset_version']!='1.0.0':
        raise ValueError('camera3d library movement algorithm or preset version is unsupported')


def validate_library_preset(document):
    import jsonschema
    canonical_bytes(document)
    try:jsonschema.Draft202012Validator(schema()).validate(document)
    except jsonschema.ValidationError as exc:raise ValueError('camera3d library preset: '+exc.message) from exc
    if document['family']=='camera':_camera(document['content'])
    elif document['family']=='movement':_movement(document['content'])
    else:
        _camera(document['content']['camera']);_movement(document['content']['movement'])
    # Definitions are immutable v1 algorithms; a library document's own revision
    # can increase while the exact built-in algorithm revision stays explicit.
    content=document['content']
    if content['preset_version']!='1.0.0' or (document['family']=='shot' and content['camera']['preset_version']!='1.0.0'):
        raise ValueError('camera3d library preset algorithm version is unsupported')
    if len(canonical_bytes(document))>MAX_BYTES:
        raise ValueError('camera3d library preset exceeds byte budget')
    return document


def create_library_preset(family,name,*,identifier=None,version='1.0.0',overrides=None):
    factories=dict(camera=camera_preset,movement=movement_preset,shot=shot_preset)
    if family not in factories:raise ValueError('camera3d unknown library preset family')
    document=dict(schema_version=1,kind='camera3d-library-preset',id=name if identifier is None else identifier,
                  version=version,family=family,content=factories[family](name))
    if overrides is not None:
        if not isinstance(overrides,dict):raise ValueError('camera3d library overrides must be an object')
        allowed={'camera':{'optics','transform'},'movement':{'parameters'},
                 'shot':{'camera','movement','subject_frame_fraction'}}[family]
        if set(overrides)-allowed:raise ValueError('camera3d unsupported library preset override')
        document['content'].update(deepcopy(overrides))
    return validate_library_preset(document)


def revise_library_preset(document,version,*,identifier=None,overrides=None):
    from camera3d_catalog import _next_version
    validate_library_preset(document)
    if identifier is None:_next_version(document['version'],version)
    elif identifier==document['id']:raise ValueError('camera3d duplicate library preset requires a new ID')
    result=deepcopy(document);result.update(id=document['id'] if identifier is None else identifier,version=version)
    if overrides is not None:
        if not isinstance(overrides,dict):raise ValueError('camera3d library overrides must be an object')
        allowed={'camera':{'optics','transform'},'movement':{'parameters'},
                 'shot':{'camera','movement','subject_frame_fraction'}}[document['family']]
        if set(overrides)-allowed:raise ValueError('camera3d unsupported library preset override')
        result['content'].update(deepcopy(overrides))
    return validate_library_preset(result)


def export_library_preset(document,destination):
    from camera3d_catalog import _publish_document
    validate_library_preset(document)
    return _publish_document(document,destination)


def import_library_preset(path):
    from asset_library import read_source_bytes
    from rig_errors import decode_json
    return validate_library_preset(decode_json(read_source_bytes(path,MAX_BYTES)))


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--generate',action='store_true');parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    path=ROOT/'profiles/camera3d-library-preset.schema.json';data=json.dumps(schema(),indent=2)+'\n'
    if args.generate:path.write_text(data)
    if args.check and (not path.is_file() or path.read_text()!=data):
        raise SystemExit('schema drift: '+str(path))


if __name__=='__main__':main()
