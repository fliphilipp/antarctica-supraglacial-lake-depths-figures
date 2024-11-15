import os
# os.environ["GDAL_DATA"] = "/home/parndt/anaconda3/envs/geo_py37/share/gdal"
# os.environ["PROJ_LIB"] = "/home/parndt/anaconda3/envs/geo_py37/share/proj"
import h5py
import math
import zipfile
import traceback
import shapely
import pandas as pd
import numpy as np
import geopandas as gpd
import matplotlib
import matplotlib.pylab as plt
from matplotlib.patches import Rectangle
from cmcrameri import cm as cmc
from mpl_toolkits.axes_grid1 import make_axes_locatable
from IPython.display import Image, display
from matplotlib.collections import PatchCollection
import matplotlib.patches as mpatches
from sklearn.neighbors import KDTree
from scipy.stats import binned_statistic
from scipy.signal import find_peaks
import ee
import requests
from datetime import datetime 
from datetime import timedelta
from datetime import timezone
import rasterio as rio
from rasterio import plot as rioplot
from rasterio import warp
import shutil
from shapely.geometry import Point, LinearRing
from shapely.ops import nearest_points
from matplotlib.legend_handler import HandlerTuple
import matplotlib.patheffects as path_effects
from collections import defaultdict
from matplotlib.lines import Line2D
from matplotlib.transforms import TransformedBbox, Bbox
from matplotlib.image import BboxImage
from matplotlib.legend_handler import HandlerBase
import PIL
import urllib

import sys
# sys.path.append('../utils/')
# from lakeanalysis.utils import dictobj, convert_time_to_string, read_melt_lake_h5

sys.path.append('../GlacierLakeDetectionICESat2/GlacierLakeIS2ML/')

from IS2ML_utils import *

#####################################################################
def get_sentinel2_cloud_collection(area_of_interest, date_time, days_buffer, min_sun_elevation=10, source='sentinel'):

    datetime_requested = datetime.strptime(date_time, '%Y-%m-%dT%H:%M:%SZ')
    start_date = (datetime_requested - timedelta(days=days_buffer)).strftime('%Y-%m-%dT%H:%M:%SZ')
    end_date = (datetime_requested + timedelta(days=days_buffer)).strftime('%Y-%m-%dT%H:%M:%SZ')
    print('Looking for %s images from %s to %s' % (source, start_date, end_date), end=' ')

    if source == 'landsat':
        def get_landsat_collection_TOA(area_of_interest, start_date, end_date):
            L8T1 = ee.ImageCollection('LANDSAT/LC08/C02/T1_TOA')
            L8T2 = ee.ImageCollection('LANDSAT/LC08/C02/T2_TOA')
            L9T1 = ee.ImageCollection('LANDSAT/LC09/C02/T1_TOA')
            L9T2 = ee.ImageCollection('LANDSAT/LC09/C02/T2_TOA')
            return (L8T1.merge(L8T2).merge(L9T2).merge(L9T2)
                    .filterBounds(area_of_interest)
                    .filterDate(start_date, end_date)
                       .filterMetadata('SUN_ELEVATION', 'greater_than', min_sun_elevation))
    
        def get_landsat_collection_SR(area_of_interest, start_date, end_date):
            L8T1 = ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
            L8T2 = ee.ImageCollection("LANDSAT/LC08/C02/T2_L2")
            L9T1 = ee.ImageCollection("LANDSAT/LC09/C02/T1_L2")
            L9T2 = ee.ImageCollection("LANDSAT/LC09/C02/T2_L2")
            return (L8T1.merge(L8T2).merge(L9T2).merge(L9T2)
                    .filterBounds(area_of_interest)
                    .filterDate(start_date, end_date)
                       .filterMetadata('SUN_ELEVATION', 'greater_than', min_sun_elevation))
        
        def landsat_cloud_score(image):
            cloud = ee.Algorithms.Landsat.simpleCloudScore(image).select('cloud').rename('probability')
            scene_id = image.get('LANDSAT_PRODUCT_ID')
            return image.addBands(cloud).set('PRODUCT_ID', scene_id)
    
        def set_cloudiness(img, aoi=area_of_interest):
            cloudprob = img.select(['probability']).reduceRegion(reducer=ee.Reducer.mean(), 
                                                                 geometry=aoi, 
                                                                 bestEffort=True, 
                                                                 maxPixels=1e6)
            return img.set('ground_track_cloud_prob', cloudprob.get('probability'))
    
        def rename_LST_bands(image):
            band_names = image.bandNames()
            new_band_names = band_names.map(lambda name: ee.String(name).replace('SR_', ''))
            return image.rename(new_band_names)
    
        cloud_collection = (get_landsat_collection_TOA(area_of_interest, start_date, end_date)
                                #.map( rename_LST_bands)
                                .map(landsat_cloud_score)
                                .map(set_cloudiness))

    else:
        # Import and filter S2 SR HARMONIZED
        s2_sr_collection = (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
            .filterBounds(area_of_interest)
            .filterDate(start_date, end_date)
            .filterMetadata('MEAN_SOLAR_ZENITH_ANGLE', 'less_than', ee.Number(90).subtract(min_sun_elevation)))
    
        # Import and filter s2cloudless.
        s2_cloudless_collection = (ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY')
            .filterBounds(area_of_interest)
            .filterDate(start_date, end_date))
    
        # Join the filtered s2cloudless collection to the SR collection by the 'system:index' property.
        cloud_collection = ee.ImageCollection(ee.Join.saveFirst('s2cloudless').apply(**{
            'primary': s2_sr_collection,
            'secondary': s2_cloudless_collection,
            'condition': ee.Filter.equals(**{
                'leftField': 'system:index',
                'rightField': 'system:index'
            })
        }))
    
        cloud_collection = cloud_collection.map(lambda img: img.addBands(ee.Image(img.get('s2cloudless')).select('probability')))
    
        def set_is2_cloudiness(img, aoi=area_of_interest):
            cloudprob = img.select(['probability']).reduceRegion(reducer=ee.Reducer.mean(), 
                                                                 geometry=aoi, 
                                                                 bestEffort=True, 
                                                                 maxPixels=1e6)
            return img.set('ground_track_cloud_prob', cloudprob.get('probability'))
            
        cloud_collection = cloud_collection.map(set_is2_cloudiness)
    
    return cloud_collection

    
