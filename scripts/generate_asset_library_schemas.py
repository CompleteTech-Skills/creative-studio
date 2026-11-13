"""Generate/check the additive Asset Library schema family without 3D imports."""
import argparse
import json
from pathlib import Path
from asset_library_contracts import schemas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'profiles'
    for name, schema in schemas().items():
        path = root / name
        content = json.dumps(schema, indent=2) + '\n'
        if args.check:
            if not path.is_file() or path.read_text(encoding='utf-8') != content:
                raise SystemExit('schema drift: ' + str(path))
        else:
            path.write_text(content, encoding='utf-8')


if __name__ == '__main__': main()
