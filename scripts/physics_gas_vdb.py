"""Independent bounded OpenVDB field reader, run in an isolated interpreter."""
import hashlib
import json
import math
from pathlib import Path
import sys

import openvdb


def need(value, message):
    if not value:
        raise ValueError(message)


def inspect(path, maximum_cells):
    headers = openvdb.readAllGridMetadata(str(path))
    need(1 <= len(headers) <= 16, 'Unexpected VDB grid count.')
    names = [g.name for g in headers]
    need(len(names) == len(set(names)), 'Duplicate VDB fields.')
    need(set(names) <= {'density','flame','shadow','temperature','velocity','fuel','reaction','heat'}, 'Unsupported VDB fields.')
    need({'density','temperature','velocity'} <= set(names), 'Missing required gas fields.')
    result = {}
    for header in headers:
        need(header.valueTypeName==('vec3s' if header.name=='velocity' else 'float'), 'Gas field has an incompatible voxel value type.')
        metadata = header.metadata
        count = metadata.get('file_voxel_count')
        resolution = metadata.get('file_base_resolution')
        memory = metadata.get('file_mem_bytes')
        need(type(count) is int and 0 <= count <= maximum_cells, 'VDB voxel count exceeds admission.')
        need(type(memory) is int and 0 <= memory <= maximum_cells*512+16*1024**2, 'VDB memory estimate exceeds admission.')
        need(isinstance(resolution,tuple) and len(resolution)==3 and all(type(n) is int and 1<=n<=128 for n in resolution)
             and math.prod(resolution)<=maximum_cells, 'VDB resolution exceeds admission.')
        grid = openvdb.read(str(path), header.name)
        need(grid.activeVoxelCount() == count, 'VDB active count disagrees with header.')
        total=0.;minimum=math.inf;maximum=-math.inf;weighted=[0.,0.,0.];visited=0
        for entry in grid.citerOnValues():
            visited += entry.count
            need(visited <= maximum_cells, 'VDB expanded active cells exceed admission.')
            value = entry.value
            scalar = math.sqrt(sum(v*v for v in value)) if isinstance(value,tuple) else float(value)
            need(math.isfinite(scalar), 'Nonfinite VDB field value.')
            need(header.name not in ('density','flame','fuel','reaction') or scalar>=0, 'Negative physical gas field.')
            minimum=min(minimum,scalar);maximum=max(maximum,scalar);total+=scalar*entry.count
            point=grid.transform.indexToWorld(tuple((a+b)/2 for a,b in zip(entry.min,entry.max)))
            need(all(math.isfinite(p) for p in point), 'Nonfinite VDB coordinate.')
            for i in range(3):weighted[i]+=point[i]*max(0,scalar)*entry.count
        need(visited == count, 'VDB iterator count disagrees with active grid.')
        result[header.name]=dict(active_voxels=count, resolution=list(resolution), minimum=minimum if count else 0.,
            maximum=maximum if count else 0., total=total,
            centroid_native=[x/total for x in weighted] if total>0 else None)
    return result


def main():
    if sys.argv[1:] == ['--probe']:
        module=Path(openvdb.__file__)
        print(json.dumps(dict(reader='OpenVDB',version=list(openvdb.LIBRARY_VERSION),
            module_sha256=hashlib.sha256(module.read_bytes()).hexdigest())))
        return
    root = Path(sys.argv[1]).resolve()
    job_path=root/'vdb-job.json'
    need(not job_path.is_symlink() and job_path.is_file() and job_path.stat().st_size<=32768,'Invalid VDB reader job.')
    job=json.loads(job_path.read_bytes())
    need(set(job)=={'frames','maximum_cells'} and type(job['frames']) is int and 2<=job['frames']<=240
         and type(job['maximum_cells']) is int and 1<=job['maximum_cells']<=128**3,'Invalid VDB limits.')
    folder=root/'cache'/'data'
    need(folder.is_dir() and not folder.is_symlink(), 'Missing native data directory.')
    expected={f'fluid_data_{frame:04d}.vdb' for frame in range(1,job['frames']+1)}
    need({p.name for p in folder.iterdir()}==expected,'Incomplete or unexpected native VDB frame inventory.')
    samples=[]
    for frame in range(1,job['frames']+1):
        path=folder/f'fluid_data_{frame:04d}.vdb'
        need(path.is_file() and not path.is_symlink() and 0<path.stat().st_size<=128*1024**2,'Invalid native VDB frame.')
        samples.append(dict(frame=frame-1,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),fields=inspect(path,job['maximum_cells'])))
    module=Path(openvdb.__file__)
    report=dict(protocol=1,reader='OpenVDB',version=list(openvdb.LIBRARY_VERSION),module_sha256=hashlib.sha256(module.read_bytes()).hexdigest(),samples=samples)
    (root/'volume-fields.json').write_text(json.dumps(report,sort_keys=True,separators=(',',':'),allow_nan=False))


if __name__=='__main__':main()