#####################################################################
def download_imagery(fn, lk, gt, imagery_filename, days_buffer=5, max_cloud_prob=15, gamma_value=1.8, buffer_factor=1.2, imagery_shift_days=0,
                     max_images=5, stretch_color=True, source='sentinel'):

    lake_mean_delta_time = lk.mframe_data.dt.mean()
    ATLAS_SDP_epoch_datetime = datetime(2018, 1, 1, tzinfo=timezone.utc) # 2018-01-01:T00.00.00.000000 UTC, from ATL03 data dictionary 
    ATLAS_SDP_epoch_timestamp = datetime.timestamp(ATLAS_SDP_epoch_datetime)
    lake_mean_timestamp = ATLAS_SDP_epoch_timestamp + lake_mean_delta_time
    lake_mean_datetime = datetime.fromtimestamp(lake_mean_timestamp, tz=timezone.utc)
    time_format_out = '%Y-%m-%dT%H:%M:%SZ'
    is2time = datetime.strftime(lake_mean_datetime, time_format_out)

    # get the bounding box
    lon_rng = gt.lon.max() - gt.lon.min()
    lat_rng = gt.lat.max() - gt.lat.min()
    fac = 0.25
    bbox = [gt.lon.min()-fac*lon_rng, gt.lat.min()-fac*lat_rng, gt.lon.max()+fac*lon_rng, gt.lat.max()+fac*lat_rng]
    poly = [(bbox[x[0]], bbox[x[1]]) for x in [(0,1), (2,1), (2,3), (0,3), (0,1)]]
    roi = ee.Geometry.Polygon(poly)

    # get the earth engine collection
    collection_size = 0
    if days_buffer > 100:
        days_buffer = 100
    increment_days = days_buffer
    while (collection_size<max_images) & (days_buffer <= 100):
    
        collection = get_sentinel2_cloud_collection(area_of_interest=roi, date_time=lk.date_time, days_buffer=days_buffer, source=source)
    
        # filter collection to only images that are (mostly) cloud-free along the ICESat-2 ground track
        cloudfree_collection = collection.filter(ee.Filter.lt('ground_track_cloud_prob', max_cloud_prob))
        
        collection_size = cloudfree_collection.size().getInfo()
        if collection_size == 1: 
            print('--> there is %i cloud-free image.' % collection_size)
        elif collection_size > 1: 
            print('--> there are %i cloud-free images.' % collection_size)
        else:
            print('--> there are not enough cloud-free images: widening date range...')
        days_buffer += increment_days
    
        # get the time difference between ICESat-2 and Sentinel-2 and sort by it 
        # is2time = lk.date_time
        def set_time_difference(img, is2time=is2time, imagery_shift_days=imagery_shift_days):
            ref_time = ee.Date(is2time).advance(imagery_shift_days, 'day')
            timediff = ref_time.difference(img.get('system:time_start'), 'second').abs()
            return img.set('timediff', timediff)
        cloudfree_collection = cloudfree_collection.map(set_time_difference).sort('timediff').limit(max_images)

    # create a region around the ground track over which to download data
    lon_center = gt.lon.mean()
    lat_center = gt.lat.mean()
    gt_length = gt.x10.max() - gt.x10.min()
    point_of_interest = ee.Geometry.Point(lon_center, lat_center)
    region_of_interest = point_of_interest.buffer(gt_length*0.5*buffer_factor)

    if collection_size > 0:
        # select the first image, and turn the colleciton into an 8-bit RGB for download
        selectedImage = cloudfree_collection.first()
        # mosaic_crs = selectedImage.select('B3').projection().crs()
        def get_utm_epsg(lat, lon):
            zone_number = int((lon + 180) / 6) + 1
            if lat >= 0:
                epsg_code = 32600 + zone_number  # Northern Hemisphere
            else:
                epsg_code = 32700 + zone_number  # Southern Hemisphere
            return 'EPSG:%i' % epsg_code
            
        mosaic_crs = get_utm_epsg(lk.lat, lk.lon)
        
        print('mosaic_crs:', mosaic_crs)
        scale_reproj = 5

        # def resample_to_crs(image):
        #     return image.resample('bilinear').reproject(**{'crs': mosaic_crs, 'scale': scale_reproj})

        # try with fixing the mask
        def resample_to_crs(image):
            # Resample the image
            # image = image.updateMask(image.mask())
            # resampled = image.reproject(crs=mosaic_crs, scale=scale_reproj)
            resampled = image.resample('bilinear').reproject(crs=mosaic_crs, scale=scale_reproj) # looks like bilinear messes up missing data
            # Update mask after resampling to ensure it's correctly applied
            # mask = image.mask().reproject(crs=mosaic_crs, scale=scale_reproj)
            return resampled #.updateMask(mask)

        cloudfree_collection = cloudfree_collection.map(resample_to_crs)

        def apply_combined_mask(image):
            mask_b4 = image.select('B4').mask()
            mask_b3 = image.select('B3').mask()
            mask_b2 = image.select('B2').mask()
            combined_mask = mask_b4.And(mask_b3).And(mask_b2)
            return image.select('B4', 'B3', 'B2').updateMask(combined_mask)

        def create_combined_mask(image):
            mask_b4 = image.select('B4').mask()
            mask_b3 = image.select('B3').mask()
            mask_b2 = image.select('B2').mask()
            combined_mask = mask_b4.And(mask_b3).And(mask_b2)
            return combined_mask
        
        cloudfree_collection = cloudfree_collection.map(apply_combined_mask)

        # stretch the color values 
        def color_stretch(image):
            percentiles = image.select(['B4', 'B3', 'B2']).reduceRegion(**{
                'reducer': ee.Reducer.percentile(**{'percentiles': [1, 99], 'outputNames': ['lower', 'upper']}),
                'geometry': region_of_interest,
                'scale': 10,
                'maxPixels': 1e9,
                'bestEffort': True
            })
            lower = percentiles.select(['.*_lower']).values().reduce(ee.Reducer.min())
            upper = percentiles.select(['.*_upper']).values().reduce(ee.Reducer.max())
            return image.select('B4', 'B3', 'B2').unitScale(lower, upper).clamp(0.0, 1.0)

        mosaic = cloudfree_collection.sort('timediff', False).mosaic()
        mosaic = mosaic.updateMask(mosaic.mask())
        combined_mask = create_combined_mask(mosaic)
        if stretch_color:
            rgb = color_stretch(mosaic).updateMask(combined_mask)
        else:
            rgb = mosaic.select('B4', 'B3', 'B2').unitScale(0, 10000).clamp(0.0, 1.0).updateMask(combined_mask)

        rgb = rgb.unmask(0) # set masked values to zero
        rgb_gamma = rgb.pow(1/gamma_value)
        rgb8bit= rgb_gamma.multiply(255).uint8()
        
        # from the selected image get some stats: product id, cloud probability and time difference from icesat-2
        prod_id = selectedImage.get('PRODUCT_ID').getInfo()
        cld_prb = selectedImage.get('ground_track_cloud_prob').getInfo()
        s2datetime = datetime.fromtimestamp(selectedImage.get('system:time_start').getInfo()/1e3)
        s2datestr = datetime.strftime(s2datetime, '%Y-%b-%d')
        s2time = datetime.strftime(s2datetime, time_format_out)
        is2datetime = datetime.strptime(is2time, '%Y-%m-%dT%H:%M:%SZ')
        timediff = s2datetime - is2datetime
        days_diff = timediff.days
        if days_diff == 0: diff_str = 'Same day as'
        if days_diff == 1: diff_str = '1 day after'
        if days_diff == -1: diff_str = '1 day before'
        if days_diff > 1: diff_str = '%i days after' % np.abs(days_diff)
        if days_diff < -1: diff_str = '%i days before' % np.abs(days_diff)
        
        print('--> Closest cloud-free Sentinel-2 image:')
        print('    - product_id: %s' % prod_id)
        print('    - time difference: %s' % timediff)
        print('    - mean cloud probability: %.1f' % cld_prb)

        if not imagery_filename:
            imagery_filename = 'data/imagery/' + prod_id + '_' + fn.split('/')[-1].replace('.h5','') + '.tif'
            print(imagery_filename)

        try:
            with h5py.File(fn, 'r+') as f:
                if 'time_utc' in f['properties'].keys():
                    del f['properties/time_utc']
                dset = f.create_dataset('properties/time_utc', data=is2time)
                if 'imagery_info' in f.keys():
                    del f['imagery_info']
                props = f.create_group('imagery_info')
                props.create_dataset('product_id', data=prod_id)
                props.create_dataset('mean_cloud_probability', data=cld_prb)
                props.create_dataset('time_imagery', data=s2time)
                props.create_dataset('time_icesat2', data=is2time)
                props.create_dataset('time_diff_from_icesat2', data='%s' % timediff)
                props.create_dataset('time_diff_string', data='%s ICESat-2' % diff_str)
        except:
            print('WARNING: Imagery attributes could not be written to the associated lake file!')
            traceback.print_exc()
        
        # get the download URL and download the selected image
        success = False
        scale = 5
        tries = 0
        while (success == False) & (tries <= 7):
            try:
                downloadURL = rgb8bit.unmask(0).getDownloadUrl({'name': 'mySatelliteImage',
                                                          'crs': mosaic_crs,
                                                          'scale': scale,
                                                          'region': region_of_interest,
                                                          'filePerBand': False,
                                                          'format': 'GEO_TIFF'})
        
                response = requests.get(downloadURL)
                with open(imagery_filename, 'wb') as f:
                    f.write(response.content)
        
                print('--> Downloaded the 8-bit RGB image as %s.' % imagery_filename)
                success = True
                tries += 1
                return imagery_filename
                
            except:
                traceback.print_exc()
                scale *= 2
                print('-> download unsuccessful, increasing scale to %.1f...' % scale)
                success = False
                tries += 1

            
