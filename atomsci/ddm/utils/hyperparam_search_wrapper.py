#!/usr/bin/env python

# noinspection SpellCheckingInspection
"""
Script to generate hyperparameter combinations based on input params and send off jobs to a slurm system.
Author: Amanda Minnich
"""

# from __future__ import unicode_literals

import collections
import os, os.path
import sys
import numpy as np
import logging
import itertools
import pandas as pd
import uuid

import subprocess
import time


from atomsci.ddm.pipeline import featurization as feat
from atomsci.ddm.pipeline import parameter_parser as parse
from atomsci.ddm.pipeline import model_datasets as model_datasets
from atomsci.ddm.utils import datastore_functions as dsf
from atomsci.ddm.pipeline import mlmt_client_wrapper as mlmt_client_wrapper
from atomsci.ddm.pipeline import model_tracker as trkr
logging.basicConfig(format='%(asctime)-15s %(message)s')

import logging
import socket
import traceback
import copy


def run_command(shell_script, python_path, script_dir, params):
    """
    Function to submit jobs on a slurm system
    Args:
        shell_script: Name of shell script to run
        python_path: Path to python version
        script_dir: Directory where script lives
        params: parameters in dictionary format
    Returns:
        None

    """
    params_str = parse.to_str(params)
    slurm_command = 'sbatch {0} {1} {2} "{3}"'.format(shell_script, python_path, script_dir, params_str)
    print(slurm_command)
    os.system(slurm_command)


def run_cmd(cmd):
    """
    Function to submit a job using subprocess
    
    Args:
        
        cmd: Command to run

    Returns:
        
        output: Output of command
    """
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, shell=True)
    (output, err) = p.communicate()
    p.wait()
    return output


def reformat_filter_dict(filter_dict):
    """
    Function to reformat a filter dictionary to match the Model Tracker metadata structure. May be obsolete for updated
    model tracker.
    
    Args:
        
        filter_dict: Dictionary containing metadata for model of interest

    Returns:
        
        new_filter_dict: Filter dict reformatted

    """
    rename_dict = {'ModelMetadata.ModelParameters':
                       {'featurizer', 'model_choice_score_type', 'model_dataset_oid', 'model_type',
                        'num_model_tasks', 'prediction_type', 'transformer_bucket', 'transformer_key',
                        'transformer_oid', 'transformers', 'uncertainty'},
                   'ModelMetadata.SplittingParameters.Splitting':
                       {'num_folds', 'split_strategy', 'split_test_frac', 'split_uuid', 'split_valid_frac', 'splitter'},
                   'ModelMetadata.TrainingDataset':
                       {'dataset_key', 'dataset_bucket', 'dataset_oid', 'num_classes',
                        'feature_transform_type', 'response_transform_type', 'id_col', 'smiles_col', 'response_cols'},
                   'ModelMetadata.UmapSpecific':
                       {'umap_dim', 'umap_metric', 'umap_targ_wt', 'umap_neighbors', 'umap_min_dist'}}
    if filter_dict['model_type'] == 'NN':
        rename_dict['ModelMetadata.NNSpecific'] = {
                'batch_size', 'bias_init_consts', 'dropouts', 'layer_sizes', 'learning_rate', 'max_epochs', 'optimizer_type', 'weight_init_stddevs'}
        # Need to omit baseline_epoch because we hadn't been saving it in the model metadata
    elif filter_dict['model_type'] == 'RF':
        rename_dict['ModelMetadata.RFSpecific'] = {'rf_estimators', 'rf_max_features'}
    elif filter_dict['model_type'] == 'xgboost':
        rename_dict['ModelMetadata.xgboostSpecific'] = {'xgb_learning_rate',
                                                        'xgb_gamma'}
    if filter_dict['featurizer'] == 'ecfp':
        rename_dict['ModelMetadata.ECFPSpecific'] = {'ecfp_radius', 'ecfp_size'}
    elif filter_dict['featurizer'] == 'descriptor':
        rename_dict['ModelMetadata.DescriptorSpecific'] = {'descriptor_key', 'descriptor_bucket', 'descriptor_oid', 'descriptor_type'}
    elif filter_dict['featurizer'] == 'molvae':
        rename_dict['ModelMetadata.AutoencoderSpecific'] = {'autoencoder_model_key', 'autoencoder_model_bucket', 'autoencoder_model_oid', 'autoencoder_type'}
    new_filter_dict = {}
    for key, values in rename_dict.items():
        for value in values:
            if value in filter_dict:
                filter_val = filter_dict[value]
                if type(filter_val) == np.int64:
                    filter_dict[value] = int(filter_val)
                elif type(filter_val) == np.float64:
                    filter_dict[value] = float(filter_val)
                elif type(filter_val) == list:
                    for i, item in enumerate(filter_val):
                        if type(item) == np.int64:
                            filter_dict[value][i] = int(item)
                        elif type(filter_val) == np.float64:
                            filter_dict[value][i] = float(item)
                new_filter_dict['%s.%s' % (key, value)] = filter_dict[value]
    return new_filter_dict


