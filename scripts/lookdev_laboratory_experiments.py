"""Run a fixed trusted laboratory experiment with immutable execution sources."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from rig_errors import require
ROOT=Path(__file__).resolve().parents[1]
FIXTURES={'shadows':'lookdev_laboratory_shadows.py'}


def run(experiment,destination,blender,*,python_site=None,temporary_root=None):
    require(experiment in FIXTURES,'LOOKDEV_LAB_EXPERIMENT','Choose a fixed trusted laboratory experiment.')
    destination=Path(destination).resolve();destination.mkdir(parents=True,exist_ok=False)
    inputs=sorted((ROOT/'scripts').glob('*.py'))+sorted((ROOT/'profiles').glob('*.json'))+[
        ROOT/'examples/lookdev-contracts/ceramic.json',ROOT/'tests/fixtures'/FIXTURES[experiment]]
    payloads={str(p.relative_to(ROOT)):p.read_bytes() for p in inputs}
    hashes={name:hashlib.sha256(data).hexdigest() for name,data in payloads.items()}
    snapshot=destination/'source'
    for name,data in payloads.items():
        target=snapshot/name;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    job={'destination':str(destination),'scripts':str(snapshot/'scripts')}
    (destination/'job.json').write_text(json.dumps(job,indent=2)+'\n')
    command=[str(Path(blender).resolve()),'--background','--factory-startup','--disable-autoexec',
        '--python-use-system-env','--threads','1','--python-exit-code','1','--python',
        str(snapshot/'tests/fixtures'/FIXTURES[experiment]),'--',str(destination/'job.json')]
    temporary=Path(temporary_root or destination.parent/'.lookdev-lab-tmp')/destination.name
    temporary.mkdir(parents=True,exist_ok=False)
    env=dict(os.environ,TMPDIR=str(temporary))
    if python_site:env['PYTHONPATH']=str(Path(python_site).resolve())
    started=time.monotonic()
    try:
        result=subprocess.run(command,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,env=env,timeout=1200)
    except subprocess.TimeoutExpired as exc:
        (destination/'render.log').write_bytes(exc.stdout or b'')
        require(False,'LOOKDEV_LAB_TIMEOUT','Experiment timed out; inspect retained render.log.')
    (destination/'render.log').write_bytes(result.stdout)
    require(result.returncode==0,'LOOKDEV_LAB_RENDER','Experiment failed; inspect retained render.log.')
    require(all(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest for name,digest in hashes.items()),
        'LOOKDEV_LAB_SOURCE_CHANGED','Sources changed; retained snapshot is diagnostic evidence only.')
    report=json.loads((destination/'report.json').read_text())
    report.update(source_sha256=hashes,command=command,elapsed_seconds=time.monotonic()-started)
    (destination/'report.json').write_text(json.dumps(report,indent=2)+'\n')
    inventory={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in destination.iterdir() if p.is_file()}
    (destination/'sha256.json').write_text(json.dumps(inventory,indent=2,sort_keys=True)+'\n')
    return report


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('experiment',choices=sorted(FIXTURES))
    p.add_argument('--output',required=True,type=Path);p.add_argument('--blender',required=True,type=Path)
    p.add_argument('--python-site',type=Path);p.add_argument('--temporary-root',type=Path);a=p.parse_args()
    run(a.experiment,a.output,a.blender,python_site=a.python_site,temporary_root=a.temporary_root)
    print(a.output/'report.json')
if __name__=='__main__':main()
