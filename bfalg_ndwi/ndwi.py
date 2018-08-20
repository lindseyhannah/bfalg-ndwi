"""
bfalg-ndwi
https://github.com/venicegeo/bfalg-ndwi

Copyright 2016, RadiantBlue Technologies, Inc.

This program is free software; you can redistribute it and/or
modify it under the terms of the GNU General Public License
as published by the Free Software Foundation; either version 2
of the License, or (at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.
"""
import os
import tempfile
import sys
import argparse
import json
import logging
import math
from pyproj import Proj, transform
try:
    from osgeo import gdal, osr
except:
    import gdal, osr
import gippy
import gippy.algorithms as alg
import beachfront.mask as bfmask
import beachfront.process as bfproc
import beachfront.vectorize as bfvec
from version import __version__

logger = logging.getLogger(__name__)


# defaults
defaults = {
    'minsize': 100.0,
    'close': 5,
    'coastmask': False,
    'simple': None,
    'smooth': 0.0,
}


def parse_args(args):
    """ Parse arguments for the NDWI algorithm """
    desc = 'Beachfront Algorithm: NDWI (v%s)' % __version__
    dhf = argparse.ArgumentDefaultsHelpFormatter
    parser = argparse.ArgumentParser(description=desc, formatter_class=dhf)

    parser.add_argument('-i', '--input', help='Input image (1 or 2 files)', required=True, action='append')
    parser.add_argument('-b', '--bands', help='Band numbers for Green and NIR bands', default=[1, 1], nargs=2, type=int)

    parser.add_argument('--outdir', help='Save intermediate files to this dir (otherwise temp)', default='')
    h = 'Basename to give to output files (no extension, defaults to first input filename'
    parser.add_argument('--basename', help=h, default=None)

    parser.add_argument('--nodata', help='Nodata value in input image(s)', default=0, type=int)
    parser.add_argument('--l8bqa', help='Landat 8 Quality band (used to mask clouds)')
    parser.add_argument('--coastmask', help='Mask non-coastline areas', default=defaults['coastmask'], action='store_true')
    parser.add_argument('--minsize', help='Minimum coastline size', default=defaults['minsize'], type=float)
    parser.add_argument('--close', help='Close line strings within given pixels', default=defaults['close'], type=int)
    parser.add_argument('--simple', help='Simplify using tolerance in map units', default=None, type=float)
    parser.add_argument('--smooth', help='Smoothing from 0 (none) to 1.33 (no corners', default=defaults['smooth'], type=float)
    parser.add_argument('--chunksize', help='Chunk size (MB) to use when processing', default=128.0, type=float)
    h = '0: Quiet, 1: Debug, 2: Info, 3: Warn, 4: Error, 5: Critical'
    parser.add_argument('--verbose', help=h, default=2, type=int)
    parser.add_argument('--version', help='Print version and exit', action='version', version=__version__)

    return parser.parse_args(args)


def validate_outdir(outdir):
    #Prevent path traversal vulnerability by validating outdirectory

    try:
        #An empty outdir variable is functionally equivalent to the current directory in this case, so the outdir is switched to
        #    the cwd for compatibility with the validator
        if outdir == '':
            outdir = os.getcwd()

        #convert to realpath
        outdir = os.path.realpath(outdir)
        check = 0

        #verify path is subordinate to current directory
        if outdir.startswith(os.getcwd()) is False:
            check = check + 1

        #verify path exists and is a directory
        if os.path.isdir(outdir) is False:
            check = check + 1

        if check != 0:
            # Not currently doing anything with the check value other than making sure its zero
            #    It could be valuable in the log to see how many errors occured
            #    Or to use different values to distinguish the combination of errors that occured.
            logger.info('Outdir invalid.  Changing outdir to current working directory')
            outdir = os.getcwd()
        return outdir
    except:
        return os.getcwd()


def validate_basename(basename):
    basename = basename.replace('.','')
    basename = basename.replace('/','')
    basename = basename.replace('\\','')
    return basename


