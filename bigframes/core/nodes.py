# Copyright 2023 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

import abc
from dataclasses import dataclass, field, fields, replace
import datetime
import functools
import itertools
import typing
from typing import Callable, Tuple

import google.cloud.bigquery as bq

import bigframes.core.expression as ex
import bigframes.core.guid
from bigframes.core.join_def import JoinColumnMapping, JoinDefinition, JoinSide
from bigframes.core.ordering import OrderingExpression
import bigframes.core.schema as schemata
import bigframes.core.window_spec as window
import bigframes.dtypes
import bigframes.operations.aggregations as agg_ops

if typing.TYPE_CHECKING:
    import bigframes.core.ordering as orderings
    import bigframes.session


# A fixed number of variable to assume for overhead on some operations
OVERHEAD_VARIABLES = 5


@dataclass(frozen=True)
class BigFrameNode:
    """
    Immutable node for representing 2D typed array as a tree of operators.

    All subclasses must be hashable so as to be usable as caching key.
    """

    @property
    def deterministic(self) -> bool:
        """Whether this node will evaluates deterministically."""
        return True

    @property
    def row_preserving(self) -> bool:
        """Whether this node preserves input rows."""
        return True

    @property
    def non_local(self) -> bool:
        """
        Whether this node combines information across multiple rows instead of processing rows independently.
        Used as an approximation for whether the expression may require shuffling to execute (and therefore be expensive).
        """
        return False

    @property
    def child_nodes(self) -> typing.Sequence[BigFrameNode]:
        """Direct children of this node"""
        return tuple([])

    @functools.cached_property
    def session(self):
        sessions = []
        for child in self.child_nodes:
            if child.session is not None:
                sessions.append(child.session)
        unique_sessions = len(set(sessions))
        if unique_sessions > 1:
            raise ValueError("Cannot use combine sources from multiple sessions.")
        elif unique_sessions == 1:
            return sessions[0]
        return None

    # BigFrameNode trees can be very deep so its important avoid recalculating the hash from scratch
    # Each subclass of BigFrameNode should use this property to implement __hash__
    # The default dataclass-generated __hash__ method is not cached
    @functools.cached_property
    def _node_hash(self):
        return hash(tuple(hash(getattr(self, field.name)) for field in fields(self)))

    @property
    def roots(self) -> typing.Set[BigFrameNode]:
        roots = itertools.chain.from_iterable(
            map(lambda child: child.roots, self.child_nodes)
        )
        return set(roots)

    @property
    @abc.abstractmethod
    def schema(self) -> schemata.ArraySchema:
        ...

    @property
    @abc.abstractmethod
    def variables_introduced(self) -> int:
        """
        Defines number of values created by the current node. Helps represent the "width" of a query
        """
        ...

    @property
    def relation_ops_created(self) -> int:
        """
        Defines the number of relational ops generated by the current node. Used to estimate query planning complexity.
        """
        return 1

    @property
    def joins(self) -> bool:
        """
        Defines whether the node joins data.
        """
        return False

    @property
    @abc.abstractmethod
    def order_ambiguous(self) -> bool:
        """
        Whether row ordering is potentially ambiguous. For example, ReadTable (without a primary key) could be ordered in different ways.
        """
        ...

    @functools.cached_property
    def total_variables(self) -> int:
        return self.variables_introduced + sum(
            map(lambda x: x.total_variables, self.child_nodes)
        )

    @functools.cached_property
    def total_relational_ops(self) -> int:
        return self.relation_ops_created + sum(
            map(lambda x: x.total_relational_ops, self.child_nodes)
        )

    @functools.cached_property
    def total_joins(self) -> int:
        return int(self.joins) + sum(map(lambda x: x.total_joins, self.child_nodes))

    @property
    def planning_complexity(self) -> int:
        """
        Empirical heuristic measure of planning complexity.

        Used to determine when to decompose overly complex computations. May require tuning.
        """
        return self.total_variables * self.total_relational_ops * (1 + self.total_joins)

    @abc.abstractmethod
    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        """Apply a function to each child node."""
        ...


@dataclass(frozen=True)
class UnaryNode(BigFrameNode):
    child: BigFrameNode

    @property
    def child_nodes(self) -> typing.Sequence[BigFrameNode]:
        return (self.child,)

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        return self.child.schema

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return replace(self, child=t(self.child))

    @property
    def order_ambiguous(self) -> bool:
        return self.child.order_ambiguous


