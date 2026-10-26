"""Keep structural JSON schemas aligned with bounded runtime contracts."""
import argparse
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def object_schema(properties, required=()):
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(required)}


def build():
    profile=json.loads((ROOT/'profiles/profile.schema.json').read_text(encoding='utf-8'))
    profile['title']='Hatch-pet animation profile v1/v2'
    profile['description']='Structural schema; runtime also checks references, frame/timing relationships, resource bounds and derivation cycles.'
    profile['properties']['schema_version']={'type':'integer','enum':[1,2]}
    state=profile['properties']['states']['items']['properties']
    state['frames']['maximum']=64
    state['alignment']['properties']['resampling']={'enum':['nearest','bilinear','bicubic','lanczos']}
    state['playback']=object_schema({'mode':{'enum':['loop','once','hold']},'completion':{'type':'string'},
        'hold_frame':{'type':'integer','minimum':0},'interruptible':{'type':'boolean'},
        'reduced_motion_frame':{'type':'integer','minimum':0}})
    event=object_schema({'target':{'type':'string'},'priority':{'type':'integer','minimum':0,'maximum':100},
        'interrupt':{'enum':['always','higher','never']},'restart':{'type':'boolean'},'resume':{'type':'boolean'},
        'from':{'type':'array','minItems':1,'uniqueItems':True,'items':{'type':'string'}}},['target'])
    profile['properties']['playback']=object_schema({'initial':{'type':'string'},'cancel_state':{'type':'string'},
        'max_consecutive_interruptions':{'type':'integer','minimum':0,'maximum':32},
        'queue_limit':{'type':'integer','minimum':0,'maximum':32},'events':{'type':'object','additionalProperties':event}})
    export=profile['properties']['export']['properties']
    export['formats']['items']['enum']=['frames','atlas','gif','webp','browser','cards','flipbook','board','stimuli','cursor','craft']
    map_text={'type':'object','additionalProperties':{'type':'string','minLength':1}}
    export['options']=object_schema({'labels':map_text,'conditions':map_text,'content_review':{'type':'string','minLength':1},
        'palette':{'type':'object','minProperties':1,'maxProperties':32,'additionalProperties':{'type':'string','pattern':'^#[0-9a-fA-F]{6}$'}},
        'hotspot':{'type':'array','minItems':2,'maxItems':2,'items':{'type':'integer','minimum':0}},
        'cell_mm':{'type':'number','minimum':5,'maximum':100},'dpi':{'type':'number','minimum':72,'maximum':300}})
    text={'type':'string','minLength':1};strings={'type':'array','items':text}
    rect={'type':'array','minItems':4,'maxItems':4,'items':{'type':'integer','minimum':0}}
    identity=object_schema({'schema_version':{'const':1},'markings':strings,'materials':strings,'approved_props':strings,
        'asymmetries':strings,'unresolved_conflicts':strings,'forbid_mirror':{'type':'boolean'},
        'proportions':{'type':'object','additionalProperties':{'type':'number','exclusiveMinimum':0}},
        'palette':{'type':'object','additionalProperties':{'type':'string','pattern':'^#[0-9a-fA-F]{6}$'}},
        'directions':{'type':'object','additionalProperties':{'enum':['left','right','front','back','any']}}},['schema_version'])
    source=object_schema({'schema_version':{'const':1},'kind':{'const':'import'},'layout':{'enum':['image','strip','frames']},
        'files':{'type':'array','minItems':1,'items':text},'origin':text,'fixture':{'type':'boolean'}},['schema_version','kind','layout','files','origin'])
    permission=object_schema({'bounds':rect,'permit':{'enum':['disconnected','bridge']},'reason':text},['bounds','permit','reason'])
    annotation=object_schema({'schema_version':{'const':1},'source_sha256':{'type':'string','pattern':'^[0-9a-f]{64}$'},
        'author':text,'note':text,'frames':{'type':'array','minItems':1,'items':object_schema({'rect':rect,
            'allow_components':{'type':'array','items':permission}},['rect'])}},['schema_version','source_sha256','author','note','frames'])
    inputs={'$schema':'https://json-schema.org/draft/2020-12/schema','title':'Hatch asset input interfaces v1',
        '$defs':{'identity':identity,'source':source,'annotation':annotation},
        'oneOf':[{'$ref':'#/$defs/'+name} for name in ('identity','source','annotation')]}
    from style_registry import catalog_schema
    from decomposition_spec import schemas as decomposition_schemas
    from rig_contracts import schemas as rig_schemas
    from model3d_contracts import schemas as model3d_schemas
    from animation3d_contracts import schemas as animation3d_schemas
    return {ROOT/'profiles/profile.schema.json':profile,ROOT/'profiles/asset-inputs.schema.json':inputs,
            ROOT/'profiles/style-catalog.schema.json':catalog_schema(),
            **{ROOT / 'profiles' / name: value for name, value in decomposition_schemas().items()},
            **{ROOT / 'profiles' / name: value for name, value in rig_schemas().items()},
            **{ROOT / 'profiles' / name: value for name, value in model3d_schemas().items()},
            **{ROOT / 'profiles' / name: value for name, value in animation3d_schemas().items()}}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--check',action='store_true');args=p.parse_args()
    for path,value in build().items():
        content=json.dumps(value,indent=2)+'\n'
        if args.check:
            if path.read_text(encoding='utf-8')!=content:raise SystemExit('schema drift: '+str(path))
        else:path.write_text(content,encoding='utf-8')
