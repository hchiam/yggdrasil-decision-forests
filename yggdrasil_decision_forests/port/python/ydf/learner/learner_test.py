# Copyright 2022 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for model learning."""

import os
import signal
from typing import Tuple

from absl import logging
from absl.testing import absltest
from absl.testing import parameterized
import numpy as np
import numpy.testing as npt
import pandas as pd

from yggdrasil_decision_forests.dataset import data_spec_pb2
from yggdrasil_decision_forests.learner import abstract_learner_pb2
from yggdrasil_decision_forests.model import abstract_model_pb2
from ydf.dataset import dataspec
from ydf.learner import generic_learner
from ydf.learner import specialized_learners
from ydf.learner import tuner as tuner_lib
from ydf.metric import metric
from ydf.model import generic_model
from ydf.model import model_lib
from ydf.model.decision_forest_model import decision_forest_model
from ydf.utils import log
from ydf.utils import test_utils

ProtoMonotonicConstraint = abstract_learner_pb2.MonotonicConstraint
Column = dataspec.Column


class LearnerTest(parameterized.TestCase):

  def setUp(self):
    super().setUp()
    self.dataset_directory = os.path.join(
        test_utils.ydf_test_data_path(), "dataset"
    )

    self.adult = test_utils.load_datasets("adult")
    self.two_center_regression = test_utils.load_datasets(
        "two_center_regression"
    )
    self.synthetic_ranking = test_utils.load_datasets(
        "synthetic_ranking",
        [Column("GROUP", semantic=dataspec.Semantic.HASH)],
    )
    self.sim_pte = test_utils.load_datasets(
        "sim_pte",
        [
            Column("y", semantic=dataspec.Semantic.CATEGORICAL),
            Column("treat", semantic=dataspec.Semantic.CATEGORICAL),
        ],
    )

  def _check_adult_model(
      self,
      learner: generic_learner.GenericLearner,
      minimum_accuracy: float,
      check_serialization: bool = True,
  ) -> Tuple[generic_model.GenericModel, metric.Evaluation, np.ndarray]:
    """Runs a battery of test on a model compatible with the adult dataset.

    The following tests are run:
      - Train the model.
      - Run and evaluate the model.
      - Serialize the model to a YDF model.
      - Load the serialized model.
      - Make sure predictions of original model and serialized model match.

    Args:
      learner: A learner for on the adult dataset.
      minimum_accuracy: minimum accuracy.
      check_serialization: If true, check the serialization of the model.

    Returns:
      The model, its evaluation and the predictions on the test dataset.
    """
    # Train the model.
    model = learner.train(self.adult.train)

    # Evaluate the trained model.
    evaluation = model.evaluate(self.adult.test)
    self.assertGreaterEqual(evaluation.accuracy, minimum_accuracy)

    predictions = model.predict(self.adult.test)

    if check_serialization:
      ydf_model_path = os.path.join(
          self.create_tempdir().full_path, "ydf_model"
      )
      model.save(ydf_model_path)
      loaded_model = model_lib.load_model(ydf_model_path)
      npt.assert_equal(predictions, loaded_model.predict(self.adult.test))

    return model, evaluation, predictions