#####################################################################
def plot_imagery(fn, days_buffer=5, max_cloud_prob=30, xlm=[None, None], ylm=[None, None], gamma_value=1.8, imagery_filename=None,
                 re_download=True, ax=None, buffer_factor=1.2, imagery_shift_days=0, increase_gtwidth=1, stretch_color=True, min_conf=0.1,
                source='sentinel'):

    lk = dictobj(read_melt_lake_h5(fn))
    df = lk.photon_data.copy()
    dfd = lk.depth_data.copy()
    if not xlm[0]:
        # xlm[0] = np.max((df.xatc.min(), xmin))
        xlm[0] = df.xatc.min()
    if not xlm[1]:
        # xlm[1] = np.min((df.xatc.max(), xmax))
        xlm[1] = df.xatc.max()
    if not ylm[0]:
        ylm[0] = lk.surface_elevation-1.8*lk.max_depth
    if not ylm[1]:
        ylm[1] = lk.surface_elevation+1.4*lk.max_depth
    
    df = df[(df.xatc >= xlm[0]) & (df.xatc <= xlm[1]) & (df.h >= ylm[0]) & (df.h <= ylm[1])].reset_index(drop=True).copy()
    # x_off = np.min(df.xatc)
    # df.xatc -= x_off
    # dfd.xatc -= x_off

    # get the ground track
    df['x10'] = np.round(df.xatc, -1)
    gt = df.groupby(by='x10')[['lat', 'lon']].median().reset_index()
    lon_center = gt.lon.mean()
    lat_center = gt.lat.mean()
    
    thefile = 'none' if not imagery_filename else imagery_filename
    if ((not os.path.isfile(thefile)) or re_download) and ('modis' not in thefile):
        imagery_filename = download_imagery(fn=fn, lk=lk, gt=gt, imagery_filename=imagery_filename, days_buffer=days_buffer, 
                         max_cloud_prob=max_cloud_prob, gamma_value=gamma_value, buffer_factor=buffer_factor, 
                         imagery_shift_days=imagery_shift_days, stretch_color=stretch_color, source=source)
    
    try:
        myImage = rio.open(imagery_filename)
        
        # make the figure
        if not ax:
            fig, ax = plt.subplots(figsize=[6,6])
        
        rioplot.show(myImage, ax=ax)
        ax.axis('off')
    
        ximg, yimg = warp.transform(src_crs='epsg:4326', dst_crs=myImage.crs, xs=np.array(gt.lon), ys=np.array(gt.lat))
        if 'modis' in thefile:
            xrng = ximg[-1] - ximg[0]
            yrng = yimg[-1] - yimg[0]
            fac = 3
            print('using saved modis image')
            ax.plot([ximg[-1]+fac*xrng,ximg[0]-fac*xrng], [yimg[-1]+fac*yrng, yimg[0]-fac*yrng], 'k:', lw=1)
            ax.annotate('', xy=(ximg[-1]+fac*xrng, yimg[-1]+fac*yrng), xytext=(ximg[0]-fac*xrng, yimg[0]-fac*yrng),
                             arrowprops=dict(width=0, lw=0, headwidth=5, headlength=5, color='k'),zorder=1000)
            ax.plot(ximg, yimg, 'r-', lw=1, zorder=5000, solid_capstyle='butt')
        else:
            ax.annotate('', xy=(ximg[-1], yimg[-1]), xytext=(ximg[0], yimg[0]),
                             arrowprops=dict(width=0.7*increase_gtwidth, headwidth=5*increase_gtwidth, 
                                             headlength=5*increase_gtwidth, color='k'),zorder=1000)

            isdepth = dfd.depth>0
            bed = dfd.h_fit_bed
            bed[~isdepth] = np.nan
            bed[(dfd.depth>2) & (dfd.conf < min_conf)] = np.nan
            surf = np.ones_like(dfd.xatc) * lk.surface_elevation
            surf[~isdepth] = np.nan
            xatc_surf = np.array(dfd.xatc)[~np.isnan(surf)]
            lon_bed = np.array(dfd.lon)
            lat_bed = np.array(dfd.lat)
            lon_bed[(np.isnan(surf)) | (np.isnan(bed))] = np.nan
            lat_bed[(np.isnan(surf)) | (np.isnan(bed))] = np.nan
            xb, yb = warp.transform(src_crs='epsg:4326', dst_crs=myImage.crs, xs=lon_bed, ys=lat_bed)
            ax.plot(xb, yb, 'r-', lw=increase_gtwidth, zorder=5000, solid_capstyle='butt')
        
        if not ax:
            fig.tight_layout(pad=0)
    
        return myImage, lon_center, lat_center
    except: 
        traceback.print_exc()
        return None, lon_center, lat_center
                     
