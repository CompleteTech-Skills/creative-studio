# Real local image-analysis demonstration

`astronaut.png` is the exact public-domain photograph of NASA astronaut Eileen
Collins distributed by scikit-image v0.25.2. Source:
[upstream bytes](https://github.com/scikit-image/scikit-image/blob/v0.25.2/skimage/data/astronaut.png),
[scikit-image attribution](https://scikit-image.org/docs/0.25.x/api/skimage.data.html#skimage.data.astronaut).
Its SHA-256 is `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5`.
It is a photographic human-character input, not user artwork or a synthetic
segmentation fixture. No hand-authored masks were used in the recorded detector
execution. Training-set membership of this photograph is unknown.

```bash
python -m pip install -r requirements-analysis.txt
python scripts/hatch.py decompose --source examples/decomposition/analysis/astronaut.png \
  --run-dir output/astronaut-analysis --background keep \
  --origin 'NASA public-domain photograph distributed by scikit-image v0.25.2'
python scripts/hatch.py decompose-analyze --run-dir output/astronaut-analysis \
  --analysis-backend opencv-haar --enable-analysis
# Use the returned job SHA in the next command:
python scripts/hatch.py decompose-analysis-apply --run-dir output/astronaut-analysis --job JOB_SHA
python scripts/hatch.py decompose-validate --run-dir output/astronaut-analysis
python scripts/hatch.py decompose-review-ui --run-dir output/astronaut-analysis \
  --destination output/astronaut-analysis-review
```

The recorded local execution detected one face region and two eye regions.
The rectangles are coarse proposals; they include surrounding face/background
pixels and require mask correction. Applying them retains all original pixels,
leaves most of the photograph semantically unassigned, and does not approve the
face/eye interpretation, foreground selection, character identity or any rig.
See [the analysis guide](../../../references/decomposition-analysis.md) and the
retained `execution.json` for actual hashes, observed timing and limitations.
