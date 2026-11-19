"""Portable diagnostic-count observation. All records are simulated, and both inventories reject."""
from contextlib import redirect_stdout
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import sys
from PIL import Image
import silent_video as media

def run():
    payloads = {
        'ordinary': '{"streams":[{"codec_type":"audio"}]}',
        'duplicate': '{"streams":[{"codec_type":"audio"},{"codec_type":"audio"}],"streams":[{"codec_type":"audio"}]}',
    }
    reports = []
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        Image.new('RGB', (16, 16), 'red').save(root / 'frame.png')
        request = media.make_request(media.build_source(root, ['frame.png'], {'numerator': 24, 'denominator': 1}))
        request_file = root / 'request.json'
        request_file.write_text(json.dumps(request))
        artifact = root / 'simulated.mp4'
        artifact.write_bytes(b'portable synthetic fixture with no encoded media')
        for case, raw in payloads.items():
            record = dict(command=['fixture-ffprobe'], exit_status=0, stdout=raw, stderr='')
            for interface in ('python', 'cli'):
                with patch.object(media, '_binary', return_value='fixture-ffprobe'), patch.object(media, '_run', return_value=record) as simulated:
                    if interface == 'python':
                        try:
                            media.verify_export(artifact, request)
                        except ValueError as exc:
                            evidence = exc.evidence
                        else:
                            raise AssertionError('audio-only fixture should always reject')
                    else:
                        out = StringIO()
                        args = ['silent_video', 'verify-export', str(artifact), '--request', str(request_file), '--require-silent']
                        with patch.object(sys, 'argv', args), redirect_stdout(out):
                            try:
                                media.main()
                            except SystemExit as exc:
                                assert exc.code == 1
                            else:
                                raise AssertionError('audio-only fixture should always reject')
                        evidence = json.loads(out.getvalue())
                    assert simulated.call_count == 1
                assert evidence['verified'] is False
                assert evidence['probe']['stdout'] == raw
                assert evidence['artifact_sha256'] == sha256(artifact.read_bytes()).hexdigest()
                assert evidence['request_sha256'] == media._digest(request)
                assert evidence['source_manifest_sha256'] == media._digest(request['source'])
                reports.append(dict(case=case, interface=interface, verified=evidence['verified'],
                    transcript_sha256=sha256(raw.encode()).hexdigest(), exact_transcript_retained=True,
                    hashes_match=True, counts={k:v for k,v in evidence.items() if k.endswith('_stream_count')}))
    print(json.dumps(reports, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(run())
