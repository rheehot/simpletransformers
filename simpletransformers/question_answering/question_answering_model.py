from __future__ import absolute_import, division, print_function

import os
import math
import json
import logging
import random
import warnings

from multiprocessing import cpu_count

import torch
import numpy as np
import pandas as pd

from scipy.stats import pearsonr
from sklearn.metrics import (
    mean_squared_error,
    matthews_corrcoef,
    confusion_matrix,
    label_ranking_average_precision_score,
)
from tensorboardX import SummaryWriter
from tqdm.auto import trange, tqdm

from torch.utils.data.distributed import DistributedSampler
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler, TensorDataset

from transformers import AdamW, get_linear_schedule_with_warmup
from transformers import (
    WEIGHTS_NAME,
    BertConfig,
    BertForQuestionAnswering,
    BertTokenizer,
    XLMConfig,
    XLMForQuestionAnswering,
    XLMTokenizer,
    XLNetConfig,
    XLNetForQuestionAnswering,
    XLNetTokenizer,
    DistilBertConfig,
    DistilBertForQuestionAnswering,
    DistilBertTokenizer,
    AlbertConfig,
    AlbertForQuestionAnswering,
    AlbertTokenizer,
)

from simpletransformers.question_answering.question_answering_utils import (
    get_examples,
    convert_examples_to_features,
    RawResult,
    write_predictions,
    RawResultExtended,
    write_predictions_extended,
    to_list,
    build_examples,
    get_best_predictions,
    get_best_predictions_extended,
)
from simpletransformers.config.global_args import global_args

try:
    import wandb

    wandb_available = True
except ImportError:
    wandb_available = False


logger = logging.getLogger(__name__)


