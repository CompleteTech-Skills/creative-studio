"""Fixed local inventory probes; no providers, downloads, or implicit execution.

Resource observations describe this instant and this filesystem. They are not
reservations, worker isolation, or promises that a production job will fit.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys


OPERATIONS = ('runtime.python', 'resource.memory', 'resource.storage',
              'blender.engines', 'format.image', 'rigging.native',
              'animation.native', 'procedural.native', 'geometry.provider',
              'motion.provider', 'visual-qa.provider')
MODULES = {'rigging.native': 'rig_templates', 'animation.native': 'rig_animation',
           'procedural.native': 'rig_demo'}
PROVIDERS = {'geometry.provider': 'geometry_provider',
             'motion.provider': 'motion_provider',
             'visual-qa.provider': 'visual_qa_provider'}


def metadata(operation):
    """Public descriptions are not proof of installed or executable capability."""
    backend = ('configured-provider' if operation in PROVIDERS or operation == 'planning.provider'
               else 'blender' if operation.startswith('blender.') or operation == 'importer.blend'
               else 'ffmpeg' if operation.startswith('codec.')
               else 'hatch-imagegen-acquisition' if operation == 'native.acquisition'
               else 'native-rig' if operation in MODULES or operation.startswith('importer.')
               else 'local')
    formats = {'codec.h264': ['MP4/H264'], 'codec.vp9': ['WebM/VP9'],
               'codec.av1': ['WebM/AV1'], 'importer.obj': ['OBJ'],
               'importer.glb': ['GLB'], 'importer.gltf': ['glTF'],
               'importer.blend': ['BLEND']}.get(operation, [])
    return dict(backend=backend, supported_operations=[operation], supported_formats=formats,
                required_permissions=(['explicit-provider-network-and-budget-authority']
                                      if operation in PROVIDERS else ['explicit-local-probe']),
                performance=None)


def memory_observation():
    if sys.platform.startswith('linux'):
        # This kernel interface has a fixed local source and bounded input.
        with open('/proc/meminfo', 'rb') as stream:
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError('memory observation exceeds bound')
        fields = {}
        for line in raw.decode('ascii').splitlines():
            name, _, value = line.partition(':')
            if name in ('MemAvailable', 'MemTotal'):
                amount, unit = value.split()
                if unit != 'kB' or not amount.isdecimal():
                    raise ValueError('invalid kernel memory observation')
                fields[name] = int(amount) * 1024
        if set(fields) != {'MemAvailable', 'MemTotal'}:
            raise ValueError('kernel memory availability is unsupported')
        available, total = fields['MemAvailable'], fields['MemTotal']
        source = 'linux-proc-meminfo-MemAvailable'
    elif os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class MemoryStatus(ctypes.Structure):
            _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in
                ('total', 'available', 'page_total', 'page_available',
                 'virtual_total', 'virtual_available', 'extended_available')]
        state = MemoryStatus()
        state.length = ctypes.sizeof(state)
        api = ctypes.WinDLL('kernel32', use_last_error=True).GlobalMemoryStatusEx
        api.argtypes = [ctypes.POINTER(MemoryStatus)]
        api.restype = wintypes.BOOL
        if not api(ctypes.byref(state)):
            raise OSError('memory observation unavailable')
        available, total = int(state.available), int(state.total)
        source = 'windows-GlobalMemoryStatusEx'
    else:
        # Some supported POSIX hosts expose actual available physical pages;
        # others explicitly return unavailable, rather than invented free RAM.
        available = int(os.sysconf('SC_AVPHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
        total = int(os.sysconf('SC_PHYS_PAGES')) * int(os.sysconf('SC_PAGE_SIZE'))
        source = 'posix-sysconf-available-physical-pages'
    if not 0 <= available <= total or total <= 0:
        raise ValueError('invalid memory observation')
    return dict(available_bytes=available, total_bytes=total, source=source,
                scope='host-physical-memory; excludes container/job policy ceilings',
                reservation=False)


def measure(operation, directory):
    if operation == 'runtime.python':
        # The actual Director validators have already imported in this process.
        # Report the executed interpreter, not the documented pinned CI version.
        return dict(success=True, version=platform.python_version(),
                    implementation=platform.python_implementation(),
                    platform=sys.platform, pointer_bits=8*__import__('struct').calcsize('P'),
                    scope='current-interpreter-and-imported-Director-contracts',
                    production_matrix_verified=False)
    if operation == 'resource.memory':
        return dict(success=True, version='local-inventory-v1', **memory_observation())
    if operation == 'resource.storage':
        usage = shutil.disk_usage(directory)
        return dict(success=True, version='local-inventory-v1', available_bytes=usage.free,
                    total_bytes=usage.total, source='shutil.disk_usage',
                    scope='probe-destination-filesystem', reservation=False)
    raise ValueError('unsupported local measurement')


def worker_script(operation):
    if operation == 'blender.engines':
        return '''import bpy,json
bpy.ops.wm.read_factory_settings(use_empty=True)
engines=[]
choices={x.identifier for x in bpy.types.RenderSettings.bl_rna.properties['engine'].enum_items}
for name in sorted(choices|{'CYCLES','BLENDER_EEVEE_NEXT','BLENDER_WORKBENCH'}):
 try:
  bpy.context.scene.render.engine=name
  if bpy.context.scene.render.engine==name:engines.append(name)
 except (TypeError,ValueError):pass
assert engines
print('DIRECTOR_PROBE='+json.dumps({'success':True,'version':bpy.app.version_string,'engines':engines,'rendered':False}))
'''
    if operation == 'format.image':
        return '''from PIL import Image,__version__
import json
Image.new('RGBA',(2,2),(12,34,56,78)).save('probe.png')
with Image.open('probe.png') as image:
 image.load()
 assert image.size==(2,2) and image.convert('RGBA').getpixel((0,0))==(12,34,56,78)
print('DIRECTOR_PROBE='+json.dumps({'success':True,'version':__version__,'formats':['PNG'],'decoded_pixels':4}))
'''
    if operation == 'procedural.native':
        return '''import json
from rig_demo import make_geometry
from rig_models import validate_geometry
from rig_gltf import export_geometry
from rig_import import load_character_model
geometry,*_=make_geometry('robotic')
validate_geometry(geometry)
export_geometry(geometry,'probe.glb',provenance={'fixture':True,'operation':'capability-probe'})
loaded=load_character_model('probe.glb').geometry
assert loaded.vertex_count==geometry.vertex_count and loaded.triangle_count==geometry.triangle_count
print('DIRECTOR_PROBE='+json.dumps({'success':True,'version':'native-rig-v1','vertices':loaded.vertex_count,'triangles':loaded.triangle_count,'formats':['GLB'],'scope':'procedural-robotic-fixture-only'}))
'''
    return None
