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

"""Utilities to export JAX models."""

import dataclasses
import enum
import functools
import array
from typing import Any, Sequence, Dict, Optional, List, Set, Tuple
import logging

from yggdrasil_decision_forests.dataset import data_spec_pb2 as ds_pb
from ydf.dataset import dataspec as dataspec_lib
from ydf.model import generic_model
from ydf.model import tree as tree_lib
from ydf.model.decision_forest_model import decision_forest_model
from ydf.model.gradient_boosted_trees_model import gradient_boosted_trees_model
from ydf.learner import custom_loss

# pytype: disable=import-error
# pylint: disable=g-import-not-at-top
try:
  import jax.numpy as jnp
  import jax
except ImportError as exc:
  raise ImportError("Cannot import jax") from exc
# pylint: enable=g-import-not-at-top
# pytype: enable=import-error

# Typehint for arrays
ArrayFloat = array.array
ArrayInt = array.array
ArrayBool = array.array


# Index of the type of conditions in the intermediate tree representation.
class ConditionType(enum.IntEnum):
  GREATER_THAN = 0
  IS_IN = 1


def compact_dtype(values: Sequence[int]) -> Any:
  """Selects the most compact dtype to represent a list of signed integers.

  Only supports: int{8, 16, 32}.

  Note: Jax operations between unsigned and signed integers can be expensive.

  Args:
    values: List of integer values.

  Returns:
    Dtype compatible with all the values.
  """

  if not values:
    raise ValueError("No values provided")

  min_value = min(values)
  max_value = max(values)

  for candidate in [jnp.int8, jnp.int16, jnp.int32]:
    info = jnp.iinfo(candidate)
    if min_value >= info.min and max_value <= info.max:
      return candidate
  raise ValueError("No supported compact dtype")


def to_compact_jax_array(values: Sequence[int]) -> jax.Array:
  """Converts a list of integers to a compact Jax array."""

  if not values:
    # Note: Because of the way Jax handle virtual out of bound access in vmap,
    # it is important for arrayes to never be empty.
    return jnp.asarray([0], dtype=jnp.int32)

  return jnp.asarray(values, dtype=compact_dtype(values))


@dataclasses.dataclass
class FeatureEncoding:
  """Utility to prepare feature values before being fed into the Jax model.

  Does the following:
  - Encodes categorical strings into categorical integers.

  Attributes:
    categorical: Mapping between categorical-string feature to the dictionary of
      categorical-string value to categorical-integer value.
    categorical_out_of_vocab_item: Integer value representing an out of
      vocabulary item.
  """

  categorical: Dict[str, Dict[str, int]]
  categorical_out_of_vocab_item: int = 0

  @classmethod
  def build(
      cls,
      input_features: Sequence[generic_model.InputFeature],
      dataspec: ds_pb.DataSpecification,
  ) -> Optional["FeatureEncoding"]:
    """Creates a FeatureEncoding object.

    If the input feature does not require feature encoding, returns None.

    Args:
      input_features: All the input features of a model.
      dataspec: Dataspec of the model.

    Returns:
      A FeatureEncoding or None.
    """

    categorical = {}
    for input_feature in input_features:
      column_spec = dataspec.columns[input_feature.column_idx]
      if (
          input_feature.semantic
          in [
              dataspec_lib.Semantic.CATEGORICAL,
              dataspec_lib.Semantic.CATEGORICAL_SET,
          ]
          and not column_spec.categorical.is_already_integerized
      ):
        categorical[input_feature.name] = {
            key: item.index
            for key, item in column_spec.categorical.items.items()
        }
    if not categorical:
      return None
    return FeatureEncoding(categorical=categorical)

  def encode(self, feature_values: Dict[str, Any]) -> Dict[str, jax.Array]:
    """Encodes feature values for a model."""

    def encode_item(key: str, value: Any) -> jax.Array:
      categorical_map = self.categorical.get(key)
      if categorical_map is not None:
        # Categorical string encoding.
        value = [
            categorical_map.get(x, self.categorical_out_of_vocab_item)
            for x in value
        ]
      return jax.numpy.asarray(value)

    return {k: encode_item(k, v) for k, v in feature_values.items()}


