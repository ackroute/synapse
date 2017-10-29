import math

import synapse.lib.syntax as s_syntax

'''
Synapse module with helpers for geo-spacial calculations.
'''

# base geo distances will be in mm
r_mm = 6371008800.0
r_km = 6371.0088

# investigate perf impact of using WGS-84 ellipsoid for dist calc

def haversine(px, py, r=r_km):
    '''
    Calculate the haversine distance between two points
    defined by (lat,lon) tuples.

    Args:
        px ((float,float)): lat/long position 1
        py ((float,float)): lat/long position 2
        r (float): Radius of sphere

    Returns:
        (int):  Distance in km.
    '''
    lat1, lon1 = px
    lat2, lon2 = py

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)

    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    return c * r

def bbox(lat, lon, dist):
    '''
    Calculate a min/max bounding box for the circle defined by lalo/dist.

    Args:
        lat (float): The latitude in degrees
        lon (float): The longitude in degrees
        dist (int): A distance in geo:dist base units

    Returns:
        (float,float,float,float): (latmin, latmax, lonmin, lonmax)
    '''
    latr = math.radians(lat)
    lonr = math.radians(lon)

    rad = r_mm
    prad = rad * math.cos(latr)

    print('RAD: %r PRAD: %r' % (rad, prad))
    latd = dist / rad
    lond = dist / prad

    latmin = math.degrees(latr - latd)
    latmax = math.degrees(latr + latd)
    lonmin = math.degrees(lonr - lond)
    lonmax = math.degrees(lonr + lond)

    return (latmin, latmax, lonmin, lonmax)

def dms2dec(degs, mins, secs):
    '''
    Convert degrees, minutes, seconds lat/long form to degrees float.

    Args:
        degs (int): Degrees
        mins (int): Minutes
        secs (int): Seconds

    Returns:
        (float): Degrees
    '''
    return degs + (mins / 60.0) + (secs / 3600.0)
