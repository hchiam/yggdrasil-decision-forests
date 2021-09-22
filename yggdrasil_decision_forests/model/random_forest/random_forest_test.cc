/*
 * Copyright 2021 Google LLC.
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "yggdrasil_decision_forests/model/random_forest/random_forest.h"

#include <memory>
#include <string>
#include <vector>

#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "absl/container/node_hash_set.h"
#include "absl/memory/memory.h"
#include "absl/status/status.h"
#include "yggdrasil_decision_forests/dataset/data_spec.pb.h"
#include "yggdrasil_decision_forests/dataset/example.pb.h"
#include "yggdrasil_decision_forests/dataset/vertical_dataset.h"
#include "yggdrasil_decision_forests/metric/metric.pb.h"
#include "yggdrasil_decision_forests/model/abstract_model.h"
#include "yggdrasil_decision_forests/model/abstract_model.pb.h"
#include "yggdrasil_decision_forests/model/decision_tree/decision_tree.h"
#include "yggdrasil_decision_forests/model/decision_tree/decision_tree.pb.h"
#include "yggdrasil_decision_forests/model/model_library.h"
#include "yggdrasil_decision_forests/model/prediction.pb.h"
#include "yggdrasil_decision_forests/utils/filesystem.h"
#include "yggdrasil_decision_forests/utils/logging.h"
#include "yggdrasil_decision_forests/utils/test.h"

namespace yggdrasil_decision_forests {
namespace model {
namespace random_forest {
namespace {

using test::EqualsProto;

// Build a forest with two decision trees as follow:
// [a>1]
//   ├── [b=0] (pos)
//   └── [b=1] (neg)
// [a>3]
//   ├── [b=2] (pos)
//   └── [b=1] (neg)
//
// Build the dataset:
// "a" : {0, 2, 4}
// "b" : {1, 2, 1}
void BuildToyModelAndToyDataset(const model::proto::Task task,
                                RandomForestModel* model,
                                dataset::VerticalDataset* dataset) {
  dataset::proto::DataSpecification dataspec = PARSE_TEST_PROTO(R"pb(
    columns { type: NUMERICAL name: "a" }
    columns {
      type: CATEGORICAL
      name: "b"
      categorical { is_already_integerized: true number_of_unique_values: 3 }
    }
  )pb");

  dataset->set_data_spec(dataspec);
  CHECK_OK(dataset->CreateColumnsFromDataspec());
  auto* col_1 =
      dataset->MutableColumnWithCast<dataset::VerticalDataset::NumericalColumn>(
          0);
  col_1->Add(0);
  col_1->Add(2);
  col_1->Add(4);

  auto* col_2 =
      dataset
          ->MutableColumnWithCast<dataset::VerticalDataset::CategoricalColumn>(
              1);
  col_2->Add(1);
  col_2->Add(2);
  col_2->Add(1);
  dataset->set_nrow(3);

  // Create a tree of the form.
  // [a> alpha]
  //   ├── [b=beta]
  //   └── [b=gamma]
  auto create_tree = [&task](const float alpha, const int beta,
                             const int gamma) {
    auto tree = absl::make_unique<decision_tree::DecisionTree>();
    tree->CreateRoot();
    tree->mutable_root()->CreateChildren();
    tree->mutable_root()->mutable_node()->mutable_condition()->set_attribute(0);
    tree->mutable_root()
        ->mutable_node()
        ->mutable_condition()
        ->mutable_condition()
        ->mutable_higher_condition()
        ->set_threshold(alpha);
    tree->mutable_root()
        ->mutable_node()
        ->set_num_pos_training_examples_without_weight(10);

    auto* pos_child = tree->mutable_root()->mutable_pos_child()->mutable_node();
    auto* neg_child = tree->mutable_root()->mutable_neg_child()->mutable_node();

    pos_child->set_num_pos_training_examples_without_weight(8);
    neg_child->set_num_pos_training_examples_without_weight(2);

    switch (task) {
      case model::proto::Task::CLASSIFICATION:
        pos_child->mutable_classifier()->set_top_value(beta);
        neg_child->mutable_classifier()->set_top_value(gamma);
        break;
      case model::proto::Task::REGRESSION:
        pos_child->mutable_regressor()->set_top_value(beta);
        neg_child->mutable_regressor()->set_top_value(gamma);
        break;
      default:
        CHECK(false);
    }
    return tree;
  };

  model->AddTree(create_tree(1, 0, 1));
  model->AddTree(create_tree(3, 2, 1));

  model->set_task(task);
  model->set_label_col_idx(1);
  model->set_data_spec(dataspec);
}

TEST(DecisionTree, CountFeatureUsage) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  std::unordered_map<int32_t, int64_t> feature_usage;
  model.CountFeatureUsage(&feature_usage);

  EXPECT_EQ(feature_usage.size(), 1);
  EXPECT_EQ(feature_usage[0], 2);
}

TEST(DecisionTree, CallOnAllLeafs) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  int num_calls = 0;
  model.CallOnAllLeafs(dataset, 1, [&](const decision_tree::proto::Node& node) {
    EXPECT_TRUE(
        &node == &model.decision_trees()[0]->root().pos_child()->node() ||
        &node == &model.decision_trees()[1]->root().neg_child()->node());
    num_calls++;
  });
  EXPECT_EQ(num_calls, 2);
}

TEST(DecisionTree, PredictClassification) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  model::proto::Prediction prediction;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  model.Predict(dataset, 1, &prediction);
  model::proto::Prediction expected_prediction = PARSE_TEST_PROTO(
      R"(
        classification {
          value: 0
          distribution { counts: 1 counts: 1 counts: 0 sum: 2 }
        }
      )");
  EXPECT_THAT(prediction, EqualsProto(expected_prediction));

  dataset::proto::Example example;
  dataset.ExtractExample(1, &example);
  model::proto::Prediction prediction2;
  model.Predict(example, &prediction2);
  EXPECT_THAT(prediction2, EqualsProto(expected_prediction));
}

TEST(DecisionTree, PredictRegression) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  model::proto::Prediction prediction;
  BuildToyModelAndToyDataset(model::proto::Task::REGRESSION, &model, &dataset);
  model.Predict(dataset, 1, &prediction);
  model::proto::Prediction expected_prediction = PARSE_TEST_PROTO(
      R"(
        regression { value: 0.5 }
      )");
  EXPECT_THAT(prediction, EqualsProto(expected_prediction));

  dataset::proto::Example example;
  dataset.ExtractExample(1, &example);
  model::proto::Prediction prediction2;
  model.Predict(example, &prediction2);
  EXPECT_THAT(prediction2, EqualsProto(expected_prediction));
}

TEST(DecisionTree, AppendDescriptionAndStatisticsToy) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  std::string description;
  model.AppendDescriptionAndStatistics(false, &description);
  LOG(INFO) << "description:\n" << description;

  CHECK_NE(description.find("Type: \"RANDOM_FOREST\""), -1);
  CHECK_NE(description.find("Task: CLASSIFICATION"), -1);
  CHECK_NE(description.find("Label: \"b\""), -1);
  CHECK_NE(description.find("Number of trees: 2"), -1);
  CHECK_NE(description.find("Total number of nodes: 6"), -1);
  CHECK_NE(description.find("Number of nodes by tree:\nCount: 2 Average: 3"),
           -1);
  CHECK_NE(description.find("Depth by leafs:\nCount: 4 Average: 1"), -1);
  CHECK_NE(description.find("2 : HigherCondition"), -1);
}

TEST(DecisionTree, StructuralVariableImportance) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  std::string description;
  model.AppendDescriptionAndStatistics(false, &description);
  LOG(INFO) << "description:\n" << description;

  const auto imp_num_nodes = model.GetVariableImportance("NUM_NODES").value();
  EXPECT_EQ(imp_num_nodes.size(), 1);
  EXPECT_EQ(imp_num_nodes[0].attribute_idx(), 0);
  EXPECT_NEAR(imp_num_nodes[0].importance(), 2.0, 0.0001);

  const auto imp_as_root = model.GetVariableImportance("NUM_AS_ROOT").value();
  EXPECT_EQ(imp_as_root.size(), 1);
  EXPECT_EQ(imp_as_root[0].attribute_idx(), 0);
  EXPECT_NEAR(imp_as_root[0].importance(), 2.0, 0.0001);

  const auto imp_sum_score = model.GetVariableImportance("SUM_SCORE").value();
  EXPECT_EQ(imp_sum_score.size(), 1);
  EXPECT_EQ(imp_sum_score[0].attribute_idx(), 0);
  EXPECT_NEAR(imp_sum_score[0].importance(), 0.0, 0.0001);

  const auto imp_mean_min_depth =
      model.GetVariableImportance("MEAN_MIN_DEPTH").value();
  EXPECT_EQ(imp_mean_min_depth.size(), 2);
  EXPECT_EQ(imp_mean_min_depth[0].attribute_idx(), 1);
  EXPECT_NEAR(imp_mean_min_depth[0].importance(), 1.0, 0.0001);
  EXPECT_EQ(imp_mean_min_depth[1].attribute_idx(), 0);
  EXPECT_NEAR(imp_mean_min_depth[1].importance(), 0.0, 0.0001);
}

TEST(DecisionTree, AppendModelStructure) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  std::string description;
  model.AppendModelStructure(&description);
  EXPECT_EQ(description, R"(Number of trees:2
Tree #0
Condition:: "a">=1 score:0.000000 training_examples:0 positive_training_examples:0 missing_value_evaluation:0
Positive child
  Value:: top:0
Negative child
  Value:: top:1

Tree #1
Condition:: "a">=3 score:0.000000 training_examples:0 positive_training_examples:0 missing_value_evaluation:0
Positive child
  Value:: top:2
Negative child
  Value:: top:1

)");
}

TEST(DecisionTree, IterateOnNodes) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  absl::node_hash_set<const decision_tree::NodeWithChildren*> visited_nodes;
  model.IterateOnNodes(
      [&](const decision_tree::NodeWithChildren& node, const int depth) {
        EXPECT_TRUE(visited_nodes.find(&node) == visited_nodes.end());
        visited_nodes.insert(&node);
      });
  EXPECT_EQ(visited_nodes.size(), 6);
  EXPECT_EQ(visited_nodes.size(), model.NumNodes());
}

TEST(DecisionTree, IterateOnMutableNodes) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  absl::node_hash_set<const decision_tree::NodeWithChildren*> visited_nodes;
  model.IterateOnMutableNodes(
      [&](decision_tree::NodeWithChildren* node, const int depth) {
        EXPECT_TRUE(visited_nodes.find(node) == visited_nodes.end());
        visited_nodes.insert(node);
      });
  EXPECT_EQ(visited_nodes.size(), 6);
  EXPECT_EQ(visited_nodes.size(), model.NumNodes());
}

TEST(RandomForest, EvaluationSnippet) {
  const metric::proto::EvaluationResults evaluation = PARSE_TEST_PROTO(R"pb(
    classification {
      confusion {
        sum: 10
        counts: 4
        counts: 1
        counts: 1
        counts: 4
        nrow: 2
        ncol: 2
      }
      sum_log_loss: 10
    }
    count_predictions: 10
    task: CLASSIFICATION
  )pb");
  EXPECT_EQ(internal::EvaluationSnippet(evaluation), "accuracy:0.8 logloss:1");
}

TEST(DecisionTree, MinNumberObs) {
  RandomForestModel model;
  dataset::VerticalDataset dataset;
  BuildToyModelAndToyDataset(model::proto::Task::CLASSIFICATION, &model,
                             &dataset);
  CHECK_EQ(model.MinNumberObs(), 2);
}

}  // namespace
}  // namespace random_forest
}  // namespace model
}  // namespace yggdrasil_decision_forests
