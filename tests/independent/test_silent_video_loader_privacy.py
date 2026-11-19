import json,os,subprocess,sys,tempfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import silent_video as video
with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp)
    image=root/'source.png'
    Image.new('RGB',(32,32)).save(image)
    request=video.make_request(video.build_source(root,[image.name],{'numerator':24,'denominator':1}))
    request_path=root/'request.json'
    request_path.write_text(json.dumps(request))
    artifact=root/'test.mp4'
    artifact.write_bytes(b'ordinary synthetic non-media fixture')
    missing=str(root/'instrumentation-fixture'/'uninstalled.so')
    with patch.dict(os.environ,LD_PRELOAD=missing):
        try:
            video.verify_export(artifact,request)
        except ValueError as exc:
            reports={'python':exc.evidence}
        else:
            raise AssertionError('unexpected success')
        cli=subprocess.run([sys.executable,video.__file__,'verify-export',str(artifact),'--request',str(request_path),'--require-silent'],capture_output=True,timeout=15)
        reports['cli_json']=json.loads(cli.stdout)
        assert cli.returncode==1
    issues=[]
    for name,report in reports.items():
        record=report['probe']
        leak=missing in record['stderr']
        print(name,'real corrupt ffprobe status',record['exit_status'],'hash bound',report['artifact_sha256']==video.file_hash(artifact),'synthetic instrumentation pathname present in retained probe stderr',leak)
        if leak: issues.append(name)
    print('privacy expectation: omitted from both structured reports; observed disclosure count',len(issues))
    sys.exit(bool(issues))