#####################################################################
def plotIS2(fn, ax=None, xlm=[None, None], ylm=[None,None], cmap=cmc.lapaz_r, name='ICESat-2 data',increase_linewidth=1, rasterize_scatter=False, min_conf=0.1):
    
    lk = dictobj(read_melt_lake_h5(fn))
    df = lk.photon_data.copy()
    dfd = lk.depth_data.copy()

    # add exact intersection points at zero depth
    inters = intersection(dfd.xatc, dfd.h_fit_bed, dfd.xatc, np.ones_like(dfd.xatc)*lk.surface_elevation)
    inters_depth = pd.DataFrame({ 
        'conf': 1.0, 
        'depth': 1e-10, 
        'h_fit_bed': lk.surface_elevation,
        'h_fit_surf': np.nan,
        'lat': np.nan,
        'lon': np.nan,
        'std_bed': np.nan,
        'std_surf': np.nan,
        'xatc': inters[0],
        
    })
    dfd = pd.concat((dfd, inters_depth)).sort_values(by='xatc').reset_index(drop=True)

    isdepth = (dfd.depth > 0) & (dfd.conf > min_conf)
    xmin = dfd.xatc[isdepth].min()
    xmax = dfd.xatc[isdepth].max()
    xrng = xmax - xmin
    xtend = np.max((0.2*xrng, 250))
    # xmin = dfd.xatc.min() - 0.1*xrng
    # xmax = dfd.xatc.max() + 0.05*xrng
    if not xlm[0]:
        # xlm[0] = np.max((df.xatc.min(), xmin))
        xlm[0] =  np.max((df.xatc.min(), xmin - xtend))
    if not xlm[1]:
        # xlm[1] = np.min((df.xatc.max(), xmax))
        xlm[1] = np.min((df.xatc.max(), xmax + xtend))
    if not ylm[0]:
        ylm[0] = lk.surface_elevation-1.8*lk.max_depth
    if not ylm[1]:
        ylm[1] = lk.surface_elevation+1.4*lk.max_depth
    # df = df[(df.xatc >= xlm[0]) & (df.xatc <= xlm[1]) & (df.h >= ylm[0]) & (df.h <= ylm[1])].reset_index(drop=True).copy()
    # # df = df[(df.xatc <= xlm[1]) & (df.h <= ylm[1])].reset_index(drop=True).copy()
    # dfd = dfd[(dfd.xatc >= xlm[0]) & (dfd.xatc <= xlm[1])].reset_index(drop=True).copy()
    # x_off = np.min(dfd.xatc)
    # df.xatc -= x_off
    # dfd.xatc -= x_off
    # xlm[0] -= x_off
    # xlm[1] -= x_off
    
    isdepth = dfd.depth>0
    bed = dfd.h_fit_bed
    bed[~isdepth] = np.nan
    bed[(dfd.depth>2) & (dfd.conf < min_conf)] = np.nan
    surf = np.ones_like(dfd.xatc) * lk.surface_elevation
    surf[~isdepth] = np.nan
    surf_only = surf[~np.isnan(surf)]
    bed_only = bed[(~np.isnan(surf)) & (~np.isnan(bed))]
    xatc_surf = np.array(dfd.xatc)[~np.isnan(surf)]
    xatc_bed = np.array(dfd.xatc)[(~np.isnan(surf)) & (~np.isnan(bed))]
    
    # make the figure
    if not ax:
        fig, ax = plt.subplots(figsize=[8,5])

    df['is_afterpulse']= df.prob_afterpulse > np.random.uniform(0,1,len(df))
    if not cmap:
        # ax.scatter(df.xatc[~df.is_afterpulse], df.h[~df.is_afterpulse], s=1, c='k')
        dfp = df[~df.is_afterpulse].copy()
        # dfp = df.copy()
        area = (ylm[1]-ylm[0]) * (xlm[1] - xlm[0])
        sz = np.min((300000 / area, 5))
        # minval = 0.1
        # colvals = np.clip(dfp.snr, minval, 1)
        # phot_cols = cmc.grayC(colvals)
        # dfp['colvals'] = colvals
        # dfp['phot_colors'] = list(map(tuple, phot_cols))
        # dfp = dfp.sort_values(by='colvals')
        # ax.scatter(dfp.xatc, dfp.h, s=sz, c=dfp.phot_colors, edgecolors='none', alpha=1)
        ax.scatter(dfp.xatc, dfp.h, s=sz, c='k', edgecolors='none', alpha=1, rasterized=rasterize_scatter)
    else:
        ax.scatter(df.xatc, df.h, s=1, c=df.snr, cmap=cmap, rasterize=rasterized_scatter)
        
    # ax.scatter(dfd.xatc[isdepth], dfd.h_fit_bed[isdepth], s=4, color='r', alpha=dfd.conf[isdepth])
    # ax.plot(dfd.xatc, dfd.h_fit_bed, color='gray', lw=0.5)
    ax.plot(dfd.xatc, bed, color='r', lw=increase_linewidth)
    ax.plot(dfd.xatc, surf, color='C0', lw=increase_linewidth)

    # add the length of surface
    arr_y = lk.surface_elevation+lk.max_depth*0.25
    txty = arr_y + +lk.max_depth*0.1
    x_start = np.min(xatc_surf)
    x_end = np.max(xatc_surf)
    x_mid = (x_end + x_start) / 2
    len_surf_m = np.floor((x_end-x_start)/100)*100
    len_surf_km = len_surf_m/1000
    arr_x1 = x_mid - len_surf_m / 2
    arr_x2 = x_mid + len_surf_m / 2
    arr_xshorten = np.abs(arr_x2 - arr_x1) / 3

    arrs_size = 1.0
    head_size = 7.5
    ax.annotate('', xy=(arr_x1, arr_y), xytext=(arr_x2 - arr_xshorten, arr_y),
                         arrowprops=dict(width=arrs_size, headwidth=head_size, headlength=head_size, color='C0'),zorder=1000)
    ax.annotate('', xy=(arr_x2, arr_y), xytext=(arr_x1 + arr_xshorten, arr_y),
                         arrowprops=dict(width=arrs_size, headwidth=head_size, headlength=head_size, color='C0'),zorder=1000)
    ax.text(x_mid, txty, r'\textbf{%.1f km}' % len_surf_km, fontsize=plt.rcParams['font.size'], ha='center', va='bottom', color='C0', fontweight='bold',
            bbox=dict(facecolor='white', alpha=1.0, boxstyle='round,pad=0.1,rounding_size=0.3', lw=0))

    # add surface length based on what was determined by the confidence threshold here
    try:
        with h5py.File(fn, 'r+') as f:
            if 'len_surf_km' in f['properties'].keys():
                del f['properties/len_surf_km']
            dset = f.create_dataset('properties/len_surf_km', data=len_surf_km)
    except:
        print('WARNING: Surface length could not be written to the associated lake file!')
        traceback.print_exc()

    # add the max depth
    y_low = np.min(bed_only)
    y_up = lk.surface_elevation
    # arr_x = xatc_bed[np.argmin(bed_only)]
    arr_x = np.mean((xlm[0], np.min(xatc_bed)))
    txt_x = arr_x-0.01*(xlm[1]-xlm[0])
    # arr_x = xlm[0] - 0.0* (xlm[1] - xlm[0])
    y_len = y_up - y_low
    y_mid = (y_up + y_low) / 2
    arr_len = y_len
    arr_y1 = y_mid + arr_len / 2
    arr_y2 = y_mid - arr_len / 2
    arr_yshorten = np.abs(arr_y2 - arr_y1) / 3
    ref_index = 1.336
    dep_round = np.round(y_len / ref_index, 1)
    ax.annotate('', xy=(arr_x, arr_y2), xytext=(arr_x, arr_y1 - arr_yshorten),
                         arrowprops=dict(width=arrs_size, headwidth=head_size, headlength=head_size, color='r'),zorder=1000)
    ax.annotate('', xy=(arr_x, arr_y1), xytext=(arr_x, arr_y2 + arr_yshorten),
                         arrowprops=dict(width=arrs_size, headwidth=head_size, headlength=head_size, color='r'),zorder=1000)
    ax.text(txt_x, y_mid, r'\textbf{%.1f m}' % dep_round, fontsize=plt.rcParams['font.size'], ha='right', va='center', color='r', fontweight='bold',
            bbox=dict(facecolor='white', alpha=1.0, lw=0, boxstyle='round,pad=0.03,rounding_size=0.3'), rotation=90)

    # change the maximum depth to what was determined by the confidence threshold here
    try:
        with h5py.File(fn, 'r+') as f:
            if 'max_depth' in f['properties'].keys():
                del f['properties/max_depth']
            dset = f.create_dataset('properties/max_depth', data= y_len/ref_index)
    except:
        print('WARNING: Maximum depth could not be written to the associated lake file!')
        traceback.print_exc()

    # add the title
    datestr = datetime.strftime(datetime.strptime(lk.date_time[:10],'%Y-%m-%d'), '%d %B %Y')
    if True:
        sheet = lk.ice_sheet
        region = lk.polygon_filename.split('_')[-1].replace('.geojson', '')
        if sheet == 'AIS':
            region = region + ' (%s)' % lk.polygon_filename.split('_')[-2]
        latstr = lk.lat_str[:-2] + '°' + lk.lat_str[-1]
        lonstr = lk.lon_str[:-2] + '°' + lk.lon_str[-1]
        name = '(%s, %s), %d m.a.s.l.' % (latstr, lonstr, np.round(lk.surface_elevation))

    # ax.set_xlim(xlm)
    # ax.set_xlim(df.xatc.min(), dfd.xatc.max())
    ax.set_xlim(xlm)
    ax.set_ylim(ylm)
    # ax.text(0.5, 0.87, '%s' % name, fontsize=plt.rcParams['font.size'], ha='center', va='top', transform=ax.transAxes,
    #        bbox=dict(facecolor='white', alpha=0.9, boxstyle='round,pad=0.2,rounding_size=0.5', lw=0), fontweight='bold')
    # ax.text(0.5, 0.89, r'\textbf{%s}' % datestr, fontsize=plt.rcParams['font.size']+2, ha='center', va='bottom', transform=ax.transAxes,
    #        bbox=dict(facecolor='white', alpha=0.9, boxstyle='round,pad=0.2,rounding_size=0.5', lw=0))
    # print(name, datestr, 'quality:', lk.lake_quality)
    ax.text(0.998, 0.003, r'quality: \textbf{%.1f}' % lk.lake_quality, fontsize=plt.rcParams['font.size']-2, ha='right', va='bottom', transform=ax.transAxes,
           bbox=dict(facecolor='white', alpha=1.0, boxstyle='round,pad=0.2,rounding_size=0.3', lw=0))
    ax.axis('off')

    return xlm, ylm

    
