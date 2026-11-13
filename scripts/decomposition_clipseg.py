"""Pinned optional text-conditioned part proposals, without semantic approval.

Only local verified safetensors and builtin Transformers classes are used. No
remote code or model download is supported by inference or discovery.
"""
from __future__ import annotations
import hashlib
import importlib.metadata
import os
from pathlib import Path
from decomposition_spec import obj

REVISION = '999e0328d9e10b484360c477313983f9afdd7050'
FILES = {
 'config.json':'c023375966d31b3b1392764f7bd91df47098ce19f62f11b0263d8eedcf708bcd',
 'merges.txt':'9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a',
 'preprocessor_config.json':'4fb09ebcfd7651205ca8299b993c30088e5535ef350a72d46c5c4580eeac0440',
 'special_tokens_map.json':'c4864a9376a8401918425bed71fc14fc0e81f9b59ec45c1cf96cccb2df508eac',
 'tokenizer_config.json':'4c75d57fd9bd0be8478ad2d6f8b9cebdd4a45338eb108547329c2b6333476ca6',
 'vocab.json':'e089ad92ba36837a0d31433e555c8f45fe601ab5c221d4f607ded32d9f7a4349',
 'model.safetensors':'d00ca85d6b859f9d07b7cfb8ef26fe9771cb275b34c9368f2ecf603139307f55'}
VERSIONS={'torch':'2.10.0','transformers':'4.57.6'}
PARAMETERS = obj({'mask_threshold':{'type':'number','minimum':.05,'maximum':.95},
                  'presence_threshold':{'type':'number','minimum':.05,'maximum':.99}})
DEFAULT_PARAMETERS={'mask_threshold':.5,'presence_threshold':.7}
DEFAULT_TAXONOMY=['anatomy/head','face/eyes','face/mouth','anatomy/ears','anatomy/hands',
                  'anatomy/feet','anatomy/torso','anatomy/tail','anatomy/wings','anatomy/horns',
                  'clothing/scarf','accessory/helmet']


def _hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def model_directory():
    value=os.environ.get('HATCH_CLIPSEG_MODEL_DIR')
    if not value:raise ValueError('CLIPSeg unavailable: explicitly provision HATCH_CLIPSEG_MODEL_DIR')
    root=Path(value).expanduser()
    if root.is_symlink() or not root.is_dir():raise ValueError('CLIPSeg model directory must be a real local directory')
    for name,digest in FILES.items():
        path=root/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size>610_000_000 or _hash(path)!=digest:
            raise ValueError('CLIPSeg model file missing or differs from pinned snapshot: '+name)
    return root.resolve()


def identity():
    versions={}
    for package,expected in VERSIONS.items():
        try:versions[package]=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError as exc:raise ValueError('CLIPSeg unavailable: missing '+package) from exc
        if versions[package].split('+')[0]!=expected:raise ValueError('CLIPSeg requires '+package+' '+expected)
    model_directory()
    return {'provider':'local-transformers','name':'CIDAS/clipseg-rd64-refined',
            'version':REVISION+'; '+', '.join(k+' '+v for k,v in versions.items()),
            'files':([{'name':name,'sha256':digest} for name,digest in sorted(FILES.items())] +
                     [{'name':'adapter/'+name,'sha256':_hash(Path(__file__).parent/name)}
                      for name in ('decomposition_clipseg.py','decomposition_analysis.py')]),
            'identity_evidence':'Pinned local model/tokenizer and adapter/controller bytes, builtin Transformers/Torch versions; no remote code or network dispatch.'}


def validate_options(request):
    from decomposition_spec import validate_structure
    validate_structure(request['parameters'],PARAMETERS,'CLIPSeg parameters')
    if not request['taxonomy'] or len(request['taxonomy'])>32 or len(set(request['taxonomy']))!=len(request['taxonomy']):
        raise ValueError('CLIPSeg accepts 1–32 unique requested categories')
    if request['prompt'] or any(len(category)>128 for category in request['taxonomy']):
        raise ValueError('CLIPSeg accepts bounded category text, no additional free-form prompt')