@dataclasses.dataclass
class InternalFeatureValues:
  """Internal representation of feature values.

  In the internal model format, features with the same semantic are grouped
  together i.e. densified.
  """

  numerical: jax.Array
  categorical: jax.Array
  boolean: jax.Array


@dataclasses.dataclass
class InternalFeatureSpec:
  """Spec of the internal feature value representation.

  Attributes:
    input_features: Input features of the model.
    numerical: Name of numerical features in internal order.
    categorical: Name of categorical features in internal order.
    boolean: Name of boolean features in internal order.
    inv_numerical: Column idx to internal idx mapping for numerical features.
    inv_categorical: Column idx to internal idx mapping for categorical features
    inv_boolean: Column idx to internal idx mapping for boolean features.
    feature_names: Name of all the input features.
  """

  input_features: dataclasses.InitVar[Sequence[generic_model.InputFeature]]

  numerical: List[str] = dataclasses.field(default_factory=list)
  categorical: List[str] = dataclasses.field(default_factory=list)
  boolean: List[str] = dataclasses.field(default_factory=list)

  inv_numerical: Dict[int, int] = dataclasses.field(default_factory=dict)
  inv_categorical: Dict[int, int] = dataclasses.field(default_factory=dict)
  inv_boolean: Dict[int, int] = dataclasses.field(default_factory=dict)

  feature_names: Set[str] = dataclasses.field(default_factory=set)

  def __post_init__(self, input_features: Sequence[generic_model.InputFeature]):
    for input_feature in input_features:
      self.feature_names.add(input_feature.name)
      if input_feature.semantic == dataspec_lib.Semantic.NUMERICAL:
        self.inv_numerical[input_feature.column_idx] = len(self.numerical)
        self.numerical.append(input_feature.name)

      elif input_feature.semantic == dataspec_lib.Semantic.CATEGORICAL:
        self.inv_categorical[input_feature.column_idx] = len(self.categorical)
        self.categorical.append(input_feature.name)

      elif input_feature.semantic == dataspec_lib.Semantic.BOOLEAN:
        self.inv_boolean[input_feature.column_idx] = len(self.boolean)
        self.boolean.append(input_feature.name)

      else:
        raise ValueError(
            f"The semantic of feature {input_feature} is not supported by the"
            " YDF to Jax exporter"
        )

  def convert_features(
      self, feature_values: Dict[str, jax.Array]
  ) -> InternalFeatureValues:
    """Converts user provided user values into the internal model format.

    Args:
      feature_values: User input features.

    Returns:
      Internal feature values.
    """

    if not feature_values:
      raise ValueError("At least one feature should be provided")

    batch_size = next(iter(feature_values.values())).shape[0]

    if set(feature_values) != self.feature_names:
      raise ValueError(
          f"Expecting values with keys {set(self.feature_names)!r}. Got"
          f" {set(feature_values.keys())!r}"
      )

    def stack(features, dtype):
      if not features:
        return jnp.zeros(shape=[batch_size, 0], dtype=dtype)
      return jnp.stack(
          [feature_values[feature] for feature in features],
          dtype=dtype,
          axis=1,
      )

    return InternalFeatureValues(
        numerical=stack(self.numerical, jnp.float32),
        categorical=stack(self.categorical, jnp.int32),
        boolean=stack(self.boolean, jnp.bool_),
    )


@dataclasses.dataclass(frozen=True)
class BeginNodeIdx:
  """Index of the first leaf and non leaf node in a tree."""

  leaf_node: int
  non_leaf_node: int


@dataclasses.dataclass(frozen=True)
class NodeIdx:
  """Index of a leaf or a non-leaf node."""

  leaf_node: Optional[int] = None
  non_leaf_node: Optional[int] = None

  def __post_init__(self):
    if (self.leaf_node is None) == (self.non_leaf_node is None):
      raise ValueError(
          "Exactly one of leaf_node and non_leaf_node must be set."
      )

  def offset(
      self,
      begin_node_idx: BeginNodeIdx,
  ) -> int:
    """Gets the offset of a node to allow for compact representation."""

    if self.non_leaf_node is not None:
      # This is not a leaf
      return self.non_leaf_node - begin_node_idx.non_leaf_node
    else:
      # This is a leaf
      return -(self.leaf_node - begin_node_idx.leaf_node) - 1


