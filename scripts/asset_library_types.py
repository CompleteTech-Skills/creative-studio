"""Static types for the canonical v1 asset-library API.

These records describe the existing JSON contracts. Runtime admission remains
owned by :mod:`asset_library_contracts`; type annotations never verify content,
licensing, geometry, or review approval.
"""
from typing import Literal, TypedDict


Parameter = str | int | float | bool | list[str | int | float | bool]
Parameters = dict[str, Parameter]


class AssetRef(TypedDict):
    id: str
    version: str
    sha256: str


class _AssetFileOptional(TypedDict, total=False):
    format_version: str
    technical_metadata: Parameters


class AssetFile(_AssetFileOptional):
    path: str
    role: str
    sha256: str
    bytes: int | float
    format: Literal['glb', 'gltf', 'obj', 'png', 'jpeg', 'webp', 'json', 'binary']
    mime_type: str
    required: bool


class AssetIdentity(TypedDict):
    id: str
    namespace: str
    library: str
    slug: str
    name: str
    kind: Literal['character', 'rig', 'animation', 'prop', 'material', 'texture',
                  'environment', 'light', 'camera', 'prefab', 'reference',
                  'legacy-pet', 'legacy-cast', 'legacy-clip', 'document']
    subtype: str
    created_at: str
    attribution: str
    owner: str


class AssetDependency(TypedDict):
    target: AssetRef
    purpose: str
    required: bool
    origin: Literal['authored', 'imported', 'discovered']
    relationship: Literal['direct', 'transitive']
    capabilities: list[str]


class AssetMetadata(TypedDict):
    description: str
    category: str
    author: str
    publisher: str
    source_url: str
    license: str
    allowed_uses: list[str]
    keywords: list[str]
    style_refs: list[AssetRef]
    capabilities: list[str]
    review_status: Literal['unreviewed']


class AssetCompatibility(TypedDict):
    coordinate_system: Literal['right-handed-y-up-z-forward', 'legacy-2d']
    unit_scale: int | float
    rig_signature: list[str]
    skeleton_signature: list[str]
    material_capabilities: list[str]
    texture_channels: list[str]
    animation_channels: list[str]
    renderers: list[str]
    formats: list[str]
    extensions: list[str]
    consumer_capabilities: list[str]


class AssetPreview(TypedDict):
    type: Literal['thumbnail', 'turntable', 'motion', 'material']
    source_version_sha256: str
    backend: str
    backend_version: str
    settings_sha256: str
    camera_sha256: str
    lighting_sha256: str
    settings_file: AssetFile
    camera_file: AssetFile
    lighting_file: AssetFile
    file: AssetFile
    width: int | float
    height: int | float
    status: Literal['generated', 'verified-externally']


class _AssetProviderOptional(TypedDict, total=False):
    identifier: str
    parameters: Parameters


class AssetProvider(_AssetProviderOptional):
    name: str
    receipt: AssetFile


class AssetTool(TypedDict):
    name: str
    version: str


class _AssetSourceRecordOptional(TypedDict, total=False):
    copyright_holder: str


class AssetSourceRecord(_AssetSourceRecordOptional):
    file: AssetFile
    source_uri: str
    original_filename: str
    acquired_at: str
    author: str


class AssetConversion(TypedDict):
    tool: str
    version: str
    timestamp: str
    operation: str
    inputs: list[AssetFile]
    outputs: list[AssetFile]


class AssetEdit(TypedDict):
    kind: Literal['human', 'automated']
    description: str
    timestamp: str
    evidence: list[AssetFile]


class _AssetProvenanceOptional(TypedDict, total=False):
    source_records: list[AssetSourceRecord]
    conversions: list[AssetConversion]
    edit_history: list[AssetEdit]


class AssetProvenance(_AssetProvenanceOptional):
    method: Literal['import', 'generated', 'derived', 'fixture']
    origin: str
    fixture: bool
    sources: list[AssetFile]
    provider: list[AssetProvider]
    tools: list[AssetTool]
    parents: list[AssetRef]
    edits: list[str]
    license_evidence: list[AssetFile]
    permissions: list[AssetFile]
    uncertainty: list[str]


class AssetPublication(TypedDict):
    visibility: Literal['private', 'project-local']
    distribution: Literal['not-evaluated', 'restricted']
    license_evaluation: Literal['not-evaluated', 'evidence-retained']
    inventory: list[str]
    verification: Literal['payload-verified']
    integration: Literal['not-exercised', 'native-reference']


class AssetVersion(TypedDict):
    schema_version: Literal[1]
    kind: Literal['asset-library-version']
    identity: AssetIdentity
    version: str
    files: list[AssetFile]
    dependencies: list[AssetDependency]
    metadata: AssetMetadata
    compatibility: AssetCompatibility
    previews: list[AssetPreview]
    provenance: AssetProvenance
    publication: AssetPublication
    manifest_sha256: str


class AssetAnnotations(TypedDict):
    schema_version: Literal[1]
    asset: AssetRef
    revision: int | float
    tags: list[str]
    favorite: bool
    note: str


class SceneAssetReference(TypedDict):
    id: str
    version: str
    path: str
    sha256: str
    kind: str
