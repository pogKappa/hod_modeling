# This script uses variables without little-h, but halomod uses units with little-h (Mpc versus Mpc/h etc.), so there are conversions throughout

## ==STANDARD MODULES==
import random
import time, sys
import numpy as np
from ConfigParser import SafeConfigParser
from scipy.interpolate import\
    InterpolatedUnivariateSpline as spline
from astropy.cosmology import default_cosmology

## ==HALO MODEL & FITTING MODULES==
import halomod as hm
import emcee


# sections in main() function
# (1): load in tables
# (2): initialize halo model using halomod
# (3): build probability functions for running mcmc
# (4): generate inital points from random
# (5): MAIN PART for mcmc process
# (6): based on mcmc samples, derive some parameters like effective bias
# (7): save results to files

def main():

    paramDict = GetParams()

    version = paramDict['VERSION']
    stdout("Version : %s" % version)

    wd               = paramDict['WORKING_DIRECTORY']
    corrFilename     = paramDict['INPUT_ACF']
    rdFilename       = paramDict['INPUT_ZD']
    covFilename      = paramDict['INPUT_COV']
    mcmcFilename     = paramDict['OUTPUT_MCMC_SAMPLES']
    acfModelFilename = paramDict['OUTPUT_ACF_SAMPLES']
    paramsFilename   = paramDict['OUTPUT_DERIVED_SAMPLES']

    ## ==load in tables (1)==
    # main file: contains theta, acf, RR
    data = np.genfromtxt(wd+corrFilename)
    obsSep = data[:,0]
    obsACF = data[:,1]
    obsRR  = data[:,2]
    
    nz = np.loadtxt(wd+rdFilename)
    redshift_distribution = spline(nz[:,0], nz[:,1])

    cov = np.loadtxt(wd+covFilename)
    invCov = np.linalg.inv(cov)

    stdout('Files are successfully loaded')
    ## ==finish loading==
    
    ## ==initialize halo model (2)==
    h = hm.AngularCF(hod_model=paramDict['HOD_MODEL'], z=paramDict['z_mean'])
    h.update(hod_params          =    {"central":True})
    h.update(hmf_model           =    paramDict['HALO_MASS_FUNCTION'])
    h.update(bias_model          =    paramDict['HALO_BIAS_FUNCTION'])
    h.update(concentration_model =    paramDict['CONCENTRAION_TO_MASS_RELATION'])
    h.update(zmin                =    paramDict['z_min'])
    h.update(zmax                =    paramDict['z_max'])
    h.update(znum                =    paramDict['z_num'])
    h.update(p1                  =    redshift_distribution)
    h.update(theta_min           =    paramDict['theta_min']*(np.pi/180.0))
    h.update(theta_max           =    paramDict['theta_max']*(np.pi/180.0))
    h.update(theta_num           =    paramDict['theta_num'])
    h.update(logu_min            =    paramDict['logu_min'])
    h.update(logu_max            =    paramDict['logu_max'])
    h.update(unum                =    paramDict['unum'])

    cosmo_model = default_cosmology.get_cosmology_from_string(paramDict['COSMOLOGY'])
    h.update(cosmo_model         =    cosmo_model)
    
    if paramDict['LITTLE_H_INCUSION']:
        little_h = h.cosmo.H0.value/100.
    else:
        little_h = 1.
    stdout('Halo model is established')
    ## ==halo model established==

    ## ==build probability functions for mcmc (3)==
    # Likelihood function in log scale
    def LnLikeli(th, obsSep, obsACF, obsRR, _invCov, obsNdens, obsNdensErr):
        M_min, M_1, alpha, sig_logm, M_0 = th
        h.update(hod_params={"M_min":    M_min + np.log10(little_h),
                             "M_1":      M_1   + np.log10(little_h),
                             "alpha":    alpha,
                             "sig_logm": sig_logm,
                             "M_0":      M_0   + np.log10(little_h)})
        
        modelSep     = np.degrees(h.theta)
        modelACF     = h.angular_corr_gal
        modelACF_spl = spline(modelSep, modelACF, k=3) # callable function
        
        if paramDict['APPLY_INTEGRAL_CONSTRAIN']:
            ic = np.sum(obsRR * modelACF_spl(obsSep)) / np.sum(obsRR)
        else:
            ic = 0
            
        modelACF   = modelACF_spl(obsSep) - ic
        modelNdens = h.mean_gal_den*(little_h**3)

        diffACF = obsACF - modelACF
        lnACFLike   = -0.5 * np.sum(diffACF[:,np.newaxis] * _invCov* diffACF)
        lnNdensLike = -0.5 * ((modelNdens - obsNdens)/obsNdensErr) * ((modelNdens - obsNdens)/obsNdensErr)
    
        #obsACF_model_difference_trans = np.transpose(obsACF_model_difference)
        #log_liklihood_clustering = -0.5 * (obsACF_model.dot(cov_matrix_inverse.dot(obsACF_model_trans)))
        #log_L_acf = -0.5 * np.sum(np.power((obsACF - model)/obsACFerr, 2.))
        #log_L_n   = -0.5 * np.power((model_num_density - obsNdens)/obsNdensErr, 2.)
        return lnACFLike + lnNdensLike
        
    # Prior probability function in log scale
    def LnPrior(th, paramDict):
        M_min, M_1, alpha, sig_logm, M_0 = th
        
        if  paramDict['log_Mmin_min'] < M_min    < paramDict['log_Mmin_max'] and \
            M_min                     < M_1      < paramDict['log_Msat_max'] and \
            paramDict['alpha_min']    < alpha    < paramDict['alpha_max'] and \
            paramDict['sigma_min']    < sig_logm < paramDict['sigma_max'] and \
            paramDict['log_Mcut_min'] < M_0      < M_1: 
            return 0.0
        else:
            return -np.inf
    
    # Log posterior
    def LnProb(th, obsSep, obsACF, obsRR, _invCov, obsNdens, obsNdensErr, paramDict):
        prior = LnPrior(th, paramDict)
        if not np.isfinite(prior):
            return -np.inf
        return prior + LnLikeli(th, obsSep, obsACF, obsRR, _invCov, obsNdens, obsNdensErr)
    stdout('Probability functions are defined')
    ## ==functions defined==
    
    ## ==generate initial points for mcmc (4)==
    mcmcNumSteps = paramDict['mcmc_steps']
    ndim         = paramDict['Ndim']
    nwalkers     = paramDict['Nwalkers']
    sampleRate   = paramDict['sample_rate']
    burninRate   = paramDict['burnin_rate']
    nprocessors  = paramDict['Nprocessors']

    ipoints = []
    for i in range(nwalkers):
        rand1 = random.uniform(paramDict['log_Mmin_min'], paramDict['log_Mmin_max'])
        rand2 = random.uniform(rand1                    , paramDict['log_Msat_max'])
        rand3 = random.uniform(paramDict['alpha_min']   , paramDict['alpha_max'])
        rand4 = random.uniform(paramDict['sigma_min']   , paramDict['sigma_max'])
        rand5 = random.uniform(paramDict['log_Mcut_min'], rand2)
        ipoints.append([rand1,rand2,rand3,rand4,rand5])
    stdout('Initialized mcmc points')
    ## ==done==
    
    ## ==main part for running mcmc (5)==
    obsNdens = paramDict['obs_number_density']
    obsNdensErr = paramDict['err_obs_ndens']

    sampler = emcee.EnsembleSampler(
        nwalkers,
        ndim,
        LnProb,
        #args=(obsSep, obsACF, obsRR, invCov, paramDict['obs_number_density'], paramDict['err_obs_ndens']),
        args=(obsSep, obsACF, obsRR, invCov, obsNdens, obsNdensErr, paramDict)
    )
    stdout('MCMC sampler is created')
    
    t1 = time.time()
    sampler.run_mcmc(ipoints, mcmcNumSteps) 
    t2 = time.time()
    print 'MCMC Finished'
    print 'MCMC took '+str(np.floor((t2-t1)/60))+' minutes' # Prints how long the MCMC fitting took
    
    nBurnin = int(mcmcNumSteps*burninRate)

    samples = sampler.chain[:,nBurnin:,:].reshape((-1, ndim))
    LnProb  = sampler.lnprobability[:,nBurnin:].reshape(-1)
    ## ==done mcmc==
    
    ## ==now start to derive some parameters (6)==
    nsamples = nwalkers*mcmcNumSteps/sampleRate
    modelACFDistr   = np.zeros(shape=(len(obsSep), nsamples))
    effBiasDistr    = np.zeros(nsamples)
    effMassDistr    = np.zeros(nsamples)
    fsatDistr       = np.zeros(nsamples)
    nDensModelDistr = np.zeros(nsamples)
    
    for i in range(nsamples):
        
        h.update(hod_params={"M_min"    : samples[i*sampleRate, 0]+np.log10(little_h),
                             "M_1"      : samples[i*sampleRate, 1]+np.log10(little_h),
                             "alpha"    : samples[i*sampleRate, 2]                   ,
                             "sig_logm" : samples[i*sampleRate, 3]                   ,
                             "M_0"      : samples[i*sampleRate, 4]+np.log10(little_h)})
        
        modelSep     = np.degrees(h.theta)
        modelACF     = h.angular_corr_gal
        modelACF_spl = spline(modelSep, modelACF, k=3)
        
        if paramDict['APPLY_INTEGRAL_CONSTRAIN']:
            ic = np.sum(obsRR * modelACF_spl(obsSep)) / np.sum(obsRR)
        else:
            ic = 0

        modelACF = modelACF_spl(obsSep) - ic
        
        modelACFDistr[:,i] = modelACF
        effBiasDistr[i]    = h.bias_effective
        fsatDistr[i]       = h.satellite_fraction
        effMassDistr[i]    = h.mass_effective-np.log10(little_h)
        nDensModelDistr[i] = h.mean_gal_den*(little_h**3)
    
    # Create objects for holding the models
    modelACF_best  = np.zeros(len(obsSep))
    modelACF_lower = np.zeros(len(obsSep))
    modelACF_upper = np.zeros(len(obsSep))
    
    # Find percentiles of acf
    for i in range(len(obsSep)):
        modelACF_best[i]  = np.percentile(modelACFDistr[i,:],50)
        modelACF_lower[i] = np.percentile(modelACFDistr[i,:],16)
        modelACF_upper[i] = np.percentile(modelACFDistr[i,:],84)
    ## ==parameters are derived==
    
    
    ## ==save all derived parameters to file (7)==
    
    np.savetxt(wd+mcmcFilename+version+".dat", samples) # Save HOD param samples
    
    derrived_parameters = np.transpose([fsatDistr, effBiasDistr, effMassDistr, nDensModelDistr])
    np.savetxt(wd+paramsFilename+version+".dat", derrived_parameters)
    
    model_acf = np.transpose([model_lower,model_best,model_upper])
    np.savetxt(wd+acfModelFilename+version+".dat",model_acf) # Save model acfs

    ##################################
    ##### end of main() function #####
    ##################################