@dataclass(frozen=True)
class JoinNode(BigFrameNode):
    left_child: BigFrameNode
    right_child: BigFrameNode
    join: JoinDefinition

    @property
    def row_preserving(self) -> bool:
        return False

    @property
    def non_local(self) -> bool:
        return True

    @property
    def child_nodes(self) -> typing.Sequence[BigFrameNode]:
        return (self.left_child, self.right_child)

    @property
    def order_ambiguous(self) -> bool:
        return True

    def __hash__(self):
        return self._node_hash

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        def join_mapping_to_schema_item(mapping: JoinColumnMapping):
            result_id = mapping.destination_id
            result_dtype = (
                self.left_child.schema.get_type(mapping.source_id)
                if mapping.source_table == JoinSide.LEFT
                else self.right_child.schema.get_type(mapping.source_id)
            )
            return schemata.SchemaItem(result_id, result_dtype)

        items = tuple(
            join_mapping_to_schema_item(mapping) for mapping in self.join.mappings
        )
        return schemata.ArraySchema(items)

    @functools.cached_property
    def variables_introduced(self) -> int:
        """Defines the number of variables generated by the current node. Used to estimate query planning complexity."""
        return OVERHEAD_VARIABLES

    @property
    def joins(self) -> bool:
        return True

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return replace(
            self, left_child=t(self.left_child), right_child=t(self.right_child)
        )


@dataclass(frozen=True)
class ConcatNode(BigFrameNode):
    children: Tuple[BigFrameNode, ...]

    def __post_init__(self):
        if len(self.children) == 0:
            raise ValueError("Concat requires at least one input table. Zero provided.")
        child_schemas = [child.schema.dtypes for child in self.children]
        if not len(set(child_schemas)) == 1:
            raise ValueError("All inputs must have identical dtypes. {child_schemas}")

    @property
    def child_nodes(self) -> typing.Sequence[BigFrameNode]:
        return self.children

    @property
    def order_ambiguous(self) -> bool:
        return any(child.order_ambiguous for child in self.children)

    def __hash__(self):
        return self._node_hash

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        # TODO: Output names should probably be aligned beforehand or be part of concat definition
        items = tuple(
            schemata.SchemaItem(f"column_{i}", dtype)
            for i, dtype in enumerate(self.children[0].schema.dtypes)
        )
        return schemata.ArraySchema(items)

    @functools.cached_property
    def variables_introduced(self) -> int:
        """Defines the number of variables generated by the current node. Used to estimate query planning complexity."""
        return len(self.schema.items) + OVERHEAD_VARIABLES

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return replace(self, children=tuple(t(child) for child in self.children))


# Input Nodex
@dataclass(frozen=True)
class ReadLocalNode(BigFrameNode):
    feather_bytes: bytes
    data_schema: schemata.ArraySchema
    session: typing.Optional[bigframes.session.Session] = None

    def __hash__(self):
        return self._node_hash

    @property
    def roots(self) -> typing.Set[BigFrameNode]:
        return {self}

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        return self.data_schema

    @functools.cached_property
    def variables_introduced(self) -> int:
        """Defines the number of variables generated by the current node. Used to estimate query planning complexity."""
        return len(self.schema.items) + 1

    @property
    def order_ambiguous(self) -> bool:
        return False

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return self


## Put ordering in here or just add order_by node above?
@dataclass(frozen=True)
class ReadTableNode(BigFrameNode):
    project_id: str = field()
    dataset_id: str = field()
    table_id: str = field()

    physical_schema: Tuple[bq.SchemaField, ...] = field()
    # Subset of physical schema columns, with chosen BQ types
    columns: schemata.ArraySchema = field()

    table_session: bigframes.session.Session = field()
    # Empty tuple if no primary key (primary key can be any set of columns that together form a unique key)
    # Empty if no known unique key
    total_order_cols: Tuple[str, ...] = field()
    # indicates a primary key that is exactly offsets 0, 1, 2, ..., N-2, N-1
    order_col_is_sequential: bool = False
    at_time: typing.Optional[datetime.datetime] = None
    # Added for backwards compatibility, not validated
    sql_predicate: typing.Optional[str] = None

    def __post_init__(self):
        # enforce invariants
        physical_names = set(map(lambda i: i.name, self.physical_schema))
        if not set(self.columns.names).issubset(physical_names):
            raise ValueError(
                f"Requested schema {self.columns} cannot be derived from table schemal {self.physical_schema}"
            )
        if self.order_col_is_sequential and len(self.total_order_cols) != 1:
            raise ValueError("Sequential primary key must have only one component")

    @property
    def session(self):
        return self.table_session

    def __hash__(self):
        return self._node_hash

    @property
    def roots(self) -> typing.Set[BigFrameNode]:
        return {self}

    @property
    def schema(self) -> schemata.ArraySchema:
        return self.columns

    @property
    def relation_ops_created(self) -> int:
        # Assume worst case, where readgbq actually has baked in analytic operation to generate index
        return 3

    @property
    def order_ambiguous(self) -> bool:
        return len(self.total_order_cols) == 0

    @functools.cached_property
    def variables_introduced(self) -> int:
        return len(self.schema.items) + 1

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return self


