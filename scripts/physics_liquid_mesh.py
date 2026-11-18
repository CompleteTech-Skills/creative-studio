"""Bounded independent decoding of Blender's native gzip BOBJ liquid mesh cache."""
import math
import struct
import zlib


class LiquidMeshError(ValueError):
    pass


def decode_bobj(payload, *, max_vertices=100000, max_triangles=200000, max_expanded_bytes=16*1024*1024):
    if any(type(x) is not int or x<=0 for x in (max_vertices,max_triangles,max_expanded_bytes)):
        raise LiquidMeshError('Decoder limits must be positive integers.')
    if type(payload) is not bytes or not payload or len(payload)>max_expanded_bytes:
        raise LiquidMeshError('Invalid or excessive compressed native mesh size.')
    try:
        decoder=zlib.decompressobj(16+zlib.MAX_WBITS)
        raw=decoder.decompress(payload,max_expanded_bytes+1)
    except zlib.error as exc:
        raise LiquidMeshError('Corrupt gzip BOBJ mesh.') from exc
    if len(raw)>max_expanded_bytes or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
        raise LiquidMeshError('Oversized, truncated or concatenated gzip mesh.')
    offset=0
    def integer():
        nonlocal offset
        if offset+4>len(raw):raise LiquidMeshError('Truncated native mesh count.')
        value=struct.unpack_from('<i',raw,offset)[0];offset+=4;return value
    def triples(count, kind):
        nonlocal offset
        size=count*12
        if offset+size>len(raw):raise LiquidMeshError('Truncated native mesh values.')
        values=[list(x) for x in struct.iter_unpack('<'+kind*3,raw[offset:offset+size])]
        offset+=size
        return values
    n=integer()
    if not 0<=n<=max_vertices:raise LiquidMeshError('Native vertex count exceeds admission.')
    vertices=triples(n,'f')
    normals_count=integer()
    if normals_count!=n:raise LiquidMeshError('Native normal count differs from vertices.')
    normals=triples(n,'f')
    count=integer()
    if not 0<=count<=max_triangles:raise LiquidMeshError('Native triangle count exceeds admission.')
    triangles=triples(count,'i')
    if offset!=len(raw):raise LiquidMeshError('Unexpected trailing native mesh bytes.')
    if any(not math.isfinite(x) for v in vertices+normals for x in v):raise LiquidMeshError('Nonfinite native mesh coordinates.')
    if any(len(set(t))!=3 or min(t)<0 or max(t)>=n for t in triangles):raise LiquidMeshError('Invalid native mesh triangle indices.')
    if (n==0)!=(count==0):raise LiquidMeshError('Partial empty native mesh.')
    return dict(vertices=vertices,triangles=triangles)


def canonical_vertices(mesh, component):
    """BOBJ normalized space uses the longest side of the native domain.

    The native domain is unrotated. Blender Z-up maps back to canonical Y-up;
    this is the inverse of the existing proper-basis adapter.
    """
    size=max(component['size']);tx,ty,tz=component['transform']['translation']
    return [[tx+size*x,ty+size*z,tz-size*y] for x,y,z in mesh['vertices']]


def measurements(vertices,triangles):
    if not vertices:return dict(vertex_count=0,triangle_count=0,bounds=None,signed_volume=0.)
    bounds=[[min(v[k] for v in vertices) for k in range(3)],[max(v[k] for v in vertices) for k in range(3)]]
    terms=[]
    for i,j,k in triangles:
        a,b,c=vertices[i],vertices[j],vertices[k]
        terms.append((a[0]*(b[1]*c[2]-b[2]*c[1])+a[1]*(b[2]*c[0]-b[0]*c[2])+a[2]*(b[0]*c[1]-b[1]*c[0]))/6)
    volume=math.fsum(terms)
    if not math.isfinite(volume):raise LiquidMeshError('Nonfinite mesh volume.')
    return dict(vertex_count=len(vertices),triangle_count=len(triangles),bounds=bounds,signed_volume=volume)