#####################################################################
def plot_IS2_imagery(fn, axes=None, xlm=[None,None], ylm=[None,None], cmap=None, days_buffer=5, max_cloud_prob=40, 
                     gamma_value=1.3, imagery_filename=None, re_download=True, img_aspect=3/2, name='ICESat-2 data',
                     return_fig=False, imagery_shift_days=0.0, increase_linewidth=1, increase_gtwidth=1, buffer_factor=1.2,
                     stretch_color=True, rasterize_scatter=False, min_conf=0.1, source='sentinel'):

    if not axes:
        fig = plt.figure(figsize=[12,6], dpi=80)
        gs = fig.add_gridspec(1,3)
        axp = [fig.add_subplot(gs[0, 0]), fig.add_subplot(gs[0, 1:])]
    else:
        axp = axes
        
    ax = axp[1]
    xlim, ylim = plotIS2(fn=fn, ax=ax, xlm=xlm, ylm=ylm, cmap=cmap, name=name, increase_linewidth=increase_linewidth, rasterize_scatter=rasterize_scatter,
            min_conf=min_conf)
    
    ax = axp[0]
    img, center_lon, center_lat = plot_imagery(fn=fn, days_buffer=days_buffer, max_cloud_prob=max_cloud_prob, xlm=xlm, ylm=ylm, 
        gamma_value=gamma_value, imagery_filename=imagery_filename, re_download=re_download, ax=ax, imagery_shift_days=imagery_shift_days,
        increase_gtwidth=increase_gtwidth, buffer_factor=buffer_factor, stretch_color=stretch_color, min_conf=min_conf, source=source)
        
    if img:        
        if imagery_filename:
            if 'modis' in imagery_filename:
                center_x, center_y = warp.transform(src_crs='epsg:4326', dst_crs=img.crs, xs=[center_lon], ys=[center_lat])
                center_x = center_x[0]
                center_y = center_y[0]
                rng = 40000
                if img_aspect > 1:
                    ax.set_xlim(center_x - 0.5*rng/img_aspect, center_x + 0.5*rng/img_aspect)
                    ax.set_ylim(center_y - 0.5*rng, center_y + 0.5*rng)
                if img_aspect < 1:
                    ax.set_xlim(center_x - 0.5*rng, center_x + 0.5*rng)
                    ax.set_ylim(center_y - 0.5*rng*img_aspect, center_y + 0.5*rng*img_aspect)
                
        elif (img_aspect > 1): 
            h_rng = img.bounds.top - img.bounds.bottom
            cntr = (img.bounds.right + img.bounds.left) / 2
            ax.set_xlim(cntr-0.5*h_rng/img_aspect, cntr+0.5*h_rng/img_aspect)
        elif img_aspect < 1: 
            w_rng = img.bounds.right - img.bounds.left
            cntr = (img.bounds.top + img.bounds.bottom) / 2
            ax.set_ylim(cntr-0.5*w_rng*img_aspect, cntr+0.5*w_rng/img_aspect)
            
    
    if not axes:
        fig.tight_layout(pad=1, h_pad=0, w_pad=0)
        if not name:
            name = 'zzz' + lk.polygon_filename.split('_')[-1].replace('.geojson', '')
        outname = 'figplots/' + name.replace(' ', '') + fn[fn.rfind('/')+1:].replace('.h5','.jpg')
        fig.savefig(outname, dpi=300)

    if return_fig:
        plt.close(fig)
        return center_lon, center_lat, fig
    else:
        return center_lon, center_lat

