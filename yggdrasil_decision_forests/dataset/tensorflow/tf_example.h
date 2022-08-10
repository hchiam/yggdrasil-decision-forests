/*
 * Copyright 2022 Google LLC.
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

#ifndef YGGDRASIL_DECISION_FORESTS_DATASET_TENSORFLOW_TF_EXAMPLE_H_
#define YGGDRASIL_DECISION_FORESTS_DATASET_TENSORFLOW_TF_EXAMPLE_H_

#include "absl/status/status.h"
#include "tensorflow/core/example/example.pb.h"
#include "tensorflow/core/example/feature.pb.h"
#include "yggdrasil_decision_forests/dataset/data_spec.pb.h"
#include "yggdrasil_decision_forests/dataset/example.pb.h"
#include "yggdrasil_decision_forests/serving/example_set.h"

namespace yggdrasil_decision_forests {
namespace dataset {

// Converts a tf.Example into an Example.
absl::Status TfExampleToYdfExample(const ::tensorflow::Example& tf_example,
                                   const proto::DataSpecification& data_spec,
                                   proto::Example* example);

// Converts a proto::Example into a tensorflow::Example.
absl::Status YdfExampleToTfExample(const proto::Example& example,
                                   const proto::DataSpecification& data_spec,
                                   ::tensorflow::Example* tf_example);

// Copies a tf.Example into a ExampleSet.
absl::Status TfExampleToExampleSet(const ::tensorflow::Example& src,
                                   int example_idx,
                                   const serving::FeaturesDefinition& features,
                                   serving::AbstractExampleSet* dst);

}  // namespace dataset
}  // namespace yggdrasil_decision_forests

#endif  // THIRD_PARTY_YGGDRASIL_DECISION_FORESTS_DATASET_TENSORFLOW_TF_EXAMPLE_H_
