# Copyright 2018 HTCondor Team, Computer Sciences Department,
# University of Wisconsin-Madison, WI.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Tuple, List, Iterable, Any, Optional, Union, Callable, Iterator, Dict
import logging

import datetime
import enum
import shutil
import time
import functools
import inspect
import collections
from copy import copy
from pathlib import Path

from tqdm import tqdm

import htcondor
import classad

from . import htio, exceptions, utils, mapping

logger = logging.getLogger(__name__)


class Status(enum.IntEnum):
    IDLE = 1
    RUNNING = 2
    REMOVED = 3
    COMPLETED = 4
    HELD = 5
    TRANSFERRING_OUTPUT = 6
    SUSPENDED = 7

    def __str__(self):
        return JOB_STATUS_STRINGS[self]

    @classmethod
    def display_statuses(cls) -> Tuple['Status', ...]:
        return (
            cls.HELD,
            cls.IDLE,
            cls.RUNNING,
            cls.COMPLETED,
        )


JOB_STATUS_STRINGS = {
    Status.IDLE: 'Idle',
    Status.RUNNING: 'Run',
    Status.REMOVED: 'Removed',
    Status.COMPLETED: 'Done',
    Status.HELD: 'Held',
    Status.TRANSFERRING_OUTPUT: 'Transferring Output',
    Status.SUSPENDED: 'Suspended',
}


def _protector(method):
    @functools.wraps(method)
    def _protect(self, *args, **kwargs):
        if self._is_removed:
            raise exceptions.MapWasRemoved(f'cannot call {method} for map {self.map_id} because it has been removed')
        return method(self, *args, **kwargs)

    return _protect


def _protect_map_after_remove(result_class):
    # decorate all public instance methods
    for key, member in inspect.getmembers(result_class, predicate = inspect.isfunction):
        if not key.startswith('_'):
            setattr(result_class, key, _protector(member))

    return result_class


MAPS = {}