def permutate_NNlayer_combo_params(layer_nums, node_nums, dropout_list, max_final_layer_size):
    """
    to generate combos of layer_sizes(str) and dropouts(str) params from the layer_nums (list), node_nums (list), dropout_list (list).

    The permutation will make the NN funnel shaped, so that the next layer can only be smaller or of the same size of the current layer.
    Example:
    permutate_NNlayer_combo_params([2], [4,8,16], [0], 16)
    returns [[16, 4], [16, 8], [8,4]] [[0,0],[0,0],[0,0]]

    If there are duplicates of the same size, it will create consecutive layers of the same size.
    Example:
    permutate_NNlayer_combo_params([2], [4,8,8], [0], 16)
    returns [[8, 8], [8, 4]] [[0,0],[0,0]]
    
    Args:
        layer_nums: specify numbers of layers.
        
        node_nums: specify numbers of nodes per layer.
        
        dropout_list: specify the dropouts.
        
        max_last_layer_size: sets the max size of the last layer. It will be set to the smallest node_num if needed.
        
    Returns:
        layer_sizes, dropouts: the layer sizes and dropouts generated based on the input parameters
    """
    import itertools
    import numpy as np
    layer_sizes = []
    dropouts = []
    node_nums = np.sort(np.array(node_nums))[::-1]
    max_final_layer_size = int(max_final_layer_size)
    # set to the smallest node_num in the provided list, if necessary.
    if node_nums[-1] > max_final_layer_size:
        max_final_layer_size = node_nums[-1]
        
    for dropout in dropout_list:
        _repeated_layers =[]
        for layer_num in layer_nums:
            for layer in itertools.combinations(node_nums, layer_num):
                layer = [i for i in layer]
                if (layer[-1] <= max_final_layer_size) and (layer not in _repeated_layers):
                    _repeated_layers.append(layer)
                    layer_sizes.append(layer)
                    dropouts.append([(dropout) for i in layer])
    return layer_sizes, dropouts


def get_num_params(combo):
    """
    Calculates the number of parameters in a fully-connected neural networ
    Args:
        
        combo: Model parameters

    Returns:
        
        tmp_sum: Calculated number of parameters

    """
    layers = combo['layer_sizes']
    # All layers multiplied by adjacent layers, summed, plus the final layer times the number of samples. Extra addition is for bias terms
    tmp_sum = layers[0] + sum(layers[i] * layers[i + 1] + layers[i+1] for i in range(len(layers) - 1))
    # Add in first layer times the feature vector size. Estimate 300 for descriptors.
    #TODO: Update for moe vs mordred
    if combo['featurizer'] == 'ecfp':
        return tmp_sum + layers[0]*1024
    if combo['featurizer'] == 'descriptors':
        if combo['descriptor_type'] == 'moe':
            return tmp_sum + layers[0]*306
        if combo['descriptor_type'] == 'mordred_filtered':
            return tmp_sum + layers[0]*1555
    else:
        return tmp_sum


# Global variable with keys that should not be used to generate hyperparameters
excluded_keys = {'shortlist_key', 'use_shortlist', 'dataset_key', 'object_oid', 'script_dir',
                  'python_path', 'config_file', 'hyperparam', 'search_type', 'split_only', 'layer_nums',
                  'node_nums', 'dropout_list', 'max_final_layer_size', 'splitter', 'nn_size_scale_factor',
                  'rerun', 'max_jobs'}


