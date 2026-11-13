"""Generate bounded synthetic silent-media acceptance artifacts, never production art."""
from pathlib import Path
import argparse
import json
import wave
from PIL import Image, ImageDraw

from silent_video import build_source, capabilities, encode, make_request, verify_export


def demonstrate(destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    frames = destination/'frames'
    frames.mkdir()
    paths = []
    for index in range(12):
        path = frames/('%06d.png' % index)
        image = Image.new('RGB', (160, 120), (20+index*8, 35, 70))
        draw = ImageDraw.Draw(image)
        draw.rectangle((index*10, 30, index*10+25, 75), fill=(220, 180, 30))
        image.save(path)
        image.close()
        paths.append('frames/'+path.name)
    # An adjacent real PCM WAV must never be implicitly mapped by the encoder.
    with wave.open(str(destination/'adjacent.wav'), 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(48000)
        audio.writeframes(b'\0\0'*48000)
    report = {'schema_version': 1, 'fixture': True, 'production_art': False,
              'visual_review': 'not-performed', 'capabilities': capabilities(), 'outputs': []}
    for numerator, denominator in [(24, 1), (25, 1), (30, 1), (60, 1), (30000, 1001)]:
        source = build_source(destination, paths, {'numerator': numerator, 'denominator': denominator})
        request = make_request(source)
        name = 'h264-%d-%d' % (numerator, denominator)
        (destination/(name+'-request.json')).write_text(json.dumps(request, indent=2)+'\n')
        receipt = encode(request, destination, destination/(name+'.mp4'))
        # Exercise the public final-file verifier separately after publication.
        final = verify_export(destination/(name+'.mp4'), request, root=destination)
        (destination/(name+'-receipt.json')).write_text(json.dumps(receipt, indent=2)+'\n')
        report['outputs'].append({'path': name+'.mp4', 'request': name+'-request.json',
                                  'receipt': name+'-receipt.json', 'final_verification': final})
    source = build_source(destination, paths, {'numerator': 24, 'denominator': 1})
    request = make_request(source, 'vp9-webm')
    (destination/'vp9-request.json').write_text(json.dumps(request, indent=2)+'\n')
    receipt = encode(request, destination, destination/'vp9.webm')
    (destination/'vp9-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    report['outputs'].append({'path': 'vp9.webm', 'request': 'vp9-request.json',
                              'receipt': 'vp9-receipt.json', 'final_verification': verify_export(
                                  destination/'vp9.webm', request, root=destination)})
    (destination/'acceptance.json').write_text(json.dumps(report, indent=2)+'\n')
    return {'fixture': True, 'outputs': len(report['outputs']), 'acceptance': str(destination/'acceptance.json')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    print(json.dumps(demonstrate(parser.parse_args().destination), indent=2))
