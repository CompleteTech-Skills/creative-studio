"""Offline diagnostic only. Does not execute a model or approve semantic parts."""
from pathlib import Path
import re,json,hashlib
from PIL import Image,ImageDraw,ImageChops
root=Path('/home/complete/.cache/hatch-coordination-20261112/parts226-evidence')
original=root/'florence-probe-1'; out=root/'florence-offline-decode-1';out.mkdir(exist_ok=False)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
manifest={'kind':'offline-recorded-output-diagnostic','semantic_approval':False,'production_adapter_executed':False,'new_model_calls':0,'algorithm':'Only diagnostic extraction of complete whitespace-separated loc tokens; original1000bin center integer coordinates; raw output untouched; alpha-intersected polygon closure is visualization, not semantic validation.','source_script_sha256':sha(Path(__file__)),'cases':[],'inference_progress':json.loads((original/'progress.json').read_text())}
rows=[]
for name in ('milo','dog-face'):
 source=json.loads((original/name/'source.json').read_text());p=Path(source['path']);assert sha(p)==source['sha256']
 image=Image.open(p).convert('RGBA');case={'name':name,'source':source,'proposals':[]};folder=out/name;folder.mkdir()
 for f in sorted((original/name).glob('*.json')):
  data=json.loads(f.read_text())
  if data.get('task')!='<REFERRING_EXPRESSION_SEGMENTATION>':continue
  raw=data['raw_output'];body=raw.replace('<s>','').replace('</s>','').strip();tokens=re.findall(r'<loc_(\d+)>',body)
  assert re.sub(r'<loc_\d+>','',body).strip()=='' and len(tokens)>=6 and len(tokens)%2==0 and len(tokens)<=256
  points=[(int((int(tokens[i])+.5)*image.width/1000),int((int(tokens[i+1])+.5)*image.height/1000)) for i in range(0,len(tokens),2)]
  assert all(0<=x<image.width and 0<=y<image.height for x,y in points)
  mask=Image.new('L',image.size);ImageDraw.Draw(mask).polygon(points,fill=255);mask=ImageChops.multiply(mask,image.getchannel('A').point(lambda v:255 if v else 0));maskpath=folder/(data['text']+'-diagnostic.png');mask.save(maskpath)
  overlay=image.copy();color=Image.new('RGBA',image.size,(255,20,80,0));color.putalpha(mask.point(lambda v:110 if v else 0));overlay=Image.alpha_composite(overlay,color)
  tile=Image.new('RGB',(600,400),'#eeeeee');thumb=overlay.copy();thumb.thumbnail((290,360));tile.paste(thumb,(305,35),thumb);before=image.copy();before.thumbnail((290,360));tile.paste(before,(0,35),before);ImageDraw.Draw(tile).text((8,8),name+': '+data['text']+' (diagnostic only)',fill='black');rows.append(tile)
  case['proposals'].append({'name':data['text'],'recorded_output':{'path':str(f),'sha256':sha(f)},'builtin_parser_polygon_count':len(data['parsed'][data['task']]['polygons']),'diagnostic_vertices':len(points),'mask':{'path':str(maskpath),'sha256':sha(maskpath),'pixels':sum(mask.histogram()[1:])},'generation_at_token_budget':data['generated_token_count']>=129,'semantic_status':'unreviewed','warning':'Polygon closure and token budget may omit anatomy; no production claim.'})
 manifest['cases'].append(case)
sheet=Image.new('RGB',(600,400*len(rows)),'white')
for i,tile in enumerate(rows):sheet.paste(tile,(0,400*i))
sheet.save(out/'contact-sheet.png');manifest['contact_sheet']={'path':str(out/'contact-sheet.png'),'sha256':sha(out/'contact-sheet.png')};(out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n');print(out/'manifest.json')
