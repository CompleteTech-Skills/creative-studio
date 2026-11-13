"""Deterministic heightfields and bounded spatially indexed placement."""
import math
import random
import numpy as np
from scene3d_geometry import Mesh, positive, count, numeric_array
from scene3d_graph import require


class Heightfield:
    def __init__(self, width=20, depth=20, resolution=33, seed=0, elevation=2, heights=None, max_slope=None):
        self.width=positive(width,'width'); self.depth=positive(depth,'depth')
        self.resolution=count(resolution,'resolution',2,257)
        require(type(seed) is int and 0<=seed<2**32,'Seed must be an unsigned 32-bit integer.')
        require(type(elevation) in (int,float) and math.isfinite(elevation) and 0<=elevation<=10000,'Invalid elevation.')
        self.elevation_limit=float(elevation)
        n=self.resolution
        if heights is None:
            rng=random.Random(seed)
            # Smooth bounded Fourier modes avoid grid seams and global RNG mutation.
            waves=[(rng.uniform(0,2*math.pi),rng.randint(1,3),rng.randint(1,3),1/(i+1)) for i in range(6)]
            norm=sum(w[3] for w in waves)
            self.heights=np.array([[elevation*sum(a*math.sin(phase+x*kx/(n-1)*math.pi)*math.cos(z*kz/(n-1)*math.pi)
                                for phase,kx,kz,a in waves)/norm for x in range(n)] for z in range(n)])
        else:
            self.heights=numeric_array(heights,'heightmap').copy()
            require(self.heights.shape==(n,n) and np.isfinite(self.heights).all() and (np.abs(self.heights)<=elevation).all(),
                    'Heightmap dimensions/elevations violate bounds.')
        self.max_slope=max_slope
        if max_slope is not None:
            require(type(max_slope) in (int,float) and 0<=max_slope<89,'Slope limit must be degrees in [0,89).')
            dx=self.width/(n-1); dz=self.depth/(n-1)
            steepest=self._steepest()
            if steepest: self.heights*=min(1,math.tan(math.radians(max_slope))/steepest)

    def _steepest(self):
        h=self.heights; dx=self.width/(self.resolution-1); dz=self.depth/(self.resolution-1)
        first=np.hypot((h[:-1,1:]-h[:-1,:-1])/dx,(h[1:,:-1]-h[:-1,:-1])/dz)
        second=np.hypot((h[1:,1:]-h[1:,:-1])/dx,(h[1:,1:]-h[:-1,1:])/dz)
        return float(max(first.max(),second.max()))

    def mesh(self):
        if self.max_slope is not None:
            require(self._steepest() <= math.tan(math.radians(self.max_slope))+1e-12,
                    'Terrain edits exceed the configured triangle slope limit.')
        n=self.resolution
        vertices=[[x*self.width/(n-1)-self.width/2,self.heights[z,x],z*self.depth/(n-1)-self.depth/2] for z in range(n) for x in range(n)]
        triangles=[]
        for z in range(n-1):
            for x in range(n-1):
                a=z*n+x;b=a+1;c=a+n;d=c+1
                triangles.extend([[a,c,b],[b,c,d]])
        return Mesh(vertices,triangles).validate()

    def sample(self,x,z):
        """Exact triangle height and upward normal, matching the emitted diagonal."""
        require(type(x) in (int,float) and type(z) in (int,float) and math.isfinite(x) and math.isfinite(z)
                and -self.width/2<=x<=self.width/2 and -self.depth/2<=z<=self.depth/2,'Query outside heightfield.')
        n=self.resolution; u=(x/self.width+.5)*(n-1);v=(z/self.depth+.5)*(n-1)
        ix=min(int(u),n-2);iz=min(int(v),n-2);tx=u-ix;tz=v-iz
        a=self.heights[iz,ix];b=self.heights[iz,ix+1];c=self.heights[iz+1,ix];d=self.heights[iz+1,ix+1]
        if tx+tz<=1: h=a+(b-a)*tx+(c-a)*tz; gx=b-a;gz=c-a
        else: h=d+(c-d)*(1-tx)+(b-d)*(1-tz);gx=d-c;gz=d-b
        normal=np.array([-gx/(self.width/(n-1)),1,-gz/(self.depth/(n-1))]);normal/=np.linalg.norm(normal)
        return dict(height=float(h),normal=normal.tolist(),slope=math.degrees(math.acos(float(normal[1]))))

    def edit(self,updates):
        """Atomically edit bounded grid elevations while retaining slope/elevation limits."""
        require(isinstance(updates,(list,tuple)) and len(updates)<=65536,'Elevation edit budget exceeded.')
        candidate=self.heights.copy();seen=set()
        for update in updates:
            require(isinstance(update,dict) and set(update)=={'x','z','height'},'Elevation update needs x/z/height.')
            x,z,height=update['x'],update['z'],update['height']
            require(type(x) is int and type(z) is int and 0<=x<self.resolution and 0<=z<self.resolution,
                    'Elevation grid coordinate is outside the terrain.')
            require(type(height) in (int,float) and math.isfinite(height) and abs(height)<=self.elevation_limit,
                    'Edited elevation exceeds its configured bound.')
            require((x,z) not in seen,'An atomic elevation edit cannot repeat a grid coordinate.')
            candidate[z,x]=height;seen.add((x,z))
        previous=self.heights;self.heights=candidate
        try:self.mesh()
        except Exception:
            self.heights=previous
            raise
        return self

    def mask(self,values,*,target=0):
        """Blend elevations toward a bounded scalar target by an explicit [0,1] grid."""
        values=numeric_array(values,'terrain mask')
        require(values.shape==self.heights.shape and (values>=0).all() and (values<=1).all(),
                'Terrain mask must match the heightfield and contain weights in [0,1].')
        require(type(target) in (int,float) and math.isfinite(target) and abs(target)<=self.elevation_limit,
                'Masked target elevation exceeds its configured bound.')
        previous=self.heights;self.heights=self.heights*(1-values)+target*values
        try:self.mesh()
        except Exception:
            self.heights=previous
            raise
        return self

    def lod_mesh(self,stride=2):
        """Sample exact existing vertices at a divisor stride; no invented cave geometry."""
        count(stride,'LOD stride',2,256)
        require((self.resolution-1)%stride==0,'LOD stride must divide the grid interval count.')
        heights=self.heights[::stride,::stride]
        coarse=Heightfield(width=self.width,depth=self.depth,resolution=len(heights),
                           heights=heights,elevation=self.elevation_limit)
        return coarse.mesh()

    def smooth(self, iterations=1):
        count(iterations,'iterations',1,100)
        for _ in range(iterations):
            padded=np.pad(self.heights,1,mode='edge')
            self.heights=sum(padded[z:z+self.resolution,x:x+self.resolution] for z,x in ((1,1),(0,1),(2,1),(1,0),(1,2)))/5
        return self

    def path(self, points, width, elevation=0):
        """Flatten a bounded polyline corridor into this actual heightfield."""
        positive(width,'path width'); p=numeric_array(points,'path')
        require(p.ndim==2 and p.shape[1]==2 and 2<=len(p)<=256 and np.isfinite(p).all(),'Invalid path.')
        require(type(elevation) in (int,float) and math.isfinite(elevation) and abs(elevation)<=10000,'Invalid path elevation.')
        require((np.abs(p[:,0])<=self.width/2).all() and (np.abs(p[:,1])<=self.depth/2).all(),'Path exceeds terrain.')
        require((np.linalg.norm(np.diff(p,axis=0),axis=1)>1e-9).all(),'Path has empty spans.')
        axis_x=np.linspace(-self.width/2,self.width/2,self.resolution)
        axis_z=np.linspace(-self.depth/2,self.depth/2,self.resolution)
        grid_x,grid_z=np.meshgrid(axis_x,axis_z)
        distance=np.full(grid_x.shape,np.inf)
        for a,b in zip(p,p[1:]):
            delta=b-a
            t=np.clip(((grid_x-a[0])*delta[0]+(grid_z-a[1])*delta[1])/np.dot(delta,delta),0,1)
            distance=np.minimum(distance,np.hypot(grid_x-a[0]-t*delta[0],grid_z-a[1]-t*delta[1]))
        self.heights[distance<=width/2]=elevation
        return self


