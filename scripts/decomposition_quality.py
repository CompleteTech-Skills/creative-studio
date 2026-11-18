"""Pure evaluation metrics; no analyzer, source mutation or semantic approval.

Reviewed visible-part references and automatic outputs are separate inputs.
Nothing here establishes that a reference label or mask is correct.
"""
from collections import Counter
from PIL import Image
from asset_contracts import MAX_SOURCE_PIXELS
from decomposition_masks import MAX_COMPONENTS, count, intersect, union


def _binary_mask(image):
    if not isinstance(image, Image.Image) or image.mode != 'L':
        raise ValueError('evaluation masks must be source-space grayscale images')
    if not 1 <= image.width * image.height <= MAX_SOURCE_PIXELS:
        raise ValueError('evaluation mask exceeds canonical source pixel bound')
    if any(image.histogram()[1:255]):
        raise ValueError('evaluation masks must contain only 0 and 255')


def binary_mask_metrics(reference, proposal):
    """Measure observed source-space binary masks, without resizing either mask."""
    _binary_mask(reference)
    _binary_mask(proposal)
    if reference.size != proposal.size:
        raise ValueError('evaluation mask dimensions differ')
    expected, predicted = count(reference), count(proposal)
    overlap, combined = count(intersect(reference, proposal)), count(union(reference, proposal))
    return {'reference_pixels': expected, 'proposal_pixels': predicted,
            'intersection_pixels': overlap, 'union_pixels': combined,
            'missing_pixels': expected - overlap, 'extra_pixels': predicted - overlap,
            'iou': overlap / combined if combined else None,
            'precision': overlap / predicted if predicted else None,
            'recall': overlap / expected if expected else None,
            'semantic_approval': False}


def semantic_label_metrics(reference, proposal):
    """Count repeated visible labels; reviewers establish image support separately.

Two reference eyes and one proposed eye count as one missing instance. Unknown
or uncertain parts must be withheld from the scored reference, then reported
separately by the caller. Labels are exact reviewed strings, without synonyms.
"""
    def labels(values):
        if not isinstance(values, list) or len(values) > MAX_COMPONENTS:
            raise ValueError('evaluation labels must be bounded lists')
        if any(not isinstance(x, str) or not x.strip() or len(x) > 256 for x in values):
            raise ValueError('evaluation labels must be nonempty bounded strings')
        return Counter(values)
    expected, predicted = labels(reference), labels(proposal)
    matched = expected & predicted
    missing, incorrect = expected - predicted, predicted - expected
    n, p, true = sum(expected.values()), sum(predicted.values()), sum(matched.values())
    return {'reference_parts': n, 'proposed_parts': p, 'matched_parts': true,
            'missing_parts': dict(sorted(missing.items())),
            'unsupported_or_incorrect_parts': dict(sorted(incorrect.items())),
            'precision': true / p if p else None, 'recall': true / n if n else None,
            'semantic_approval': False}
