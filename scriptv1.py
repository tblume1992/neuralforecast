from neuralforecast.auto import AutoNHITS, AutoLSTM, AutoTFT
from neuralforecast.core import NeuralForecast
from datasetsforecast.m4 import M4
from datasetsforecast.m3 import M3
from neuralforecast.utils import AirPassengersDF
from ray import tune

import argparse
from neuralforecast.losses.pytorch import MAE
import pandas as pd
import os

"""
Pipeline:
 	1. Read source dataset using datasetsforecast (https://github.com/Nixtla/datasetsforecast).
        Specified with `source_dataset` paramater in script.
 	2. Fit Auto model on source dataset. Model specified with `model` argument.
 	3. Save model, using folder './results/stored_models/{dataset}/{model}/{experiment_id}/'.
 	4. Read target dataset using datasetsforecast. Specified with `target_dataset` argument in script.
 	5. Load model, predict on target dataset, store forecasts in './results/forecasts/{target_dataset}/{model_source_dataset_experiment_id}.csv'.
Script arguments:
	1. source_dataset
	2. target_dataset
	3. model
	4. experiment_id
------------------------------------------
Notes:
1. Use Transfer Learning tutorial notebook as starting point.
2. Use argparse (https://github.com/cchallu/n-hits/blob/main/nhits_multivariate.py)
3. Use dictionaries to select between models. First list: AutoNHITS, AutoLSTM, AutoTFT
	MODEL_DICT={'name_1': AutoNHITS, ..., 'name_n':model_n}.
	model = MODEL_DICT[args.model_name]
4. For first example define source datasets as: M3 or M4.
5. For target dataset use AirPassengers.
6. For using Auto models: https://nixtla.github.io/neuralforecast/examples/forecasting_tft.html
 ------------------------------------------
 Next steps:
 	1. k-shot learning
 	2. evaluation scripts
 	3. more datasets
"""

# GLOBAL parameters
horizon = 18
loss = MAE()
num_samples = 10  # how many configuration we try during tuning
config = None

nhits = [AutoNHITS(h=horizon,
				loss=loss, num_samples=num_samples,
				config={
					"input_size": tune.choice([1*horizon, 2*horizon]),
					"stack_types": tune.choice([3*['identity']]),
					"mlp_units": tune.choice([3 * [[512, 512]]]),
					"n_blocks": tune.choice([3*[5]]),
					"n_pool_kernel_size": tune.choice([3*[1], 3*[2], 3*[4],
													  [8, 4, 1], [16, 8, 1]]),
					"n_freq_downsample": tune.choice([[168, 24, 1], [24, 12, 1],
													  [180, 60, 1], [60, 8, 1],
													  [40, 20, 1], [1, 1, 1]]),
					"learning_rate": tune.loguniform(1e-4, 1e-1),
					"early_stop_patience_steps": tune.choice([5]),
					"val_check_steps": tune.choice([100]),
					"scaler_type": tune.choice(['robust']),
					"max_steps": tune.choice([5000, 10000]),
					"batch_size": tune.choice([128, 256]),
					"windows_batch_size": tune.choice([128, 512, 1024]),
					"random_seed": tune.randint(1, 20),
				})]

lstm = [AutoLSTM(h=horizon,loss=loss,config=config,num_samples=num_samples)]

tft = [AutoTFT(hh=horizon,
				loss=loss, num_samples=num_samples,
				config={
					"input_size": tune.choice([1*horizon, 2*horizon]),
					"hidden_size": tune.choice([64, 128, 256]),
					"learning_rate": tune.loguniform(1e-4, 1e-1),
					"early_stop_patience_steps": tune.choice([5]),
					"val_check_steps": tune.choice([100]),
					"scaler_type": tune.choice(['robust']),
					"max_steps": tune.choice([5000, 10000]),
					"batch_size": tune.choice([128, 256]),
					"windows_batch_size": tune.choice([128, 512, 1024]),
					"random_seed": tune.randint(1, 20),
				})]

MODEL_DICT = {'autonhits': nhits, 'autolstm': lstm, 'autotft': tft}

def main(args):

	# make sure folder exists, then check if the file exists in the folder
	model_dir = f'./results/stored_models/{args.source_dataset}/{args.model}/{args.experiment_id}/'
	os.makedirs(model_dir, exist_ok=True)
	file_exists = os.path.isfile(
		f'./results/stored_models/{args.source_dataset}/{args.model}/{args.experiment_id}/{args.model}_0.ckpt')
	
	if (not file_exists):
		# Read source data
		if (args.source_dataset == 'M4'): # add more if conditions later, expects M4 only for now
			Y_df, a, b = M4.load(directory='./', group='Monthly', cache=True)
			frequency = 'M'
		else:
			raise Exception("Dataset not defined")
		Y_df['ds'] = pd.to_datetime(Y_df['ds'])

		# Train model
		model = MODEL_DICT[args.model]
		if model is None: raise Exception("Model not defined")
		
		# frequency = sampling rate of data
		nf = NeuralForecast(models=model,freq=frequency)
		nf.fit(df=Y_df)
		
		# Save model
		nf.save(path=f'./results/stored_models/{args.source_dataset}/{args.model}/{args.experiment_id}/',
			overwrite=False, save_dataset=False)
	else:
		print('Hyperparameter optimization already done. Loading saved model!')
		# do i need to check if the file/path exists? shouldn't it already be checked
		nf = NeuralForecast.load(path=
			  f'./results/stored_models/{args.source_dataset}/{args.model}/{args.experiment_id}/')
		
	# Load target data
	if (args.target_dataset == 'AirPassengers'):
		Y_df_target = AirPassengersDF.copy()
		test_size = horizon*4
		frequency = 'M'
	elif (args.target_dataset == 'M3'):
		Y_df_target, *_ = M3.load(directory='./', group='Monthly')
		frequency = 'M'
		test_size = horizon*4
	else:
		raise Exception("Dataset not defined")
	Y_df_target['ds'] = pd.to_datetime(Y_df_target['ds'])

	# Predict on the test set of the target data
	Y_hat_df = nf.cross_validation(df=Y_df_target,
								   n_windows=None, test_size=test_size,
								   fit_models=False).reset_index()
	
	results_dir = f'./results/forecasts/{args.target_dataset}/'
	os.makedirs(results_dir, exist_ok=True)

	# store results, also check if this folder exists/create it if its done
	Y_hat_df.to_csv(f'{results_dir}/{args.model}_{args.source_dataset}_{args.experiment_id}.csv',
		 index=False)

def parse_args():
    parser = argparse.ArgumentParser(description="script arguments")
    parser.add_argument('--source_dataset', type=str, help='dataset to train models on')
    parser.add_argument('--target_dataset', type=str, help='run model on this dataset')
    parser.add_argument('--model', type=str, help='auto model to use')
    parser.add_argument('--experiment_id', type=str, help='identify experiment')
    return parser.parse_args()

if __name__ == '__main__':
    # parse arguments
    args = parse_args()
    if args is None:
        exit()
    main(args)

# CUDA_VISIBLE_DEVICES=3 python scriptv1.py --source_dataset "M4" --target_dataset "M3" --model "autonhits" --experiment_id "20230422"
# CUDA_VISIBLE_DEVICES=3 python scriptv1.py --source_dataset "M4" --target_dataset "M3" --model "autotft" --experiment_id "20230422"