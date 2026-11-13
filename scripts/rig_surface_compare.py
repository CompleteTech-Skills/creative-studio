"""Bounded tolerance-aware multiset comparison for points and oriented faces.

Bins only find candidates. Capacity matching preserves duplicate multiplicity;
cyclic triangle rotations are valid, reversed winding is never a rotation.
"""
from collections import deque
from itertools import product
import math
import numpy as np
from rig_errors import RigError,require


def _cyclic_rows(values, width):
    best=values.copy();indices=np.arange(len(values))
    for shift in (1,2):
        rotated=np.roll(values,-shift*width,axis=1)
        different=rotated!=best;first=np.argmax(different,axis=1)
        choose=np.any(different,axis=1)&(rotated[indices,first]<best[indices,first])
        best[choose]=rotated[choose]
    return best


def _orientation(row,width):
    points=row.reshape(3,width)[:,:3]
    edges=points[1:]-points[0];scale=float(np.max(np.abs(edges)))
    return np.cross(edges[0]/scale,edges[1]/scale) if scale else np.zeros(3)


def _degenerate_count(rows,counts,width):
    if not width or not len(rows):return 0
    points=rows.reshape(len(rows),3,width)[:,:,:3];edges=points[:,1:]-points[:,:1]
    scale=np.max(np.abs(edges),axis=(1,2));safe=np.where(scale>0,scale,1.)
    cross=np.cross(edges[:,0]/safe[:,None],edges[:,1]/safe[:,None])
    return int(counts[~np.any(cross,axis=1)].sum())


