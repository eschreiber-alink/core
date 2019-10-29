"""
Miscellaneous utility functions, wrappers around some subprocess procedures.
"""

import concurrent.futures
import fcntl
import hashlib
import importlib
import inspect
import json
import logging
import logging.config
import os
import shlex
import sys
from subprocess import PIPE, STDOUT, Popen

from core.errors import CoreCommandError

DEVNULL = open(os.devnull, "wb")


def execute_file(path, exec_globals=None, exec_locals=None):
    """
    Provides an alternative way to run execfile to be compatible for
    both python2/3.

    :param str path: path of file to execute
    :param dict exec_globals: globals values to pass to execution
    :param dict exec_locals:  local values to pass to execution
    :return: nothing
    """
    if exec_globals is None:
        exec_globals = {}
    exec_globals.update({"__file__": path, "__name__": "__main__"})
    with open(path, "rb") as f:
        data = compile(f.read(), path, "exec")
        exec(data, exec_globals, exec_locals)


def hashkey(value):
    """
    Provide a consistent hash that can be used in place
    of the builtin hash, that no longer behaves consistently
    in python3.

    :param str/int value: value to hash
    :return: hash value
    :rtype: int
    """
    if isinstance(value, int):
        value = str(value)
    value = value.encode("utf-8")
    return int(hashlib.sha256(value).hexdigest(), 16)


def _detach_init():
    """
    Fork a child process and exit.

    :return: nothing
    """
    if os.fork():
        # parent exits
        os._exit(0)
    os.setsid()


def _valid_module(path, file_name):
    """
    Check if file is a valid python module.

    :param str path: path to file
    :param str file_name: file name to check
    :return: True if a valid python module file, False otherwise
    :rtype: bool
    """
    file_path = os.path.join(path, file_name)
    if not os.path.isfile(file_path):
        return False

    if file_name.startswith("_"):
        return False

    if not file_name.endswith(".py"):
        return False

    return True


def _is_class(module, member, clazz):
    """
    Validates if a module member is a class and an instance of a CoreService.

    :param module: module to validate for service
    :param member: member to validate for service
    :param clazz: clazz type to check for validation
    :return: True if a valid service, False otherwise
    :rtype: bool
    """
    if not inspect.isclass(member):
        return False

    if not issubclass(member, clazz):
        return False

    if member.__module__ != module.__name__:
        return False

    return True


def close_onexec(fd):
    """
    Close on execution of a shell process.

    :param fd: file descriptor to close
    :return: nothing
    """
    fdflags = fcntl.fcntl(fd, fcntl.F_GETFD)
    fcntl.fcntl(fd, fcntl.F_SETFD, fdflags | fcntl.FD_CLOEXEC)


def which(command, required):
    """
    Find location of desired executable within current PATH.

    :param str command: command to find location for
    :param bool required: command is required to be found, false otherwise
    :return: command location or None
    :raises ValueError: when not found and required
    """
    found_path = None
    for path in os.environ["PATH"].split(os.pathsep):
        command_path = os.path.join(path, command)
        if os.path.isfile(command_path) and os.access(command_path, os.X_OK):
            found_path = command_path
            break

    if found_path is None and required:
        raise ValueError(f"failed to find required executable({command}) in path")

    return found_path


def make_tuple(obj):
    """
    Create a tuple from an object, or return the object itself.

    :param obj: object to convert to a tuple
    :return: converted tuple or the object itself
    :rtype: tuple
    """
    if hasattr(obj, "__iter__"):
        return tuple(obj)
    else:
        return (obj,)


def make_tuple_fromstr(s, value_type):
    """
    Create a tuple from a string.

    :param str s: string to convert to a tuple
    :param value_type: type of values to be contained within tuple
    :return: tuple from string
    :rtype: tuple
    """
    # remove tuple braces and strip commands and space from all values in the tuple
    # string
    values = []
    for x in s.strip("(), ").split(","):
        x = x.strip("' ")
        if x:
            values.append(x)
    return tuple(value_type(i) for i in values)


def mute_detach(args, **kwargs):
    """
    Run a muted detached process by forking it.

    :param list[str]|str args: arguments for the command
    :param dict kwargs: keyword arguments for the command
    :return: process id of the command
    :rtype: int
    """
    args = shlex.split(args)
    kwargs["preexec_fn"] = _detach_init
    kwargs["stdout"] = DEVNULL
    kwargs["stderr"] = STDOUT
    return Popen(args, **kwargs).pid


def cmd(args, env=None, cwd=None, wait=True, shell=False):
    """
    Execute a command on the host and return a tuple containing the exit status and
    result string. stderr output is folded into the stdout result string.

    :param str args: command arguments
    :param dict env: environment to run command with
    :param str cwd: directory to run command in
    :param bool wait: True to wait for status, False otherwise
    :param bool shell: True to use shell, False otherwise
    :return: combined stdout and stderr
    :rtype: str
    :raises CoreCommandError: when there is a non-zero exit status or the file to
        execute is not found
    """
    logging.debug("command cwd(%s) wait(%s): %s", cwd, wait, args)
    if shell is False:
        args = shlex.split(args)
    try:
        p = Popen(args, stdout=PIPE, stderr=PIPE, env=env, cwd=cwd, shell=shell)
        if wait:
            stdout, stderr = p.communicate()
            status = p.wait()
            if status != 0:
                raise CoreCommandError(status, args, stdout, stderr)
            return stdout.decode("utf-8").strip()
        else:
            return ""
    except OSError:
        raise CoreCommandError(-1, args)