def _categorical_list_to_bitmap(
    column_spec: ds_pb.Column, items: Sequence[int]
) -> Sequence[bool]:
  """Converts a list of categorical integer values to a bitmap."""

  size = column_spec.categorical.number_of_unique_values
  bitmap = [False] * size
  for item in items:
    if item < 0 or item >= size:
      raise ValueError(f"Invalid item {item} for column {column_spec!r}")
    bitmap[item] = True
  return bitmap


@dataclasses.dataclass
class InternalForest:
  """Internal representation of a forest before being converted to Jax code.

  Several fields (negative_children, positive_children, root_nodes) encode
  collections of node indexes where the sign of the offset indicates if the node
  is a non-leaf node (non strict positive value) or a leaf node (strict negative
  value), and the the absolute value of the offset is relative to the first leaf
  / non-leaf node in the tree containing the node.

  Example:
    Assume a node index offset X = 1 in the Y = 4th tree.
      The value is positive => This is a non-leaf node.
      The non-leaf node index is: begin_non_leaf_nodes[Y] + X

    Assume a node index offset X = -2 in the Y = 4th tree.
      The value is strictly negative => This is a leaf node.
      The non-leaf node index is: begin_leaf_nodes[Y] - X - 1

  Attributes:
    model: Input decision forest model.
    feature_spec: Internal feature indexing.
    feature_encoding: How to encode features before feeding them to the model.
    dataspec: Dataspec.
    leaf_outputs: Prediction values for each leaf node.
    split_features: Internal idx of the feature being tested for each non-leaf
      node.
    split_parameters: Parameter of the condition for each non-leaf nodes. (1)
      For "greather than" condition (i.e., feature >= threshold),
      "split_parameter" is the threshold. (2) For "is in" condition (i.e.,
      feature in mask), "split_parameter" is an uint32 offset in the mask
      "catgorical_mask" wheret the condition is evaluated as
      "catgorical_mask[split_parameter + attribute_value]".
    negative_children: Node offset of the negative children for each non-leaf
      node in the forest.
    positive_children: Node offset of the positive children for each non-leaf
      node in the forest.
    condition_types: Condition type for each non-leaf nodes in the forest.
    root_nodes: Index of the root node for each of the trees.
    begin_non_leaf_nodes: Index of the first non leaf node for each of the
      trees.
    begin_leaf_nodes: Index of the first leaf node for each of the trees.
    catgorical_mask: Boolean mask used in "is in" conditions.
    initial_predictions: Initial predictions of the forest (before any tree is
      applied).
    max_depth: Maximum depth of the trees.
    activation: Activation (a.k.a linkage) function applied on the model output.
  """

  model: dataclasses.InitVar[generic_model.GenericModel]
  feature_spec: InternalFeatureSpec = dataclasses.field(init=False)
  feature_encoding: Optional[FeatureEncoding] = dataclasses.field(init=False)
  dataspec: Any = dataclasses.field(repr=False, init=False)
  leaf_outputs: ArrayFloat = dataclasses.field(
      default_factory=lambda: array.array("f", [])
  )
  split_features: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  split_parameters: ArrayFloat = dataclasses.field(
      default_factory=lambda: array.array("f", [])
  )
  negative_children: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  positive_children: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  condition_types: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  root_nodes: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  begin_non_leaf_nodes: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  begin_leaf_nodes: ArrayInt = dataclasses.field(
      default_factory=lambda: array.array("l", [])
  )
  catgorical_mask: ArrayBool = dataclasses.field(
      default_factory=lambda: array.array("b", [])
  )
  initial_predictions: ArrayFloat = dataclasses.field(
      default_factory=lambda: array.array("f", [])
  )
  max_depth: int = 0
  activation: custom_loss.Activation = dataclasses.field(init=False)

  def clear_array_data(self) -> None:
    """Clear all the array data."""
    self.leaf_outputs = array.array("f", [])
    self.split_features = array.array("l", [])
    self.split_parameters = array.array("l", [])
    self.negative_children = array.array("l", [])
    self.positive_children = array.array("l", [])
    self.condition_types = array.array("l", [])
    self.root_nodes = array.array("l", [])
    self.begin_non_leaf_nodes = array.array("l", [])
    self.begin_leaf_nodes = array.array("l", [])
    self.catgorical_mask = array.array("l", [])
    # Note: We don't release "initial_predictions".

  def __post_init__(self, model: generic_model.GenericModel):
    if not isinstance(model, decision_forest_model.DecisionForestModel):
      raise ValueError("The model is not a decision forest")

    input_features = model.input_features()
    self.dataspec = model.data_spec()
    self.feature_encoding = FeatureEncoding.build(input_features, self.dataspec)
    self.feature_spec = InternalFeatureSpec(input_features)

    if isinstance(
        model, gradient_boosted_trees_model.GradientBoostedTreesModel
    ):
      self.activation = model.activation()
    else:
      self.activation = custom_loss.Activation.IDENTITY

    if isinstance(
        model, gradient_boosted_trees_model.GradientBoostedTreesModel
    ):
      self.initial_predictions = model.initial_predictions().tolist()
    else:
      self.initial_predictions = [0.0]

    if not isinstance(model, decision_forest_model.DecisionForestModel):
      raise ValueError("The model is not a decision forest")
    for tree in model.iter_trees():
      self._add_tree(tree)

  def _add_tree(self, tree: tree_lib.Tree) -> None:
    """Adds a tree to the forest."""

    begin_node_idx = BeginNodeIdx(
        leaf_node=self.num_leaf_nodes(),
        non_leaf_node=self.num_non_leaf_nodes(),
    )
    self.begin_leaf_nodes.append(begin_node_idx.leaf_node)
    self.begin_non_leaf_nodes.append(begin_node_idx.non_leaf_node)

    root_node = self._add_node(tree.root, begin_node_idx, depth=0)
    self.root_nodes.append(root_node.offset(begin_node_idx))

  def num_trees(self) -> int:
    """Number of trees in the forest."""

    return len(self.root_nodes)

  def num_non_leaf_nodes(self) -> int:
    """Number of non leaf nodes in the forest so far."""

    n = len(self.split_features)
    # Check data consistency.
    assert n == len(self.split_parameters)
    assert n == len(self.negative_children)
    assert n == len(self.positive_children)
    assert n == len(self.condition_types)
    return n

  def num_leaf_nodes(self) -> int:
    """Number of leaf nodes in the forest so far."""

    return len(self.leaf_outputs)

  def _add_node(
      self,
      node: tree_lib.AbstractNode,
      begin_node_idx: BeginNodeIdx,
      depth: int,
  ) -> NodeIdx:
    """Adds a node to the forest."""

    if node.is_leaf:  # A leaf node
      # Keep track of the maximum depth
      self.max_depth = max(self.max_depth, depth)

      assert isinstance(node, tree_lib.Leaf)
      # TODO: Add support for other types of leaf nodes.
      if not isinstance(node.value, tree_lib.RegressionValue):
        raise ValueError(
            "The YDF Jax exporter does not support this leaf value:"
            f" {node.value!r}"
        )
      node_idx = self.num_leaf_nodes()
      self.leaf_outputs.append(node.value.value)
      return NodeIdx(leaf_node=node_idx)

    # A non leaf node
    assert isinstance(node, tree_lib.NonLeaf)
    node_idx = self.num_non_leaf_nodes()

    # Set condition
    if isinstance(node.condition, tree_lib.NumericalHigherThanCondition):
      feature_idx = self.feature_spec.inv_numerical[node.condition.attribute]
      self.split_features.append(feature_idx)
      self.split_parameters.append(node.condition.threshold)
      self.condition_types.append(ConditionType.GREATER_THAN)

    elif isinstance(node.condition, tree_lib.CategoricalIsInCondition):
      feature_idx = self.feature_spec.inv_categorical[node.condition.attribute]
      column_spec = self.dataspec.columns[node.condition.attribute]
      bitmap = _categorical_list_to_bitmap(column_spec, node.condition.mask)

      offset = len(self.catgorical_mask)
      float_offset = float(
          jax.lax.bitcast_convert_type(
              jnp.array(offset, dtype=jnp.int32), jnp.float32
          )
      )

      self.split_features.append(feature_idx)
      self.split_parameters.append(float_offset)
      self.condition_types.append(ConditionType.IS_IN)
      self.catgorical_mask.extend(bitmap)
    else:
      # TODO: Add support for other types of conditions.
      raise ValueError(
          "The YDF Jax exporter does not support this condition type:"
          f" {node.condition}"
      )

    # Placeholders until the children node indices are computed
    self.positive_children.append(-1)
    self.negative_children.append(-1)

    # Populate child nodes
    neg_child_node = self._add_node(node.neg_child, begin_node_idx, depth + 1)
    pos_child_node = self._add_node(node.pos_child, begin_node_idx, depth + 1)

    # Index the children
    self.negative_children[node_idx] = neg_child_node.offset(begin_node_idx)
    self.positive_children[node_idx] = pos_child_node.offset(begin_node_idx)

    return NodeIdx(non_leaf_node=node_idx)


