# Tim Cornwell <realtimcornwell@gmail.com>
#
"""
Functions that aid fourier transform processing. These are built on top of the core
functions in arl.fourier_transforms.
"""

import pymp

from scipy.interpolate import griddata

from astropy import units as units
from astropy import wcs
from astropy.constants import c
from astropy.wcs.utils import pixel_to_skycoord

from arl.data.data_models import *
from arl.fourier_transforms.convolutional_gridding import fixed_kernel_grid, \
    fixed_kernel_degrid, weight_gridding, w_beam, anti_aliasing_calculate
from arl.fourier_transforms.fft_support import fft, ifft, pad_mid, extract_mid
from arl.fourier_transforms.variable_kernels import variable_kernel_grid, variable_kernel_degrid, \
    standard_kernel_lambda, w_kernel_lambda
from arl.image.iterators import *
from arl.image.operations import reproject_image, export_image_to_fits
from arl.util.coordinate_support import simulate_point, skycoord_to_lmn
from arl.visibility.iterators import *
from arl.visibility.operations import phaserotate_visibility

log = logging.getLogger("fourier_transforms.ftprocessor")


def shift_vis_to_image(vis, im, params):
    """Shift visibility to the FFT phase centre of the image
    
    """
    nchan, npol, ny, nx = im.data.shape
    # Convert the FFT definition of the phase center to world coordinates (0 relative)
    image_phasecentre = pixel_to_skycoord(ny // 2, nx // 2, im.wcs)
    params['tangent'] = True
    log.debug("shift_vis_from_image: shifting phasecentre from vis phase centre %s to image phasecentre %s" %
              (vis.phasecentre, image_phasecentre))
    vis = phaserotate_visibility(vis, image_phasecentre, params)
    vis.phasecentre = im.phasecentre
    return vis


def shift_vis_from_image(vis, im, params):
    """Shift visibility from the FFT phase centre of the image to the visibility phase centre

    """
    nchan, npol, ny, nx = im.data.shape
    # Convert the FFT definition of the phase center to world coordinates (0 relative)
    image_phasecentre = pixel_to_skycoord(ny // 2, nx // 2, im.wcs)
    params['tangent'] = True
    vis_phasecentre = vis.phasecentre
    vis.phasecentre = image_phasecentre
    log.debug("shift_vis_from_image: shifting phasecentre from image phase centre %s to vis phasecentre %s" %
              (image_phasecentre, vis_phasecentre))
    vis = phaserotate_visibility(vis, vis_phasecentre, params)
    vis.phasecentre = vis_phasecentre
    return vis


def get_2d_params(vis, model, params=None):
    """ Common interface to params for 2d predict and invert
    
    :param vis: Visibility data
    :param model: Image model used to determine sampling
    :param params: Processing parameters
    :returns: nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, fov
    """
    padding = get_parameter(params, "padding", 2)
    kernelname = get_parameter(params, "kernel", "calculate")
    oversampling = get_parameter(params, "oversampling", 8)
    support = get_parameter(params, "support", 3)
    nchan, npol, ny, nx = model.data.shape
    shape = (padding * ny, padding * nx)
    cellsize = model.wcs.wcs.cdelt[0:2] * numpy.pi / 180.0
    fov = padding * nx * numpy.max(numpy.abs(cellsize))
    uvscale = numpy.outer(cellsize, vis.frequency / c.value)
    assert uvscale[0,0] != 0.0, "Error in uv scaling"

    kernel_type = 'fixed'
    gcf = 1.0
    if kernelname == 'standard-by-row':
        kernel_type = 'variable'
        log.info("get_2d_params: using calculated spheroidal function by row")
        kernel = standard_kernel_lambda(vis, shape)
    elif kernelname == 'wprojection':
        wmax = numpy.max(numpy.abs(vis.w))
        assert wmax > 0, "Maximum w must be > 0.0"
        kernel_type = 'variable'
        r_f = (fov/2) ** 2 / numpy.abs(cellsize[0])
        log.info("get_2d_params: Fresnel number = %f" % (r_f))
        delA = get_parameter(params, 'wloss', 0.02)
        # Following equation is from Cornwell, Humphreys, and Voronkov (2012) (equation 24)
        recommended_wstep = numpy.sqrt(2.0 * delA) / (numpy.pi * fov ** 2)
        log.info("get_2d_params: Recommended wstep = %f" % (recommended_wstep))
        wstep = get_parameter(params, "wstep", recommended_wstep)
        log.info("get_2d_params: Using w projection with wstep = %f" % (wstep))
        # Now calculate the maximum support for the w kernel
        npixel_kernel = get_parameter(params, "kernelwidth", (int(round(numpy.sin(0.5 * fov) * nx)) // 2))
        log.info("get_2d_params: w kernel full width = %d pixels" % (npixel_kernel))
        kernel, _ = w_kernel_lambda(vis, shape, fov, wstep=wstep, npixel_kernel=npixel_kernel,
                                    oversampling=oversampling)
        gcf, _ = anti_aliasing_calculate(shape, oversampling)
    else:
        log.info("get_2d_params: using calculated spheroidal function")
        gcf, kernel = anti_aliasing_calculate(shape, oversampling)
    
    return nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, fov, uvscale


def predict_2d_base(vis, model, params=None):
    """ Predict using convolutional degridding.

    This is at the bottom of the layering i.e. all transforms are eventually expressed in terms of
    this function. Any shifting needed is performed here.

    :param vis: Visibility to be predicted
    :param model: model image
    :param params: Parameters for processing
    :param predict_function: Function to be used for prediction (allows nesting)
    :returns: resulting visibility (in place works)
    """
    nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, \
    fov, uvscale = get_2d_params(vis, model, params)
    
    uvgrid = fft((pad_mid(model.data, padding * nx) * gcf).astype(dtype=complex))
    # uvw is in metres, v.frequency / c.value converts to wavelengths, the cellsize converts to phase
    uvw = vis.data['uvw']
    
    result = copy.copy(vis)
    
    if kernel_type == 'variable':
        result.data['vis'] = variable_kernel_degrid(kernel, uvgrid, uvw, uvscale)
    else:
        result.data['vis'] = fixed_kernel_degrid(kernel, uvgrid, uvw, uvscale)
        
    vis.data['vis'] = numpy.conj(shift_vis_to_image(result, model, params).data['vis'])

    return vis

def predict_2d(vis, model, params=None):
    """ Predict using convolutional degridding and w projection
    :param vis: Visibility to be predicted
    :param model: model image
    :param params: Parameters for processing
    :param predict_function: Function to be used for prediction (allows nesting)
    :returns: resulting visibility (in place works)
    """
    params['kernel']='2d'
    return predict_2d_base(vis, model, params=params)


def predict_wprojection(vis, model, params=None):
    """ Predict using convolutional degridding and w projection
    :param vis: Visibility to be predicted
    :param model: model image
    :param params: Parameters for processing
    :param predict_function: Function to be used for prediction (allows nesting)
    :returns: resulting visibility (in place works)
    """
    params['kernel']='wprojection'
    return predict_2d_base(vis, model, params=params)


def invert_2d_base(vis, im, dopsf=False, params=None):
    """ Invert using 2D convolution function, including w projection
    
    Use the image im as a template. Do PSF in a separate call.

    This is at the bottom of the layering i.e. all transforms are eventually expressed in terms
    of this function. . Any shifting needed is performed here.
    
    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image
    
    """
    
    svis = shift_vis_to_image(vis, im, params)
    
    nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, \
    fov, uvscale = get_2d_params(vis, im, params)
    
    # uvw is in metres, v.frequency / c.value converts to wavelengths, the cellsize converts to phase
    # Optionally pad to control aliasing
    imgridpad = numpy.zeros([nchan, npol, padding * ny, padding * nx], dtype='complex')
    uvw = vis.data['uvw']
    if kernel_type == 'variable':
        if dopsf:
            weights = numpy.ones_like(svis.data['vis'])
            imgridpad = variable_kernel_grid(kernel, imgridpad, uvw, uvscale, weights,
                                             svis.data['imaging_weight'])
        else:
            imgridpad = variable_kernel_grid(kernel, imgridpad, uvw, uvscale, svis.data['vis'],
                                             svis.data['imaging_weight'])
    else:
        if dopsf:
            weights = numpy.ones_like(svis.data['vis'])
            imgridpad = fixed_kernel_grid(kernel, imgridpad, uvw, uvscale, weights,
                                          svis.data['imaging_weight'])
        else:
            imgridpad = fixed_kernel_grid(kernel, imgridpad, uvw, uvscale, svis.data['vis'],
                                          svis.data['imaging_weight'])
    
    imgrid = extract_mid(numpy.real(ifft(imgridpad)) * gcf, npixel=nx)
    
    return create_image_from_array(imgrid, im.wcs)


def invert_2d(vis, im, dopsf=False, params=None):
    """ Predict using 2D convolution function

    Use the image im as a template. Do PSF in a separate call.

    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image

    """
    params['kernel']='2d'
    return invert_2d_base(vis, im, dopsf, params=params)


def invert_wprojection(vis, im, dopsf=False, params=None):
    """ Predict using 2D convolution function, including w projection

    Use the image im as a template. Do PSF in a separate call.

    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image

    """
    params['kernel']='wprojection'
    return invert_2d_base(vis, im, dopsf, params=params)


def fit_uvwplane(vis):
    """ Fit and remove the best fitting plane p u + q v = w

    :param vis: visibility to be fitted
    :returns: direction cosines defining plane
    """
    nvis = len(vis.data)
    before = numpy.std(vis.w)

    su2 = numpy.sum(vis.u * vis.u)
    sv2 = numpy.sum(vis.v * vis.v)
    suv = numpy.sum(vis.u * vis.v)
    suw = numpy.sum(vis.u * vis.w)
    svw = numpy.sum(vis.v * vis.w)
    det = su2 * sv2 - suv ** 2
    p = (sv2 * suw - suv * svw) / det
    q = (su2 * svw - suv * suw) / det
    
    vis.data['uvw'][:, 2] -= p * vis.u + q * vis.v
    after = numpy.max(numpy.std(vis.w))
    log.info('predict_timeslice: Fit to %d rows reduces rms w from %.1f to %.1f wavelengths'
             % (nvis, before, after))
    
    return vis, p, q


def predict_timeslice(vis, model, params=None):
    """ Predict using time slices.

    :param vis: Visibility to be predicted
    :param model: model image
    :param params: Parameters for processing
    :param predict_function: Function to be used for prediction (allows nesting)
    :returns: resulting visibility (in place works)
    """
    nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, \
    fov, uvscale = get_2d_params(vis, model, params)

    vis.data['vis']*=0.0

    for rows in vis_timeslice_iter(vis, params):
        visslice = vis.select_rows(rows)

        visslice, p, q = fit_uvwplane(visslice)

        lnominal, mnominal, ldistorted, mdistorted = lm_distortion(model, -p, -q)

        workimage = create_image_from_array(model.data, model.wcs)

        for chan in range(nchan):
            for pol in range(npol):
                workimage.data[chan, pol, ...] = \
                    griddata((mnominal.flatten(), lnominal.flatten()),
                             values=model.data[chan, pol, ...].flatten(),
                             xi=(mdistorted.flatten(), ldistorted.flatten()),
                             method='cubic',
                             fill_value=0.0).reshape(workimage.data[chan, pol, ...].shape)
        export_image_to_fits(workimage, "workimage_%s.fits" %(numpy.average(visslice.time)))
        # Now we can do the transformation using a 2D transform
        visslice = predict_2d(visslice, workimage, params=params)
        vis.data['vis'][rows] += numpy.conj(visslice.data['vis'])

    return vis


def invert_timeslice(vis, im, dopsf=False, params=None):
    """ Invert using time slices

    Use the image im as a template. Do PSF in a separate call.

    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image

    """
    resultimage = create_image_from_array(im.data, im.wcs)
    resultimage.wcs.wcs.set_pv([])
    resultimage.data = pymp.shared.array(resultimage.data.shape)
    resultimage.data *= 0.0
    
    nproc = get_parameter(params, "nprocessor", 1)
    if nproc > 1:
        # Extract the slices and run invert_timeslice_single on each one in parallel
        nslices = 0
        visslices = []
        for rows in vis_timeslice_iter(vis, params):
            visslice=vis.select_rows(rows)
            nslices += 1
            visslices.append(visslice)
        
        log.debug("invert_timeslice: Processing %d time slices %d-way parallel" % (nslices, nproc))
        with pymp.Parallel(nproc) as p:
            for index in p.range(0, nslices):
                visslice = visslices[index]
                workimage = invert_timeslice_single(visslice, im, dopsf, params=params)
                resultimage.data += workimage.data
    
    else:
        # Do each slice in turn
        for rows in vis_timeslice_iter(vis, params):
            visslice=vis.select_rows(rows)
            workimage = invert_timeslice_single(visslice, im, dopsf, params=params)
            resultimage.data += workimage.data
    
    return resultimage


def lm_distortion(im, a, b):
    ny = im.shape[2]
    nx = im.shape[3]
    cy = im.wcs.wcs.crpix[1]
    cx = im.wcs.wcs.crpix[0]
    dy = im.wcs.wcs.cdelt[1] * numpy.pi / 180.0
    dx = im.wcs.wcs.cdelt[0] * numpy.pi / 180.0
    
    lnominal1d = (numpy.arange(0, nx) - cx) * dx
    mnominal1d = (numpy.arange(0, ny) - cy) * dy
    
    l2d, m2d = numpy.meshgrid(lnominal1d, mnominal1d)
    
    dn2d = numpy.sqrt(1.0 - (l2d * l2d + m2d * m2d)) - 1.0
    
    ldistorted = l2d + a * dn2d
    mdistorted = m2d + b * dn2d
    
    return l2d, m2d, ldistorted, mdistorted


def invert_timeslice_single(visslice, im, dopsf, params):
    """Process single time slice
    
    Extracted for re-use in parallel version
    :param params:
    """
    nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, \
    fov, uvscale = get_2d_params(visslice, im, params)

    visslice, p, q = fit_uvwplane(visslice)
    
    imgridpad = numpy.zeros([nchan, npol, padding * ny, padding * nx], dtype='complex')
    if kernel_type == 'variable':
        if dopsf:
            weights = numpy.ones_like(visslice.data['vis'])
            imgridpad = variable_kernel_grid(kernel, imgridpad, visslice.data['uvw'], uvscale, weights,
                                             visslice.data['imaging_weight'])
        else:
            imgridpad = variable_kernel_grid(kernel, imgridpad, visslice.data['uvw'], uvscale,
                                             visslice.data['vis'], visslice.data['imaging_weight'])
    else:
        if dopsf:
            weights = numpy.ones_like(visslice.data['vis'])
            imgridpad = fixed_kernel_grid(kernel, imgridpad, visslice.data['uvw'], uvscale, weights,
                                          visslice.data['imaging_weight'])
        else:
            imgridpad = fixed_kernel_grid(kernel, imgridpad, visslice.data['uvw'], uvscale,
                                          visslice.data['vis'],
                                          visslice.data['imaging_weight'])
    imgrid = extract_mid(numpy.real(ifft(imgridpad)) * gcf, npixel=nx)
    workimage = create_image_from_array(imgrid, im.wcs)

    lnominal, mnominal, ldistorted, mdistorted = lm_distortion(workimage, -p, -q)

    finalimage = create_image_from_array(im.data, im.wcs)
    
    for chan in range(nchan):
        for pol in range(npol):
            finalimage.data[chan, pol, ...] = \
                griddata((mdistorted.flatten(), ldistorted.flatten()),
                         values=workimage.data[chan, pol, ...].flatten(),
                         method='cubic',
                         xi=(mnominal.flatten(), lnominal.flatten()),
                         fill_value=0.0).reshape(finalimage.data[chan, pol, ...].shape)

    return finalimage


def predict_by_image_partitions(vis, model, image_iterator=raster_iter, predict_function=predict_2d,
                                params=None):
    """ Predict using image partitions, calling specified predict function

    :param vis: Visibility to be predicted
    :param model: model image
    :param image_iterator: Image iterator used to access the image
    :param predict_function: Function to be used for prediction (allows nesting)
    :param params: Parameters for processing
    :returns: resulting visibility (in place works)
    """
    vis.data['vis']*=0.0
    result = copy.deepcopy(vis)
    for dpatch in image_iterator(model, params):
        result = predict_function(result, dpatch, params=params)
        vis.data['vis'] += result.data['vis']
    return vis


def predict_by_vis_partitions(vis, model, vis_iterator, predict_function=predict_2d, params=None):
    """ Predict using vis partitions

    :param vis: Visibility to be predicted
    :param model: model image
    :param vis_iterator: Iterator to use for partitioning
    :param params: Parameters for processing
    :param predict_function: Function to be used for prediction (allows nesting)
    :returns: resulting visibility (in place works)
    """
    for vslice in vis_iterator(vis, params):
        vis.data['vis'] += predict_function(vslice, model, params=params).data['vis']
    return vis


def invert_by_image_partitions(vis, im, image_iterator=raster_iter, dopsf=False, kernel=None,
                               invert_function=invert_2d, params=None):
    """ Predict using image partitions, calling specified predict function

    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param image_iterator: Iterator to use for partitioning
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image
    """
    
    i = 0
    for dpatch in image_iterator(im, params):
        result = invert_function(vis, dpatch, dopsf, params=params)
        # Ensure that we fill in the elements of dpatch instead of creating a new numpy arrray
        dpatch.data[...] = result.data[...]
        assert numpy.max(numpy.abs(dpatch.data)), "Partition image %d appears to be empty" % i
        i += 1
    assert numpy.max(numpy.abs(im.data)), "Output image appears to be empty"
    
    return im


def invert_by_vis_partitions(vis, im, vis_iterator, dopsf=False, kernel=None, invert_function=invert_2d, params=None):
    """ Invert using wslices

    :param vis: Visibility to be inverted
    :param im: image template (not changed)
    :param dopsf: Make the psf instead of the dirty image
    :param params: Parameters for processing
    :returns: resulting image
    
    """
    for visslice in vis_iterator(vis, params):
        result = invert_function(visslice, im, dopsf, invert_function, params)
    
    return result


def predict_skycomponent_visibility(vis: Visibility, sc: Skycomponent, params=None) -> Visibility:
    """Predict the visibility from a Skycomponent, add to existing visibility

    :param params:
    :param vis:
    :param sc:
    :returns: Visibility
    """
    spectral_mode = get_parameter(params, 'spectral_mode', 'channel')
    
    assert_same_chan_pol(vis, sc)
    
    l, m, n = skycoord_to_lmn(sc.direction, vis.phasecentre)
    # The data column has vis:[row,nchan,npol], uvw:[row,3]
    if spectral_mode == 'channel':
        for channel in range(sc.nchan):
            uvw = vis.uvw_lambda(channel)
            phasor = simulate_point(uvw, l, m)
            for pol in range(sc.npol):
                vis.vis[:, channel, pol] += sc.flux[channel, pol] * phasor
    else:
        raise NotImplementedError("mode %s not supported" % spectral_mode)
    
    return vis


def calculate_delta_residual(deltamodel, vis, params):
    """Calculate the delta in residual for a given delta in model
    
    This calculation does not require the original visibilities.
    """
    return deltamodel


def weight_visibility(vis, im, params=None):
    """ Reweight the visibility data using a selected algorithm

    Imaging uses the column "imaging_weight" when imaging. This function sets that column using a
    variety of algorithms
    
    :param vis:
    :param im:
    :param params: Dictionary containing parameters
    :returns: Configuration
    """
    nchan, npol, ny, nx, shape, gcf, kernel_type, kernel, padding, oversampling, support, cellsize, \
    fov, uvscale = get_2d_params(vis, im, params)

    # uvw is in metres, v.frequency / c.value converts to wavelengths, the cellsize converts to phase
    weighting = get_parameter(params, "weighting", "uniform")
    if weighting == 'uniform':
        vis.data['imaging_weight'] = weight_gridding(im.data.shape, vis.data['uvw'], uvscale, vis.data['weight'],
                                                     params)
    elif weighting == 'natural':
        vis.data['imaging_weight'] = vis.data['weight']
    else:
        log.error("Unknown visibility weighting algorithm %s" % weighting)
    
    return vis


def create_wcs_from_visibility(vis, params=None):
    """Make a world coordinate system from params and Visibility

    :param vis:
    :param params: keyword=value parameters
    :returns: WCS
    """
    log.info("create_wcs_from_visibility: Parsing parameters to get definition of WCS")
    imagecentre = get_parameter(params, "imagecentre", vis.phasecentre)
    phasecentre = get_parameter(params, "phasecentre", vis.phasecentre)
    reffrequency = get_parameter(params, "reffrequency", numpy.min(vis.frequency)) * units.Hz
    deffaultbw = vis.frequency[0]
    if len(vis.frequency) > 1:
        deffaultbw = vis.frequency[1] - vis.frequency[0]
    channelwidth = get_parameter(params, "channelwidth", deffaultbw) * units.Hz
    log.info("create_wcs_from_visibility: Defining Image at %s, frequency %s, and bandwidth %s"
             % (imagecentre, reffrequency, channelwidth))
    
    npixel = get_parameter(params, "npixel", 512)
    uvmax = (numpy.abs(vis.data['uvw'][:,0:1]).max() * numpy.max(vis.frequency) / c).value
    log.info("create_wcs_from_visibility: uvmax = %f wavelengths" % uvmax)
    criticalcellsize = 1.0 / (uvmax * 2.0)
    log.info("create_wcs_from_visibility: Critical cellsize = %f radians, %f degrees" % (
        criticalcellsize, criticalcellsize * 180.0 / numpy.pi))
    cellsize = get_parameter(params, "cellsize", 0.5 * criticalcellsize)
    log.info("create_wcs_from_visibility: Cellsize          = %f radians, %f degrees" % (cellsize,
                                                                                         cellsize * 180.0 / numpy.pi))
    if cellsize > criticalcellsize:
        log.info("create_wcs_from_visibility: Resetting cellsize %f radians to criticalcellsize %f radians" % (cellsize, criticalcellsize))
        cellsize = criticalcellsize
    
    npol = get_parameter(params, "npol", vis.data['vis'].shape[2])
    
    # Beware of python indexing order! wcs and the array have opposite ordering
    shape = [len(vis.frequency), npol, npixel, npixel]
    w = wcs.WCS(naxis=4)
    # The negation in the longitude is needed by definition of RA, DEC
    w.wcs.cdelt = [-cellsize * 180.0 / numpy.pi, cellsize * 180.0 / numpy.pi, 1.0, channelwidth.value]
    # The numpy definition of the phase centre of an FFT is n // 2 so that's what we use for
    # the reference pixel but we have to use 1-relative indxing for wcs (arghhh!)
    w.wcs.crpix = [npixel // 2 + 1, npixel // 2 + 1, 1.0, 1.0]
    w.wcs.ctype = ["RA---SIN", "DEC--SIN", 'STOKES', 'FREQ']
    w.wcs.crval = [phasecentre.ra.value, phasecentre.dec.value, 1.0, reffrequency.value]
    w.naxis = 4
    
    w.wcs.radesys = get_parameter(params, 'frame', 'ICRS')
    w.wcs.equinox = get_parameter(params, 'equinox', 2000.0)
    
    return shape, reffrequency, cellsize, w, imagecentre


def create_image_from_visibility(vis, params=None):
    """Make an empty imagefrom params and Visibility

    :param vis:
    :param params: keyword=value parameters
    :returns: WCS
    """
    shape, _, _, w, _ = create_wcs_from_visibility(vis, params=params)
    return create_image_from_array(numpy.zeros(shape), wcs=w)


def create_w_term_image(vis, w=None, params=None):
    """Create an image with a w term phase term in it
    
    """
    if w is None:
        w = numpy.median(numpy.abs(vis.data['uvw'][:, 2]))
        log.info('create_w_term_image: Creating w term image for median w %f' % w)
    
    im = create_image_from_visibility(vis, params)
    cellsize = abs(im.wcs.wcs.cdelt[0]) * numpy.pi / 180.0
    _, _, _, npixel = im.data.shape
    im.data = w_beam(npixel, npixel * cellsize, w=w)
    
    fresnel = w * (0.5 * npixel * cellsize) ** 2
    log.info('create_w_term_image: Fresnel number for median w and this field of view and sampling = '
             '%.2f' % (fresnel))
    
    return im