def proposals(root,request):
    """Return actual learned masks; uncertain labels remain pending proposals."""
    for variable in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS'):
        os.environ[variable]='1'
    import torch
    from PIL import Image
    from transformers import CLIPSegProcessor,CLIPSegForImageSegmentation
    from decomposition_spec import safe_path
    from decomposition_masks import binary,intersect,count,mask_to_runs,MAX_TOTAL_RUNS
    validate_options(request)
    current=identity()
    if current!=request['model']:raise ValueError('CLIPSeg identity changed after request')
    torch.set_num_threads(1);torch.set_num_interop_threads(1)
    directory=model_directory()
    processor=CLIPSegProcessor.from_pretrained(directory,local_files_only=True)
    model=CLIPSegForImageSegmentation.from_pretrained(directory,local_files_only=True,use_safetensors=True).eval()
    with Image.open(safe_path(root,request['source']['path'])) as image:rgb=image.convert('RGB')
    with Image.open(safe_path(root,request['references'][0]['path'])) as image:foreground=binary(image)
    support=torch.frombuffer(bytearray(foreground.tobytes()),dtype=torch.uint8).reshape(rgb.height,rgb.width)>0
    prompts=[category.rsplit('/',1)[-1].replace('-',' ') for category in request['taxonomy']]
    # Reject overlong token sequences rather than silently truncating their meaning.
    for text in prompts:
        if len(processor.tokenizer(text)['input_ids'])>77:raise ValueError('CLIPSeg category exceeds tokenizer limit')
    parts=[];diagnostics=[];remaining_runs=MAX_TOTAL_RUNS
    for start in range(0,len(prompts),4):
        batch=prompts[start:start+4]
        inputs=processor(text=batch,images=[rgb]*len(batch),padding=True,return_tensors='pt')
        with torch.inference_mode():outputs=model(**inputs).logits
        for offset,logits in enumerate(outputs):
            category=request['taxonomy'][start+offset]
            scores=torch.nn.functional.interpolate(logits[None,None],size=(rgb.height,rgb.width),mode='bilinear',align_corners=False)[0,0].sigmoid()
            if not torch.isfinite(scores).all():raise ValueError('CLIPSeg returned nonfinite scores')
            scores=scores*support
            maximum=float(scores.max());threshold=request['parameters']['mask_threshold']
            mask=intersect(Image.fromarray((scores>=threshold).to(torch.uint8).numpy()*255),foreground)
            pixels=count(mask)
            diagnostic={'category':category,'maximum_pixel_score':maximum,'proposed_pixels':pixels,
                        'retained':bool(maximum>=request['parameters']['presence_threshold'] and pixels)}
            diagnostics.append(diagnostic)
            if not diagnostic['retained']:continue
            runs=mask_to_runs(mask,remaining_runs);remaining_runs-=len(runs)
            identifier='part-'+hashlib.sha256(category.encode()).hexdigest()[:20]
            parts.append({'id':identifier,'name':category.rsplit('/',1)[-1].replace('-',' ').title(),
                'category':category,'parent_id':None,'side':'unknown','pivot':None,'attachments':[],
                'mask':{'runs':runs},'confidence':maximum,
                'occlusion':{'status':'unknown','occluded_by':[],'note':'Single-image mask does not establish hidden structure or layer order.'},
                'notes':['Actual text-conditioned model proposal; pixel score is not calibrated semantic confidence.',
                    'No anatomical side, rig readiness, hierarchy or semantic correctness is established.',
                    '352-pixel internal model map projected to source-space membership; original RGB/alpha samples stay unchanged.'],
                '_pixels':pixels})
    # Small observed proposals own their overlap before broad body regions. This
    # is explicit ownership only and does not assert depth, hierarchy or anatomy.
    parts.sort(key=lambda p:(p['_pixels'],p['id']))
    for index,part in enumerate(parts):part.pop('_pixels');part['z_index']=len(parts)-index
    return parts,diagnostics,current,{'torch_threads':torch.get_num_threads(),
        'torch_interop_threads':torch.get_num_interop_threads(),
        'numerical_environment':{key:os.environ.get(key) for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS')}}