# This node shouldn't be used in the "original" expression tree, only used as replacement for original during planning
@dataclass(frozen=True)
class CachedTableNode(BigFrameNode):
    # The original BFET subtree that was cached
    # note: this isn't a "child" node.
    original_node: BigFrameNode = field()
    # reference to cached materialization of original_node
    project_id: str = field()
    dataset_id: str = field()
    table_id: str = field()
    physical_schema: Tuple[bq.SchemaField, ...] = field()

    ordering: typing.Optional[orderings.RowOrdering] = field()

    def __post_init__(self):
        # enforce invariants
        physical_names = set(map(lambda i: i.name, self.physical_schema))
        logical_names = self.original_node.schema.names
        if not set(logical_names).issubset(physical_names):
            raise ValueError(
                f"Requested schema {logical_names} cannot be derived from table schema {self.physical_schema}"
            )
        if not set(self.hidden_columns).issubset(physical_names):
            raise ValueError(
                f"Requested hidden columns {self.hidden_columns} cannot be derived from table schema {self.physical_schema}"
            )

    @property
    def session(self):
        return self.original_node.session

    def __hash__(self):
        return self._node_hash

    @property
    def roots(self) -> typing.Set[BigFrameNode]:
        return {self}

    @property
    def schema(self) -> schemata.ArraySchema:
        return self.original_node.schema

    @functools.cached_property
    def variables_introduced(self) -> int:
        return len(self.schema.items) + OVERHEAD_VARIABLES

    @property
    def hidden_columns(self) -> typing.Tuple[str, ...]:
        """Physical columns used to define ordering but not directly exposed as value columns."""
        if self.ordering is None:
            return ()
        return tuple(
            col
            for col in sorted(self.ordering.referenced_columns)
            if col not in self.schema.names
        )

    @property
    def order_ambiguous(self) -> bool:
        return not isinstance(self.ordering, orderings.TotalOrdering)

    def transform_children(
        self, t: Callable[[BigFrameNode], BigFrameNode]
    ) -> BigFrameNode:
        return self


# Unary nodes
@dataclass(frozen=True)
class PromoteOffsetsNode(UnaryNode):
    col_id: str

    def __hash__(self):
        return self._node_hash

    @property
    def non_local(self) -> bool:
        return True

    @property
    def schema(self) -> schemata.ArraySchema:
        return self.child.schema.prepend(
            schemata.SchemaItem(self.col_id, bigframes.dtypes.INT_DTYPE)
        )

    @property
    def relation_ops_created(self) -> int:
        return 2

    @functools.cached_property
    def variables_introduced(self) -> int:
        return 1


@dataclass(frozen=True)
class FilterNode(UnaryNode):
    predicate: ex.Expression

    @property
    def row_preserving(self) -> bool:
        return False

    def __hash__(self):
        return self._node_hash

    @property
    def variables_introduced(self) -> int:
        return 1


@dataclass(frozen=True)
class OrderByNode(UnaryNode):
    by: Tuple[OrderingExpression, ...]

    def __post_init__(self):
        available_variables = self.child.schema.names
        for order_expr in self.by:
            for variable in order_expr.scalar_expression.unbound_variables:
                if variable not in available_variables:
                    raise ValueError(
                        f"Cannot over unknown id:{variable}, columns are {available_variables}"
                    )

    def __hash__(self):
        return self._node_hash

    @property
    def variables_introduced(self) -> int:
        return 0

    @property
    def relation_ops_created(self) -> int:
        # Doesnt directly create any relational operations
        return 0


@dataclass(frozen=True)
class ReversedNode(UnaryNode):
    # useless field to make sure has distinct hash
    reversed: bool = True

    def __hash__(self):
        return self._node_hash

    @property
    def variables_introduced(self) -> int:
        return 0

    @property
    def relation_ops_created(self) -> int:
        # Doesnt directly create any relational operations
        return 0


@dataclass(frozen=True)
class ProjectionNode(UnaryNode):
    assignments: typing.Tuple[typing.Tuple[ex.Expression, str], ...]

    def __post_init__(self):
        input_types = self.child.schema._mapping
        for expression, id in self.assignments:
            # throws TypeError if invalid
            _ = expression.output_type(input_types)

    def __hash__(self):
        return self._node_hash

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        input_types = self.child.schema._mapping
        items = tuple(
            schemata.SchemaItem(
                id, bigframes.dtypes.dtype_for_etype(ex.output_type(input_types))
            )
            for ex, id in self.assignments
        )
        return schemata.ArraySchema(items)

    @property
    def variables_introduced(self) -> int:
        # ignore passthrough expressions
        new_vars = sum(1 for i in self.assignments if not i[0].is_identity)
        return new_vars