@_protect_map_after_remove
class Map:
    """
    Represents the results from a map call.
    The constructor is documented here, but you should never need to build a :class:`Map` manually.
    Instead, you'll get your :class:`Map` by calling a :class:`MappedFunction` class method or by using :func:`htmap.recover`.
    """

    def __init__(self, map_id: str, cluster_ids: List[int], submit, hashes: Iterable[str]):
        """
        Parameters
        ----------
        map_id
            The ``map_id`` to assign to this :class:`Map`.
        cluster_ids
            All of the ``cluster_id`` for the jobs associated with this :class:`Map`.
            This is an implementation detail and should not be relied on.
        hashes
            The hashes of the inputs for this :class:`Map`.
            This is an implementation detail and should not be relied on.
        """
        self.map_id = map_id
        self.cluster_ids = cluster_ids
        self.submit = submit
        self.hashes = tuple(hashes)
        self.hash_set = set(self.hashes)

        self._is_removed = False

        MAPS[self.map_id] = self

    @classmethod
    def recover(cls, map_id: str) -> 'Map':
        """
        Reconstruct a :class:`Map` from its ``map_id``.

        Raises :class:`htmap.exceptions.MapIdNotFound` if the ``map_id`` does not exist.

        Parameters
        ----------
        map_id
            The ``map_id`` to search for.

        Returns
        -------
        result
            The result with the given ``map_id``.
        """
        try:
            return MAPS[map_id]
        except KeyError:
            pass

        map_dir = mapping.map_dir_path(map_id)
        try:
            with (map_dir / 'cluster_ids').open() as file:
                cluster_ids = [int(cid.strip()) for cid in file]

            hashes = htio.load_hashes(map_dir)
            submit = htio.load_submit(map_dir)

        except FileNotFoundError:
            raise exceptions.MapIdNotFound(f'the map_id {map_id} could not be found')

        logger.debug(f'recovered map result for map {map_id} from {map_dir}')

        return cls(
            map_id = map_id,
            cluster_ids = cluster_ids,
            submit = submit,
            hashes = hashes,
        )

    def __repr__(self):
        return f'<{self.__class__.__name__}(map_id = {self.map_id})>'

    def __len__(self):
        """The length of a :class:`Map` is the number of inputs it contains."""
        return len(self.hashes)

    @property
    def _map_dir(self) -> Path:
        """The path to the map directory."""
        return mapping.map_dir_path(self.map_id)

    @property
    def _inputs_dir(self) -> Path:
        """The path to the inputs directory, inside the map directory."""
        return self._map_dir / 'inputs'

    @property
    def _outputs_dir(self) -> Path:
        """The path to the outputs directory, inside the map directory."""
        return self._map_dir / 'outputs'

    @property
    def _input_file_paths(self):
        """The paths to the input files."""
        yield from (self._inputs_dir / f'{h}.in' for h in self.hashes)

    @property
    def _output_file_paths(self):
        """The paths to the output files."""
        yield from (self._outputs_dir / f'{h}.out' for h in self.hashes)

    def _remove_from_queue(self):
        return self._act(htcondor.JobAction.Remove)

    def _rm_map_dir(self):
        shutil.rmtree(self._map_dir)
        logger.debug(f'removed map directory for map {self.map_id}')

    def _clean_outputs_dir(self):
        utils.clean_dir(self._outputs_dir)

    def _item_to_hash(self, item: int) -> str:
        """Return the hash associated with an input index."""
        return self.hashes[item]

    def __getitem__(self, item: int) -> Any:
        """Return the output associated with the input index. Does not block."""
        return self.get(item, timeout = 0)

    @property
    def _missing_hashes(self) -> List[str]:
        """Return a list of input hashes that don't have output, ordered by input index."""
        done = set(f.stem for f in self._outputs_dir.iterdir())
        return [h for h in self.hashes if h not in done]

    @property
    def _completed_hashes(self) -> List[str]:
        """Return a list of input hashes that do have output, ordered by input index."""
        done = set(f.stem for f in self._outputs_dir.iterdir())
        return [h for h in self.hashes if h in done]

    @property
    def is_done(self) -> bool:
        """``True`` if all of the output is available for this map."""
        return len(self._missing_hashes) == 0

    @property
    def is_running(self) -> bool:
        """
        ``True`` if any of the map's components are in a non-completed status.
        That means that this doesn't literally mean "running" - instead, it means that components could be running, idle, held, completed according to HTCondor but ran into an error internally and didn't produce output, etc.
        """
        return any(
            v != 0
            for k, v in self.status_counts().items()
            if k != Status.COMPLETED
        )

    def wait(
        self,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
        show_progress_bar: bool = False,
    ) -> datetime.timedelta:
        """
        Wait until all output associated with this :class:`Map` is available.

        Parameters
        ----------
        timeout
            How long to wait for the map to complete before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        show_progress_bar
            If ``True``, a progress bar will be displayed.

        Returns
        -------
        elapsed_time
            The time elapsed from the beginning of the wait to the end.
        """
        t = datetime.datetime.now()
        start_time = time.time()
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()

        if show_progress_bar:
            pbar = tqdm(
                desc = self.map_id,
                total = len(self),
                unit = 'input',
                ncols = 80,
                ascii = True,
            )

            previous_pbar_len = 0

        expected_num_hashes = len(self)

        while True:
            num_missing_hashes = len(self._missing_hashes)
            if show_progress_bar:
                pbar_len = expected_num_hashes - num_missing_hashes
                pbar.update(pbar_len - previous_pbar_len)
                previous_pbar_len = pbar_len
            if num_missing_hashes == 0:
                break

            if timeout is not None and time.time() - timeout > start_time:
                raise exceptions.TimeoutError(f'timeout while waiting for {self}')

            time.sleep(1)

        if show_progress_bar:
            pbar.close()

        return datetime.datetime.now() - t

    def get(
        self,
        item: int,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Any:
        """
        Return the output associated with the input index.

        Parameters
        ----------
        item
            The index of the input to get the output for.
        timeout
            How long to wait for the output to exist before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        """
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()

        h = self._item_to_hash(item)
        path = self._outputs_dir / f'{h}.out'

        try:
            utils.wait_for_path_to_exist(path, timeout)
        except exceptions.TimeoutError as e:
            if timeout <= 0:
                raise exceptions.OutputNotFound(f'output for index {item} not found') from e
            else:
                raise e

        return htio.load_object(path)

    def __iter__(self) -> Iterable[Any]:
        """
        Iterating over the :class:`htmap.Map` yields the outputs in the same order as the inputs,
        waiting on each individual output to become available.
        """
        yield from self.iter()

    def iter(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterator[Any]:
        """
        Returns an iterator over the output of the :class:`htmap.Map` in the same order as the inputs,
        waiting on each individual output to become available.

        Parameters
        ----------
        callback
            A function to call on each output as the iteration proceeds.
        timeout
            How long to wait for each output to be available before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        """
        if callback is None:
            callback = lambda o: o

        for output_path in self._output_file_paths:
            utils.wait_for_path_to_exist(output_path, timeout)

            out = htio.load_object(output_path)
            callback(out)
            yield out

    def iter_with_inputs(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterator[Tuple[Tuple[tuple, Dict[str, Any]], Any]]:
        """
        Returns an iterator over the inputs and output of the :class:`htmap.Map` in the same order as the inputs,
        waiting on each individual output to become available.

        Parameters
        ----------
        callback
            A function to call on each (input, output) pair as the iteration proceeds.
        timeout
            How long to wait for each output to be available before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        """
        if callback is None:
            callback = lambda i, o: (i, o)

        for input_path, output_path in zip(self._input_file_paths, self._output_file_paths):
            utils.wait_for_path_to_exist(output_path, timeout)

            inp = htio.load_object(input_path)
            out = htio.load_object(output_path)
            callback(inp, out)
            yield inp, out

    def iter_as_available(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterator[Any]:
        """
        Returns an iterator over the output of the :class:`htmap.Map`,
        yielding individual outputs as they become available.

        The iteration order is initially random, but is consistent within a single interpreter session once the map is completed.

        Parameters
        ----------
        callback
            A function to call on each output as the iteration proceeds.
        timeout
            How long to wait for the entire iteration to complete before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        """
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()
        start_time = time.time()

        if callback is None:
            callback = lambda o: o

        paths = set(self._output_file_paths)
        while len(paths) > 0:
            for path in copy(paths):
                if not path.exists():
                    continue

                paths.remove(path)
                obj = htio.load_object(path)
                callback(obj)
                yield obj

            if timeout is not None and time.time() > start_time + timeout:
                break

            time.sleep(1)

    def iter_as_available_with_inputs(
        self,
        callback: Optional[Callable] = None,
        timeout: Optional[Union[int, datetime.timedelta]] = None,
    ) -> Iterator[Tuple[Tuple[tuple, Dict[str, Any]], Any]]:
        """
        Returns an iterator over the inputs and output of the :class:`htmap.Map`,
        yielding individual ``(input, output)`` pairs as they become available.

        The iteration order is initially random, but is consistent within a single interpreter session once the map is completed.

        Parameters
        ----------
        callback
            A function to call on each ``(input, output)`` as the iteration proceeds.
        timeout
            How long to wait for the entire iteration to complete before raising a :class:`htmap.exceptions.TimeoutError`.
            If ``None``, wait forever.
        """
        if isinstance(timeout, datetime.timedelta):
            timeout = timeout.total_seconds()
        start_time = time.time()

        if callback is None:
            callback = lambda i, o: (i, o)

        paths = set(zip(self._input_file_paths, self._output_file_paths))
        while len(paths) > 0:
            for input_output_paths in copy(paths):
                input_path, output_path = input_output_paths
                if not output_path.exists():
                    continue

                paths.remove(input_output_paths)
                inp = htio.load_object(input_path)
                out = htio.load_object(output_path)
                callback(inp, out)
                yield inp, out

            if timeout is not None and time.time() > start_time + timeout:
                break

            time.sleep(1)

    def _requirements(self, requirements: Optional[str] = None) -> str:
        """Build an HTCondor requirements expression that captures all of the ``cluster_id`` for this map."""
        base = f"({' || '.join(f'ClusterId=={cid}' for cid in self.cluster_ids)})"
        extra = f' && {requirements}' if requirements is not None else ''

        return base + extra

    def _query(
        self,
        requirements: Optional[str] = None,
        projection: Optional[List[str]] = None,
    ) -> Iterator[classad.ClassAd]:
        """
        Perform a _query against the HTCondor cluster to get information about the map jobs.

        Parameters
        ----------
        requirements
            A ClassAd expression to use as the requirements for the _query.
            In addition to whatever restrictions given in this expression, the _query will only target the jobs for this map.
        projection
            The ClassAd attributes to return from the _query.

        Returns
        -------
        classads :
            An iterator of matching :class:`classad.ClassAd`, with only the projected fields.
        """
        if self.cluster_ids is None:
            yield from ()
        if projection is None:
            projection = []

        req = self._requirements(requirements)

        schedd = mapping.get_schedd()
        q = schedd.xquery(
            requirements = req,
            projection = projection,
        )

        logger.debug(f'queried for map {self.map_id} (requirements = "{req}") with projection {projection}')

        yield from q

    def status_counts(self) -> collections.Counter:
        """Return a dictionary that describes how many map components are in each status."""
        query = self._query(projection = ['JobStatus'])
        counter = collections.Counter(Status(classad['JobStatus']) for classad in query)

        # if the job has fully completed, we'll get zero for everything
        # so make sure the completed count makes sense
        counter[Status.COMPLETED] = len(self._completed_hashes)

        return counter

    def status(self) -> str:
        """Return a string containing the number of jobs in each status."""
        counts = self.status_counts()
        stat = ' | '.join(f'{str(js)} = {counts[js]}' for js in Status.display_statuses())
        msg = f'Map {self.map_id} ({len(self)} inputs): {stat}'

        return utils.rstr(msg)

    def hold_reasons(self) -> str:
        """Return a string containing a table showing any held jobs, along with their hold reasons."""
        query = self._query(
            requirements = self._requirements(f'JobStatus=={Status.HELD}'),
            projection = ['ProcId', 'HoldReason', 'HoldReasonCode']
        )

        return utils.table(
            headers = ['Input Index', 'Hold Reason Code', 'Hold Reason'],
            rows = [
                [classad['ProcId'], classad['HoldReasonCode'], classad['HoldReason']]
                for classad in query
            ]
        )

    def _act(self, action: htcondor.JobAction, requirements: Optional[str] = None) -> classad.ClassAd:
        schedd = mapping.get_schedd()
        req = self._requirements(requirements)
        a = schedd.act(action, req)

        logger.debug(f'acted on map {self.map_id} (requirements = "{req}") with action {action}')

        return a

    def remove(self):
        """
        Permanently remove the map and delete all associated input and output files.

        .. warning::

            Interacting with a :class:`Map` after calling this method on it may produce unexpected and undefined behavior!
            Don't do it!
        """
        self._remove_from_queue()
        self._rm_map_dir()
        self._is_removed = True
        MAPS.pop(self.map_id)
        logger.info(f'removed map {self.map_id}')

    def hold(self):
        """Temporarily remove the map from the queue, until it is released."""
        self._act(htcondor.JobAction.Hold)
        logger.debug(f'held map {self.map_id}')

    def release(self):
        """Releases a held map back into the queue."""
        self._act(htcondor.JobAction.Release)
        logger.debug(f'released map {self.map_id}')

    def pause(self):
        self._act(htcondor.JobAction.Suspend)
        logger.debug(f'paused map {self.map_id}')

    def resume(self):
        self._act(htcondor.JobAction.Continue)
        logger.debug(f'resumed map {self.map_id}')

    def vacate(self):
        """Force the map to give up any claimed resources."""
        self._act(htcondor.JobAction.Vacate)
        logger.debug(f'vacated map {self.map_id}')

    def _edit(self, attr: str, value: str, requirements: Optional[str] = None):
        schedd = mapping.get_schedd()
        schedd.edit(self._requirements(requirements), attr, value)

        logger.debug(f'set attribute {attr} for map {self.map_id} to {value}')

    def set_memory(self, memory: Union[str, int, float]):
        """
        Change the amount of memory (RAM) each map component needs.

        .. warning::

            This doesn't change anything for map components that have already started running,
            so you may need to hold and release your map to propagate this change.

        Parameters
        ----------
        memory
            The amount of memory (RAM) to request.
            Can either be a :class:`str` (``'100MB'``, ``'1GB'``, etc.), or a number, in which case it is interpreted as a number of **MB**.
        """
        if isinstance(memory, (int, float)):
            memory = f'{memory}MB'
        self._edit('RequestMemory', memory)

    def set_disk(self, disk: Union[str, int, float]):
        """
        Change the amount of disk space each map component needs.

        .. warning::

            This doesn't change anything for map components that have already started running,
            so you may need to hold and release your map to propagate this change.

        Parameters
        ----------
        disk
            The amount of disk space to use.
            Can either be a :class:`str` (``'100MB'``, ``'1GB'``, etc.), or a number, in which case it is interpreted as a number of **GB**.
        """
        if isinstance(disk, (int, float)):
            disk = f'{disk}MB'
        self._edit('RequestDisk', disk)

    def _iter_output(self, item: int) -> Iterator[str]:
        h = self._item_to_hash(item)
        with (self._map_dir / 'job_logs' / f'{h}.output').open() as file:
            yield from file

    def _iter_error(self, item: int) -> Iterator[str]:
        h = self._item_to_hash(item)
        with (self._map_dir / 'job_logs' / f'{h}.error').open() as file:
            yield from file

    def output(self, item: int) -> str:
        """
        Return a string containing the stdout from a single completed map argument.
        The argument is the index of the input in the original arguments to the map.
        """
        return utils.rstr(''.join(self._iter_output(item)))

    def error(self, item: int) -> str:
        """
        Return a string containing the stderr from a single completed map argument.
        The argument is the index of the input in the original arguments to the map.
        """
        return utils.rstr(''.join(self._iter_error(item)))

    def tail(self):
        """
        Stream any new text added to the map's most recent log file to stdout.
        This function runs forever, so it should only be used in interactive contexts (i.e., the REPL or a Jupyter notebook or similar) where it can be cancelled.
        """
        with (self._map_dir / 'cluster_logs' / f'{self.cluster_ids[-1]}.log').open() as file:
            file.seek(0, 2)
            while True:
                current = file.tell()
                line = file.readline()
                if line == '':
                    file.seek(current)
                    time.sleep(.1)
                else:
                    print(line, end = '')

    def rerun(self):
        """Reruns the entire map from scratch."""
        self._clean_outputs_dir()
        self.rerun_incomplete()

    def rerun_incomplete(self):
        """Rerun any incomplete parts of the map from scratch."""
        self._rerun(hashes = self._missing_hashes)

    def _rerun(self, hashes):
        self._remove_from_queue()

        itemdata = htio.load_itemdata(self._map_dir)
        itemdata_by_hash = {d['hash']: d for d in itemdata}
        new_itemdata = [itemdata_by_hash[h] for h in hashes]

        submit_obj = htio.load_submit(self._map_dir)

        submit_result = mapping.execute_submit(
            submit_obj,
            new_itemdata,
        )

        self.cluster_ids.append(submit_result.cluster())

        logger.debug(f'resubmitted {len(new_itemdata)} inputs from map {self.map_id}')

    def rename(self, map_id: str, force_overwrite: bool = False) -> 'Map':
        """
        Give this map a new ``map_id``.
        This function returns a **new** :class:`Map` for the renamed map.
        The :class:`Map` you call this on will not be connected to the new ``map_id``!
        The old ``map_id`` will be available for re-use.

        .. note::

            Only completed maps can be renamed (i.e., ``result.is_done == True``).

        .. warning::

            The old :class:`Map` will not be connected to the new ``map_id``!
            This function returns a **new** result for the renamed map.

        Parameters
        ----------
        map_id
            The ``map_id`` to assign to this map.
        force_overwrite
            If ``True``, and there is already a map with the given ``map_id``, it will be removed before renaming this one.

        Returns
        -------
        map_result :
            A new :class:`Map` for the renamed map.
        """
        if map_id == self.map_id:
            raise exceptions.CannotRenameMap('cannot rename a map to the same ``map_id`` it already has')
        if not self.is_done:
            raise exceptions.CannotRenameMap(f'cannot rename a map that is not complete (job status: {self.status_counts()})')

        mapping.raise_if_map_id_is_invalid(map_id)

        if force_overwrite:
            try:
                existing_result = Map.recover(map_id)
                existing_result.remove()
                logger.debug(f'force-overwrote map {map_id}')
            except exceptions.MapIdNotFound:
                logger.debug(f'force-overwrite not needed to rename {self.map_id} to {map_id}')
        else:
            try:
                mapping.raise_if_map_id_already_exists(map_id)
            except exceptions.MapIdAlreadyExists as e:
                raise exceptions.CannotRenameMap(f'cannot rename map to {map_id} because it already exists') from e

        new_map_dir = mapping.map_dir_path(map_id)
        shutil.copytree(
            src = self._map_dir,
            dst = new_map_dir,
        )

        submit = htcondor.Submit(dict(self.submit))
        submit['JobBatchName'] = map_id

        # fix paths
        target = mapping.map_dir_path(self.map_id).as_posix()
        replace_with = mapping.map_dir_path(map_id).as_posix()
        for k, v in submit.items():
            submit[k] = v.replace(target, replace_with)

        htio.save_submit(new_map_dir, submit)

        self.remove()

        return Map(
            map_id = map_id,
            submit = submit,
            cluster_ids = self.cluster_ids,
            hashes = self.hashes,
        )