class RandomForestLearnerTest(LearnerTest):

  def test_adult_classification(self):
    learner = specialized_learners.RandomForestLearner(label="income")
    model = learner.train(self.adult.train)
    logging.info("Trained model: %s", model)

    # Evaluate the trained model.
    evaluation = model.evaluate(self.adult.test)
    logging.info("Evaluation: %s", evaluation)
    self.assertGreaterEqual(evaluation.accuracy, 0.864)

  def test_two_center_regression(self):
    learner = specialized_learners.RandomForestLearner(
        label="target", task=generic_learner.Task.REGRESSION
    )
    model = learner.train(self.two_center_regression.train)
    logging.info("Trained model: %s", model)

    # Evaluate the trained model.
    evaluation = model.evaluate(self.two_center_regression.test)
    logging.info("Evaluation: %s", evaluation)
    self.assertAlmostEqual(evaluation.rmse, 114.54, places=0)

  def test_sim_pte_uplift(self):
    learner = specialized_learners.RandomForestLearner(
        label="y",
        uplift_treatment="treat",
        task=generic_learner.Task.CATEGORICAL_UPLIFT,
    )
    model = learner.train(self.sim_pte.train)

    evaluation = model.evaluate(self.sim_pte.test)
    self.assertAlmostEqual(evaluation.qini, 0.105709, places=2)

  def test_sim_pte_uplift_pd(self):
    learner = specialized_learners.RandomForestLearner(
        label="y",
        uplift_treatment="treat",
        task=generic_learner.Task.CATEGORICAL_UPLIFT,
    )
    model = learner.train(self.sim_pte.train_pd)

    evaluation = model.evaluate(self.sim_pte.test_pd)
    self.assertAlmostEqual(evaluation.qini, 0.105709, places=2)

  def test_sim_pte_uplift_path(self):
    learner = specialized_learners.RandomForestLearner(
        label="y",
        uplift_treatment="treat",
        task=generic_learner.Task.CATEGORICAL_UPLIFT,
        num_threads=1,
        max_depth=2,
    )
    model = learner.train(self.sim_pte.train_path)
    evaluation = model.evaluate(self.sim_pte.test_path)
    self.assertAlmostEqual(evaluation.qini, 0.105709, places=2)

  def test_adult_classification_pd_and_vds_match(self):
    learner = specialized_learners.RandomForestLearner(
        label="income", num_trees=50
    )
    model_pd = learner.train(self.adult.train_pd)
    model_vds = learner.train(self.adult.train)

    predictions_pd_from_vds = model_vds.predict(self.adult.test_pd)
    predictions_pd_from_pd = model_pd.predict(self.adult.test_pd)
    predictions_vds_from_vds = model_vds.predict(self.adult.test)

    npt.assert_equal(predictions_pd_from_vds, predictions_vds_from_vds)
    npt.assert_equal(predictions_pd_from_pd, predictions_vds_from_vds)

  def test_two_center_regression_pd_and_vds_match(self):
    learner = specialized_learners.RandomForestLearner(
        label="target", task=generic_learner.Task.REGRESSION, num_trees=50
    )
    model_pd = learner.train(self.two_center_regression.train_pd)
    model_vds = learner.train(self.two_center_regression.train)

    predictions_pd_from_vds = model_vds.predict(
        self.two_center_regression.test_pd
    )
    predictions_pd_from_pd = model_pd.predict(
        self.two_center_regression.test_pd
    )
    predictions_vds_from_vds = model_vds.predict(
        self.two_center_regression.test
    )

    npt.assert_equal(predictions_pd_from_vds, predictions_vds_from_vds)
    npt.assert_equal(predictions_pd_from_pd, predictions_vds_from_vds)

  # TODO: Fix this test in OSS.
  @absltest.skip("predictions do not match")
  def test_adult_golden_predictions(self):
    data_spec_path = os.path.join(
        test_utils.ydf_test_data_path(),
        "model",
        "adult_binary_class_rf",
        "data_spec.pb",
    )
    predictions_path = os.path.join(
        test_utils.ydf_test_data_path(),
        "prediction",
        "adult_test_binary_class_rf.csv",
    )
    predictions_df = pd.read_csv(predictions_path)
    data_spec = data_spec_pb2.DataSpecification()
    with open(data_spec_path, "rb") as f:
      data_spec.ParseFromString(f.read())

    learner = specialized_learners.RandomForestLearner(
        label="income",
        num_trees=100,
        winner_take_all=False,
        data_spec=data_spec,
    )
    model = learner.train(self.adult.train_pd)
    predictions = model.predict(self.adult.test_pd)
    expected_predictions = predictions_df[">50K"].to_numpy()
    # This is not particularly exact, but enough for a confidence check.
    np.testing.assert_almost_equal(predictions, expected_predictions, decimal=1)

  def test_model_type_regression(self):
    learner = specialized_learners.RandomForestLearner(
        label="col_float",
        num_trees=1,
        task=generic_learner.Task.REGRESSION,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset()).task(),
        generic_learner.Task.REGRESSION,
    )

  def test_model_type_classification_string_label(self):
    learner = specialized_learners.RandomForestLearner(
        label="col_three_string",
        num_trees=1,
        task=generic_learner.Task.CLASSIFICATION,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset()).task(),
        generic_learner.Task.CLASSIFICATION,
    )

  def test_model_type_classification_int_label(self):
    learner = specialized_learners.RandomForestLearner(
        label="binary_int_label",
        num_trees=1,
        task=generic_learner.Task.CLASSIFICATION,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset()).task(),
        generic_learner.Task.CLASSIFICATION,
    )

  def test_regression_on_categorical_fails(self):
    learner = specialized_learners.RandomForestLearner(
        label="col_three_string",
        num_trees=1,
        task=generic_learner.Task.REGRESSION,
    )
    with self.assertRaises(ValueError):
      _ = learner.train(test_utils.toy_dataset())

  def test_classification_on_floats_fails(self):
    learner = specialized_learners.RandomForestLearner(
        label="col_float",
        num_trees=1,
        task=generic_learner.Task.CLASSIFICATION,
    )

    with self.assertRaises(ValueError):
      _ = (
          learner.train(test_utils.toy_dataset()).task(),
          generic_learner.Task.CLASSIFICATION,
      )

  def test_model_type_categorical_uplift(self):
    learner = specialized_learners.RandomForestLearner(
        label="effect_binary",
        uplift_treatment="treatement",
        num_trees=1,
        task=generic_learner.Task.CATEGORICAL_UPLIFT,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset_uplift()).task(),
        generic_learner.Task.CATEGORICAL_UPLIFT,
    )

  def test_model_type_numerical_uplift(self):
    learner = specialized_learners.RandomForestLearner(
        label="effect_numerical",
        uplift_treatment="treatement",
        num_trees=1,
        task=generic_learner.Task.NUMERICAL_UPLIFT,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset_uplift()).task(),
        generic_learner.Task.NUMERICAL_UPLIFT,
    )

  def test_adult_num_threads(self):
    learner = specialized_learners.RandomForestLearner(
        label="income", num_threads=12, num_trees=50
    )

    self._check_adult_model(learner=learner, minimum_accuracy=0.860)

  # TODO: b/310580458 - Fix this test in OSS.
  @absltest.skip("Test sometimes times out")
  def test_interrupt_training(self):
    learner = specialized_learners.RandomForestLearner(
        label="income",
        num_trees=1000000,  # Trains for a very long time
    )

    signal.alarm(3)  # Stop the training in 3 seconds
    with self.assertRaises(ValueError):
      _ = learner.train(self.adult.train)

  def test_cross_validation(self):
    learner = specialized_learners.RandomForestLearner(
        label="income", num_trees=10
    )
    evaluation = learner.cross_validation(
        self.adult.train, folds=10, parallel_evaluations=2
    )
    logging.info("evaluation:\n%s", evaluation)
    self.assertAlmostEqual(evaluation.accuracy, 0.87, 1)
    # All the examples are used in the evaluation
    self.assertEqual(
        evaluation.num_examples, self.adult.train.data_spec().created_num_rows
    )

    with open(self.create_tempfile(), "w") as f:
      f.write(evaluation._repr_html_())

  def test_tuner_manual(self):
    tuner = tuner_lib.RandomSearchTuner(
        num_trials=5,
        automatic_search_space=True,
        parallel_trials=2,
    )
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="income",
        tuner=tuner,
        num_trees=30,
    )

    model, _, _ = self._check_adult_model(learner, minimum_accuracy=0.864)
    logs = model.hyperparameter_optimizer_logs()
    self.assertIsNotNone(logs)
    self.assertLen(logs.trials, 5)

  def test_tuner_predefined(self):
    tuner = tuner_lib.RandomSearchTuner(
        num_trials=5,
        automatic_search_space=True,
        parallel_trials=2,
    )
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="income",
        tuner=tuner,
        num_trees=30,
    )

    model, _, _ = self._check_adult_model(learner, minimum_accuracy=0.864)
    logs = model.hyperparameter_optimizer_logs()
    self.assertIsNotNone(logs)
    self.assertLen(logs.trials, 5)

  def test_label_type_error_message(self):
    with self.assertRaisesRegex(
        ValueError,
        "Cannot import column 'l' with semantic=Semantic.CATEGORICAL",
    ):
      _ = specialized_learners.GradientBoostedTreesLearner(
          label="l", task=generic_learner.Task.CLASSIFICATION
      ).train(pd.DataFrame({"l": [1.0, 2.0], "f": [0, 1]}))

    with self.assertRaisesRegex(
        ValueError,
        "Cannot convert NUMERICAL column 'l' of type numpy's array of 'object'"
        " and with content=",
    ):
      _ = specialized_learners.GradientBoostedTreesLearner(
          label="l", task=generic_learner.Task.REGRESSION
      ).train(pd.DataFrame({"l": ["A", "B"], "f": [0, 1]}))

  def test_with_validation(self):
    with self.assertRaisesRegex(
        ValueError,
        "The learner 'RandomForestLearner' does not use a validation dataset",
    ):
      _ = specialized_learners.RandomForestLearner(
          label="income", num_trees=5
      ).train(self.adult.train, valid=self.adult.test)

  def test_train_with_path_validation_dataset(self):
    with self.assertRaisesRegex(
        ValueError,
        "The learner 'RandomForestLearner' does not use a validation dataset",
    ):
      _ = specialized_learners.RandomForestLearner(
          label="income", num_trees=5
      ).train(self.adult.train_path, valid=self.adult.test_path)

  def test_compare_pandas_and_path(self):
    learner = specialized_learners.RandomForestLearner(
        label="income", num_trees=50
    )
    model_from_pd = learner.train(self.adult.train)
    predictions_from_pd = model_from_pd.predict(self.adult.test)

    learner_from_path = specialized_learners.RandomForestLearner(
        label="income", data_spec=model_from_pd.data_spec(), num_trees=50
    )
    model_from_path = learner_from_path.train(self.adult.train_path)
    predictions_from_path = model_from_path.predict(self.adult.test)

    npt.assert_equal(predictions_from_pd, predictions_from_path)

  def test_default_hp_dictionary(self):
    learner = specialized_learners.RandomForestLearner(label="l", num_trees=50)
    self.assertDictContainsSubset(
        {
            "num_trees": 50,
            "categorical_algorithm": "CART",
            "categorical_set_split_greedy_sampling": 0.1,
            "compute_oob_performances": True,
            "compute_oob_variable_importances": False,
        },
        learner.hyperparameters,
    )

  def test_multidimensional_training_dataset(self):
    data = {
        "feature": np.array([[0, 1, 2, 3], [4, 5, 6, 7]]),
        "label": np.array([0, 1]),
    }
    learner = specialized_learners.RandomForestLearner(label="label")
    model = learner.train(data)

    expected_columns = [
        data_spec_pb2.Column(
            name="feature.0_of_4",
            type=data_spec_pb2.ColumnType.NUMERICAL,
            count_nas=0,
            numerical=data_spec_pb2.NumericalSpec(
                mean=2,
                standard_deviation=2,
                min_value=0,
                max_value=4,
            ),
        ),
        data_spec_pb2.Column(
            name="feature.1_of_4",
            type=data_spec_pb2.ColumnType.NUMERICAL,
            count_nas=0,
            numerical=data_spec_pb2.NumericalSpec(
                mean=3,
                standard_deviation=2,
                min_value=1,
                max_value=5,
            ),
        ),
        data_spec_pb2.Column(
            name="feature.2_of_4",
            type=data_spec_pb2.ColumnType.NUMERICAL,
            count_nas=0,
            numerical=data_spec_pb2.NumericalSpec(
                mean=4,
                standard_deviation=2,
                min_value=2,
                max_value=6,
            ),
        ),
        data_spec_pb2.Column(
            name="feature.3_of_4",
            type=data_spec_pb2.ColumnType.NUMERICAL,
            count_nas=0,
            numerical=data_spec_pb2.NumericalSpec(
                mean=5,
                standard_deviation=2,
                min_value=3,
                max_value=7,
            ),
        ),
    ]
    # Skip the first column that contains the label.
    self.assertEqual(model.data_spec().columns[1:], expected_columns)

    predictions = model.predict(data)
    self.assertEqual(predictions.shape, (2,))

  def test_multidimensional_labels(self):
    ds = {
        "feature": np.array([[0, 1], [1, 0]]),
        "label": np.array([[0, 1], [1, 0]]),
    }
    learner = specialized_learners.GradientBoostedTreesLearner(label="label")
    with self.assertRaisesRegex(
        test_utils.AbslInvalidArgumentError,
        "The column 'label' is multi-dimensional \\(shape=\\(2, 2\\)\\) while"
        " the model requires this column to be single-dimensional",
    ):
      _ = learner.train(ds)

  def test_multidimensional_weights(self):
    ds = {
        "feature": np.array([[0, 1], [1, 0]]),
        "label": np.array([0, 1]),
        "weight": np.array([[0, 1], [1, 0]]),
    }
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="label", weights="weight"
    )
    with self.assertRaisesRegex(
        test_utils.AbslInvalidArgumentError,
        "The column 'weight' is multi-dimensional \\(shape=\\(2, 2\\)\\) while"
        " the model requires this column to be single-dimensional",
    ):
      _ = learner.train(ds)

  def test_learn_and_predict_when_label_is_not_last_column(self):
    label = "age"
    learner = specialized_learners.RandomForestLearner(
        label=label, num_trees=10
    )
    # The age column is automatically interpreted as categorical
    model_from_pd = learner.train(self.adult.train_pd)
    evaluation = model_from_pd.evaluate(self.adult.test_pd)
    self.assertGreaterEqual(evaluation.accuracy, 0.05)

  def test_model_metadata_contains_framework(self):
    learner = specialized_learners.RandomForestLearner(
        label="binary_int_label", num_trees=1
    )
    model = learner.train(test_utils.toy_dataset())
    self.assertEqual(model.metadata().framework, "Python YDF")

  def test_model_metadata_does_not_populate_owner(self):
    learner = specialized_learners.RandomForestLearner(
        label="binary_int_label", num_trees=1
    )
    model = learner.train(test_utils.toy_dataset())
    self.assertEqual(model.metadata().owner, "")

  def test_adult_sparse_oblique(self):
    learner = specialized_learners.RandomForestLearner(
        label="income",
        num_trees=4,
        split_axis="SPARSE_OBLIQUE",
        sparse_oblique_weights="CONTINUOUS",
    )
    model = learner.train(self.adult.train)
    logging.info("Trained model: %s", model)

  def test_adult_mhld_oblique(self):
    learner = specialized_learners.RandomForestLearner(
        label="income", num_trees=4, split_axis="MHLD_OBLIQUE"
    )
    model = learner.train(self.adult.train)
    logging.info("Trained model: %s", model)