def _densify_conditions(
    src_conditions: ArrayInt,
) -> Tuple[Dict[int, int], ArrayInt]:
  """Creates a dense mapping of condition indices.

  For instance, if the model only uses conditions 1 and 3, creates the mapping
  { 1 -> 0, 3 -> 1} so condition index can be encoded as a value in [0, 2).
  So _densify_conditions([3, 1, 3]) == ({1: 0, 3: 1}, [1, 0, 1]).

  Args:
    src_conditions: List of conditions.

  Returns:
    Mapping of conditions and result of mapping applied to "conditions".
  """

  unique_conditions: List[int] = sorted(list(set(src_conditions)))  # pytype: disable=annotation-type-mismatch
  mapping = {old_id: new_id for new_id, old_id in enumerate(unique_conditions)}
  dst_conditions = [mapping[c] for c in src_conditions]
  return mapping, array.array("l", dst_conditions)


@dataclasses.dataclass
class InternalForestJaxArrays:
  """Jax arrays for each of the data fields in InternalForest."""

  forest: dataclasses.InitVar[InternalForest]
  leaf_outputs: jax.Array = dataclasses.field(init=False)
  split_features: jax.Array = dataclasses.field(init=False)
  split_parameters: jax.Array = dataclasses.field(init=False)
  negative_children: jax.Array = dataclasses.field(init=False)
  positive_children: jax.Array = dataclasses.field(init=False)
  dense_condition_mapping: Dict[int, int] = dataclasses.field(init=False)
  dense_condition_types: Optional[jax.Array] = dataclasses.field(init=False)
  root_nodes: jax.Array = dataclasses.field(init=False)
  begin_non_leaf_nodes: jax.Array = dataclasses.field(init=False)
  begin_leaf_nodes: jax.Array = dataclasses.field(init=False)
  catgorical_mask: Optional[jax.Array] = dataclasses.field(init=False)
  initial_predictions: jax.Array = dataclasses.field(init=False)

  def __post_init__(self, forest: InternalForest):
    asarray = jax.numpy.asarray

    self.leaf_outputs = asarray(forest.leaf_outputs, dtype=jnp.float32)
    self.split_features = to_compact_jax_array(forest.split_features)
    self.split_parameters = asarray(forest.split_parameters, dtype=jnp.float32)
    self.negative_children = to_compact_jax_array(forest.negative_children)
    self.positive_children = to_compact_jax_array(forest.positive_children)

    self.dense_condition_mapping, dense_condition_types = _densify_conditions(
        forest.condition_types
    )
    if len(self.dense_condition_mapping) == 1:
      self.dense_condition_types = None
    else:
      self.dense_condition_types = to_compact_jax_array(dense_condition_types)

    self.root_nodes = to_compact_jax_array(forest.root_nodes)
    self.begin_non_leaf_nodes = to_compact_jax_array(
        forest.begin_non_leaf_nodes
    )
    self.begin_leaf_nodes = to_compact_jax_array(forest.begin_leaf_nodes)

    if forest.catgorical_mask:
      self.catgorical_mask = asarray(forest.catgorical_mask, dtype=jnp.bool_)
    else:
      self.catgorical_mask = None

    self.initial_predictions = asarray(
        forest.initial_predictions, dtype=jnp.float32
    )


