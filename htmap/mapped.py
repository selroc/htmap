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

from typing import Iterable, Dict, Union, Optional, Callable, Any, Iterator
import logging

from . import mapping, options, maps

logger = logging.getLogger(__name__)


class MappedFunction:
    def __init__(
        self,
        func: Callable,
        map_options: Optional[options.MapOptions] = None
    ):
        """
        Parameters
        ----------
        func
            A function to wrap in a :class:`MappedFunction`.
        map_options
            An instance of :class:`htmap.MapOptions`.
            Any map calls from the :class:`MappedFunction` produced by this decorator will inherit from this.
        """
        self.func = func

        if map_options is None:
            map_options = options.MapOptions()
        self.map_options = map_options

        logger.debug(f'created mapped function for {self.func} with options {self.map_options}')

    def __repr__(self):
        return f'<{self.__class__.__name__}(func = {self.func}, map_options = {self.map_options})>'

    def __call__(self, *args, **kwargs):
        """Call the function as normal, locally."""
        return self.func(*args, **kwargs)

    def map(
        self,
        map_id: str,
        args: Iterable[Any],
        map_options: Optional[options.MapOptions] = None,
        **kwargs,
    ) -> maps.Map:
        """As :func:`htmap.map`, but the ``func`` argument is the mapped function."""
        if map_options is None:
            map_options = options.MapOptions()

        return mapping.map(
            map_id = map_id,
            func = self.func,
            args = args,
            map_options = options.MapOptions.merge(map_options, self.map_options),
            **kwargs,
        )

    def starmap(
        self,
        map_id: str,
        args: Optional[Iterable[tuple]] = None,
        kwargs: Optional[Iterable[Dict[str, Any]]] = None,
        map_options: Optional[options.MapOptions] = None,
    ) -> maps.Map:
        """As :func:`htmap.starmap`, but the ``func`` argument is the mapped function."""
        if map_options is None:
            map_options = options.MapOptions()

        return mapping.starmap(
            map_id = map_id,
            func = self.func,
            args = args,
            kwargs = kwargs,
            map_options = options.MapOptions.merge(map_options, self.map_options),
        )

    def build_map(
        self,
        map_id: str,
        map_options: Optional[options.MapOptions] = None,
    ) -> mapping.MapBuilder:
        """As :func:`htmap.build_map`, but the ``func`` argument is the mapped function."""
        if map_options is None:
            map_options = options.MapOptions()

        return mapping.build_map(
            map_id = map_id,
            func = self.func,
            map_options = options.MapOptions.merge(map_options, self.map_options),
        )

    def transient_map(
        self,
        args: Iterable[Any],
        map_options: options.MapOptions = None,
        **kwargs,
    ) -> mapping.TransientMap:
        """As :func:`htmap.htmap`, but the ``func`` argument is the mapped function."""
        if map_options is None:
            map_options = options.MapOptions()

        return mapping.transient_map(
            func = self.func,
            args = args,
            map_options = options.MapOptions.merge(map_options, self.map_options),
            **kwargs
        )

    def transient_starmap(
        self,
        args: Optional[Iterable[tuple]] = None,
        kwargs: Optional[Iterable[Dict[str, Any]]] = None,
        map_options: Optional[options.MapOptions] = None,
    ) -> mapping.TransientMap:
        """As :func:`htmap.htstarmap`, but the ``func`` argument is the mapped function."""
        if map_options is None:
            map_options = options.MapOptions()

        return mapping.transient_starmap(
            func = self.func,
            args = args,
            kwargs = kwargs,
            map_options = options.MapOptions.merge(map_options, self.map_options),
        )


def mapped(map_options: Optional[options.MapOptions] = None) -> Union[Callable, MappedFunction]:
    """
    A decorator that wraps a function in an :class:`MappedFunction`,
    which provides an interface for mapping functions calls out to an HTCondor cluster.

    Parameters
    ----------
    map_options
        An instance of :class:`htmap.MapOptions`.
        Any map calls from the :class:`MappedFunction` produced by this decorator will inherit from this.

    Returns
    -------
    mapped_function
        A :class:`MappedFunction` that wraps the function (or a wrapper function that does the wrapping).
    """
    if map_options is None:  # call with parens but no args
        def wrapper(func: Callable) -> MappedFunction:
            return MappedFunction(func)

        return wrapper

    elif callable(map_options):  # call with no parens on function
        return MappedFunction(map_options)

    elif isinstance(map_options, options.MapOptions):  # call with map options
        def wrapper(func: Callable) -> MappedFunction:
            return MappedFunction(func, map_options = map_options)

        return wrapper