class CARTLearnerTest(LearnerTest):

  def test_adult(self):
    learner = specialized_learners.CartLearner(label="income")

    self._check_adult_model(learner=learner, minimum_accuracy=0.853)

  def test_two_center_regression(self):
    learner = specialized_learners.CartLearner(
        label="target", task=generic_learner.Task.REGRESSION
    )
    model = learner.train(self.two_center_regression.train)
    evaluation = model.evaluate(self.two_center_regression.test)
    self.assertAlmostEqual(evaluation.rmse, 114.081, places=3)

  def test_monotonic_non_compatible_learner(self):
    learner = specialized_learners.CartLearner(
        label="label", features=[dataspec.Column("feature", monotonic=+1)]
    )
    ds = pd.DataFrame({"feature": [0, 1], "label": [0, 1]})
    with self.assertRaisesRegex(
        test_utils.AbslInvalidArgumentError,
        "The learner CART does not support monotonic constraints",
    ):
      _ = learner.train(ds)


class GradientBoostedTreesLearnerTest(LearnerTest):

  def test_adult(self):
    learner = specialized_learners.GradientBoostedTreesLearner(label="income")

    self._check_adult_model(learner=learner, minimum_accuracy=0.869)

  def test_ranking(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="LABEL",
        ranking_group="GROUP",
        task=generic_learner.Task.RANKING,
    )

    model = learner.train(self.synthetic_ranking.train)
    evaluation = model.evaluate(self.synthetic_ranking.test)
    self.assertGreaterEqual(evaluation.ndcg, 0.70)
    self.assertLessEqual(evaluation.ndcg, 0.74)

  def test_ranking_pd(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="LABEL",
        ranking_group="GROUP",
        task=generic_learner.Task.RANKING,
    )

    model = learner.train(self.synthetic_ranking.train_pd)
    evaluation = model.evaluate(self.synthetic_ranking.test_pd)
    self.assertGreaterEqual(evaluation.ndcg, 0.70)
    self.assertLessEqual(evaluation.ndcg, 0.74)

  def test_ranking_path(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="LABEL",
        ranking_group="GROUP",
        task=generic_learner.Task.RANKING,
    )

    model = learner.train(self.synthetic_ranking.train_path)
    evaluation = model.evaluate(self.synthetic_ranking.test_path)
    self.assertGreaterEqual(evaluation.ndcg, 0.70)
    self.assertLessEqual(evaluation.ndcg, 0.74)

  def test_adult_num_threads(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="income", num_threads=12, num_trees=50
    )

    self._check_adult_model(learner=learner, minimum_accuracy=0.869)

  def test_model_type_ranking(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="col_float",
        ranking_group="col_three_string",
        num_trees=1,
        task=generic_learner.Task.RANKING,
    )
    self.assertEqual(
        learner.train(test_utils.toy_dataset()).task(),
        generic_learner.Task.RANKING,
    )

  def test_monotonic_non_compatible_options(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="label", features=[dataspec.Column("feature", monotonic=+1)]
    )
    ds = pd.DataFrame({"feature": [0, 1], "label": [0, 1]})
    with self.assertRaisesRegex(
        test_utils.AbslInvalidArgumentError,
        "Gradient Boosted Trees does not support monotonic constraints with"
        " use_hessian_gain=false",
    ):
      _ = learner.train(ds)

  def test_monotonic_training(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="income",
        num_trees=70,
        use_hessian_gain=True,
        features=[
            dataspec.Column("age", monotonic=+1),
            dataspec.Column("hours_per_week", monotonic=-1),
            dataspec.Column("education_num", monotonic=+1),
        ],
        include_all_columns=True,
    )

    test_utils.assertProto2Equal(
        self,
        learner._get_training_config(),
        abstract_learner_pb2.TrainingConfig(
            learner="GRADIENT_BOOSTED_TREES",
            label="income",
            task=abstract_model_pb2.Task.CLASSIFICATION,
            metadata=abstract_model_pb2.Metadata(framework="Python YDF"),
            monotonic_constraints=[
                ProtoMonotonicConstraint(
                    feature="^age$",
                    direction=ProtoMonotonicConstraint.INCREASING,
                ),
                ProtoMonotonicConstraint(
                    feature="^hours_per_week$",
                    direction=ProtoMonotonicConstraint.DECREASING,
                ),
                ProtoMonotonicConstraint(
                    feature="^education_num$",
                    direction=ProtoMonotonicConstraint.INCREASING,
                ),
            ],
        ),
    )

    model, _, _ = self._check_adult_model(learner, minimum_accuracy=0.863)

    _ = model.analyze(self.adult.test)

  def test_with_validation_pd(self):
    evaluation = (
        specialized_learners.GradientBoostedTreesLearner(
            label="income", num_trees=50
        )
        .train(self.adult.train, valid=self.adult.test)
        .evaluate(self.adult.test)
    )

    logging.info("evaluation:\n%s", evaluation)
    self.assertAlmostEqual(evaluation.accuracy, 0.87, 1)

  def test_with_validation_path(self):
    evaluation = (
        specialized_learners.GradientBoostedTreesLearner(
            label="income", num_trees=50
        )
        .train(self.adult.train_path, valid=self.adult.test_path)
        .evaluate(self.adult.test_path)
    )

    logging.info("evaluation:\n%s", evaluation)
    self.assertAlmostEqual(evaluation.accuracy, 0.87, 1)

  def test_failure_train_path_validation_pd(self):
    with self.assertRaisesRegex(
        ValueError,
        "If the training dataset is a path, the validation dataset must also be"
        " a path.",
    ):
      specialized_learners.GradientBoostedTreesLearner(
          label="income", num_trees=50
      ).train(self.adult.train_path, valid=self.adult.test)

  def test_failure_train_pd_validation_path(self):
    with self.assertRaisesRegex(
        ValueError,
        "The validation dataset may only be a path if the training dataset is"
        " a path.",
    ):
      specialized_learners.GradientBoostedTreesLearner(
          label="income", num_trees=50
      ).train(self.adult.train, valid=self.adult.test_path)

  def test_resume_training(self):
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="income",
        num_trees=10,
        resume_training=True,
        working_dir=self.create_tempdir().full_path,
    )
    model_1 = learner.train(self.adult.train)
    assert isinstance(model_1, decision_forest_model.DecisionForestModel)
    self.assertEqual(model_1.num_trees(), 10)
    learner.hyperparameters["num_trees"] = 50
    model_2 = learner.train(self.adult.train)
    assert isinstance(model_2, decision_forest_model.DecisionForestModel)
    self.assertEqual(model_2.num_trees(), 50)

  def test_predict_iris(self):
    dataset_path = os.path.join(
        test_utils.ydf_test_data_path(), "dataset", "iris.csv"
    )
    ds = pd.read_csv(dataset_path)
    model = specialized_learners.RandomForestLearner(label="class").train(ds)

    predictions = model.predict(ds)

    self.assertEqual(predictions.shape, (ds.shape[0], 3))

    row_sums = np.sum(predictions, axis=1)
    # Make sure a multi-dimensional prediction always (mostly) sums to 1.
    npt.assert_array_almost_equal(
        row_sums, np.ones(predictions.shape[0]), decimal=5
    )

  def test_better_default_template(self):
    ds = test_utils.toy_dataset()
    label = "binary_int_label"
    templates = (
        specialized_learners.GradientBoostedTreesLearner.hyperparameter_templates()
    )
    self.assertIn("better_defaultv1", templates)
    better_defaultv1 = templates["better_defaultv1"]
    learner = specialized_learners.GradientBoostedTreesLearner(
        label=label, **better_defaultv1
    )
    self.assertEqual(
        learner.hyperparameters["growing_strategy"], "BEST_FIRST_GLOBAL"
    )
    _ = learner.train(ds)

  def test_model_with_na_conditions_numerical(self):
    ds = pd.DataFrame({
        "feature": [np.nan] * 10 + [1.234] * 10,
        "label": [0] * 10 + [1] * 10,
    })
    learner = specialized_learners.GradientBoostedTreesLearner(
        label="label",
        allow_na_conditions=True,
        num_trees=1,
    )
    model = learner.train(ds)
    evaluation = model.evaluate(ds)
    self.assertEqual(evaluation.accuracy, 1)


class LoggingTest(parameterized.TestCase):

  @parameterized.parameters(0, 1, 2)
  def test_logging(self, verbose):
    save_verbose = log.verbose(verbose)
    learner = specialized_learners.RandomForestLearner(label="label")
    ds = pd.DataFrame({"feature": [0, 1], "label": [0, 1]})
    _ = learner.train(ds)
    log.verbose(save_verbose)


class UtilityTest(LearnerTest):

  def test_feature_name_to_regex(self):
    self.assertEqual(
        generic_learner._feature_name_to_regex("a(z)e"), r"^a\(z\)e$"
    )


if __name__ == "__main__":
  absltest.main()