#####################################################################
def plot_coords(coords, ax, crs_dst, crs_src='EPSG:4326', text=None, color='b', ms=10, fs=18, annot_loc={}, alpha=1.0, textcolor='white'):
    coords_trans = warp.transform(src_crs=crs_src, dst_crs=crs_dst, xs=[coords[0]], ys=[coords[1]])
    x = coords_trans[0][0]
    y = coords_trans[1][0]
    if text:
        text = r'\textbf{%s}' % text
    if not text:
        ax.scatter(x, y, coords_trans[1][0], s=ms, color=color)
    elif ('x' not in annot_loc.keys()) and ('y' not in annot_loc.keys()):
        ax.text(x, y, text, fontsize=fs, color='white', ha='center', va='center', fontweight='bold',
                bbox=dict(facecolor=color, alpha=1, boxstyle='round,pad=0.3,rounding_size=0.5', lw=0))
    else:
        ax.annotate(' ', xy=(x,y), xytext=(annot_loc['x'], annot_loc['y']),
                    ha='center',va='center', arrowprops=dict(width=1, headwidth=5, headlength=5, color=color),zorder=1000)
        ax.text(annot_loc['x'], annot_loc['y'], text, fontsize=fs, color=textcolor, ha='center', va='center',
                bbox=dict(facecolor=color, boxstyle='round,pad=0.3,rounding_size=0.5', lw=0, alpha=alpha), zorder=2000, fontweight='bold')
        
def add_letter(ax, text, fs=16, col='b', alpha=1):
    ax.text(0.09,0.95,r'\textbf{%s}'%text,color='w',fontsize=fs,ha='left', va='top', fontweight='bold',
              bbox=dict(fc=col, boxstyle='round,pad=0.3,rounding_size=0.5', lw=0, alpha=alpha), transform=ax.transAxes)

def print_lake_info(fn, description='', print_imagery_info=True):
    lk = dictobj(read_melt_lake_h5(fn))
    keys = vars(lk).keys()
    print('\nLAKE INFO: %s' % description)
    print('  granule_id:            %s' % lk.granule_id)
    print('  RGT:                   %s' % lk.rgt)
    print('  GTX:                   %s' % lk.gtx.upper())
    print('  beam:                  %s (%s)' % (lk.beam_number, lk.beam_strength))
    print('  acquisition time:      %s' % lk.time_utc)
    print('  center location:       (%s, %s)' % (lk.lon_str, lk.lat_str))
    print('  ice sheet:             %s' % lk.ice_sheet)
    print('  melt season:           %s' % lk.melt_season)
    print('  SuRRF lake quality:    %.2f' % lk.lake_quality)
    print('  surface_elevation:     %.2f m' % lk.surface_elevation)
    print('  maximum water depth:   %.2f m' % lk.max_depth)
    print('  water surface length:  %.2f km' % lk.len_surf_km)
    
    if ('imagery_info' in keys) and (print_imagery_info):
        print('  IMAGERY INFO:')
        print('    product ID:                     %s' % lk.imagery_info['product_id'])
        print('    acquisition time imagery:       %s' % lk.imagery_info['time_imagery'])
        print('    acquisition time ICESat-2:      %s' % lk.imagery_info['time_icesat2'])
        print('    time difference from ICESat-2:  %s (%s)' % (lk.imagery_info['time_diff_from_icesat2'],lk.imagery_info['time_diff_string']))
        print('    mean cloud probability:         %.1f %%' % lk.imagery_info['mean_cloud_probability'])
    print('')


# labs_locs = gdf_gre_full.buffer(100000).simplify(40000).exterior
def chaikin_smooth(line, refinements=5):
    for _ in range(refinements):
        new_points = []
        for i in range(len(line.coords) - 1):
            p1 = np.array(line.coords[i])
            p2 = np.array(line.coords[i + 1])
            new_points.append((0.75 * p1 + 0.25 * p2))
            new_points.append((0.25 * p1 + 0.75 * p2))
        new_points.append(new_points[0])  # Close the ring
        line = shapely.geometry.LinearRing(new_points)
    return line

# Function to find the closest point on the LinearRing to a given point
def find_closest_point(pt_loc, labs_locs):
    # Use shapely's nearest_points to find the nearest point on the line to pt_loc
    nearest = nearest_points(pt_loc, labs_locs)
    nearest_pt = nearest[1]
    
    closest_point_dict = {'x': nearest_pt.x, 'y': nearest_pt.y}
    return closest_point_dict