class HyperparameterSearch(object):
    """
    The class for generating and running all hyperparameter combinations based on the input params given
    """
    def __init__(self, params, hyperparam_uuid=None):
        """
        
        Args:
            
            params: The input hyperparameter parameters
            
            hyperparam_uuid: Optional, UUID for hyperparameter run if you want to group this run with a previous run.
            We ended up mainly doing this via collections, so not really used
        """
        self.hyperparam_layers = {'layer_sizes', 'dropouts', 'weight_init_stddevs', 'bias_init_consts'}
        self.hyperparam_keys = {'model_type', 'featurizer', 'splitter', 'learning_rate', 'weight_decay_penalty',
                                'rf_estimators', 'rf_max_features', 'rf_max_depth',
                                'umap_dim', 'umap_targ_wt', 'umap_metric', 'umap_neighbors', 'umap_min_dist',
                                'xgb_learning_rate',
                                'xgb_gamma'}
        self.nn_specific_keys = {'learning_rate', 'layers','weight_decay_penalty'}
        self.rf_specific_keys = {'rf_estimators', 'rf_max_features', 'rf_max_depth'}
        self.xgboost_specific_keys = {'xgb_learning_rate', 'xgb_gamma'}
        self.hyperparam_keys |= self.hyperparam_layers
        self.excluded_keys = excluded_keys
        self.convert_to_float = parse.convert_to_float_list
        self.convert_to_int = parse.convert_to_int_list
        self.params = params
        # simplify NN layer construction
        if (params.layer_nums != None) and (params.node_nums != None) and (params.dropout_list != None):

            self.params.layer_sizes, self.params.dropouts = permutate_NNlayer_combo_params(params.layer_nums,
                                                                                           params.node_nums,
                                                                                           params.dropout_list,
                                                                                           params.max_final_layer_size)
        if hyperparam_uuid is None:
            self.hyperparam_uuid = str(uuid.uuid4())
        else:
            self.hyperparam_uuid = hyperparam_uuid
        self.hyperparams = {}
        self.new_params = {}
        self.layers = {}
        self.param_combos = []
        self.num_rows = {}
        self.log = logging.getLogger("hyperparam_search")
        # Create handlers
        c_handler = logging.StreamHandler()
        log_path = os.path.join(self.params.result_dir, 'logs')
        if not os.path.exists(log_path):
            os.makedirs(log_path)
        f_handler = logging.FileHandler(os.path.join(log_path, '{0}.log'.format(self.hyperparam_uuid)))
        self.out_file = open(os.path.join(log_path, '{0}.json'.format(self.hyperparam_uuid)), 'a')
        c_handler.setLevel(logging.WARNING)
        f_handler.setLevel(logging.INFO)
        # Create formatters and add it to handlers
        c_format = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
        f_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        c_handler.setFormatter(c_format)
        f_handler.setFormatter(f_format)
        # Add handlers to the logger
        self.log.addHandler(c_handler)
        self.log.addHandler(f_handler)

        self.client_wrapper = mlmt_client_wrapper.MLMTClientWrapper()
        self.client_wrapper.instantiate_mlmt_client()

        slurm_path = os.path.join(self.params.result_dir, 'slurm_files')
        if not os.path.exists(slurm_path):
            os.makedirs(slurm_path)
        self.shell_script = os.path.join(self.params.script_dir, 'utils', 'run.sh')
        with open(self.shell_script, 'w') as f:
            hostname = ''.join(list(filter(lambda x: x.isalpha(), socket.gethostname())))
            f.write("#!/bin/bash\n#SBATCH -A {2}\n#SBATCH -N 1\n#SBATCH -p partition={0}\n#SBATCH -t 24:00:00"
                    "\n#SBATCH -p {3}\n#SBATCH --export=ALL\n#SBATCH -D {1}\n".format(hostname, slurm_path,
                    self.params.lc_account,self.params.slurm_partition))
            f.write('start=`date +%s`\necho $3\n$1 $2/pipeline/model_pipeline.py $3\nend=`date +%s`\n'
                    'runtime=$((end-start))\necho "runtime: " $runtime')

    def generate_param_combos(self):
        """
        Performs additional parsing of parameters and generates all combinations

        Returns:

            None

        """
        for key, value in vars(self.params).items():
            if not value or key in self.excluded_keys:
                continue
            elif key == 'result_dir' or key == 'output_dir':
                self.new_params[key] = os.path.join(value, self.hyperparam_uuid)
            # Need to zip together layers in special way
            elif key in self.hyperparam_layers and type(value[0]) == list:
                self.layers[key] = value
            # Parses the hyperparameter keys depending on the size of the key list
            elif key in self.hyperparam_keys:
                if type(value) != list:
                    self.new_params[key] = value
                    self.hyperparam_keys.remove(key)
                elif len(value) == 1:
                    self.new_params[key] = value[0]
                    self.hyperparam_keys.remove(key)
                else:
                    self.hyperparams[key] = value
            else:
                self.new_params[key] = value
        # Adds layers to the parameter combos
        if self.layers:
            self.assemble_layers()
        # setting up the various hyperparameter combos for each model type.
        if type(self.params.model_type) == str:
            self.params.model_type = [self.params.model_type]
        if type(self.params.featurizer) == str:
            self.params.featurizer = [self.params.featurizer]
        for model_type in self.params.model_type:
            if model_type == 'NN':
                # if the model type is NN, loops through the featurizer to check for GraphConv.
                for featurizer in self.params.featurizer:
                    subcombo = {k: val for k, val in self.hyperparams.items() if k in
                                self.hyperparam_keys - self.rf_specific_keys - self.xgboost_specific_keys}
                    # could put in list
                    subcombo['model_type'] = [model_type]
                    subcombo['featurizer'] = [featurizer]
                    self.param_combos.extend(self.generate_combos(subcombo))
            elif model_type == 'RF':
                for featurizer in self.params.featurizer:
                    if featurizer == 'graphconv':
                        continue
                    # Adds the subcombo for RF
                    subcombo = {k: val for k, val in self.hyperparams.items() if k in
                                self.hyperparam_keys - self.nn_specific_keys - self.xgboost_specific_keys}
                    subcombo['model_type'] = [model_type]
                    subcombo['featurizer'] = [featurizer]
                    self.param_combos.extend(self.generate_combos(subcombo))
            elif model_type == 'xgboost':
                for featurizer in self.params.featurizer:
                    if featurizer == 'graphconv':
                        continue
                    # Adds the subcombo for xgboost
                    subcombo = {k: val for k, val in self.hyperparams.items() if k in
                                self.hyperparam_keys - self.nn_specific_keys - self.rf_specific_keys}
                    subcombo['model_type'] = [model_type]
                    subcombo['featurizer'] = [featurizer]
                    self.param_combos.extend(self.generate_combos(subcombo))

    def generate_combos(self, params_dict):
        """
        Calls sub-function generate_combo and then uses itertools.product to generate all desired combinations
        Args:
            params_dict:

        Returns:

        """
        new_dict = self.generate_combo(params_dict)
        hyperparam_combos = []
        hyperparams = new_dict.keys()
        hyperparam_vals = new_dict.values()
        for ind, hyperparameter_tuple in enumerate(itertools.product(*hyperparam_vals)):
            model_params = {}
            for hyperparam, hyperparam_val in zip(hyperparams, hyperparameter_tuple):
                model_params[hyperparam] = hyperparam_val
            hyperparam_combos.append(model_params)
        return hyperparam_combos

    def assemble_layers(self):
        """
        Reformats layer parameters
        
        Returns:
            None
        """
        tmp_list = []
        for i in range(min([len(x) for x in list(self.layers.values())])):
            tmp_dict = {}
            for key, value in self.layers.items():
                tmp_dict[key] = value[i]
            x = [len(y) for y in tmp_dict.values()]
            try:
                assert x.count(x[0]) == len(x)
            except:
                continue
            tmp_list.append(tmp_dict)
        self.hyperparams['layers'] = tmp_list
        self.hyperparam_keys.add('layers')

    def generate_assay_list(self):
        """
        Generates the list of datasets to build models for, with their key, bucket, split, and split uuid
        Returns:

        """
        # Creates the assay list with additional options for use_shortlist
        if not self.params.use_shortlist:
            if type(self.params.splitter) == str:
                splitters = [self.params.splitter]
            else:
                splitters = self.params.splitter
            self.assays = []
            for splitter in splitters:
                if 'previously_split' in self.params.__dict__.keys() and 'split_uuid' in self.params.__dict__.keys() \
                    and self.params.previously_split and self.params.split_uuid is not None:
                    self.assays.append((self.params.dataset_key, self.params.bucket, splitter, self.params.split_uuid))
                else:
                    try:
                        split_uuid = self.return_split_uuid(self.params.dataset_key, splitter=splitter)
                        self.assays.append((self.params.dataset_key, self.params.bucket, splitter, split_uuid))
                    except Exception as e:
                        print(e)
                        print(traceback.print_exc())
                        sys.exit(1)
        else:
            self.assays = self.get_shortlist_df(split_uuids=True)
        self.assays = [(t[0].strip(), t[1].strip(), t[2].strip(), t[3].strip()) for t in self.assays]

    def get_dataset_metadata(self, assay_params):
        """
        Gather the required metadata for a dataset
        
        Args:
            assay_params: dataset metadata

        Returns:
            None

        """
        print(assay_params['dataset_key'])
        retry = True
        i = 0
        #TODO: need to catch if dataset doesn't exist versus 500 failure
        while retry:
            try:
                metadata = dsf.get_keyval(dataset_key=assay_params['dataset_key'], bucket=assay_params['bucket'])
                retry = False
            except Exception as e:
                if i < 5:
                    print("Could not get metadata from datastore for dataset %s because of exception %s, sleeping..."
                            % (assay_params['dataset_key'], e))
                    time.sleep(60)
                    i += 1
                else:
                    print("Could not get metadata from datastore for dataset %s because of exception %s, exiting"
                            % (assay_params['dataset_key'], e))
                    return None
        if 'id_col' in metadata.keys():
            assay_params['id_col'] = metadata['id_col']
        if 'response_cols' not in assay_params or assay_params['response_cols'] is None:
            if 'param' in metadata.keys():
                assay_params['response_cols'] = [metadata['param']]
            if 'response_col' in metadata.keys():
                assay_params['response_cols'] = [metadata['response_col']]
            if 'response_cols' in metadata.keys():
                assay_params['response_cols'] = metadata['response_cols']
        if 'smiles_col' in metadata.keys():
            assay_params['smiles_col'] = metadata['smiles_col']
        if 'class_name' in metadata.keys():
            assay_params['class_name'] = metadata['class_name']
        if 'class_number' in metadata.keys():
            assay_params['class_number'] = metadata['class_number']
        if 'num_row' in metadata.keys():
            self.num_rows[assay_params['dataset_key']] = metadata['num_row']
        assay_params['dataset_name'] = assay_params['dataset_key'].split('/')[-1].rstrip('.csv')
        assay_params['hyperparam_uuid'] = self.hyperparam_uuid

    def split_and_save_dataset(self, assay_params):
        """
        Splits a given dataset, saves it, and sets the split_uuid in the metadata
        
        Args:
            assay_params: Dataset metadata

        Returns:
            None

        """
        self.get_dataset_metadata(assay_params)
        # TODO: check usage with defaults
        namespace_params = parse.wrapper(assay_params)
        # TODO: Don't want to recreate each time
        featurization = feat.create_featurization(namespace_params)
        data = model_datasets.create_model_dataset(namespace_params, featurization)
        data.get_featurized_data()
        data.split_dataset()
        data.save_split_dataset()
        assay_params['previously_split'] = True
        assay_params['split_uuid'] = data.split_uuid

    def return_split_uuid(self, dataset_key, bucket=None, splitter=None, split_combo=None):
        """
        Loads a dataset, splits it, saves it, and returns the split_uuid
        Args:
            dataset_key: key for dataset to split
            bucket: datastore-specific user group bucket
            splitter: Type of splitter to use to split the dataset
            split_combo: tuple of form (split_valid_frac, split_test_frac)

        Returns:

        """
        if bucket is None:
            bucket = self.params.bucket
        if splitter is None:
            splitter=self.params.splitter
        if split_combo is None:
            split_valid_frac = self.params.split_valid_frac
            split_test_frac = self.params.split_test_frac
        else:
            split_valid_frac = split_combo[0]
            split_test_frac = split_combo[1]
        retry = True
        i = 0
        #TODO: need to catch if dataset doesn't exist versus 500 failure
        while retry:
            try:
                metadata = dsf.get_keyval(dataset_key=dataset_key, bucket=bucket)
                retry = False
            except Exception as e:
                if i < 5:
                    print("Could not get metadata from datastore for dataset %s because of exception %s, sleeping..." % (dataset_key, e))
                    time.sleep(60)
                    i += 1
                else:
                    print("Could not get metadata from datastore for dataset %s because of exception %s, exiting" % (dataset_key, e))
                    return None
        assay_params = {'dataset_key': dataset_key, 'bucket': bucket, 'splitter': splitter,
                        'split_valid_frac': split_valid_frac, 'split_test_frac': split_test_frac}
        #Need a featurizer type to split dataset, but since we only care about getting the split_uuid, does not matter which featurizer you use
        if type(self.params.featurizer) == list:
            assay_params['featurizer'] = self.params.featurizer[0]
        else:
            assay_params['featurizer'] = self.params.featurizer
        if 'id_col' in metadata.keys():
            assay_params['id_col'] = metadata['id_col']
        if 'response_cols' not in assay_params or assay_params['response_cols'] is None:
            if 'param' in metadata.keys():
                assay_params['response_cols'] = [metadata['param']]
            if 'response_col' in metadata.keys():
                assay_params['response_cols'] = [metadata['response_col']]
            if 'response_cols' in metadata.keys():
                assay_params['response_cols'] = metadata['response_cols']
        if 'smiles_col' in metadata.keys():
            assay_params['smiles_col'] = metadata['smiles_col']
        if 'class_name' in metadata.keys():
            assay_params['class_name'] = metadata['class_name']
        if 'class_number' in metadata.keys():
            assay_params['class_number'] = metadata['class_number']
        assay_params['dataset_name'] = assay_params['dataset_key'].split('/')[-1].rstrip('.csv')
        assay_params['datastore'] = True
        assay_params['previously_featurized'] = self.params.previously_featurized
        try:
            assay_params['descriptor_key'] = self.params.descriptor_key
            assay_params['descriptor_bucket'] = self.params.descriptor_bucket
        except:
            print("")
        #TODO: check usage with defaults
        namespace_params = parse.wrapper(assay_params)
        # TODO: Don't want to recreate each time
        featurization = feat.create_featurization(namespace_params)
        data = model_datasets.create_model_dataset(namespace_params, featurization)
        retry = True
        i = 0
        while retry:
            try:
                data.get_featurized_data()
                data.split_dataset()
                data.save_split_dataset()
                return data.split_uuid
            except Exception as e:
                if i < 5:
                    print("Could not get metadata from datastore for dataset %s because of exception %s, sleeping" % (dataset_key, e))
                    time.sleep(60)
                    i += 1
                else:
                    print("Could not save split dataset for dataset %s because of exception %s" % (dataset_key, e))
                    return None

    def generate_split_shortlist(self):
        """
        Processes a shortlist, generates splits for each dataset on the list, and uploads a new shortlist file with the
        split_uuids included. Generates splits for the split_combos [[0.1,0.1], [0.1,0.2],[0.2,0.2]], [random, scaffold]
        
        Returns:
            None
        """
        retry = True
        i = 0
        while retry:
            try:
                shortlist_metadata = dsf.retrieve_dataset_by_datasetkey(
                    bucket=self.params.bucket, dataset_key=self.params.shortlist_key, return_metadata=True)
                retry = False
            except Exception as e:
                if i < 5:
                    print("Could not retrieve shortlist %s from datastore because of exception %s, sleeping..." %
                          (self.params.shortlist_key, e))
                    time.sleep(60)
                    i += 1
                else:
                    print("Could not retrieve shortlist %s from datastore because of exception %s, exiting" %
                          (self.params.shortlist_key, e))
                    return None

        datasets = self.get_shortlist_df()
        rows = []
        for assay, bucket in datasets:
            split_uuids = {'dataset_key': assay, 'bucket': bucket}
            for splitter in ['random', 'scaffold']:
                for split_combo in [[0.1,0.1], [0.1,0.2],[0.2,0.2]]:
                    split_name = "%s_%d_%d" % (splitter, split_combo[0]*100, split_combo[1]*100)
                    try:
                        split_uuids[split_name] = self.return_split_uuid(assay, bucket, splitter, split_combo)
                    except Exception as e:
                        print(e)
                        print("Splitting failed for dataset %s" % assay)
                        split_uuids[split_name] = None
                        continue
            rows.append(split_uuids)
        df = pd.DataFrame(rows)
        new_metadata = {}
        new_metadata['dataset_key'] = shortlist_metadata['dataset_key'].strip('.csv') + '_with_uuids.csv'
        new_metadata['has_uuids'] = True
        new_metadata['description'] = '%s, with UUIDs' % shortlist_metadata['description']
        retry = True
        i = 0
        while retry:
            try:
                dsf.upload_df_to_DS(df,
                                    bucket=self.params.bucket,
                                    filename=new_metadata['dataset_key'],
                                    title=new_metadata['dataset_key'].replace('_', ' '),
                                    description=new_metadata['description'],
                                    tags=[],
                                    key_values={},
                                    dataset_key=new_metadata['dataset_key'])
                retry=False
            except Exception as e:
                if i < 5:
                    print("Could not save new shortlist because of exception %s, sleeping..." % e)
                    time.sleep(60)
                    i += 1
                else:
                    #TODO: Add save to disk.
                    print("Could not save new shortlist because of exception %s, exiting" % e)
                    retry = False

    def get_shortlist_df(self, split_uuids=False):
        """
        
        Args:
            split_uuids: Boolean value saying if you want just datasets returned or the split_uuids as well

        Returns:
            The list of dataset_keys, along with their accompanying bucket, split type, and split_uuid if split_uuids is True
        """
        if self.params.datastore:
            retry = True
            i = 0
            while retry:
                try:
                    df = dsf.retrieve_dataset_by_datasetkey(self.params.shortlist_key, self.params.bucket)
                    retry=False
                except Exception as e:
                    if i < 5:
                        print("Could not retrieve shortlist %s because of exception %s, sleeping..." % (self.params.shortlist_key, e))
                        time.sleep(60)
                        i += 1
                    else:
                        print("Could not retrieve shortlist %s because of exception %s, exiting" % (self.params.shortlist_key, e))
                        sys.exit(1)
        else:
            if not os.path.exists(self.params.shortlist_key):
                return None
            df = pd.read_csv(self.params.shortlist_key, index_col=False)
        if df is None:
            sys.exit(1)
        if len(df.columns) == 1:
            assays = df[df.columns[0]].values.tolist()
        else:
            if 'task_name' in df.columns:
                col_name = 'task_name'
            else:
                col_name = 'dataset_key'
            assays = df[col_name].values.tolist()
        if 'bucket' in df.columns:
            datasets = list(zip(assays, df.bucket.values.tolist()))
        elif 'bucket_name' in df.columns:
            datasets = list(zip(assays, df.bucket_name.values.tolist()))
        else:
            datasets = list(zip(assays, [self.params.bucket]))
        datasets = [(d[0].strip(), d[1].strip()) for d in datasets]
        if not split_uuids:
            return datasets
        if type(self.params.splitter) == str:
            splitters = [self.params.splitter]
        else:
            splitters = self.params.splitter
        assays = []
        for splitter in splitters:
            split_name = '%s_%d_%d' % (splitter, self.params.split_valid_frac*100, self.params.split_test_frac*100)
            if split_name in df.columns:
                for i, row in df.iterrows():
                    assays.append((datasets[i][0], datasets[i][1], splitter, row[split_name]))
            else:
                for assay, bucket in datasets:
                    try:
                    # do we want to move this into loop so we ignore ones it failed for?
                        split_uuid = self.return_split_uuid(assay, bucket)
                        assays.append((assay, bucket, splitter, split_uuid))
                    except Exception as e:
                        print("Splitting failed for dataset %s, skipping..." % assay)
                        print(e)
                        print(traceback.print_exc())
                        continue
        return assays

    def submit_jobs(self):
        """
        Reformats parameters as necessary and then calls run_command in a loop to submit a job for each param combo
        
        Returns:
            None
        """
        for assay, bucket, splitter, split_uuid in self.assays:
            # Writes the series of command line arguments for scripts without a hyperparameter combo
            assay_params = copy.deepcopy(self.new_params)
            assay_params['dataset_key'] = assay
            assay_params['dataset_name'] = os.path.splitext(os.path.basename(assay))[0]
            assay_params['bucket'] = bucket
            assay_params['split_uuid'] = split_uuid
            assay_params['previously_split'] = True
            assay_params['splitter'] = splitter
            try:
                self.get_dataset_metadata(assay_params)
            except Exception as e:
                print(e)
                print(traceback.print_exc())
                continue
            base_result_dir = os.path.join(assay_params['result_dir'], assay_params['dataset_name'])
            if not self.param_combos:
                if assay_params['model_type'] == 'NN' and assay_params['featurizer'] != 'graphconv':
                    if assay_params['dataset_key'] in self.num_rows:
                        num_params = get_num_params(assay_params)
                        if num_params*self.params.nn_size_scale_factor >= self.num_rows[assay_params['dataset_key']]:
                            continue
                if not self.params.rerun and self.already_run(assay_params):
                    continue
                assay_params['result_dir'] = os.path.join(base_result_dir, str(uuid.uuid4()))
                self.log.info(assay_params)
                self.out_file.write(str(assay_params))
                run_command(self.shell_script, self.params.python_path, self.params.script_dir, assay_params)
            else:
                for combo in self.param_combos:
                    # For a temporary parameter list, appends and modifies parameters for each hyperparameter combo.
                    for key, value in combo.items():
                        if key == 'layers':
                            for k, v in value.items():
                                assay_params[k] = v
                        else:
                            assay_params[key] = value
                    if assay_params['model_type'] == 'NN' and assay_params['featurizer'] != 'graphconv':
                        if assay_params['dataset_key'] in self.num_rows:
                            num_params = get_num_params(assay_params)
                            if num_params*self.params.nn_size_scale_factor >= self.num_rows[assay_params['dataset_key']]:
                                continue
                    if not self.params.rerun and self.already_run(assay_params):
                        continue
                    i = int(run_cmd('squeue | grep $(whoami) | wc -l').decode("utf-8"))
                    while i >= self.params.max_jobs:
                        print("%d jobs in queue, sleeping" % i)
                        time.sleep(60)
                        i = int(run_cmd('squeue | grep $(whoami) | wc -l').decode("utf-8"))
                    assay_params['result_dir'] = os.path.join(base_result_dir, str(uuid.uuid4()))
                    self.log.info(assay_params)
                    self.out_file.write(str(assay_params))
                    run_command(self.shell_script, self.params.python_path, self.params.script_dir, assay_params)

    def already_run(self, assay_params):
        """
        Checks to see if a model with a given metadata combination has already been built
        Args:
            assay_params: model metadata information

        Returns:
            Boolean specifying if model has been previously built
        """
        filter_dict = copy.deepcopy(assay_params)
        for key in ['result_dir', 'previously_featurized', 'collection_name', 'time_generated', 'hyperparam_uuid', 'model_uuid']:
            if key in filter_dict:
                del filter_dict[key]
        filter_dict = reformat_filter_dict(filter_dict)
        retry = True
        i = 0
        while retry:
            try:
                models = list(trkr.get_metadata(filter_dict, self.client_wrapper, collection_name=assay_params['collection_name']))
                retry = False
            except Exception as e:
                if i < 5:
                    time.sleep(60)
                    i += 1
                else:
                    print("Could not check Model Tracker for existing model at this time because of exception %s" % e)
                    return False
        if models:
            print("Already created model for this param combo")
            return True
        return False

    def generate_combo(self, params_dict):
        """
        This is implemented in the specific sub-classes

        """
        raise NotImplementedError

    def run_search(self):
        """
        The driver code for generating hyperparameter combinations and submitting jobs
        Returns:

        """
        self.generate_param_combos()
        self.generate_assay_list()
        self.submit_jobs()