def open_image(filenames, bands, nodata=0):
    """ Take in 1 or two filenames and two band numbers to create single 2-band (green, nir) image """
    try:
        # convert if jp2k format
        geoimgs = []
        for i, f in enumerate(filenames):
            bds = bands if len(filenames) == 1 else [bands[i]]
            bstr = ' '.join([str(_b) for _b in bds])
            logger.info('Opening %s [band(s) %s]' % (f, bstr), action='Open file', actee=f, actor=__name__)
            geoimg = gippy.GeoImage.open([f], update=True).select(bds)
            geoimg.set_nodata(nodata)
            if geoimg.format()[0:2] == 'JP':
                logger.info('Converting jp2 to geotiff', action='File Conversion', actee=f, actor=__name__)
                geoimg = None
                logger.info('Opening %s' % (f), action='Open file', actee=f, actor=__name__)
                ds = gdal.Open(f)
                fout = os.path.splitext(f)[0] + '.tif'
                if not os.path.exists(fout):
                    logger.info('Saving %s as GeoTiff' % f, action='Save file', actee=fout, actor=__name__)
                    gdal.Translate(fout, ds, format='GTiff')
                    status = os.path.exists(fout)
                    logger.debug('Verifying output file exists %s' % status, action='Verify output', actee=fout, actor=__name__)
                    ds = None
                logger.debug('fout is set to %s' % fout, action='Check variable value', actee=fout, actor=__name__)
                logger.info('Opening %s [band(s) %s]' % (fout, bstr), action='Open file', actee=fout, actor=__name__)
                logger.debug('bds is set to %s' % bds, action='Check variable value', actee=fout, actor=__name__)
                geoimg = gippy.GeoImage(fout, True).select(bds) # Is this trying to open the correct bands?, the error message would look similar
            geoimgs.append(geoimg)
        if len(geoimgs) == 2:
            b1 = geoimgs[1][bands[1]-1]
            geoimgs[0].add_band(b1)
            geoimgs[0].set_bandnames(['green', 'nir'])
            return geoimgs[0]
        geoimgs[0].set_bandnames(['green', 'nir'])
        return geoimg
    except Exception, e:
        logger.error('bfalg_ndwi error opening input: %s' % str(e))
        raise SystemExit()


def process(geoimg, coastmask=defaults['coastmask'], minsize=defaults['minsize'],
            close=defaults['close'], simple=defaults['simple'], smooth=defaults['smooth'],
            outdir='', bname=None):
    """ Process data from indir to outdir """
    if bname is None:
        bname = geoimg.basename()
    if outdir is None:
        outdir = tempfile.mkdtemp()
    prefix = os.path.join(outdir, bname)

    # calculate NWDI
    fout = prefix + '_ndwi.tif'
    logger.info('Saving NDWI to file %s' % fout, action='Save file', actee=fout, actor=__name__)
    imgout = alg.indices(geoimg, ['ndwi'], filename=fout)

    # mask with coastline
    if coastmask:
        # open coastline vector
        fname = os.path.join(os.path.dirname(__file__), 'coastmask.shp')
        fout_coast = prefix + '_coastmask.tif'
        try:
            imgout = bfmask.mask_with_vector(imgout, (fname, ''), filename=fout_coast)
        except Exception as e:
            if str(e) == 'No features after masking':
                logger.warning('Image does not intersect coastal mask.  Generating empty geojson file. Error: %s/' % str(e))
                geojson =  {
                    'type': 'FeatureCollection',
                    'features': []
                 }
                fout = prefix + '.geojson'
                logger.info('Saving GeoJSON to file %s' % fout, action='Save file', actee=fout, actor=__name__)
                with open(fout, 'w') as f:
                    f.write(json.dumps(geojson))
                return geojson
            if str(e) == "'NoneType' object has no attribute 'ExportToJson'":
                logger.warning('Image does not intersect coastal mask. Generating empty geojson file. Error: %s/' % str(e))
                geojson = {
                    'type': 'FeatureCollection',
                    'features': []
                  }
                fout = prefix + '.geojson'
                logger.info('Saving GeoJSON to file %s' % fout, action='Save file', actee=fout, actor=__name__)
                with open(fout, 'w') as f:
                    f.write(json.dumps(geojson))
                return geojson
            else:
                logger.warning('Error encountered during masking. Error : %s' % str(e))
                raise RuntimeError(e)

    # calculate optimal threshold
    threshold = bfproc.otsu_threshold(imgout[0])
    logger.debug("Otsu's threshold = %s" % threshold)
    #import pdb; pdb.set_trace()
    # save thresholded image
    #if False: #logger.level <= logging.DEBUG:
    fout = prefix + '_thresh.tif'
    logger.debug('Saving thresholded image as %s' % fout)
    logger.info('Saving threshold image to file %s' % fout, action='Save file', actee=fout, actor=__name__)
    imgout2 = gippy.GeoImage.create_from(imgout, filename=fout, dtype='byte')
    imgout2.set_nodata(255)
    #pdb.set_trace()
    (imgout[0] > threshold).save(imgout2[0])
    imgout = imgout2

    # vectorize threshdolded (ie now binary) image
    coastline = bfvec.potrace(imgout[0], minsize=minsize, close=close, alphamax=smooth)
    # convert coordinates to GeoJSON
    geojson = bfvec.to_geojson(coastline, source=geoimg.basename())
    # write geojson output file
    fout = prefix + '.geojson'
    logger.info('Saving GeoJSON to file %s' % fout, action='Save file', actee=fout, actor=__name__)
    with open(fout, 'w') as f:
        f.write(json.dumps(geojson))

    if simple is not None:
        fout = bfvec.simplify(fout, tolerance=simple)

    return geojson