def GetParams():

    params = InitializeParameters()

    filename = GetParamFilename()
    fileParams = np.loadtxt(filename, dtype=np.str, usecols=(0,1))
    fileParamsDict = {}
    for idx in range(fileParams.shape[0]):
        fileParamsDict[fileParams[idx,0]] = fileParams[idx,1]

    diffFromInit = set(params.keys()).difference(fileParamsDict.keys())
    diffFromInit = list(diffFromInit)

    for pname, pvalue in params.items():
        if pname in diffFromInit:
            pass
        else:
            params[pname] = fileParamsDict[pname].astype(type(pvalue))

    return params

def GetParamFilename():

    if len(sys.argv) == 2:
        stdout('read parameter file: %s ' % sys.argv[1])
        return sys.argv[1]
    else:
        sys.exit(
            '%s... please input a parameter file by using command : \
            $python halo_modeling [YOUR_PARAMETER_FILES]' % sys.argv[0]
        )

def InitializeParameters():

    return {
            # FILES
            'WORKING_DIRECTORY':      'YOUR_WORKING_DIRECTORY',
            'INPUT_ACF':              'YOUR_CORRELATION_FILE',
            'INPUT_ZD':               'YOUR_REDSHIFT_DISTRIBUTION',
            'INPUT_COV':              'YOUR_COVARIANCE_MATRIX',
            'OUTPUT_MCMC_SAMPLES':    'MCMC_CHAIN_SAMPLE',
            'OUTPUT_ACF_SAMPLES':     'BEST_FIT_ACF_SAMPLE',
            'OUTPUT_DERIVED_SAMPLES': 'DERIVED_PARAMETER_SAMPLE',
            # MODELS
            'VERSION':                       'v1',
            'COSMOLOGY':                     'Planck15',
            'HOD_MODEL':                     'Zheng05',
            'HALO_MASS_FUNCTION':            'Tinker10',
            'HALO_BIAS_FUNCTION':            'Tinker10',
            'CONCENTRAION_TO_MASS_RELATION': 'Duffy08',
            # SWITCHES
            'APPLY_INTEGRAL_CONSTRAIN': True,
            'LITTLE_H_INCUSION':        True,
            # HOD settings
            'obs_number_density': 0.0005,
            'err_obs_ndens':      0.00002,
            'z_mean':             1.12,
            'z_min':              1.0,
            'z_max':              1.25,
            'z_num':              100,
            'logM_min':           6,
            'logM_max':           16,
            'theta_min':          1./3600.,
            'theta_max':          3600./3600.,
            'theta_num':          60,
            'logu_min':           -5.,
            'logu_max':           2.5,
            'unum':               150,
            'log_Mmin_min':       11.,
            'log_Mmin_max':       13.,
            'log_Msat_max':       14.,
            'log_Mcut_min':       9.,
            'alpha_min':          0.7,
            'alpha_max':          1.35,
            'sigma_min':          0.25,
            'sigma_max':          0.6,
            # MCMC settings
            'mcmc_steps':         1500,
            'Ndim':               5,
            'Nwalkers':           20,
            'sample_rate':        10,
            'burnin_rate':        0.25,
            'Nprocessors':        1
           }

def stdout(message):
    print('%s...  %s' % (sys.argv[0], message))

if __name__ == '__main__':
    main()