class GridSearch(HyperparameterSearch):
    """
    Generates fixed steps on a grid for a given hyperparameter range
    """

    def __init__(self, params):
        super().__init__(params)

    def split_and_save_dataset(self, assay_params):
        self.split_and_save_dataset(assay_params)

    def generate_param_combos(self):
        super().generate_param_combos()

    def generate_assay_list(self):
        super().generate_assay_list()

    def submit_jobs(self):
        super().submit_jobs()

    def generate_combo(self, params_dict):
        """
        Method to generate all combinations from a given set of key-value pairs
        
        Args:
            params_dict: Set of key-value pairs with the key being the param name and the value being the list of values
            you want to try for that param
        
        Returns:
            new_dict: The list of all combinations of parameters
        """
        if not params_dict:
            return None

        new_dict = {}
        for key, value in params_dict.items():
            assert isinstance(value, collections.Iterable)
            if key == 'layers':
                new_dict[key] = value
            elif type(value[0]) != str:
                tmp_list = list(np.linspace(value[0], value[1], value[2]))
                if key in self.convert_to_int:
                    new_dict[key] = [int(x) for x in tmp_list]
                else:
                    new_dict[key] = tmp_list
            else:
                new_dict[key] = value
        return new_dict


class RandomSearch(HyperparameterSearch):
    """
    Generates the specified number of random parameter values for within the specified range
    """

    def __init__(self, params):
        super().__init__(params)

    def split_and_save_dataset(self, assay_params):
        self.split_and_save_dataset(assay_params)

    def generate_param_combos(self):
        super().generate_param_combos()

    def generate_assay_list(self):
        super().generate_assay_list()

    def submit_jobs(self):
        super().submit_jobs()

    def generate_combo(self, params_dict):
        """
        Method to generate all combinations from a given set of key-value pairs

        Args:
            params_dict: Set of key-value pairs with the key being the param name and the value being the list of values
            you want to try for that param

        Returns:
            new_dict: The list of all combinations of parameters
        """
        if not params_dict:
            return None
        new_dict = {}
        for key, value in params_dict.items():
            assert isinstance(value, collections.Iterable)
            if key == 'layers':
                new_dict[key] = value
            elif type(value[0]) != str:
                tmp_list = list(np.random.uniform(value[0], value[1], value[2]))
                if key in self.convert_to_int:
                    new_dict[key] = [int(x) for x in tmp_list]
                else:
                    new_dict[key] = tmp_list
            else:
                new_dict[key] = value
        return new_dict