def to_jax_function(
    model: generic_model.GenericModel,
    jit: bool = True,
) -> Tuple[Any, Optional[FeatureEncoding]]:
  """Converts a model into a JAX function.

  Args:
    model: A YDF model.
    jit: If true, compiles the function with @jax.jit.

  Returns:
    A Jax function and optionally a FeatureEncoding object to encode
    features. If the model does not need any special feature
    encoding, the second returned value is None.
  """

  # TODO: Add support for Random Forest models.
  if not isinstance(
      model, gradient_boosted_trees_model.GradientBoostedTreesModel
  ):
    raise ValueError(
        "The YDF JAX convertor only support GBDT models. Instead, got model of"
        f" type {type(model)}"
    )

  forest = InternalForest(model)

  if forest.num_trees() == 0:
    raise ValueError(
        "The YDF JAX convertor only supports models with at least one tree"
    )

  if forest.num_non_leaf_nodes() == 0:
    raise ValueError(
        "The YDF JAX convertor does not support constant models e.g. models"
        " containing only stumps"
    )

  if len(forest.initial_predictions) != 1 and any(
      [v != 0.0 for v in forest.initial_predictions]
  ):
    raise ValueError(
        "JAX conversion does not support non-zero multi-dimensional"
        f" initial_predictions. Got {forest.initial_predictions!r}"
    )

  jax_arrays = InternalForestJaxArrays(forest)
  forest.clear_array_data()

  predict = functools.partial(_predict_fn, forest=forest, jax_arrays=jax_arrays)

  if jit:
    predict = jax.jit(predict)
  return predict, forest.feature_encoding