class QuestionAnsweringModel:
    def __init__(self, model_type, model_name, args=None, use_cuda=True, cuda_device=-1, **kwargs):

        """
        Initializes a QuestionAnsweringModel model.

        Args:
            model_type: The type of model (bert, xlnet, xlm, distilbert)
            model_name: Default Transformer model name or path to a directory containing Transformer model file (pytorch_nodel.bin).
            args (optional): Default args will be used if this parameter is not provided. If provided, it should be a dict containing the args that should be changed in the default args['
            use_cuda (optional): Use GPU if available. Setting to False will force model to use CPU only.
            cuda_device (optional): Specific GPU that should be used. Will use the first available GPU by default.
        """  # noqa: ignore flake8"

        MODEL_CLASSES = {
            "bert": (BertConfig, BertForQuestionAnswering, BertTokenizer),
            "xlnet": (XLNetConfig, XLNetForQuestionAnswering, XLNetTokenizer),
            "xlm": (XLMConfig, XLMForQuestionAnswering, XLMTokenizer),
            "distilbert": (DistilBertConfig, DistilBertForQuestionAnswering, DistilBertTokenizer,),
            "albert": (AlbertConfig, AlbertForQuestionAnswering, AlbertTokenizer),
        }

        if args and "manual_seed" in args:
            random.seed(args["manual_seed"])
            np.random.seed(args["manual_seed"])
            torch.manual_seed(args["manual_seed"])
            if "n_gpu" in args and args["n_gpu"] > 0:
                torch.cuda.manual_seed_all(args["manual_seed"])

        config_class, model_class, tokenizer_class = MODEL_CLASSES[model_type]
        self.model = model_class.from_pretrained(model_name, **kwargs)

        if use_cuda:
            if torch.cuda.is_available():
                if cuda_device == -1:
                    self.device = torch.device("cuda")
                else:
                    self.device = torch.device(f"cuda:{cuda_device}")
            else:
                raise ValueError(
                    "'use_cuda' set to True when cuda is unavailable."
                    " Make sure CUDA is available or set use_cuda=False."
                )
        else:
            self.device = "cpu"

        self.results = {}

        self.args = {
            "doc_stride": 384,
            "max_query_length": 64,
            "n_best_size": 20,
            "max_answer_length": 100,
            "null_score_diff_threshold": 0.0,
            "wandb_project": False,
            "wandb_kwargs": {},
        }

        self.args.update(global_args)
        self.args.update({
            "early_stopping_metric": "correct",
            "early_stopping_metric_minimize": False,
        })

        if not use_cuda:
            self.args["fp16"] = False

        if args:
            self.args.update(args)

        self.tokenizer = tokenizer_class.from_pretrained(model_name, do_lower_case=self.args["do_lower_case"], **kwargs)

        self.args["model_name"] = model_name
        self.args["model_type"] = model_type

        if self.args["wandb_project"] and not wandb_available:
            warnings.warn("wandb_project specified but wandb is not available. Wandb disabled.")
            self.args["wandb_project"] = None

    def load_and_cache_examples(self, examples, evaluate=False, no_cache=False, output_examples=False):
        """
        Converts a list of examples to a TensorDataset containing InputFeatures. Caches the InputFeatures.

        Utility function for train() and eval() methods. Not intended to be used directly.
        """

        tokenizer = self.tokenizer
        args = self.args

        if not no_cache:
            no_cache = args["no_cache"]

        os.makedirs(self.args["cache_dir"], exist_ok=True)

        examples = get_examples(examples, is_training=not evaluate)

        mode = "dev" if evaluate else "train"
        cached_features_file = os.path.join(
            args["cache_dir"],
            "cached_{}_{}_{}_{}".format(mode, args["model_type"], args["max_seq_length"], len(examples)),
        )

        if os.path.exists(cached_features_file) and (
            (not args["reprocess_input_data"] and not no_cache) or (mode == "dev" and args["use_cached_eval_features"])
        ):
            features = torch.load(cached_features_file)
            logger.info(f" Features loaded from cache at {cached_features_file}")
        else:
            logger.info(f" Converting to features started.")
            features = convert_examples_to_features(
                examples=examples,
                tokenizer=tokenizer,
                max_seq_length=args["max_seq_length"],
                doc_stride=args["doc_stride"],
                max_query_length=args["max_query_length"],
                is_training=not evaluate,
                cls_token_segment_id=2 if args["model_type"] in ["xlnet"] else 0,
                pad_token_segment_id=3 if args["model_type"] in ["xlnet"] else 0,
                cls_token_at_end=True if args["model_type"] in ["xlnet"] else False,
                sequence_a_is_doc=True if args["model_type"] in ["xlnet"] else False,
                silent=args["silent"],
            )

            if not no_cache:
                torch.save(features, cached_features_file)

        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        all_cls_index = torch.tensor([f.cls_index for f in features], dtype=torch.long)
        all_p_mask = torch.tensor([f.p_mask for f in features], dtype=torch.float)
        all_example_index = torch.arange(all_input_ids.size(0), dtype=torch.long)
        if evaluate:
            dataset = TensorDataset(
                all_input_ids, all_input_mask, all_segment_ids, all_example_index, all_cls_index, all_p_mask,
            )
        else:
            all_start_positions = torch.tensor([f.start_position for f in features], dtype=torch.long)
            all_end_positions = torch.tensor([f.end_position for f in features], dtype=torch.long)
            dataset = TensorDataset(
                all_input_ids,
                all_input_mask,
                all_segment_ids,
                all_start_positions,
                all_end_positions,
                all_cls_index,
                all_p_mask,
            )

        if output_examples:
            return dataset, examples, features
        return dataset

    def train_model(
        self, train_data, output_dir=False, show_running_loss=True, args=None, eval_data=None, verbose=True
    ):
        """
        Trains the model using 'train_data'

        Args:
            train_data: Path to JSON file containing training data OR list of Python dicts in the correct format. The model will be trained on this data.
            output_dir: The directory where model files will be saved. If not given, self.args['output_dir'] will be used.
            show_running_loss (optional): Set to False to prevent running loss from being printed to console. Defaults to True.
            args (optional): Optional changes to the args dict of the model. Any changes made will persist for the model.
            eval_data (optional): Path to JSON file containing evaluation data against which evaluation will be performed when evaluate_during_training is enabled. Is required if evaluate_during_training is enabled.
        Returns:
            None
        """  # noqa: ignore flake8"

        if args:
            self.args.update(args)

        if self.args["silent"]:
            show_running_loss = False

        if self.args["evaluate_during_training"] and eval_data is None:
            raise ValueError(
                "evaluate_during_training is enabled but eval_data is not specified."
                " Pass eval_data to model.train_model() if using evaluate_during_training."
            )

        if not output_dir:
            output_dir = self.args["output_dir"]

        if os.path.exists(output_dir) and os.listdir(output_dir) and not self.args["overwrite_output_dir"]:
            raise ValueError(
                "Output directory ({}) already exists and is not empty."
                "Use --overwrite_output_dir to overcome.".format(output_dir)
            )

        self._move_model_to_device()

        if isinstance(train_data, str):
            with open(train_data, "r", encoding=self.args["encoding"]) as f:
                train_examples = json.load(f)
        else:
            train_examples = train_data

        train_dataset = self.load_and_cache_examples(train_examples)

        os.makedirs(output_dir, exist_ok=True)

        global_step, tr_loss = self.train(
            train_dataset, output_dir, show_running_loss=show_running_loss, eval_data=eval_data,
        )

        model_to_save = self.model.module if hasattr(self.model, "module") else self.model
        model_to_save.save_pretrained(output_dir)
        self.tokenizer.save_pretrained(output_dir)
        torch.save(self.args, os.path.join(output_dir, "training_args.bin"))

        logger.info(" Training of {} model complete. Saved to {}.".format(self.args["model_type"], output_dir))

    def train(self, train_dataset, output_dir, show_running_loss=True, eval_data=None, verbose=True):
        """
        Trains the model on train_dataset.

        Utility function to be used by the train_model() method. Not intended to be used directly.
        """

        device = self.device
        model = self.model
        args = self.args

        tb_writer = SummaryWriter(logdir=args["tensorboard_dir"])
        train_sampler = RandomSampler(train_dataset)
        train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args["train_batch_size"])

        t_total = len(train_dataloader) // args["gradient_accumulation_steps"] * args["num_train_epochs"]

        no_decay = ["bias", "LayerNorm.weight"]
        optimizer_grouped_parameters = [
            {
                "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
                "weight_decay": args["weight_decay"],
            },
            {
                "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)],
                "weight_decay": 0.0,
            },
        ]

        warmup_steps = math.ceil(t_total * args["warmup_ratio"])
        args["warmup_steps"] = warmup_steps if args["warmup_steps"] == 0 else args["warmup_steps"]

        optimizer = AdamW(optimizer_grouped_parameters, lr=args["learning_rate"], eps=args["adam_epsilon"],)
        scheduler = get_linear_schedule_with_warmup(
            optimizer, num_warmup_steps=args["warmup_steps"], num_training_steps=t_total
        )

        if args["fp16"]:
            try:
                from apex import amp
            except ImportError:
                raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")

            model, optimizer = amp.initialize(model, optimizer, opt_level=args["fp16_opt_level"])

        if args["n_gpu"] > 1:
            model = torch.nn.DataParallel(model)

        global_step = 0
        tr_loss, logging_loss = 0.0, 0.0
        model.zero_grad()
        train_iterator = trange(int(args["num_train_epochs"]), desc="Epoch", disable=args["silent"])
        epoch_number = 0
        best_eval_metric = None
        early_stopping_counter = 0

        if args["evaluate_during_training"]:
            training_progress_scores = self._create_training_progress_scores()

        if args["wandb_project"]:
            wandb.init(project=args["wandb_project"], config={**args}, **args["wandb_kwargs"])
            wandb.watch(self.model)

        model.train()
        for _ in train_iterator:
            # epoch_iterator = tqdm(train_dataloader, desc="Iteration")
            for step, batch in enumerate(tqdm(train_dataloader, desc="Current iteration", disable=args["silent"])):
                batch = tuple(t.to(device) for t in batch)

                inputs = self._get_inputs_dict(batch)

                outputs = model(**inputs)
                # model outputs are always tuple in pytorch-transformers (see doc)
                loss = outputs[0]

                if args["n_gpu"] > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu parallel training

                current_loss = loss.item()

                if show_running_loss:
                    print("\rRunning loss: %f" % loss, end="")

                if args["gradient_accumulation_steps"] > 1:
                    loss = loss / args["gradient_accumulation_steps"]

                if args["fp16"]:
                    with amp.scale_loss(loss, optimizer) as scaled_loss:
                        scaled_loss.backward()
                    # torch.nn.utils.clip_grad_norm_(
                    #     amp.master_params(optimizer), args["max_grad_norm"]
                    # )
                else:
                    loss.backward()
                    # torch.nn.utils.clip_grad_norm_(
                    #     model.parameters(), args["max_grad_norm"]
                    # )

                tr_loss += loss.item()
                if (step + 1) % args["gradient_accumulation_steps"] == 0:
                    if args["fp16"]:
                        torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args["max_grad_norm"])
                    else:
                        torch.nn.utils.clip_grad_norm_(model.parameters(), args["max_grad_norm"])

                    optimizer.step()
                    scheduler.step()  # Update learning rate schedule
                    model.zero_grad()
                    global_step += 1

                    if args["logging_steps"] > 0 and global_step % args["logging_steps"] == 0:
                        # Log metrics
                        tb_writer.add_scalar("lr", scheduler.get_lr()[0], global_step)
                        tb_writer.add_scalar(
                            "loss", (tr_loss - logging_loss) / args["logging_steps"], global_step,
                        )
                        logging_loss = tr_loss
                        if args["wandb_project"]:
                            wandb.log(
                                {
                                    "Training loss": current_loss,
                                    "lr": scheduler.get_lr()[0],
                                    "global_step": global_step,
                                }
                            )

                    if args["save_steps"] > 0 and global_step % args["save_steps"] == 0:
                        # Save model checkpoint
                        output_dir_current = os.path.join(output_dir, "checkpoint-{}".format(global_step))

                        self._save_model(output_dir_current, model=model)

                    if args["evaluate_during_training"] and (
                        args["evaluate_during_training_steps"] > 0
                        and global_step % args["evaluate_during_training_steps"] == 0
                    ):
                        # Only evaluate when single GPU otherwise metrics may not average well
                        results, _ = self.eval_model(eval_data, verbose=True)
                        for key, value in results.items():
                            tb_writer.add_scalar("eval_{}".format(key), value, global_step)

                        output_dir_current = os.path.join(output_dir, "checkpoint-{}".format(global_step))

                        if args["save_eval_checkpoints"]:
                            self._save_model(output_dir_current, model=model, results=results)

                        training_progress_scores["global_step"].append(global_step)
                        training_progress_scores["train_loss"].append(current_loss)
                        for key in results:
                            training_progress_scores[key].append(results[key])
                        report = pd.DataFrame(training_progress_scores)
                        report.to_csv(
                            os.path.join(args["output_dir"], "training_progress_scores.csv"), index=False,
                        )

                        if args["wandb_project"]:
                            wandb.log(self._get_last_metrics(training_progress_scores))

                        if not best_eval_metric:
                            best_eval_metric = results[args["early_stopping_metric"]]
                            self._save_model(args["best_model_dir"], model=model, results=results)
                        if best_eval_metric and args["early_stopping_metric_minimize"]:
                            if results[args["early_stopping_metric"]] - best_eval_metric < args["early_stopping_delta"]:
                                best_eval_metric = results[args["early_stopping_metric"]]
                                self._save_model(args["best_model_dir"], model=model, results=results)
                                early_stopping_counter = 0
                            else:
                                if args["use_early_stopping"]:
                                    if early_stopping_counter < args["early_stopping_patience"]:
                                        early_stopping_counter += 1
                                        if verbose:
                                            logger.info(f" No improvement in {args['early_stopping_metric']}")
                                            logger.info(f" Current step: {early_stopping_counter}")
                                            logger.info(f" Early stopping patience: {args['early_stopping_patience']}")
                                    else:
                                        if verbose:
                                            logger.info(f" Patience of {args['early_stopping_patience']} steps reached")
                                            logger.info(" Training terminated.")
                                            train_iterator.close()
                                        return global_step, tr_loss / global_step
                        else:
                            if results[args["early_stopping_metric"]] - best_eval_metric > args["early_stopping_delta"]:
                                best_eval_metric = results[args["early_stopping_metric"]]
                                self._save_model(args["best_model_dir"], model=model, results=results)
                                early_stopping_counter = 0
                            else:
                                if args["use_early_stopping"]:
                                    if early_stopping_counter < args["early_stopping_patience"]:
                                        early_stopping_counter += 1
                                        if verbose:
                                            logger.info(f" No improvement in {args['early_stopping_metric']}")
                                            logger.info(f" Current step: {early_stopping_counter}")
                                            logger.info(f" Early stopping patience: {args['early_stopping_patience']}")
                                    else:
                                        if verbose:
                                            logger.info(f" Patience of {args['early_stopping_patience']} steps reached")
                                            logger.info(" Training terminated.")
                                            train_iterator.close()
                                        return global_step, tr_loss / global_step

            epoch_number += 1
            output_dir_current = os.path.join(output_dir, "checkpoint-{}-epoch-{}".format(global_step, epoch_number))

            if args["save_model_every_epoch"] or args["evaluate_during_training"]:
                os.makedirs(output_dir_current, exist_ok=True)

            if args["save_model_every_epoch"]:
                self._save_model(output_dir_current, model=model)

            if args["evaluate_during_training"]:
                results, _ = self.eval_model(eval_data, verbose=True)

                self._save_model(output_dir_current, results=results)

                training_progress_scores["global_step"].append(global_step)
                training_progress_scores["train_loss"].append(current_loss)
                for key in results:
                    training_progress_scores[key].append(results[key])
                report = pd.DataFrame(training_progress_scores)
                report.to_csv(os.path.join(args["output_dir"], "training_progress_scores.csv"), index=False)

                if not best_eval_metric:
                    best_eval_metric = results[args["early_stopping_metric"]]
                    self._save_model(args["best_model_dir"], model=model, results=results)
                if best_eval_metric and args["early_stopping_metric_minimize"]:
                    if results[args["early_stopping_metric"]] - best_eval_metric < args["early_stopping_delta"]:
                        best_eval_metric = results[args["early_stopping_metric"]]
                        self._save_model(args["best_model_dir"], model=model, results=results)
                        early_stopping_counter = 0
                else:
                    if results[args["early_stopping_metric"]] - best_eval_metric > args["early_stopping_delta"]:
                        best_eval_metric = results[args["early_stopping_metric"]]
                        self._save_model(args["best_model_dir"], model=model, results=results)
                        early_stopping_counter = 0

        return global_step, tr_loss / global_step

    def eval_model(self, eval_data, output_dir=None, verbose=False):
        """
        Evaluates the model on eval_data. Saves results to output_dir.

        Args:
            eval_data: Path to JSON file containing evaluation data OR list of Python dicts in the correct format. The model will be evaluated on this data.
            output_dir: The directory where model files will be saved. If not given, self.args['output_dir'] will be used.
            verbose: If verbose, results will be printed to the console on completion of evaluation.

        Returns:
            result: Dictionary containing evaluation results. (correct, similar, incorrect)
            text: A dictionary containing the 3 dictionaries correct_text, similar_text (the predicted answer is a substring of the correct answer or vise versa), incorrect_text.
        """  # noqa: ignore flake8"

        if not output_dir:
            output_dir = self.args["output_dir"]

        self._move_model_to_device()

        all_predictions, all_nbest_json, scores_diff_json = self.evaluate(eval_data, output_dir)

        if isinstance(eval_data, str):
            with open(eval_data, "r", encoding=self.args["encoding"]) as f:
                truth = json.load(f)
        else:
            truth = eval_data

        result, texts = self.calculate_results(truth, all_predictions)

        self.results.update(result)

        if verbose:
            logger.info(self.results)

        return result, texts

    def evaluate(self, eval_data, output_dir):
        """
        Evaluates the model on eval_data.

        Utility function to be used by the eval_model() method. Not intended to be used directly.
        """
        tokenizer = self.tokenizer
        device = self.device
        model = self.model
        args = self.args

        if isinstance(eval_data, str):
            with open(eval_data, "r", encoding=self.args["encoding"]) as f:
                eval_examples = json.load(f)
        else:
            eval_examples = eval_data

        eval_dataset, examples, features = self.load_and_cache_examples(
            eval_examples, evaluate=True, output_examples=True
        )

        eval_sampler = SequentialSampler(eval_dataset)
        eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args["eval_batch_size"])

        model.eval()

        all_results = []
        for batch in tqdm(eval_dataloader, disable=args["silent"]):
            batch = tuple(t.to(device) for t in batch)

            with torch.no_grad():
                inputs = {
                    "input_ids": batch[0],
                    "attention_mask": batch[1],
                }

                if args["model_type"] != "distilbert":
                    inputs["token_type_ids"] = None if args["model_type"] == "xlm" else batch[2]

                example_indices = batch[3]

                if args["model_type"] in ["xlnet", "xlm"]:
                    inputs.update({"cls_index": batch[4], "p_mask": batch[5]})

                outputs = model(**inputs)

                for i, example_index in enumerate(example_indices):
                    eval_feature = features[example_index.item()]
                    unique_id = int(eval_feature.unique_id)
                    if args["model_type"] in ["xlnet", "xlm"]:
                        # XLNet uses a more complex post-processing procedure
                        result = RawResultExtended(
                            unique_id=unique_id,
                            start_top_log_probs=to_list(outputs[0][i]),
                            start_top_index=to_list(outputs[1][i]),
                            end_top_log_probs=to_list(outputs[2][i]),
                            end_top_index=to_list(outputs[3][i]),
                            cls_logits=to_list(outputs[4][i]),
                        )
                    else:
                        result = RawResult(
                            unique_id=unique_id, start_logits=to_list(outputs[0][i]), end_logits=to_list(outputs[1][i]),
                        )
                    all_results.append(result)

        prefix = "test"
        os.makedirs(output_dir, exist_ok=True)

        output_prediction_file = os.path.join(output_dir, "predictions_{}.json".format(prefix))
        output_nbest_file = os.path.join(output_dir, "nbest_predictions_{}.json".format(prefix))
        output_null_log_odds_file = os.path.join(output_dir, "null_odds_{}.json".format(prefix))

        if args["model_type"] in ["xlnet", "xlm"]:
            # XLNet uses a more complex post-processing procedure
            (all_predictions, all_nbest_json, scores_diff_json,) = write_predictions_extended(
                examples,
                features,
                all_results,
                args["n_best_size"],
                args["max_answer_length"],
                output_prediction_file,
                output_nbest_file,
                output_null_log_odds_file,
                eval_data,
                model.config.start_n_top,
                model.config.end_n_top,
                True,
                tokenizer,
                not args["silent"],
            )
        else:
            all_predictions, all_nbest_json, scores_diff_json = write_predictions(
                examples,
                features,
                all_results,
                args["n_best_size"],
                args["max_answer_length"],
                False,
                output_prediction_file,
                output_nbest_file,
                output_null_log_odds_file,
                not args["silent"],
                True,
                args["null_score_diff_threshold"],
            )

        return all_predictions, all_nbest_json, scores_diff_json

    def predict(self, to_predict, n_best_size=None):
        """
        Performs predictions on a list of python dicts containing contexts and qas.

        Args:
            to_predict: A python list of python dicts containing contexts and questions to be sent to the model for prediction.
                        E.g: predict([
                            {
                                'context': "Some context as a demo",
                                'qas': [
                                    {'id': '0', 'question': 'What is the context here?'},
                                    {'id': '1', 'question': 'What is this for?'}
                                ]
                            }
                        ])
            n_best_size (Optional): Number of predictions to return. args['n_best_size'] will be used if not specified.

        Returns:
            preds: A python list containg the predicted answer, and id for each question in to_predict.
        """  # noqa: ignore flake8"
        tokenizer = self.tokenizer
        device = self.device
        model = self.model
        args = self.args

        if not n_best_size:
            n_best_size = args["n_best_size"]

        self._move_model_to_device()

        eval_examples = build_examples(to_predict)
        eval_dataset, examples, features = self.load_and_cache_examples(
            eval_examples, evaluate=True, output_examples=True, no_cache=True
        )

        eval_sampler = SequentialSampler(eval_dataset)
        eval_dataloader = DataLoader(eval_dataset, sampler=eval_sampler, batch_size=args["eval_batch_size"])

        model.eval()

        all_results = []
        for batch in tqdm(eval_dataloader, disable=args["silent"]):
            batch = tuple(t.to(device) for t in batch)

            with torch.no_grad():
                inputs = {
                    "input_ids": batch[0],
                    "attention_mask": batch[1],
                }

                if args["model_type"] != "distilbert":
                    inputs["token_type_ids"] = None if args["model_type"] == "xlm" else batch[2]

                example_indices = batch[3]

                if args["model_type"] in ["xlnet", "xlm"]:
                    inputs.update({"cls_index": batch[4], "p_mask": batch[5]})

                outputs = model(**inputs)

                for i, example_index in enumerate(example_indices):
                    eval_feature = features[example_index.item()]
                    unique_id = int(eval_feature.unique_id)
                    if args["model_type"] in ["xlnet", "xlm"]:
                        # XLNet uses a more complex post-processing procedure
                        result = RawResultExtended(
                            unique_id=unique_id,
                            start_top_log_probs=to_list(outputs[0][i]),
                            start_top_index=to_list(outputs[1][i]),
                            end_top_log_probs=to_list(outputs[2][i]),
                            end_top_index=to_list(outputs[3][i]),
                            cls_logits=to_list(outputs[4][i]),
                        )
                    else:
                        result = RawResult(
                            unique_id=unique_id, start_logits=to_list(outputs[0][i]), end_logits=to_list(outputs[1][i]),
                        )
                    all_results.append(result)

        if args["model_type"] in ["xlnet", "xlm"]:
            answers = get_best_predictions_extended(
                examples,
                features,
                all_results,
                n_best_size,
                args["max_answer_length"],
                model.config.start_n_top,
                model.config.end_n_top,
                True,
                tokenizer,
                args["null_score_diff_threshold"],
            )
        else:
            answers = get_best_predictions(
                examples, features, all_results, n_best_size, args["max_answer_length"], False, False, True, False,
            )

        return answers

    def calculate_results(self, truth, predictions):
        truth_dict = {}
        questions_dict = {}
        logger.info(truth)
        for item in truth:
            for answer in item["qas"]:
                if answer["answers"]:
                    truth_dict[answer["id"]] = answer["answers"][0]["text"]
                else:
                    truth_dict[answer["id"]] = ""
                questions_dict[answer["id"]] = answer["question"]

        correct = 0
        incorrect = 0
        similar = 0
        correct_text = {}
        incorrect_text = {}
        similar_text = {}

        for q_id, answer in truth_dict.items():
            if predictions[q_id].strip() == answer.strip():
                correct += 1
                correct_text[q_id] = answer
            elif predictions[q_id].strip() in answer.strip() or answer.strip() in predictions[q_id].strip():
                similar += 1
                similar_text[q_id] = {
                    "truth": answer,
                    "predicted": predictions[q_id],
                    "question": questions_dict[q_id],
                }
            else:
                incorrect += 1
                incorrect_text[q_id] = {
                    "truth": answer,
                    "predicted": predictions[q_id],
                    "question": questions_dict[q_id],
                }

        result = {
            "correct": correct,
            "similar": similar,
            "incorrect": incorrect,
        }

        texts = {
            "correct_text": correct_text,
            "similar_text": similar_text,
            "incorrect_text": incorrect_text,
        }

        return result, texts

    def _move_model_to_device(self):
        self.model.to(self.device)

    def _get_last_metrics(self, metric_values):
        return {metric: values[-1] for metric, values in metric_values.items()}

    def _get_inputs_dict(self, batch):
        inputs = {
            "input_ids": batch[0],
            "attention_mask": batch[1],
            "start_positions": batch[3],
            "end_positions": batch[4],
        }

        if self.args["model_type"] != "distilbert":
            inputs["token_type_ids"] = None if self.args["model_type"] == "xlm" else batch[2]
        if self.args["model_type"] in ["xlnet", "xlm"]:
            inputs.update({"cls_index": batch[5], "p_mask": batch[6]})

        return inputs

    def _create_training_progress_scores(self):
        training_progress_scores = {
            "global_step": [],
            "correct": [],
            "similar": [],
            "incorrect": [],
            "train_loss": [],
        }

        return training_progress_scores

    def _save_model(self, output_dir, model=None, results=None):
        os.makedirs(output_dir, exist_ok=True)

        if model:
            # Take care of distributed/parallel training
            model_to_save = model.module if hasattr(model, "module") else model
            model_to_save.save_pretrained(output_dir)
            self.tokenizer.save_pretrained(output_dir)

        if results:
            output_eval_file = os.path.join(output_dir, "eval_results.txt")
            with open(output_eval_file, "w") as writer:
                for key in sorted(results.keys()):
                    writer.write("{} = {}\n".format(key, str(results[key])))
