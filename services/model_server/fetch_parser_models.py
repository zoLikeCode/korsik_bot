"""Connected operator/build host only: import pinned layout/table artifacts."""
import argparse,hashlib,json,urllib.request
from pathlib import Path
MODELS=[
 ('docling-project/docling-layout-heron','8f39ad3c0b4c58e9c2d2c84a38465abf757272d8','docling-project--docling-layout-heron',
  ['config.json','preprocessor_config.json','model.safetensors'], {'model.safetensors':'00333a43451945aaf89db8ca9c0a17e75d1537c17db60fdb91aa95f4c7929e0c'}),
 ('docling-project/docling-models','2199320848bb9a8a519d22e4b528185a4f9a6f64','docling-project--docling-models',
  ['model_artifacts/tableformer/fast/tm_config.json','model_artifacts/tableformer/fast/tableformer_fast.safetensors'],
  {'model_artifacts/tableformer/fast/tableformer_fast.safetensors':'3119563aab5a7c96fda4d621119b63fd8806272b86c30936d15507616422f718'})]

def install(target):
 for repo,revision,folder,names,expected in MODELS:
  root=Path(target)/folder;root.mkdir(parents=True,exist_ok=True)
  with urllib.request.urlopen(f'https://huggingface.co/api/models/{repo}/revision/{revision}') as response:info=json.load(response)
  licenses=[r['rfilename'] for r in info['siblings'] if '/' not in r['rfilename'] and r['rfilename'].lower().startswith(('license','licence','notice','readme'))]
  declared=info.get('cardData',{}).get('license',[])
  declared=[declared] if isinstance(declared,str) else declared
  if not declared:raise RuntimeError('License declaration missing in '+repo)
  files=[]
  for name in names+licenses:
   dest=root/name;dest.parent.mkdir(parents=True,exist_ok=True)
   h=hashlib.sha256();size=0
   with urllib.request.urlopen(f'https://huggingface.co/{repo}/resolve/{revision}/{name}',timeout=120) as response,dest.open('wb') as out:
    while chunk:=response.read(1024*1024):out.write(chunk);h.update(chunk);size+=len(chunk)
   if name in expected and h.hexdigest()!=expected[name]:raise RuntimeError('Pinned upstream hash mismatch '+name)
   files.append({'path':name,'bytes':size,'sha256':h.hexdigest()})
  license_map={'apache-2.0':'Apache-2.0','cdla-permissive-2.0':'CDLA-Permissive-2.0'}
  for license_id in declared:
   standard=license_map.get(license_id)
   if not standard:raise RuntimeError('Unsupported declared license '+license_id)
   url=f'https://raw.githubusercontent.com/spdx/license-list-data/main/text/{standard}.txt'
   data=urllib.request.urlopen(url,timeout=60).read();name='LICENSE-'+standard+'.txt';(root/name).write_bytes(data)
   files.append({'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'source_url':url})
  declaration=json.dumps({'repository':repo,'revision':revision,'declared_license':declared,'declaration_source':'Pinned Hugging Face model card metadata'},ensure_ascii=False).encode()
  (root/'LICENSE-DECLARATION.json').write_bytes(declaration)
  files.append({'path':'LICENSE-DECLARATION.json','bytes':len(declaration),'sha256':hashlib.sha256(declaration).hexdigest()})
  (root/'manifest.json').write_text(json.dumps({'repository':repo,'revision':revision,'files':files},indent=2))
  print(json.dumps({'repository':repo,'files':len(files),'bytes':sum(f['bytes'] for f in files)}))
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--target',required=True);install(parser.parse_args().target)
