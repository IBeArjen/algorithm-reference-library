# Tim Cornwell <realtimcornwell@gmail.com>
#
# Definition of the function interface. Although the data structures are classes,
# we use stateless functions.
#
from functions.fvistable import *
from functions.fconfiguration import *
from functions.fcomponent import *
from functions.fimage import *
from functions.fskymodel import *
from functions.fpolarisation import *
from functions.fgaintable import *

def fsimulate(config: fconfiguration, sm: fskymodel, times: numpy.array, params: dict) -> fvistable:
    """ Simulate an observation from a configuration and a skymodel, over hour angle range
    """
    return fvistable()

def finvert(vis: fvistable, template: fimage, params: dict) -> (fimage, fimage):
    """ Invert to make dirty image and PSF
    """
    return (fimage(template), fimage(template))

def fpredict(vis: fvistable, sm: fskymodel, params: dict) -> fvistable:
    """ Predict the visibility from a skymodel
    """
    return vis

def fcalibrate(vis: fvistable, sm: fskymodel, params: dict) -> fgaintable:
    """ Selfcalibrate using a sky model
    """
    return fgaintable()

def fminorcycle(dirty: fimage, psf: fimage, window: fimage, params: dict) -> fimage:
    """ Perform minor cycles
    """

def fmajorcycle(vis: fvistable, sm: fskymodel, minorcycle: fminorcycle, params: dict) -> fvistable:
    """ Perform major cycles
    """
    return vis
