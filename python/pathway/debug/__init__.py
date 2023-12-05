# Copyright © 2023 Pathway

from __future__ import annotations

import functools
import io
import itertools
import re
from collections.abc import Iterable
from os import PathLike
from warnings import warn

import pandas as pd

from pathway import persistence
from pathway.internals import Json, api, parse_graph
from pathway.internals.datasource import DataSourceOptions, PandasDataSource
from pathway.internals.decorators import table_from_datasource
from pathway.internals.fingerprints import fingerprint
from pathway.internals.graph_runner import GraphRunner
from pathway.internals.monitoring import MonitoringLevel
from pathway.internals.runtime_type_check import runtime_type_check
from pathway.internals.schema import Schema, schema_from_pandas
from pathway.internals.table import Table
from pathway.internals.trace import trace_user_frame
from pathway.io._utils import read_schema
from pathway.io.python import ConnectorSubject, read


@runtime_type_check
def _compute_table(table: Table) -> api.CapturedStream:
    [captured] = GraphRunner(
        parse_graph.G, debug=True, monitoring_level=MonitoringLevel.NONE
    ).run_tables(table)
    return captured


def table_to_dicts(
    table: Table,
) -> tuple[list[api.Pointer], dict[str, dict[api.Pointer, api.Value]]]:
    captured = _compute_table(table)
    output_data = api.squash_updates(captured)
    keys = list(output_data.keys())
    columns = {
        name: {key: output_data[key][index] for key in keys}
        for index, name in enumerate(table._columns.keys())
    }
    return keys, columns


@functools.total_ordering
class _NoneAwareComparisonWrapper:
    def __init__(self, inner):
        if isinstance(inner, dict | Json):
            self.inner = str(inner)
        else:
            self.inner = inner

    def __eq__(self, other):
        if not isinstance(other, _NoneAwareComparisonWrapper):
            return NotImplemented
        return self.inner == other.inner

    def __lt__(self, other):
        if not isinstance(other, _NoneAwareComparisonWrapper):
            return NotImplemented
        if self.inner is None:
            return other.inner is not None
        if other.inner is None:
            return False
        return self.inner < other.inner


def _compute_and_print_internal(
    table: Table,
    *,
    squash_updates: bool,
    include_id: bool,
    short_pointers: bool,
    n_rows: int | None,
) -> None:
    captured = _compute_table(table)
    columns = list(table._columns.keys())
    if squash_updates:
        output_data = list(api.squash_updates(captured).items())
    else:
        columns.extend([api.TIME_PSEUDOCOLUMN, api.DIFF_PSEUDOCOLUMN])
        output_data = []
        for row in captured:
            output_data.append((row.key, tuple(row.values) + (row.time, row.diff)))

    if not columns and not include_id:
        return

    if include_id or len(columns) > 1:
        none = ""
    else:
        none = "None"

    def _format(x):
        if x is None:
            return none
        if isinstance(x, api.Pointer) and short_pointers:
            s = str(x)
            if len(s) > 8:
                s = s[:8] + "..."
            return s
        return str(x)

    if squash_updates:

        def _key(row: tuple[api.Pointer, tuple[api.Value, ...]]):
            return tuple(_NoneAwareComparisonWrapper(value) for value in row[1])

    else:
        # sort by time and diff first if there is no squashing
        def _key(row: tuple[api.Pointer, tuple[api.Value, ...]]):
            return row[1][-2:] + tuple(
                _NoneAwareComparisonWrapper(value) for value in row[1]
            )

    try:
        output_data = sorted(output_data, key=_key)
    except ValueError:
        pass  # Some values (like arrays) cannot be sorted this way, so just don't sort them.
    output_data_truncated = itertools.islice(output_data, n_rows)
    data = []
    if include_id:
        name = "" if columns else "id"
        data.append([name] + columns)
    else:
        data.append(columns)
    for key, values in output_data_truncated:
        formatted_row = []
        if include_id:
            formatted_row.append(_format(key))
        formatted_row.extend(_format(value) for value in values)
        data.append(formatted_row)
    max_lens = [max(len(row[i]) for row in data) for i in range(len(data[0]))]
    max_lens[-1] = 0
    for row in data:
        formatted = " | ".join(
            value.ljust(max_len) for value, max_len in zip(row, max_lens)
        )
        print(formatted.rstrip())


@runtime_type_check
@trace_user_frame
def compute_and_print(
    table: Table,
    *,
    include_id=True,
    short_pointers=True,
    n_rows: int | None = None,
) -> None:
    """
    A function running the computations and printing the table.
    Args:
        table: a table to be computed and printed
        include_id: whether to show ids of rows
        short_pointers: whether to shorten printed ids
        n_rows: number of rows to print, if None whole table will be printed
    """
    _compute_and_print_internal(
        table,
        squash_updates=True,
        include_id=include_id,
        short_pointers=short_pointers,
        n_rows=n_rows,
    )