# Function to sort points in clockwise order along the LinearRing
def sort_points_clockwise(points, labs_locs, start_labsort):
    # Convert start_labsort to a Point object
    start_point = Point(start_labsort['x'], start_labsort['y'])
    
    # Find the closest point on the LinearRing to the start point
    closest_start = find_closest_point(start_point, labs_locs)
    closest_start_point = Point(closest_start['x'], closest_start['y'])
    
    # Project each point onto the LinearRing and calculate its distance along the ring
    ring = labs_locs  # Use the LinearRing directly
    distances = []
    
    for i, pt in enumerate(points):
        point = Point(pt['x'], pt['y'])
        closest_pt_on_ring = nearest_points(point, ring)[1]
        distance = ring.project(closest_pt_on_ring) - ring.project(closest_start_point)
        
        # Normalize the distance to always be positive and within the ring length
        if distance < 0:
            distance += ring.length
            
        distances.append((i, distance))
    
    # Sort the distances and return the sorted indices
    sorted_indices = [i for i, dist in sorted(distances, key=lambda x: x[1])]
    return sorted_indices

#####################################################################
class HandlerLinesVertical(HandlerTuple):
    def create_artists(self, legend, orig_handle,
                   xdescent, ydescent, width, height, fontsize,
                   trans):
        ndivide = len(orig_handle)
        ydescent = height/float(ndivide+1)
        a_list = []
        for i, handle in enumerate(orig_handle):
            # y = -height/2 + (height / float(ndivide)) * i -ydescent
            y = -(height/2+ydescent/2) + 2*i*ydescent
            line = plt.Line2D(np.array([0,1])*width, [-y,-y])
            line.update_from(handle)
            # line.set_marker(None)
            point = plt.Line2D(np.array([.5])*width, [-y])
            point.update_from(handle)
            for artist in [line, point]:
                artist.set_transform(trans)
            a_list.extend([line,point])
        return a_list
                       
#####################################################################
def getstats_comparison(dfsel, stat, verb=False):
    dfsel = dfsel.reset_index(drop=True)
    diffs = (dfsel.manual - dfsel[stat])
    diffs = diffs[~np.isnan(diffs)]
    bias = np.mean(diffs)
    std = np.std(diffs)
    mae = np.mean(np.abs(diffs))
    rmse = np.sqrt(np.mean(diffs**2))
    sel = (~np.isnan(dfsel[stat])) & (~np.isnan(dfsel.manual))
    correl = pearsonr(dfsel.manual[sel], dfsel.loc[sel, stat]).statistic
    percent = np.round((dfsel.loc[sel, stat].sum() / dfsel.manual[sel].sum() - 1) * 100, 1)
    if verb:
        print('- mean diff:', bias)
        print('- std diff:', std)
        print('- MAE:', mae)
        print('- RMSE:', rmse)
        print('- correl:', correl)
        
    return pd.DataFrame({'bias': bias, 'std': std, 'mae': mae, 'rmse': rmse, 'R': correl, 'percent': percent}, index=[stat])


#####################################################################
def get_stats_string_latex(statsdf, estimate):
    stats = statsdf.loc[estimate]
    thissign = '+' if stats.percent > 0 else '-'
    vals = (stats.mae, stats.R, stats.bias, thissign, np.abs(stats.percent))
    add_stats = 'MAE: $%.2f\\mathrm{\\,m}$, $r$: $%.2f$, bias: $%.2f\\mathrm{\\,m}$ ($%s%.0f\\,\\%%$)' % vals
    return add_stats


#####################################################################
def compile_IS2_comparison_data():
    fn_fricker = 'data/is2comp/raw/data_fricker_2021_surrfcorrected.csv'
    fn_melling = 'data/is2comp/raw/data_melling_2024_surrfcorrected.csv'
    fn_predict = 'data/is2comp/raw/predicted_depths_7d_ensemble_estimates.csv'
    df_f = pd.read_csv(fn_fricker)
    df_m = pd.read_csv(fn_melling)
    df_p = pd.read_csv(fn_predict)
    
    df_f['id_lake'] = df_f.lake_id.apply(lambda x: 'lake_amery_fricker_%i' % x)
    df_m['id_lake'] = df_m.lake_id.apply(lambda x: 'lake_greenland_melling_%i' % x)
    
    def interp_preds(thisid, df_):
        dfp_ = df_p[df_p.id_lake == thisid].copy().sort_values(by='lat')
        dfl_ = df_[df_.id_lake == thisid].copy()
        return np.interp(dfl_.lat, dfp_.lat, dfp_.predicted_depth)
    
    for thisid in df_f.id_lake.unique():
        df_f.loc[df_f.id_lake == thisid, 'predicted_depth'] = interp_preds(thisid, df_f)
    for thisid in df_m.id_lake.unique():
        df_m.loc[df_m.id_lake == thisid, 'predicted_depth'] = interp_preds(thisid, df_m)
    
    add_f = set(list(df_m)) - set(list(df_f))
    add_m = set(list(df_f)) - set(list(df_m))
    common = set(list(df_f) + list(df_m)) - add_f - add_m
    add_f = list(add_f)
    add_m = list(add_m)
    add_f.sort()
    add_m.sort()
    df_m[add_m] = np.nan
    df_f[add_f] = np.nan
    common = ['id_lake', 'lon', 'lat', 'dist_along_track_m', 'manual', 'predicted_depth', 'surrf_2024', 'surrf_corr', 'surrf_corr_conf']
    keys = common + add_m + add_f
    df_m = df_m[keys]
    df_f = df_f[keys]
    df_all = pd.concat((df_m, df_f)).reset_index(drop=True)
    df_all.loc[df_all.manual.isna(), 'manual'] = 0.0
    df_all.to_csv('data/is2comp/comparison_melling_fricker.csv', index=False)

    
#####################################################################
def get_xylims_aspect(ax, img, fig):
    axbbx = ax.get_window_extent().transformed(fig.dpi_scale_trans.inverted())
    axis_aspect = axbbx.height / axbbx.width
    img_wid = img.bounds.right - img.bounds.left
    img_centerx = (img.bounds.right + img.bounds.left) / 2
    img_centery = (img.bounds.top + img.bounds.bottom) / 2
    img_hgt = img.bounds.top - img.bounds.bottom
    img_aspect = img_hgt / img_wid
    
    if axis_aspect > img_aspect:
        yl = (img.bounds.bottom, img.bounds.top)
        xl = (img_centerx - img_hgt/axis_aspect/2, img_centerx + img_hgt/axis_aspect/2)
    else:
        xl = (img.bounds.left, img.bounds.right)
        yl = (img_centery - img_wid*axis_aspect/2, img_centery + img_wid*axis_aspect/2)
    return xl, yl


