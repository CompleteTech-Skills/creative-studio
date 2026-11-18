"""Human/agent visual evidence receipts distinct from numerical or synthetic checks."""
from rig_contracts import validate_contract
from rig_errors import atomic_json, object_hash, require, safe_path, sha256
from rig_models import rig_signature
from rig_validation import REVIEW_DIMENSIONS, validate_rig


def apply_review(rig, receipt):
    validate_contract(receipt,'RigReviewReceipt')
    require(receipt['rig_id']==rig.manifest['id'] and receipt['rig_version']==rig.manifest['version']
            and receipt['expected_sha256']==rig_signature(rig.manifest),'RIG_STALE_REVIEW','Review does not match the active rig revision.')
    require(receipt['fixture']==rig.manifest['source']['fixture'],'RIG_REVIEW_PROVENANCE','Review must preserve the original fixture classification.')
    require(set(receipt['dimensions'])==set(REVIEW_DIMENSIONS),'RIG_REVIEW_DIMENSIONS','Review must explicitly cover all nine visual dimensions.')
    for item in receipt['evidence']:
        require(sha256(safe_path(rig.root,item['path'],exists=True))==item['sha256'],'RIG_REVIEW_EVIDENCE','Review evidence is stale or modified.')
    verdicts=[d['verdict'] for d in receipt['dimensions'].values()]
    approved=all(v=='approved' for v in verdicts)
    if approved:
        require(receipt['reviewer_kind']!='synthetic-test','RIG_SYNTHETIC_REVIEW','Automated fixtures cannot grant visual approval.')
        require(validate_rig(rig,require_animation=True)['ok'],'RIG_REVIEW_VALIDATION','Resolve failed numerical checks before visual approval.')
        require(not any(u['resolution']=='pending' for u in rig.manifest['uncertainties']),
                'RIG_REVIEW_UNCERTAINTY','Resolve anatomical uncertainties before approval.')
    name='review/'+object_hash(receipt)+'.json';target=safe_path(rig.root,name);atomic_json(target,receipt)
    rig.manifest['review']['receipts'].append(dict(path=name,sha256=sha256(target)))
    rig.manifest['review']['status']='approved' if approved else 'rejected' if 'rejected' in verdicts else 'pending'
    atomic_json(rig.root/'rig.json',validate_contract(rig.manifest,'Rig'))
    return dict(status=rig.manifest['review']['status'],fixture=receipt['fixture'],production_artwork_approved=False,
                note='Visual approval applies only to this rig and its declared source classification.')