@runtime_type_check
@trace_user_frame
def compute_and_print_update_stream(
    table: Table,
    *,
    include_id=True,
    short_pointers=True,
    n_rows: int | None = None,
) -> None:
    """
    A function running the computations and printing the update stream of the table.
    Args:
        table: a table for which the update stream is to be computed and printed
        include_id: whether to show ids of rows
        short_pointers: whether to shorten printed ids
        n_rows: number of rows to print, if None whole update stream will be printed
    """
    _compute_and_print_internal(
        table,
        squash_updates=False,
        include_id=include_id,
        short_pointers=short_pointers,
        n_rows=n_rows,
    )


@runtime_type_check
@trace_user_frame
def table_to_pandas(table: Table, *, include_id: bool = True):
    keys, columns = table_to_dicts(table)
    if include_id:
        res = pd.DataFrame(columns, index=keys)
    else:
        # we need to remove keys, otherwise pandas will use them to create index
        columns_wo_keys = {name: columns[name].values() for name in columns}
        res = pd.DataFrame(columns_wo_keys)
    return res


def _validate_dataframe(df: pd.DataFrame) -> None:
    for pseudocolumn in api.PANDAS_PSEUDOCOLUMNS:
        if pseudocolumn in df.columns:
            if not pd.api.types.is_integer_dtype(df[pseudocolumn].dtype):
                raise ValueError(f"Column {pseudocolumn} has to contain integers only.")
    if api.TIME_PSEUDOCOLUMN in df.columns:
        if any(df[api.TIME_PSEUDOCOLUMN] < 0):
            raise ValueError(
                f"Column {api.TIME_PSEUDOCOLUMN} cannot contain negative times."
            )
        if any(df[api.TIME_PSEUDOCOLUMN] % 2 == 1):
            warn("timestamps are required to be even; all timestamps will be doubled")
            df[api.TIME_PSEUDOCOLUMN] = 2 * df[api.TIME_PSEUDOCOLUMN]

    if api.DIFF_PSEUDOCOLUMN in df.columns:
        if any((df[api.DIFF_PSEUDOCOLUMN] != 1) & (df[api.DIFF_PSEUDOCOLUMN] != -1)):
            raise ValueError(
                f"Column {api.DIFF_PSEUDOCOLUMN} can only have 1 and -1 values."
            )


@runtime_type_check
@trace_user_frame
def table_from_pandas(
    df: pd.DataFrame,
    id_from: list[str] | None = None,
    unsafe_trusted_ids: bool = False,
    schema: type[Schema] | None = None,
) -> Table:
    """
    A function for creating a table from a pandas DataFrame. If it contains a special
    column ``__time__``, rows will be split into batches with timestamps from the column.
    A special column ``__diff__`` can be used to set an event type - with ``1`` treated
    as inserting the row and ``-1`` as removing it.
    """
    if id_from is not None and schema is not None:
        raise ValueError("parameters `schema` and `id_from` are mutually exclusive")

    ordinary_columns_names = [
        column for column in df.columns if column not in api.PANDAS_PSEUDOCOLUMNS
    ]
    if schema is None:
        schema = schema_from_pandas(
            df, id_from=id_from, exclude_columns=api.PANDAS_PSEUDOCOLUMNS
        )
    elif ordinary_columns_names != schema.column_names():
        raise ValueError("schema does not match given dataframe")

    _validate_dataframe(df)

    if id_from is None:
        ids_df = pd.DataFrame({"id": df.index})
        ids_df.index = df.index
    else:
        ids_df = df[id_from].copy()

    for column in api.PANDAS_PSEUDOCOLUMNS:
        if column in df.columns:
            ids_df[column] = df[column]

    as_hashes = [fingerprint(x) for x in ids_df.to_dict(orient="records")]
    key = fingerprint((unsafe_trusted_ids, sorted(as_hashes)))

    ret: Table = table_from_datasource(
        PandasDataSource(
            schema=schema,
            data=df.copy(),
            data_source_options=DataSourceOptions(
                unsafe_trusted_ids=unsafe_trusted_ids,
            ),
        )
    )
    from pathway.internals.parse_graph import G

    if key in G.static_tables_cache:
        ret = ret.with_universe_of(G.static_tables_cache[key])
    else:
        G.static_tables_cache[key] = ret

    return ret


