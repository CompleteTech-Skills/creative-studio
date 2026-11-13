"""Narrow input-read diagnostics shared by rig package and native ZIP readers."""
from contextlib import contextmanager
import importlib
import zipfile
from rig_errors import RigError


def _codec_errors(compression, *, path, member, codec_code, stage):
    if compression is None or compression == zipfile.ZIP_STORED:return ()
    modules = {zipfile.ZIP_DEFLATED:'zlib', zipfile.ZIP_BZIP2:'bz2', zipfile.ZIP_LZMA:'lzma'}
    if hasattr(zipfile, 'ZIP_ZSTANDARD'):
        modules.update({20:'compression.zstd', zipfile.ZIP_ZSTANDARD:'compression.zstd'})
    name = modules.get(compression)
    if name is None:
        raise RigError(codec_code, 'ZIP member uses an unsupported compression method.', stage=stage,
                       asset=member, observed=compression, preserved=str(path),
                       correction='Repack the original input using a supported ZIP compression method.')
    try:module = importlib.import_module(name)
    except ImportError as exc:
        raise RigError(codec_code, 'The Python codec needed to read this ZIP member is unavailable.',
                       stage=stage, asset=member, observed=name, preserved=str(path),
                       correction='Install a Python build with this codec, or repack the original input using an available codec.') from exc
    if name == 'zlib':return (module.error,)
    if name == 'lzma':return (module.LZMAError,)
    if name == 'compression.zstd':return (module.ZstdError,)
    return ()


@contextmanager
def zip_read_scope(path, *, member=None, compression=None, code='RIG_BINARY_FORMAT',
                   codec_code='RIG_BINARY_CODEC', stage='binary-read'):
    """Use only around input open/read operations, never writes or promotion.

    Optional codecs load only when selected. BZIP2 documents OSError for invalid
    streams; errno-bearing operating-system failures remain distinguishable.
    """
    codec_errors = _codec_errors(compression, path=path, member=member,
                                 codec_code=codec_code, stage=stage)
    def invalid(exc):
        return RigError(code, 'ZIP input is corrupt or incomplete.', stage=stage,
            asset=member or str(path), observed=dict(exception=type(exc).__name__, message=str(exc)[:1000]),
            preserved=str(path), correction='Use complete input with valid ZIP data and checksums; preserve the original for diagnosis.')
    try:yield
    except RigError:raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError)+codec_errors as exc:
        raise invalid(exc) from exc
    except OSError as exc:
        if compression == zipfile.ZIP_BZIP2 and type(exc) is OSError and exc.errno is None:
            raise invalid(exc) from exc
        raise


def open_zip_reader(path, **diagnostics):
    """Open ZIP input; constructor-only unsupported features are input errors."""
    with zip_read_scope(path, **diagnostics):
        try:return zipfile.ZipFile(path)
        except NotImplementedError as exc:
            raise RigError(diagnostics.get('code','RIG_BINARY_FORMAT'),
                'ZIP input requires unsupported extraction features.',
                stage=diagnostics.get('stage','binary-read'), asset=str(path),
                observed=dict(exception=type(exc).__name__,message=str(exc)[:1000]), preserved=str(path),
                correction='Repack the original input using supported ZIP extraction features.') from exc
