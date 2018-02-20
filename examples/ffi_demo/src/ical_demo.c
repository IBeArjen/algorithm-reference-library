#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <math.h>

#include "arlwrap.h"
#include "wrap_support.h"

/*
 * Verifies that:
 * - vt and vtmp are unique in memory
 * - vt and vtmp have equivalent values
 */
int verify_arl_copy(ARLVis *vt, ARLVis *vtmp)
{
	char *vtdata_bytes, *vtmpdata_bytes;
	int ARLVisDataSize;
	int i;

	if (vt == vtmp) {
		fprintf(stderr, "vt == vtmp\n");
		return 1;
	}

	if (!((vt->nvis == vtmp->nvis) && (vt->npol == vtmp->npol))) {
		return 2;
	}

	if (vt->data == vtmp->data) {
		return 3;
	}

	ARLVisDataSize = 80 + (32 * vt->npol * vt->nvis);
	vtdata_bytes = (char*) vt->data;
	vtmpdata_bytes = (char*) vtmp->data;

	for (i=0; i<ARLVisDataSize; i++) {
		if (vtdata_bytes[i] != vtmpdata_bytes[i]) {
			return 4;
		}
	}

	return 0;
}

int main(int argc, char **argv)
{
	int *shape  = malloc(4*sizeof(int));
	int *shape1 = malloc(4*sizeof(int));
	int status;
	int nvis;

	// ICAL section
	int wprojection_planes, i;
	double fstart, fend, fdelta, tstart, tend, tdelta, rmax;
	ARLadvice adv;
	ant_t nb;			//nant and nbases
	long long int *cindex_predict;
	int cindex_nbytes;
	ARLGt *gt;			//GainTable
	// end ICAL section

	double cellsize = 0.0005;
	char config_name[] = "LOWBD2-CORE";
	char pol_frame [] = "stokesI";

	ARLVis *vt;			//Blockvisibility
	ARLVis *vtmodel;
	ARLVis *vtmp;
	ARLVis *vtpredicted;		//Visibility
	ARLVis *vt_predictfunction;	//Blockvisibility
	ARLVis *vt_gt;			//Blockvisibility

	ARLConf *lowconfig;

	Image *gleam_model;		//Image (GLEAM model)
	Image *model;			//Image (a model for CLEAN)
	Image *dirty;			//Image (dirty image by invert_function)
	Image *deconvolved;		//Image (ICAL result)
	Image *residual;		//Image (ICAL result)
	Image *restored;		//Image (ICAL result)

	arl_initialize();

	lowconfig = allocate_arlconf_default(config_name);

	// ICAL section
	lowconfig->polframe = pol_frame;
	lowconfig->rmax = 300.0;
	// Get new nant and nbases w.r.t. to a maximum radius rmax
	helper_get_nbases_rmax(config_name, lowconfig->rmax, &nb);
	lowconfig->nant = nb.nant;
	lowconfig->nbases = nb.nbases;
	// Overwriting default values for a phasecentre
	lowconfig->pc_ra = 30.0;					// Phasecentre RA
	lowconfig->pc_dec = -60.0;					// Phasecentre Dec
	// Setting values for the frequencies and times
	lowconfig->nfreqs = 5; 						// Number of frequencies
	lowconfig->nchanwidth = lowconfig->nfreqs;			// Number of channel bandwidths
	fstart = 0.8e8;							// Starting frequency
	fend = 1.2e8;							// Ending frequency
	fdelta = (fend - fstart)/ (double)(lowconfig->nfreqs - 1);	// Frequency step
	lowconfig->ntimes = 11;						// Number of the times
	tstart = -M_PI/3.0;						// Starting time (in radians)
	tend = M_PI/3.0;						// Ending time (in radians)
	tdelta = (tend - tstart)/(double)(lowconfig->ntimes - 1);	// Time between the snapshots
	// Overwrining defalut frequency list
	free(lowconfig->freqs);
	free(lowconfig->channel_bandwidth);
	lowconfig->freqs = malloc(lowconfig->nfreqs * sizeof(double));
	lowconfig->channel_bandwidth = malloc(lowconfig->nfreqs * sizeof(double));
	printf("Frequency and bandwidth list\n");
	for(i = 0; i < lowconfig->nfreqs; i++) {
		lowconfig->freqs[i] = fstart + (double)i*fdelta;		
		lowconfig->channel_bandwidth[i] = fdelta;
		printf("%d %e %e\n", i,lowconfig->freqs[i], lowconfig->channel_bandwidth[i] );
		}
	// Overwriting default time list
	free(lowconfig->times);
	lowconfig->times = calloc(lowconfig->ntimes, sizeof(double));	
	printf("\nA list of the times (in rad)\n");
	for(i = 0; i < lowconfig->ntimes; i++) {
		lowconfig->times[i] = tstart + (double)i*tdelta;		
		printf("%d %e\n", i,lowconfig->times[i]);
		}
	// end ICAL section

	nvis = (lowconfig->nbases)*(lowconfig->nfreqs)*(lowconfig->ntimes);
	printf("Nvis = %d\n", nvis);
	
//	vt = allocate_vis_data(lowconfig->npol, nvis);
//	vtmp = allocate_vis_data(lowconfig->npol, nvis);
	vt 		   = allocate_blockvis_data(lowconfig->nant, lowconfig->nfreqs, lowconfig->npol, lowconfig->ntimes); //Blockvisibility
	vt_predictfunction = allocate_blockvis_data(lowconfig->nant, lowconfig->nfreqs, lowconfig->npol, lowconfig->ntimes); //Blockvisibility
	vt_gt              = allocate_blockvis_data(lowconfig->nant, lowconfig->nfreqs, lowconfig->npol, lowconfig->ntimes); //Blockvisibility
	vtpredicted        = allocate_vis_data(lowconfig->npol, nvis);							     //Visibility

	// Allocating cindex array where 8*sizeof(char) is sizeof(python int)
	cindex_nbytes = lowconfig->ntimes * lowconfig->nant * lowconfig->nant * lowconfig->nfreqs * sizeof(long long int);
	
	if (!(cindex_predict = malloc(cindex_nbytes))) {
		free(cindex_predict);
		return 1;
	}
	
	// ICAL section	
	// create_blockvisibility()
	printf("Create blockvisibility... ");
	arl_create_blockvisibility(lowconfig, vt);
	printf("Nrec = %d\n", lowconfig->nrec);
	// Allocating gaintable data
	gt = allocate_gt_data(lowconfig->nant, lowconfig->nfreqs, lowconfig->nrec, lowconfig->ntimes);

	// adwise_wide_field()
	adv.guard_band_image = 4.0;
	adv.delA=0.02;
	adv.wprojection_planes = 1;
	printf("Calculating wide field parameters... ");
	arl_advise_wide_field(lowconfig, vt, &adv);
	printf("Done.\n");
	printf("Vis_slices = %d,  npixel = %d, cellsize = %e\n", adv.vis_slices, adv.npixel, adv.cellsize);
	cellsize = adv.cellsize;

	// create_low_test_image_from_gleam
	helper_get_image_shape_multifreq(lowconfig, adv.cellsize, adv.npixel, shape);
	printf("A shape of the modeled GLEAM image: [ %d, %d, %d, %d]\n", shape[0], shape[1], shape[2], shape[3]);
	gleam_model = allocate_image(shape);
	arl_create_low_test_image_from_gleam(lowconfig, adv.cellsize, adv.npixel, vt->phasecentre, gleam_model);

	// FITS file output
	status = mkdir("results", S_IRWXU | S_IRWXG | S_IROTH | S_IXOTH);
	status = export_image_to_fits_c(gleam_model, "!results/gleam_model.fits");

	// predict_function()
	arl_predict_function(lowconfig, vt, gleam_model, vtpredicted, vt_predictfunction, cindex_predict);

	// convert_visibility_to_blockvisibility()
	arl_convert_visibility_to_blockvisibility(lowconfig, vtpredicted, vt_predictfunction, cindex_predict, vt);

	// create_gaintable_from_blockvisibility()
	arl_create_gaintable_from_blockvisibility(lowconfig, vt, gt);

	// simulate_gaintable()
	arl_simulate_gaintable(lowconfig, gt);

	// apply_gaintable()
	arl_apply_gaintable(lowconfig, vt, gt, vt_gt);

	// create_image_from_blockvisibility()
	// Create an image with nchan = 1
	for(i = 0; i< 4; i++) {
		shape1[i] = shape[i];
		}
	shape1[0] = 1;
	model = allocate_image(shape1);
	arl_create_image_from_blockvisibility(lowconfig, vt, adv.cellsize, adv.npixel, vt->phasecentre, model);

	// invert_function()
	dirty = allocate_image(shape1);
	arl_invert_function(lowconfig, vtpredicted, model, adv.vis_slices, dirty);

	// FITS file output
	status = export_image_to_fits_c(dirty, "!results/dirty.fits");

	// ical() - serial version
	deconvolved = allocate_image(shape1);
	residual    = allocate_image(shape1);
	restored    = allocate_image(shape1);

	arl_ical(lowconfig, vt_gt, model, adv.vis_slices, deconvolved, residual, restored);

	// FITS file output
	status = export_image_to_fits_c(deconvolved, 	"!results/deconvolved.fits");
	status = export_image_to_fits_c(residual, 	"!results/residual.fits");
	status = export_image_to_fits_c(restored, 	"!results/restored.fits");


	// Cleaning up
	gleam_model 	= destroy_image(gleam_model);
	model		= destroy_image(model);
	dirty		= destroy_image(dirty);
	deconvolved	= destroy_image(deconvolved);
	residual	= destroy_image(residual);
	restored	= destroy_image(restored);
	vt 		= destroy_vis(vt);
	vtpredicted 	= destroy_vis(vtpredicted);
	vt_predictfunction = destroy_vis(vt_predictfunction);
	vt_gt 		= destroy_vis(vt_gt);
	gt 		= destroy_gt(gt);
	free(cindex_predict);
	free(shape);
	free(shape1);
	// end ICAL section

	return 0;

}