def _predict_fn(
    feature_values: Dict[str, jax.Array],
    forest: InternalForest,
    jax_arrays: InternalForestJaxArrays,
) -> jax.Array:
  """Computes the predictions of the model in Jax.

  Following are some notes about the implementation of the routing algorithm in
    JAX:
  - Because of the vmap operators, all branches of switchs / selects /
    for-conditions can be executed. Therefore, non-active branch should be
    robust to out-of-bounds array access. After this execution, only the result
    of the active branch is kept.
  - To minimize the amount of synchronizations between the host and device, the
    number of iterations of the routing algorithm is constant and equal to the
    maximum node depth access the entire model (instead of depending on the
    depth of the active leaf). When the routing algorithm reaches a leaf, it
    "loops on itself" until the pre-defined number of routing iterations are
    executed.
  - Model dependent optimizations can be applied during the generation of the
    XLA code. For example, if all the conditions have the same type, the
    condition switch block can be removed from the XLA code.

  Args:
    feature_values: Dictionary of input feature values.
    forest: Forest data.
    jax_arrays: JAX array data.

  Returns:
    Model predictions.
  """

  def predict_one_example(intern_feature_values):
    """Compute model predictions on a single example."""

    def predict_one_example_one_tree(
        root_node, begin_non_leaf_node, begin_leaf_node
    ):
      """Generates the prediction of a single tree on a single example."""

      node_offset_idx = jax.lax.fori_loop(
          0,
          forest.max_depth,
          functools.partial(
              _get_leaf_idx,
              begin_non_leaf_node=begin_non_leaf_node,
              intern_feature_values=intern_feature_values,
              jax_arrays=jax_arrays,
          ),
          root_node,
          unroll=True,
      )
      value_idx = begin_leaf_node - 1 - node_offset_idx
      return jax_arrays.leaf_outputs[value_idx]

    # Compute forest prediction.
    all_predictions = jax.vmap(predict_one_example_one_tree)(
        jax_arrays.root_nodes,
        jax_arrays.begin_non_leaf_nodes,
        jax_arrays.begin_leaf_nodes,
    )

    if len(forest.initial_predictions) == 1:
      raw_output = jax.numpy.sum(
          all_predictions, initial=jax_arrays.initial_predictions[0]
      )
    else:
      shaped_predictions = jax.numpy.reshape(
          all_predictions, (-1, len(forest.initial_predictions))
      )
      raw_output = jax.numpy.sum(shaped_predictions, axis=0)

    if forest.activation == custom_loss.Activation.IDENTITY:
      return raw_output
    elif forest.activation == custom_loss.Activation.SIGMOID:
      return jax.nn.sigmoid(raw_output)
    elif forest.activation == custom_loss.Activation.SOFTMAX:
      return jax.nn.softmax(raw_output)
    else:
      raise ValueError(f"Unsupported activation: {forest.activation!r}")

  # Process the feature values for the model consuption.
  intern_feature_values = dataclasses.asdict(
      forest.feature_spec.convert_features(feature_values)
  )

  return jax.vmap(predict_one_example)(intern_feature_values)


