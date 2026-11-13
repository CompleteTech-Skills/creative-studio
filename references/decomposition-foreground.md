# Optional foreground instance proposals

This stage runs the actual Torchvision COCO Mask R-CNN model locally and retains
separate source-sized binary masks for detected objects. It helps select people,
animals and other COCO objects in photographs with complex backgrounds. It does
not detect anatomical parts, identify arbitrary fantasy/robot characters, promise
pixel-perfect segmentation, or approve semantic quality. Missing detections and
thin/disconnected detail remain limitations. Confidence is the model's score,
not a calibrated likelihood or a human review.

The existing alpha/solid/manual foreground modes remain available. Use source
alpha directly when its complete silhouette is already correct; model proposals
can omit source pixels. Inference uses internal resizing, then Torchvision
projects masks back to source dimensions. Original bytes and RGBA samples are
never resized or smoothed by this stage. Binary ownership is separate from source
alpha, which the canonical decomposition applies exactly once.

## Provision explicitly

Use a separate environment. Installation and the following explicit download
require network; discovery, inference and selection do not download anything.

```bash
python -m venv foreground-env
foreground-env/bin/pip install -r requirements-foreground.txt
curl -fL https://download.pytorch.org/models/maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth \
  -o maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth
sha256sum maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth
foreground-env/bin/python scripts/decomposition_foreground.py doctor \
  --weights maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth
```

The required complete digest is
`bf2d0c1efbc936eeee2bc95a48e80ebc86b891f61b0106485937fc29f9315fc0`.
Only this checkpoint and Torch 2.10.0 / Torchvision 0.25.0 are admitted.
`torch.load` uses `weights_only=True`; alternate arbitrary serialized models are
not supported. Torchvision's [official model documentation](https://docs.pytorch.org/vision/main/models/generated/torchvision.models.detection.maskrcnn_resnet50_fpn.html)
describes the model's object categories, boxes and masks. Consult the model/data
license terms for your intended use. No third-party weights are redistributed in
this repository.

## Propose, inspect, select

```bash
foreground-env/bin/python scripts/decomposition_foreground.py propose \
  photo.png output/proposals --enable-analysis --origin 'Actual source attribution' \
  --weights maskrcnn_resnet50_fpn_coco-bf2d0c1e.pth
foreground-env/bin/python scripts/decomposition_foreground.py select \
  output/proposals output/character --candidate candidate-1
python scripts/hatch.py decompose-review-ui --run-dir output/character \
  --destination output/review
```

`propose` requires a new output directory and explicit analysis opt-in. It runs a
child process with one CPU thread, at most 1.5 million source pixels, 32 candidate
masks and a configurable wall-clock limit (default 120 seconds, maximum 300).
Current receipts retain actual Torch/interop counts. Historical v1 receipts
with two threads remain readable; current execution uses one.
The generated schema is `profiles/decomposition-foreground.schema.json`.
Source images and proposal artifacts are admitted through bounded regular-file
reads. Hash verification and image decoding use the same captured bytes, and
selection rechecks the original digest before creating the selected run. Image
inputs retain the existing 64 MiB encoded-byte bound; JSON records retain the
8 MiB analysis bound. Checkpoint loading hashes and deserializes the same captured
bytes within the explicit 200 MB checkpoint bound. These checks reject malformed
or replaced inputs; they do not provide isolation from a hostile process with the
same operating-system account.
`doctor` reports missing dependencies or weights without attempting inference.
A missing backend, a failed worker or no useful candidates leaves manual
selection available and does not create a falsely successful character.

Inspect `overlay.png`, each `masks/candidate-N.png` and `masks/overlap.png`.
Candidate masks can overlap: no detection silently steals pixels from another.
Explicitly selecting several IDs unions their masks into one imported character;
select each separately into separate new runs for multiple characters. The union
is a user choice, not proof those detections belong to one character. A model
miss may be foreground, not background; no detection proves complete coverage.

The selected run retains the original source, proposal receipt, all candidate
masks and overlay in `foreground-analysis/`; its annotation records the exact
receipt digest and selected IDs. It has source-bound unassigned foreground,
zero-error recomposition of selected pixels, and pending semantic review.
The run's `analysis.semantic_model_called` remains false because this stage does
not run the component analyzer; the retained foreground receipt separately records
the actual local instance-model execution. Running the separate face/eye analyzer
later does not turn these whole-object masks into anatomical parts. Use existing manual correction for bad boundaries.

## Evidence boundaries

`tests/test_decomposition_foreground.py` uses explicitly synthetic fixture
receipts to test validation, overlap, malicious paths and alpha preservation; it
is not model-quality evidence. Optional real-model tests in `tests/optional/` run through their dedicated CI
job, keeping optional dependencies outside the strict core delivery suite. They
use the retained NASA
astronaut photograph and explicitly provided Penn-Fudan photographs. Dataset
masks are used only after inference to measure output quality, never as inputs.
Even accurate foreground segmentation is insufficient to close automatic
anatomy, broad character diversity, or complete semantic-decomposition issues.
