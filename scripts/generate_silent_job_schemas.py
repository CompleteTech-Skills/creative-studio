"""Regenerate strict silent-video persistent job schemas."""
import json
from pathlib import Path
from silent_jobs import schemas


def main():
    destination=Path(__file__).resolve().parents[1]/'profiles'
    for name,schema in schemas().items():
        (destination/f'silent-video-job-{name}.schema.json').write_text(
            json.dumps(schema,indent=2,allow_nan=False)+'\n',encoding='utf-8')


if __name__=='__main__':main()