def _markdown_to_pandas(table_def):
    table_def = table_def.lstrip("\n")
    sep = r"(?:\s*\|\s*)|\s+"
    header = table_def.partition("\n")[0].strip()
    column_names = re.split(sep, header)
    for index, name in enumerate(column_names):
        if name in ("", "id"):
            index_col = index
            break
    else:
        index_col = None
    return pd.read_table(
        io.StringIO(table_def),
        sep=sep,
        index_col=index_col,
        engine="python",
        na_values=("", "None", "NaN", "nan", "NA", "NULL"),
        keep_default_na=False,
    ).convert_dtypes()


def table_from_markdown(
    table_def,
    id_from=None,
    unsafe_trusted_ids=False,
    schema: type[Schema] | None = None,
) -> Table:
    """
    A function for creating a table from its definition in markdown. If it contains a special
    column ``__time__``, rows will be split into batches with timestamps from the column.
    A special column ``__diff__`` can be used to set an event type - with ``1`` treated
    as inserting the row and ``-1`` as removing it.
    """
    df = _markdown_to_pandas(table_def)
    return table_from_pandas(
        df, id_from=id_from, unsafe_trusted_ids=unsafe_trusted_ids, schema=schema
    )


def parse_to_table(*args, **kwargs) -> Table:
    warn(
        "pw.debug.parse_to_table is deprecated, use pw.debug.table_from_markdown instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return table_from_markdown(*args, **kwargs)


@runtime_type_check
def table_from_parquet(
    path: str | PathLike,
    id_from=None,
    unsafe_trusted_ids=False,
) -> Table:
    """
    Reads a Parquet file into a pandas DataFrame and then converts that into a Pathway table.
    """

    df = pd.read_parquet(path)
    return table_from_pandas(df, id_from=None, unsafe_trusted_ids=False)


@runtime_type_check
def table_to_parquet(table: Table, filename: str | PathLike):
    """
    Converts a Pathway Table into a pandas DataFrame and then writes it to Parquet
    """
    df = table_to_pandas(table)
    df = df.reset_index()
    df = df.drop(["index"], axis=1)
    return df.to_parquet(filename)


class _EmptyConnectorSubject(ConnectorSubject):
    def run(self):
        pass


class StreamGenerator:
    _persistent_id = itertools.count()
    events: dict[tuple[str, int], list[api.SnapshotEvent]] = {}

    def _get_next_persistent_id(self) -> str:
        return str(f"_stream_generator_{next(self._persistent_id)}")

    def _advance_time_for_all_workers(
        self, persistent_id: str, workers: Iterable[int], timestamp: int
    ):
        for worker in workers:
            self.events[(persistent_id, worker)].append(
                api.SnapshotEvent.advance_time(timestamp)
            )

    def _table_from_dict(
        self,
        batches: dict[int, dict[int, list[tuple[int, api.Pointer, list[api.Value]]]]],
        schema: type[Schema],
    ) -> Table:
        """
        A function that creates a table from a mapping of timestamps to batches. Each batch
        is a mapping from worker id to list of rows processed in this batch by this worker,
        and each row is tuple (diff, key, values).

        Note: unless you need to specify timestamps and keys, consider using
        `table_from_list_of_batches` and `table_from_list_of_batches_by_workers`.

        Args:
            batches: dictionary with specified batches to be put in the table
            schema: schema of the table
        """
        persistent_id = self._get_next_persistent_id()
        workers = set([worker for batch in batches.values() for worker in batch])
        for worker in workers:
            self.events[(persistent_id, worker)] = []

        timestamps = set(batches.keys())

        if any(timestamp for timestamp in timestamps if timestamp < 0):
            raise ValueError("negative timestamp cannot be used")
        elif any(timestamp for timestamp in timestamps if timestamp == 0):
            warn(
                "rows with timestamp 0 are only backfilled and are not processed by output connectors"
            )

        if any(timestamp for timestamp in timestamps if timestamp % 2 == 1):
            warn("timestamps are required to be even; all timestamps will be doubled")
            batches = {2 * timestamp: batches[timestamp] for timestamp in batches}

        for timestamp in sorted(batches):
            self._advance_time_for_all_workers(persistent_id, workers, timestamp)
            batch = batches[timestamp]
            for worker, changes in batch.items():
                for diff, key, values in changes:
                    if diff == 1:
                        event = api.SnapshotEvent.insert(key, values)
                        self.events[(persistent_id, worker)] += [event] * diff
                    elif diff == -1:
                        event = api.SnapshotEvent.delete(key, values)
                        self.events[(persistent_id, worker)] += [event] * (-diff)
                    else:
                        raise ValueError("only diffs of 1 and -1 are supported")

        return read(
            _EmptyConnectorSubject(), persistent_id=persistent_id, schema=schema
        )

    def table_from_list_of_batches_by_workers(
        self,
        batches: list[dict[int, list[dict[str, api.Value]]]],
        schema: type[Schema],
    ) -> Table:
        """
        A function that creates a table from a list of batches, where each batch is a mapping
        from worker id to a list of rows processed by this worker in this batch.
        Each row is a mapping from column name to a value.

        Args:
            batches: list of batches to be put in the table
            schema: schema of the table
        """
        key = itertools.count()
        schema, api_schema = read_schema(schema=schema)
        value_fields: list[api.ValueField] = api_schema["value_fields"]

        def next_key() -> api.Pointer:
            api_key = api.ref_scalar(next(key))
            return api_key

        def add_diffs_and_keys(list_of_values: list[dict[str, api.Value]]):
            return [
                (1, next_key(), [values[field.name] for field in value_fields])
                for values in list_of_values
            ]

        formatted_batches: dict[
            int, dict[int, list[tuple[int, api.Pointer, list[api.Value]]]]
        ] = {}
        timestamp = itertools.count(2, 2)

        for batch in batches:
            changes = {worker: add_diffs_and_keys(batch[worker]) for worker in batch}
            formatted_batches[next(timestamp)] = changes

        return self._table_from_dict(formatted_batches, schema)

    def table_from_list_of_batches(
        self,
        batches: list[list[dict[str, api.Value]]],
        schema: type[Schema],
    ) -> Table:
        """
        A function that creates a table from a list of batches, where each batch is a list of
        rows in this batch. Each row is a mapping from column name to a value.

        Args:
            batches: list of batches to be put in the table
            schema: schema of the table
        """
        batches_by_worker = [{0: batch} for batch in batches]
        return self.table_from_list_of_batches_by_workers(batches_by_worker, schema)

    def table_from_pandas(
        self,
        df: pd.DataFrame,
        id_from: list[str] | None = None,
        unsafe_trusted_ids: bool = False,
        schema: type[Schema] | None = None,
    ) -> Table:
        """
        A function for creating a table from a pandas DataFrame. If the DataFrame
        contains a column ``_time``, rows will be split into batches with timestamps from ``_time`` column.
        Then ``_worker`` column will be interpreted as the id of a worker which will process the row and
        ``_diff`` column as an event type with ``1`` treated as inserting row and ``-1`` as removing.
        """
        if schema is None:
            schema = schema_from_pandas(
                df, exclude_columns={"_time", "_diff", "_worker"}
            )
        schema, api_schema = read_schema(schema=schema)
        value_fields: list[api.ValueField] = api_schema["value_fields"]

        if "_time" not in df:
            df["_time"] = [2] * len(df)
        if "_worker" not in df:
            df["_worker"] = [0] * len(df)
        if "_diff" not in df:
            df["_diff"] = [1] * len(df)

        batches: dict[
            int, dict[int, list[tuple[int, api.Pointer, list[api.Value]]]]
        ] = {}

        ids = api.ids_from_pandas(
            df, api.ConnectorProperties(unsafe_trusted_ids=unsafe_trusted_ids), id_from
        )

        for row_index in range(len(df)):
            row = df.iloc[row_index]
            time = row["_time"]
            key = ids[df.index[row_index]]
            worker = row["_worker"]

            if time not in batches:
                batches[time] = {}

            if worker not in batches[time]:
                batches[time][worker] = []

            values = []
            for value_field in value_fields:
                column = value_field.name
                value = api.denumpify(row[column])
                values.append(value)
            diff = row["_diff"]

            batches[time][worker].append((diff, key, values))

        return self._table_from_dict(batches, schema)

    def table_from_markdown(
        self,
        table: str,
        id_from: list[str] | None = None,
        unsafe_trusted_ids: bool = False,
        schema: type[Schema] | None = None,
    ) -> Table:
        """
        A function for creating a table from its definition in markdown. If it
        contains a column ``_time``, rows will be split into batches with timestamps from ``_time`` column.
        Then ``_worker`` column will be interpreted as the id of a worker which will process the row and
        ``_diff`` column as an event type - with ``1`` treated as inserting row and ``-1`` as removing.
        """
        df = _markdown_to_pandas(table)
        return self.table_from_pandas(df, id_from, unsafe_trusted_ids, schema)

    def persistence_config(self) -> persistence.Config | None:
        """
        Returns a persistece config to be used during run. Needs to be passed to ``pw.run``
        so that tables created using StreamGenerator are filled with data.
        """

        if len(self.events) == 0:
            return None
        return persistence.Config.simple_config(
            persistence.Backend.mock(self.events),
            snapshot_access=api.SnapshotAccess.REPLAY,
            persistence_mode=api.PersistenceMode.SPEEDRUN_REPLAY,
        )