def match_rows(expected,actual,*,tolerance=2e-5,memory_bytes=2*1024**3,
               maximum_work=5000000,cyclic_width=None,position_tolerance=None):
    """Match float rows with multiplicity and explicit memory/work ceilings.

    For triangles, each row contains three equal-width corner records beginning
    with XYZ; cyclic_width selects that width. Extra fields (UVs, normals, morph
    deltas) follow XYZ. All fields are compared without rounding into equality.
    The returned error is the largest absolute component error on final matches.
    Degenerate faces have no geometric orientation; their count is explicit.
    """
    require(isinstance(expected,np.ndarray) and isinstance(actual,np.ndarray)
            and expected.ndim==2 and expected.shape==actual.shape and expected.shape[1]>=3
            and expected.dtype.kind=='f' and actual.dtype.kind=='f',
            'RIG_ROUNDTRIP_IDENTITY','Surface record inventory differs.')
    require(type(memory_bytes) is int and memory_bytes>0 and type(maximum_work) is int and maximum_work>0,
            'RIG_ROUNDTRIP_COMPARE_LIMIT','Comparison budgets must be positive integers.')
    require(cyclic_width is None or type(cyclic_width) is int and cyclic_width>=3
            and expected.shape[1]==3*cyclic_width,
            'RIG_ROUNDTRIP_IDENTITY','Oriented triangle records need three XYZ-first corners.')
    require((expected.nbytes+actual.nbytes)*12<=memory_bytes,
            'RIG_ROUNDTRIP_COMPARE_LIMIT','Surface records exceed the pre-allocation working-memory budget.')
    require(np.isfinite(expected).all() and np.isfinite(actual).all(),
            'RIG_ROUNDTRIP_IDENTITY','Surface records contain nonfinite values.')
    try:tol=np.asarray(tolerance,dtype=float)
    except (TypeError,ValueError,OverflowError) as exc:
        raise RigError('RIG_ROUNDTRIP_IDENTITY','Comparison tolerance must contain finite numeric values.') from exc
    require(tol.ndim==0 or tol.shape==(expected.shape[1],),
            'RIG_ROUNDTRIP_IDENTITY','Tolerance must be scalar or one value per record field.')
    require(np.isfinite(tol).all() and np.all(tol>0),
            'RIG_ROUNDTRIP_IDENTITY','Comparison tolerances must be finite and positive.')
    tol=np.broadcast_to(tol,(expected.shape[1],));anchor_tol=tol[:3]
    require(position_tolerance is None or type(position_tolerance) in (int,float)
            and math.isfinite(position_tolerance) and position_tolerance>0,
            'RIG_ROUNDTRIP_IDENTITY','Position tolerance must be finite and positive.')
    if cyclic_width:
        expected=_cyclic_rows(expected,cyclic_width);actual=_cyclic_rows(actual,cyclic_width)
    a,counts_a=np.unique(expected,axis=0,return_counts=True)
    b,counts_b=np.unique(actual,axis=0,return_counts=True)
    if not len(a):return dict(max_component_error=0.,records=0,degenerate_faces=0,work=0,max_position_error=0.)
    if np.array_equal(a,b) and np.array_equal(counts_a,counts_b):
        degenerate=_degenerate_count(a,counts_a,cyclic_width)
        return dict(max_component_error=0.,records=int(sum(counts_a)),degenerate_faces=degenerate,work=0,max_position_error=0.)
    require((len(a)+len(b))*1536<=memory_bytes//2,
            'RIG_ROUNDTRIP_COMPARE_LIMIT','Surface matching exceeds its conservative record-memory budget.')
    bins={};work=0;edges=0
    def key(point):return tuple(math.floor(float(value)/float(step)) for value,step in zip(point[:3],anchor_tol))
    shifts=(0,1,2) if cyclic_width else (0,)
    for index,row in enumerate(b):
        for shift in shifts:
            start=shift*cyclic_width if cyclic_width else 0
            bins.setdefault(key(row[start:]),[]).append((index,shift))
    adjacency={};distances={};position_errors={};owners=[{} for _ in b];free=counts_b.copy()
    def charge(count=1):
        nonlocal work
        work+=count
        require(work<=maximum_work,'RIG_ROUNDTRIP_COMPARE_LIMIT','Surface matching exhausted its bounded comparison work.')
    def neighbors(index):
        nonlocal edges
        if index in adjacency:return adjacency[index]
        center=key(a[index]);candidates={};source_normal=_orientation(a[index],cyclic_width) if cyclic_width else None
        for offset in product((-1,0,1),repeat=3):
            charge()
            for target,shift in bins.get(tuple(center[axis]+offset[axis] for axis in range(3)),[]):
                charge(max(1,expected.shape[1]//3))
                row=np.roll(b[target],-shift*cyclic_width) if cyclic_width and shift else b[target]
                difference=np.abs(a[index]-row)
                if np.any(difference>tol):continue
                if cyclic_width:
                    normal=_orientation(row,cyclic_width)
                    source_degenerate=not np.any(source_normal);target_degenerate=not np.any(normal)
                    if source_degenerate!=target_degenerate or not source_degenerate and float(np.dot(source_normal,normal))<=0:continue
                positions=(a[index]-row).reshape(3,cyclic_width)[:,:3] if cyclic_width else (a[index]-row)[:3].reshape(1,3)
                position_error=float(np.linalg.norm(positions,axis=1).max())
                if position_tolerance is not None and position_error>position_tolerance:continue
                distance=float(np.max(difference))
                if target not in candidates or distance<candidates[target][0]:candidates[target]=(distance,position_error)
        edges+=len(candidates)
        require((len(a)+len(b))*1536+edges*512<=memory_bytes,
                'RIG_ROUNDTRIP_COMPARE_LIMIT','Surface correspondence graph exceeds its memory budget.')
        adjacency[index]=sorted(candidates,key=lambda target:(candidates[target],target))
        for target,(distance,position_error) in candidates.items():
            distances[index,target]=distance;position_errors[index,target]=position_error
        return adjacency[index]
    for start,needed in enumerate(counts_a):
        remaining=int(needed)
        while remaining:
            queue=deque([start]);seen={start};parent_a={};parent_b={};sink=None
            while queue and sink is None:
                source=queue.popleft()
                for target in neighbors(source):
                    charge()
                    if target in parent_b:continue
                    parent_b[target]=source
                    if free[target]>0:sink=target;break
                    for previous,amount in owners[target].items():
                        charge()
                        if amount and previous not in seen:
                            seen.add(previous);parent_a[previous]=target;queue.append(previous)
            require(sink is not None,'RIG_ROUNDTRIP_IDENTITY','Round trip changed an oriented face or corner field beyond tolerance.')
            amount=min(remaining,int(free[sink]));cursor=sink
            while parent_b[cursor]!=start:
                source=parent_b[cursor];cursor=parent_a[source];amount=min(amount,owners[cursor][source]);charge()
            cursor=sink
            while True:
                source=parent_b[cursor];owners[cursor][source]=owners[cursor].get(source,0)+amount;free[cursor]-=amount
                if source==start:break
                old=parent_a[source];owners[old][source]-=amount;free[old]+=amount;cursor=old;charge()
            remaining-=amount
    require(not free.any(),'RIG_ROUNDTRIP_IDENTITY','Round trip added unmatched surface records.')
    maximum=max((distances[source,target] for target,entries in enumerate(owners)
                 for source,amount in entries.items() if amount),default=0.)
    max_position=max((position_errors[source,target] for target,entries in enumerate(owners)
                      for source,amount in entries.items() if amount),default=0.)
    degenerate=_degenerate_count(a,counts_a,cyclic_width)
    return dict(max_component_error=maximum,max_position_error=max_position,records=int(sum(counts_a)),degenerate_faces=degenerate,work=work)