class GeometricSearch(HyperparameterSearch):
    """
    Generates parameter values in logistic steps, rather than linear like GridSearch does
    """
    
    def __init__(self, params):
        super().__init__(params)

    def split_and_save_dataset(self, assay_params):
        self.split_and_save_dataset(assay_params)

    def generate_param_combos(self):
        super().generate_param_combos()

    def generate_assay_list(self):
        super().generate_assay_list()

    def submit_jobs(self):
        super().submit_jobs()

    def generate_combo(self, params_dict):
        """
        Method to generate all combinations from a given set of key-value pairs
        
        Args:
            params_dict: Set of key-value pairs with the key being the param name and the value being the list of values
            you want to try for that param
        
        Returns:
            new_dict: The list of all combinations of parameters
        """
        if not params_dict:
            return None

        new_dict = {}
        for key, value in params_dict.items():
            assert isinstance(value, collections.Iterable)
            if key == 'layers':
                new_dict[key] = value
            elif type(value[0]) != str:
                tmp_list = list(np.geomspace(value[0], value[1], value[2]))
                if key in self.convert_to_int:
                    new_dict[key] = [int(x) for x in tmp_list]
                else:
                    new_dict[key] = tmp_list
            else:
                new_dict[key] = value
        return new_dict

class UserSpecifiedSearch(HyperparameterSearch):
    """
    Generates combinations using the user-specified steps
    """
    
    def __init__(self, params):
        super().__init__(params)

    def split_and_save_dataset(self, assay_params):
        self.split_and_save_dataset(assay_params)

    def generate_param_combos(self):
        super().generate_param_combos()

    def generate_assay_list(self):
        super().generate_assay_list()

    def submit_jobs(self):
        super().submit_jobs()

    def generate_combo(self, params_dict):
        """
        Method to generate all combinations from a given set of key-value pairs
        
        Args:
            params_dict: Set of key-value pairs with the key being the param name and the value being the list of values
            you want to try for that param
        
        Returns:
            new_dict: The list of all combinations of parameters
        """
        
        if not params_dict:
            return None
        new_dict = {}
        for key, value in params_dict.items():
            assert isinstance(value, collections.Iterable)
            if key == 'layers':
                new_dict[key] = value
            elif key in self.convert_to_int:
                new_dict[key] = [int(x) for x in value]
            elif key in self.convert_to_float:
                new_dict[key] = [float(x) for x in value]
            else:
                new_dict[key] = value
        return new_dict


def main():
    """Entry point when script is run"""
    print(sys.argv[1:])
    params = parse.wrapper(sys.argv[1:])
    keep_params = {'model_type',
                   'featurizer',
                   'splitter',
                   'datastore',
                   'previously_featurized',
                   'descriptor_key',
                   'descriptor_type',
                   'split_valid_frac',
                   'split_test_frac',
                   'bucket',
                   'lc_account',
                   'slurm_partition'} | excluded_keys
    params.__dict__ = parse.prune_defaults(params, keep_params=keep_params)
    if params.search_type == 'grid':
        hs = GridSearch(params)
    elif params.search_type == 'random':
        hs = RandomSearch(params)
    elif params.search_type == 'geometric':
        hs = GeometricSearch(params)
    elif params.search_type == 'user_specified':
        hs = UserSpecifiedSearch(params)
    else:
        print("Incorrect search type specified")
        sys.exit(1)
    if params.split_only:
        hs.generate_split_shortlist()
    else:
        hs.run_search()

if __name__ == '__main__' and len(sys.argv) > 1:
    main()
    sys.exit(0)