def _get_leaf_idx(
    iter_idx,
    node_offset,
    begin_non_leaf_node,
    intern_feature_values: Dict[str, jax.Array],
    jax_arrays: InternalForestJaxArrays,
):
  """Finds the leaf reached by an example using a routing algorithm.

  Args:
    iter_idx: Iterator index. Not used.
    node_offset: Current node offset.
    begin_non_leaf_node: Index of the root node of the tree.
    intern_feature_values: Feature values.
    jax_arrays: JAX array data.

  Returns:
    Active child node offset.
  """
  del iter_idx

  node_idx = node_offset + begin_non_leaf_node

  # Implementation of the various conditions.

  def condition_greater_than(node_idx):
    """Evaluates a "greather-than" condition."""
    feature_value = intern_feature_values["numerical"][
        jax_arrays.split_features[node_idx]
    ]
    return feature_value >= jax_arrays.split_parameters[node_idx]

  def condition_is_in(node_idx):
    """Evaluates a "is-in" condition."""
    feature_value = intern_feature_values["categorical"][
        jax_arrays.split_features[node_idx]
    ]
    categorical_mask_offset = feature_value + jax.lax.bitcast_convert_type(
        jax_arrays.split_parameters[node_idx], jnp.uint32
    )
    return jax_arrays.catgorical_mask[categorical_mask_offset]

  # Assemble the condition map.
  condition_fns = [None] * len(jax_arrays.dense_condition_mapping)
  if ConditionType.GREATER_THAN in jax_arrays.dense_condition_mapping:
    condition_fns[
        jax_arrays.dense_condition_mapping[ConditionType.GREATER_THAN]
    ] = condition_greater_than
  if ConditionType.IS_IN in jax_arrays.dense_condition_mapping:
    condition_fns[jax_arrays.dense_condition_mapping[ConditionType.IS_IN]] = (
        condition_is_in
    )

  if len(condition_fns) == 1:
    # Since there is only one type of conditions, there is not need for a
    # condition switch.
    assert jax_arrays.dense_condition_types is None
    condition_value = condition_fns[0](node_idx)

  else:
    # Condition switch on the type of conditions.
    assert jax_arrays.dense_condition_types is not None
    condition_value = jax.lax.switch(
        jax_arrays.dense_condition_types[node_idx],
        condition_fns,
        node_idx,
    )

  new_node_offset_if_non_leaf = jax.lax.select(
      condition_value,
      jax_arrays.positive_children[node_idx],
      jax_arrays.negative_children[node_idx],
  )

  # Repeats forever the leaf node if we are already in a leaf node.
  return jax.lax.select(
      node_offset >= 0,
      new_node_offset_if_non_leaf,  # Non-leaf
      node_offset,  # Leaf
  )