def file_munge(pathname, header, text):
    """
    Insert text at the end of a file, surrounded by header comments.

    :param str pathname: file path to add text to
    :param str header: header text comments
    :param str text: text to append to file
    :return: nothing
    """
    # prevent duplicates
    file_demunge(pathname, header)

    with open(pathname, "a") as append_file:
        append_file.write(f"# BEGIN {header}\n")
        append_file.write(text)
        append_file.write(f"# END {header}\n")


def file_demunge(pathname, header):
    """
    Remove text that was inserted in a file surrounded by header comments.

    :param str pathname: file path to open for removing a header
    :param str header: header text to target for removal
    :return: nothing
    """
    with open(pathname, "r") as read_file:
        lines = read_file.readlines()

    start = None
    end = None

    for i, line in enumerate(lines):
        if line == f"# BEGIN {header}\n":
            start = i
        elif line == f"# END {header}\n":
            end = i + 1

    if start is None or end is None:
        return

    with open(pathname, "w") as write_file:
        lines = lines[:start] + lines[end:]
        write_file.write("".join(lines))


def expand_corepath(pathname, session=None, node=None):
    """
    Expand a file path given session information.

    :param str pathname: file path to expand
    :param core.emulator.session.Session session: core session object to expand path
    :param core.nodes.base.CoreNode node: node to expand path with
    :return: expanded path
    :rtype: str
    """
    if session is not None:
        pathname = pathname.replace("~", f"/home/{session.user}")
        pathname = pathname.replace("%SESSION%", str(session.id))
        pathname = pathname.replace("%SESSION_DIR%", session.session_dir)
        pathname = pathname.replace("%SESSION_USER%", session.user)

    if node is not None:
        pathname = pathname.replace("%NODE%", str(node.id))
        pathname = pathname.replace("%NODENAME%", node.name)

    return pathname


def sysctl_devname(devname):
    """
    Translate a device name to the name used with sysctl.

    :param str devname: device name to translate
    :return: translated device name
    :rtype: str
    """
    if devname is None:
        return None
    return devname.replace(".", "/")


def load_config(filename, d):
    """
    Read key=value pairs from a file, into a dict. Skip comments; strip newline
    characters and spacing.

    :param str filename: file to read into a dictionary
    :param dict d: dictionary to read file into
    :return: nothing
    """
    with open(filename, "r") as f:
        lines = f.readlines()

    for line in lines:
        if line[:1] == "#":
            continue

        try:
            key, value = line.split("=", 1)
            d[key] = value.strip()
        except ValueError:
            logging.exception("error reading file to dict: %s", filename)


def load_classes(path, clazz):
    """
    Dynamically load classes for use within CORE.

    :param path: path to load classes from
    :param clazz: class type expected to be inherited from for loading
    :return: list of classes loaded
    """
    # validate path exists
    logging.debug("attempting to load modules from path: %s", path)
    if not os.path.isdir(path):
        logging.warning("invalid custom module directory specified" ": %s", path)
    # check if path is in sys.path
    parent_path = os.path.dirname(path)
    if parent_path not in sys.path:
        logging.debug("adding parent path to allow imports: %s", parent_path)
        sys.path.append(parent_path)

    # retrieve potential service modules, and filter out invalid modules
    base_module = os.path.basename(path)
    module_names = os.listdir(path)
    module_names = filter(lambda x: _valid_module(path, x), module_names)
    module_names = map(lambda x: x[:-3], module_names)

    # import and add all service modules in the path
    classes = []
    for module_name in module_names:
        import_statement = f"{base_module}.{module_name}"
        logging.debug("importing custom module: %s", import_statement)
        try:
            module = importlib.import_module(import_statement)
            members = inspect.getmembers(module, lambda x: _is_class(module, x, clazz))
            for member in members:
                valid_class = member[1]
                classes.append(valid_class)
        except Exception:
            logging.exception(
                "unexpected error during import, skipping: %s", import_statement
            )

    return classes


def load_logging_config(config_path):
    """
    Load CORE logging configuration file.

    :param str config_path: path to logging config file
    :return: nothing
    """
    with open(config_path, "r") as log_config_file:
        log_config = json.load(log_config_file)
        logging.config.dictConfig(log_config)


def threadpool(funcs, workers=10):
    """
    Run provided functions, arguments, and keywords within a threadpool
    collecting results and exceptions.

    :param iter funcs: iterable that provides a func, args, kwargs
    :param int workers: number of workers for the threadpool
    :return: results and exceptions from running functions with args and kwargs
    :rtype: tuple
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = []
        for func, args, kwargs in funcs:
            future = executor.submit(func, *args, **kwargs)
            futures.append(future)
        results = []
        exceptions = []
        for future in concurrent.futures.as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                exceptions.append(e)
    return results, exceptions