# TODO: Merge RowCount into Aggregate Node?
# Row count can be compute from table metadata sometimes, so it is a bit special.
@dataclass(frozen=True)
class RowCountNode(UnaryNode):
    @property
    def row_preserving(self) -> bool:
        return False

    @property
    def non_local(self) -> bool:
        return True

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        return schemata.ArraySchema(
            (schemata.SchemaItem("count", bigframes.dtypes.INT_DTYPE),)
        )

    @property
    def variables_introduced(self) -> int:
        return 1


@dataclass(frozen=True)
class AggregateNode(UnaryNode):
    aggregations: typing.Tuple[typing.Tuple[ex.Aggregation, str], ...]
    by_column_ids: typing.Tuple[str, ...] = tuple([])
    dropna: bool = True

    @property
    def row_preserving(self) -> bool:
        return False

    def __hash__(self):
        return self._node_hash

    @property
    def non_local(self) -> bool:
        return True

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        by_items = tuple(
            schemata.SchemaItem(id, self.child.schema.get_type(id))
            for id in self.by_column_ids
        )
        input_types = self.child.schema._mapping
        agg_items = tuple(
            schemata.SchemaItem(
                id, bigframes.dtypes.dtype_for_etype(agg.output_type(input_types))
            )
            for agg, id in self.aggregations
        )
        return schemata.ArraySchema(tuple([*by_items, *agg_items]))

    @property
    def variables_introduced(self) -> int:
        return len(self.aggregations) + len(self.by_column_ids)

    @property
    def order_ambiguous(self) -> bool:
        return False


@dataclass(frozen=True)
class WindowOpNode(UnaryNode):
    column_name: str
    op: agg_ops.UnaryWindowOp
    window_spec: window.WindowSpec
    output_name: typing.Optional[str] = None
    never_skip_nulls: bool = False
    skip_reproject_unsafe: bool = False

    def __hash__(self):
        return self._node_hash

    @property
    def non_local(self) -> bool:
        return True

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        input_type = self.child.schema.get_type(self.column_name)
        new_item_dtype = self.op.output_type(input_type)
        if self.output_name is None:
            return self.child.schema.update_dtype(self.column_name, new_item_dtype)
        if self.output_name in self.child.schema.names:
            return self.child.schema.update_dtype(self.output_name, new_item_dtype)
        return self.child.schema.append(
            schemata.SchemaItem(self.output_name, new_item_dtype)
        )

    @property
    def variables_introduced(self) -> int:
        return 1

    @property
    def relation_ops_created(self) -> int:
        # Assume that if not reprojecting, that there is a sequence of window operations sharing the same window
        return 0 if self.skip_reproject_unsafe else 4


# TODO: Remove this op
@dataclass(frozen=True)
class ReprojectOpNode(UnaryNode):
    def __hash__(self):
        return self._node_hash

    @property
    def variables_introduced(self) -> int:
        return 0

    @property
    def relation_ops_created(self) -> int:
        # This op is not a real transformation, just a hint to the sql generator
        return 0


@dataclass(frozen=True)
class RandomSampleNode(UnaryNode):
    fraction: float

    @property
    def deterministic(self) -> bool:
        return False

    @property
    def row_preserving(self) -> bool:
        return False

    def __hash__(self):
        return self._node_hash

    @property
    def variables_introduced(self) -> int:
        return 1


@dataclass(frozen=True)
class ExplodeNode(UnaryNode):
    column_ids: typing.Tuple[str, ...]

    @property
    def row_preserving(self) -> bool:
        return False

    def __hash__(self):
        return self._node_hash

    @functools.cached_property
    def schema(self) -> schemata.ArraySchema:
        items = tuple(
            schemata.SchemaItem(
                name,
                bigframes.dtypes.arrow_dtype_to_bigframes_dtype(
                    self.child.schema.get_type(name).pyarrow_dtype.value_type
                ),
            )
            if name in self.column_ids
            else schemata.SchemaItem(name, self.child.schema.get_type(name))
            for name in self.child.schema.names
        )
        return schemata.ArraySchema(items)

    @property
    def relation_ops_created(self) -> int:
        return 3

    @functools.cached_property
    def variables_introduced(self) -> int:
        return len(self.column_ids) + 1