def scatter(terrain, total, spacing=1, seed=0, max_slope=30, exclusions=(), retries=50, clearance=0):
    """Poisson-like rejection sampling with grid broad phase and bounded failure."""
    count(total,'placement count',0,10000);positive(spacing,'spacing');count(retries,'retries',1,1000)
    require(type(seed) is int and 0<=seed<2**32 and type(max_slope) in (int,float) and 0<=max_slope<=90,'Invalid placement seed/slope.')
    require(isinstance(exclusions,(list,tuple)) and len(exclusions)<=128,'At most 128 exclusion rectangles are supported.')
    for rect in exclusions:
        require(isinstance(rect,(list,tuple)) and len(rect)==4 and all(type(x) in (int,float) and math.isfinite(x) for x in rect) and rect[0]<=rect[2] and rect[1]<=rect[3],'Invalid exclusion rectangle.')
    require(type(clearance) in (int,float) and math.isfinite(clearance) and 0<=clearance<min(terrain.width,terrain.depth)/2,
            'Invalid placement clearance.')
    require(spacing>=2*clearance,'Spacing must accommodate the complete prop footprint.')
    rng=random.Random(seed);grid={};points=[]
    for _ in range(min(total*retries,200000)):
        if len(points)==total: break
        x=rng.uniform(-terrain.width/2+clearance,terrain.width/2-clearance);z=rng.uniform(-terrain.depth/2+clearance,terrain.depth/2-clearance)
        if any(a-clearance<=x<=c+clearance and b-clearance<=z<=d+clearance for a,b,c,d in exclusions): continue
        sample=terrain.sample(x,z)
        if sample['slope']>max_slope:continue
        key=(math.floor(x/spacing),math.floor(z/spacing))
        if any((x-p[0])**2+(z-p[2])**2<spacing**2 for dx in (-1,0,1) for dz in (-1,0,1)
               for p in grid.get((key[0]+dx,key[1]+dz),())):continue
        point=[x,sample['height'],z];points.append(point);grid.setdefault(key,[]).append(point)
    require(len(points)==total,f'Impossible placement: found {len(points)} of {total} within retry budget.','SCENE3D_LAYOUT')
    return points
