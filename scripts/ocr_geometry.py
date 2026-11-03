"""Convex source-quadrilateral overlap, without flattening slanted text lines."""
from __future__ import annotations

import math

PROFILE = 'convex-source-quadrilaterals-v1'


def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def signed_area(points):
    return sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(points,points[1:]+points[:1]))/2


def polygon(block):
    bounds = block['b']
    points = block.get('q')
    if (isinstance(points, list) and len(points)==4
            and all(isinstance(p,(list,tuple)) and len(p)==2
                    and all(type(v) in (int,float) and math.isfinite(v) for v in p)
                    and bounds[0]-1e-6<=p[0]<=bounds[2]+1e-6
                    and bounds[1]-1e-6<=p[1]<=bounds[3]+1e-6 for p in points)):
        turns=[cross(points[i],points[(i+1)%4],points[(i+2)%4]) for i in range(4)]
        if all(v>1e-12 for v in turns) or all(v<-1e-12 for v in turns):
            return list(points) if signed_area(points)>0 else list(reversed(points))
    x,y,u,v=bounds
    return [(x,y),(u,y),(u,v),(x,v)]


def overlap(first, second):
    """Intersection divided by smaller true region, with conservative box fallback."""
    subject,clip=polygon(first),polygon(second)
    smaller=min(abs(signed_area(subject)),abs(signed_area(clip)))
    if smaller<=0:return 0.
    for a,b in zip(clip,clip[1:]+clip[:1]):
        output=[]
        if not subject:return 0.
        previous=subject[-1]
        prev_distance=cross(a,b,previous)
        for point in subject:
            distance=cross(a,b,point)
            if (distance>=0)!=(prev_distance>=0):
                ratio=prev_distance/(prev_distance-distance)
                output.append((previous[0]+ratio*(point[0]-previous[0]),
                               previous[1]+ratio*(point[1]-previous[1])))
            if distance>=0:output.append(point)
            previous,prev_distance=point,distance
        subject=output
    return min(1.,max(0.,abs(signed_area(subject))/smaller)) if len(subject)>=3 else 0.
