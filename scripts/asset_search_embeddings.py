"""Optional genuine local Sentence Transformers adapter; no model downloads.

Only an explicitly pinned, locally supplied MiniLM-L3 safetensors model is
accepted. No embedding library is imported by ordinary asset/search workflows.
"""
from __future__ import annotations

import importlib.util
import importlib.metadata
import os
from pathlib import Path
import re
import stat
import tempfile

from asset_library import read_source_bytes
from asset_library_contracts import portable_path
from asset_library_search import fail
from rig_errors import decode_json, object_hash

MODEL_ID='sentence-transformers/paraphrase-MiniLM-L3-v2'
ALLOWED_MODULES={'sentence_transformers.models.Transformer',
                 'sentence_transformers.models.Pooling',
                 'sentence_transformers.models.Normalize'}


def model_snapshot(directory):
    """Read a bounded no-follow model closure and compute its exact tree hash."""
    root=Path(directory).absolute()
    if not root.is_dir() or root.is_symlink():fail('MODEL','Model must be an explicit local directory.')
    payloads={};total=0;entries_seen=0
    pending=[(root, 0)]
    while pending:
        directory,depth=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                entries_seen+=1
                if entries_seen>512:fail('LIMIT','Model closure exceeds 512 directory entries.')
                path=Path(entry.path)
                info=entry.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
                    fail('MODEL','Model closure cannot contain symbolic links/reparse points.')
                relative=path.relative_to(root).as_posix();portable_path(relative)
                if stat.S_ISDIR(info.st_mode):
                    if depth>=128:fail('LIMIT','Model closure exceeds 128 directory levels.')
                    pending.append((path,depth+1))
                    continue
                if not stat.S_ISREG(info.st_mode) or path.suffix not in {'.json','.txt','.safetensors','.model'}:
                    fail('MODEL','Only local config, tokenizer and safetensors files are supported.')
                if len(payloads)>=128:fail('LIMIT','Model closure exceeds 128 files.')
                payloads[relative]=read_source_bytes(path)
                total+=len(payloads[relative])
                if total>256*1024*1024:fail('LIMIT','Model closure exceeds 256 MiB.')
    if not {'modules.json','config.json'}<=set(payloads) or not any(p.endswith('.safetensors') for p in payloads):
        fail('MODEL','Model requires modules.json, config.json and safetensors weights.')
    modules=decode_json(payloads['modules.json'])
    if not isinstance(modules,list) or not 1<=len(modules)<=8:fail('MODEL','Invalid module list.')
    for module in modules:
        if (not isinstance(module,dict) or module.get('type') not in ALLOWED_MODULES or
            not isinstance(module.get('path'),str)):
            fail('MODEL','Custom model code/modules are unsupported.')
        if module['path']:portable_path(module['path'])
    config=decode_json(payloads['config.json'])
    if not isinstance(config,dict) or config.get('model_type')!='bert' or 'auto_map' in config:
        fail('MODEL','Only the known built-in BERT model implementation is supported.')
    import hashlib
    digest=object_hash({p:hashlib.sha256(b).hexdigest() for p,b in payloads.items()})
    return digest,payloads


class LocalSentenceTransformer:
    model_id=MODEL_ID
    requires_credentials=False
    credentials_available=True
    transmits_metadata=False

    def __init__(self,directory,*,expected_sha256,model_id=MODEL_ID):
        if model_id!=MODEL_ID:fail('MODEL','Unknown local embedding model identity.')
        if not isinstance(expected_sha256,str) or not re.fullmatch('[0-9a-f]{64}',expected_sha256):
            fail('MODEL','Supply an exact model-tree SHA-256.')
        self.directory=Path(directory)
        self.model_version=expected_sha256
        self.runtime_available=importlib.util.find_spec('sentence_transformers') is not None
        self.runtime_version=(importlib.metadata.version('sentence-transformers') if self.runtime_available else 'unavailable')

    def encode(self,texts):
        if not self.runtime_available:fail('PROVIDER','Optional sentence-transformers runtime is unavailable.')
        if (not isinstance(texts,list) or len(texts)>4096 or
            any(not isinstance(t,str) or len(t)>65536 for t in texts) or
            sum(len(t) for t in texts)>16*1024*1024):
            fail('LIMIT','Embedding text batch exceeds configured bounds.')
        digest,payloads=model_snapshot(self.directory)
        if digest!=self.model_version:fail('MODEL','Pinned model files changed.')
        from sentence_transformers import SentenceTransformer
        try:
            # Load only the verified private snapshot, never the mutable source.
            with tempfile.TemporaryDirectory(prefix='asset-embedding-') as name:
                root=Path(name)
                for path,payload in payloads.items():
                    target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(payload)
                model=SentenceTransformer(str(root),device='cpu',local_files_only=True,
                                          trust_remote_code=False,model_kwargs={'use_safetensors':True})
                model.max_seq_length=min(model.max_seq_length,512)
                vectors=model.encode(texts,batch_size=16,show_progress_bar=False,normalize_embeddings=True)
                return vectors.tolist()
        except (ImportError,OSError,ValueError,TypeError,RuntimeError) as exc:
            fail('PROVIDER','Local embedding execution failed: '+str(exc))
