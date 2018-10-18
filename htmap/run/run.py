#!/usr/bin/env python3

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

import sys
import socket
import datetime
import os
import traceback
import subprocess
from pathlib import Path

import cloudpickle


class ComponentResult:
    def __init__(
        self,
        *,
        input_hash,
        status,
    ):
        self.input_hash = input_hash
        self.status = status


class ComponentOk(ComponentResult):
    status = 'OK'

    def __init__(
        self,
        *,
        output,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.output = output

    def __repr__(self):
        return '<OK for input hash {}>'.format(self.input_hash)


class ComponentError(ComponentResult):
    status = 'ERR'

    def __init__(
        self,
        *,
        exception,
        traceback,
        node_info,
        working_dir_contents,
        stack_summary,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.exception = exception
        self.traceback = traceback

        self.node_info = node_info
        self.working_dir_contents = working_dir_contents
        self.stack_summary = stack_summary

    def __repr__(self):
        return '<ERROR for input hash {}>'.format(self.input_hash)


def get_node_info():
    return (
        socket.getfqdn(),
        socket.gethostbyname(socket.gethostname()),
        datetime.datetime.utcnow(),
    )


def print_node_info(node_info):
    print('Landed on execute node {} ({}) at {}'.format(*node_info))


# def print_python_info():
#     print('Python executable is\n    {}'.format(sys.executable))
#     print('with installed packages')
#     print('\n'.join('    {}'.format(line) for line in pip_freeze().splitlines()))
#
#
# def pip_freeze() -> str:
#     return subprocess.run(
#         [sys.executable, '-m', 'pip', 'freeze', '--disable-pip-version-check'],
#         stdout = subprocess.PIPE,
#     ).stdout.decode('utf-8')

def get_working_dir_contents():
    return [str(p) for p in Path.cwd().iterdir()]


def print_working_dir_contents(contents):
    print('Working directory contents:')
    for path in contents:
        print('    ' + path)


def load_func():
    with Path('func').open(mode = 'rb') as file:
        return cloudpickle.load(file)


def load_args_and_kwargs(arg_hash):
    with Path('{}.in'.format(arg_hash)).open(mode = 'rb') as file:
        return cloudpickle.load(file)


def save_output(arg_hash, output):
    with Path('{}.out'.format(arg_hash)).open(mode = 'wb') as file:
        cloudpickle.dump(output, file)


def print_run_info(arg_hash, func, args, kwargs):
    s = '\n'.join((
        'Running',
        '    {}'.format(func),
        'with args',
        '    {}'.format(args),
        'and kwargs',
        '    {}'.format(kwargs),
        'from input hash',
        '    {}'.format(arg_hash),
    ))

    print(s)


def main(input_hash):
    node_info = get_node_info()
    print_node_info(node_info)
    print()
    contents = get_working_dir_contents()
    print_working_dir_contents(contents)
    print()
    # print_python_info()
    # print()

    os.environ['HTMAP_ON_EXECUTE'] = "1"

    print('\n----- MAP COMPONENT OUTPUT START -----\n')

    try:
        func = load_func()
        args, kwargs = load_args_and_kwargs(input_hash)
        print_run_info(input_hash, func, args, kwargs)
        output = func(*args, **kwargs)
        result = ComponentOk(
            input_hash = input_hash,
            status = 'OK',
            output = output,
        )
    except Exception as e:
        def skip_first(tb):
            iterator = traceback.walk_tb(tb)
            # next(iterator)

            for frame, lineno in iterator:
                fname = frame.f_code.co_filename
                print(fname, os.path.exists(fname))
                summ = traceback.FrameSummary(
                    filename = fname,
                    lineno = lineno,
                    name = frame.f_code.co_name,
                    lookup_line = os.path.exists(fname),
                    locals = frame.f_locals,
                )

                yield summ

        stack_summ = traceback.StackSummary.from_list(
            skip_first(e.__traceback__),
        )

        (type, value, tb) = sys.exc_info()
        result = ComponentError(
            input_hash = input_hash,
            status = 'ERR',
            exception = e,
            traceback = traceback.format_exception_only(type, value),
            stack_summary = stack_summ,
            node_info = node_info,
            working_dir_contents = contents,
        )

        # print(stack_summ)
        # print(stack_summ.format())
        # for line in stack_summ.format():
        #     print(line)

        # frame = tb.tb_frame
        # frames = []
        # while frame is not None:
        #     frames.append(frame)
        #     frame = frame.f_back
        #
        # for frame in reversed(frames):
        #     print(frame)
        #     print(frame.f_code)
        #     print(frame.f_code.co_name)
        #     print(frame.f_code.co_code)
        #     print(frame.f_code.co_names)
        #     print(frame.f_code.co_varnames)
        #     print(frame.f_code.co_filename)
        #     print(frame.f_code.co_firstlineno)
        #     print()

    print('\n-----  MAP COMPONENT OUTPUT END  -----\n')

    save_output(input_hash, result)

    print('Finished executing component at {}'.format(datetime.datetime.utcnow()))


if __name__ == '__main__':
    main(input_hash = sys.argv[1])