def main(filenames, bands=[1, 1], l8bqa=None, coastmask=defaults['coastmask'], minsize=defaults['minsize'],
         close=defaults['close'], simple=defaults['simple'], smooth=defaults['smooth'], outdir='', bname=None,
         nodata=0):
    """ Parse command line arguments and call process() """
    getImageSize(filenames)
    geoimg = open_image(filenames, bands, nodata=nodata)
    calculateExtent(geoimg)
    if geoimg is None:
        logger.critical('bfalg-ndwi error opening input file %s' % ','.join(filenames))
        raise SystemExit()

    if bname is None:
        bname = geoimg[0].basename()

    logger.info('bfalg-ndwi start: %s' % bname)

    # landsat cloudmask
    if l8bqa is not None:
        logger.debug('Applying landsat quality mask %s to remove clouds' % l8bqa)
        try:
            fout_cloud = os.path.join(outdir, '%s_cloudmask.tif' % bname)
            logger.info('Opening %s BQA file' % l8bqa, action='Open file', actee=l8bqa, actor=__name__)
            maskimg = bfmask.create_mask_from_bitmask(gippy.GeoImage(l8bqa), filename=fout_cloud)
            geoimg.add_mask(maskimg[0] == 1)
        except Exception, e:
            logger.critical('bfalg-ndwi error creating cloudmask: %s' % str(e))
            raise SystemExit()

    try:
        fout = os.path.join(outdir, bname + '.geojson')
        if not os.path.exists(fout):
            geojson = process(geoimg, coastmask=coastmask, minsize=minsize, close=close,
                          simple=simple, smooth=smooth, outdir=outdir, bname=bname)
            size = os.path.getsize(fout)
            logger.info('Output geojson size: %s bytes' % size, action='Calculate output filesize', actee=fout, actor=__name__)
            logger.info('bfalg-ndwi complete: %s' % bname)
            return geojson
        else:
            logger.info('bfalg-ndwi: %s already run' % bname)
            with open(os.path.join(outdir, bname + '.geojson')) as f:
                geojson = json.loads(f.read())
            size = os.path.getsize(f)
            logger.info('Output geojson size: %s bytes' % size, action='Calculate output filesize', actee=f, actor=__name__)
            return geojson
    except Exception, e:
        logger.critical('bfalg-ndwi error: %s' % str(e))
        raise SystemExit()


def calculateExtent(geoimg):
    try:
        ext = geoimg.geo_extent()
        srs = osr.SpatialReference(geoimg.srs()).ExportToProj4()
        projin = Proj(srs)
        utm =  convert_wgs_to_utm(ext.x0(), ext.y0())
        utmproj = 'epsg:%s' % utm
        logger.debug('utmproj: %s' % utmproj, action='Calculate extent', actee='geoimg', actor=__name__)
        projout = Proj(init=utmproj)
        logger.debug('converting extent to UTM. Input proj is %s' % projin, action='Convert BBOX', actee='geoimg', actor=__name__)
        x0,y0 = projout(ext.x0(), ext.y0())
        x1,y1 = projout(ext.x1(), ext.y1())
        logger.debug('Extent in UTM, x0,x1,y0,y1: %s,%s,%s,%s' % (x0,x1,y0,y1), action='Verify extent', actee='geoimg', actor=__name__)
	area = (x1 - x0) * (y1 - y0)
	area = area/1000000.0
	logger.info('Scene size: %s sq kilometers' % area, action='Calculate extent', actee='geoimg', actor=__name__)
    except Exception, e:
        logger.error('Unable to calculate area extent of image. Error: %s' % str(e), action='Calculate extent', actee='geoimg', actor=__name__)


def convert_wgs_to_utm(lon, lat):
    try:
        logger.info('Identifying UTM zone for area calculation', action='Convert_wgs_to_utm', actee='geoimg', actor=__name__)
        utm_band = str(int((math.floor((lon + 180) / 6 ) % 60) + 1))
        if len(utm_band) == 1:
            utm_band = '0'+utm_band
        if lat >= 0:
            epsg_code = '326' + utm_band
        else:
            epsg_code = '327' + utm_band
        return int(epsg_code)
    except Exception, e:
        logger.error('Unable to identify UTM zone for area calculation. Error: %s' % str(e), action='Convert_wgs_to_utm', actee='geoimg', actor=__name__)


def getImageSize(filenames):
    try:
        size = 0
        for i in filenames:
            size = size + os.path.getsize(i)
            logger.info('Scene size: %s bytes' % size, action='Calculate Input filesize', actee=i, actor=__name__)
    except Exception, e:
        logger.error('Unable to calculate size of input file(s). Error: %s' % str(e), action='Calculate Input filesize', actee=filenames[0], actor=__name__) 


def cli():
    args = parse_args(sys.argv[1:])
    logger.setLevel(args.verbose * 10)
    gippy.Options.set_verbose(3)
    gippy.Options.set_chunksize(args.chunksize)
    outdir = validate_outdir(args.outdir)
    bname = validate_basename(args.basename)
    main(args.input, bands=args.bands, l8bqa=args.l8bqa, coastmask=args.coastmask, minsize=args.minsize,
         close=args.close, simple=args.simple, smooth=args.smooth, outdir=outdir, bname=bname, nodata=args.nodata)


if __name__ == "__main__":
    cli()