#####################################################################
def get_rotated_ground_track_image(lakeid, df_data, axis_aspect=0.2, buffer_image=0.2, scale=5, gamma_value=1.0, 
                                   output_file='auto', plot=False):
    if output_file == 'auto': 
        output_file = '%s.tiff' % lakeid
    
    img_path = 'projects/ee-philipparndt/assets/%s_ensemble_depth_estimates' % lakeid
    image = ee.Image(img_path)
    this_crs = image.select('b2').projection().crs().getInfo()
    
    thisdf = df_data[df_data.id_lake==lakeid].copy().sort_values(by='dist_along_track_m').reset_index(drop=True)
    gdf = gpd.GeoDataFrame(thisdf, geometry=gpd.points_from_xy(thisdf.lon, thisdf.lat), crs="EPSG:4326")
    gdf = gdf.to_crs(this_crs)
    gdf[['x', 'y']] = gdf.geometry.get_coordinates()
    xoff = np.nanmin(thisdf['dist_along_track_m'])
    gdf.dist_along_track_m -= xoff
    gdf['xatc'] = gdf.dist_along_track_m / 1000
    
    lon0, lat0 = gdf.lon.iloc[0], gdf.lat.iloc[0]
    lon1, lat1 = gdf.lon.iloc[-1], gdf.lat.iloc[-1]
    loncenter = (lon0 + lon1) / 2
    latcenter = (lat0 + lat1) / 2

    crs_local = pyproj.CRS("+proj=stere +lat_0={0} +lon_0={1} +datum=WGS84 +units=m".format(latcenter, loncenter))
    coordsloc = gdf.to_crs(crs_local).get_coordinates()
    dy = coordsloc.y.iloc[-1] - coordsloc.y.iloc[0]
    dx = coordsloc.x.iloc[-1] - coordsloc.x.iloc[0]
    angle_deg = math.degrees(math.atan2(dy, dx))
    
    wkt_crs = '''
    PROJCS["Hotine_Oblique_Mercator_Azimuth_Center",
    GEOGCS["GCS_WGS_1984",
    DATUM["D_unknown",
    SPHEROID["WGS84",6378137,298.257223563]],
    PRIMEM["Greenwich",0],
    UNIT["Degree",0.017453292519943295]],
    PROJECTION["Hotine_Oblique_Mercator_Azimuth_Center"],
    PARAMETER["latitude_of_center",%s],
    PARAMETER["longitude_of_center",%s],
    PARAMETER["rectified_grid_angle",%s],
    PARAMETER["scale_factor",1],
    PARAMETER["false_easting",0],
    PARAMETER["false_northing",0],
    UNIT["km",1000.0], 
    AUTHORITY["EPSG","8011112"]]''' % (lat0, lon0, angle_deg)

    # get the region of interest from ground track and aspect ratio
    buffer_img_aoi = (gdf.dist_along_track_m.max()-gdf.dist_along_track_m.min()) * axis_aspect / 2 * (1+buffer_image)
    region = ee.Geometry.LineString([[lon0, lat0], [lon1, lat1]]).buffer(buffer_img_aoi)
    
    # stretch the color values 
    def color_stretch(image):
        percentiles = image.select(['b4', 'b3', 'b2']).reduceRegion(**{
        # percentiles = image.select(['b3']).reduceRegion(**{
            'reducer': ee.Reducer.percentile(**{'percentiles': [1, 99], 'outputNames': ['lower', 'upper']}),
            'geometry': region,
            'scale': 10,
            'maxPixels': 1e9,
            'bestEffort': True
        })
        lower = percentiles.select(['.*_lower']).values().reduce(ee.Reducer.min())
        upper = percentiles.select(['.*_upper']).values().reduce(ee.Reducer.max())
        return image.select(['b4', 'b3', 'b2']).unitScale(lower, upper).clamp(0,1).resample('bilinear').reproject(**{'crs': wkt_crs,'scale': scale})

    # stretch color, apply gamma correction, and convert to 8-bit RGB
    rgb_gamma = color_stretch(image).pow(1/gamma_value)
    rgb8bit = rgb_gamma.clamp(0,1).multiply(255).uint8()
    
    # get the download URL and download the selected image
    success = False
    tries = 0
    while (success == False) & (tries <= 10):
        try:
            # Get the download URL
            url = rgb8bit.getDownloadURL({
                'scale': scale,
                'crs': wkt_crs,
                'region': region,
                'format': 'GEO_TIFF',
                'filePerBand': False
            })
                
            # Download the image
            response = requests.get(url)
            with open(output_file, 'wb') as f:
                f.write(response.content)
            if os.path.isfile(output_file):
                success = True
            tries += 1
        except:
            print('-> download unsuccessful, increasing scale to %.1f...' % scale)
            traceback.print_exc()
            scale *= 2
            success = False
            tries += 1

    if plot:
        fig,ax = plt.subplots(figsize=[8,2.5])
        with rio.open(output_file) as src:
            rioplot.show(src, ax=ax)
        ax.axhline(0, color='k')
        ax.set_title(lakeid)
        fig.tight_layout()
        plt.close(fig)
        display(fig)


#####################################################################
class ImageHandler(HandlerBase):
    def create_artists(self, legend, orig_handle, xdescent, ydescent, width, height, fontsize, trans):
        sx, sy = self.image_stretch 
        bb = Bbox.from_bounds(xdescent - sx/2, ydescent - sy/2, width + sx, height + sy)
        tbb = TransformedBbox(bb, trans)
        image = BboxImage(tbb)
        image.set_data(self.image_data)
        self.update_prop(image, orig_handle, legend)
        return [image]

    def set_image(self, image_path, image_stretch=(0, 0)):
        if not os.path.exists(image_path):
            url = 'https://storage.googleapis.com/replit/images/1608749573246_3ecaeb5cdbf14cd5f1ad8c48673dd7ce.png'
            self.image_data = np.array(PIL.Image.open(urllib.request.urlopen(url)))
        else:
            self.image_data = plt.imread(image_path)
        self.image_stretch = image_stretch


#####################################################################
def make_artist_image(filename, cmap, nx=300, ny=100, lw=20):
    xart = np.tile(np.linspace(0,1,nx), (ny, 1))
    fig, ax = plt.subplots(figsize=[2,1])
    ax.axis('off')
    ax.set_facecolor('none')
    ax.imshow(xart, cmap=cmap)
    ax.plot([0,nx], [0,0], lw=lw, color='k')
    ax.plot([0,nx], [ny,ny], lw=lw, color='k')
    ax.set_ylim((0,ny))
    ax.set_xlim((0,nx))
    fig.tight_layout(pad=0)
    fig.savefig(filename, dpi=100, bbox_inches='tight', pad_inches=0.0, transparent=True)
    plt.close(fig)

#####################################################################
def brighten_hex_color(hex_color, alpha):
    rgb_color = matplotlib.colors.hex2color(hex_color)
    white = np.array([1, 1, 1])
    blended_color = (1 - alpha) * white + alpha * np.array(rgb_color)
    brightened_hex = matplotlib.colors.to_hex(blended_color)
    return brightened_hex