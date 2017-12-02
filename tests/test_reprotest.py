# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import contextlib
import logging
import os
import subprocess
import sys

import pytest
import reprotest
from reprotest.build import VariationSpec, Variations, VARIATIONS

REPROTEST = [sys.executable, "-m", "reprotest", "--no-diffoscope", "--min-cpus", "1"]
REPROTEST_TEST_SERVERS = os.getenv("REPROTEST_TEST_SERVERS", "null").split(",")
REPROTEST_TEST_DONTVARY = os.getenv("REPROTEST_TEST_DONTVARY", "").split(",")

if REPROTEST_TEST_DONTVARY:
    REPROTEST += ["--vary=" + ",".join("-%s" % a for a in REPROTEST_TEST_DONTVARY if a)]

TEST_VARIATIONS = frozenset(VARIATIONS.keys()) - frozenset(REPROTEST_TEST_DONTVARY)

def check_reproducibility(command, virtual_server, reproducible):
    result = reprotest.check(
        reprotest.TestArgs.of(command, 'tests', 'artifact'),
        reprotest.TestbedArgs.of(virtual_server),
        Variations.of(VariationSpec.default(TEST_VARIATIONS)))
    assert result == reproducible

def check_command_line(command_line, code=None):
    try:
        retcode = 0
        return reprotest.run(command_line, True)
    except SystemExit as system_exit:
        retcode = system_exit.args[0]
    finally:
        if code is None:
            assert(retcode == 0)
        elif isinstance(code, int):
            assert(retcode == code)
        else:
            assert(retcode in code)

@pytest.fixture(scope='module', params=REPROTEST_TEST_SERVERS)
def virtual_server(request):
    if request.param == 'null':
        return [request.param]
    elif request.param == 'schroot':
        return [request.param, 'stable-amd64']
    elif request.param == 'qemu':
        return [request.param, os.path.expanduser('~/linux/reproducible_builds/adt-sid.img')]
    else:
        raise ValueError(request.param)

def test_simple_builds(virtual_server):
    check_reproducibility('python3 mock_build.py', virtual_server, True)
    with pytest.raises(Exception):
        check_reproducibility('python3 mock_failure.py', virtual_server)
    check_reproducibility('python3 mock_build.py irreproducible', virtual_server, False)

@contextlib.contextmanager
def setup_logging(debug):
    logger = logging.getLogger()
    oldLevel = logger.getEffectiveLevel()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    logger.addHandler(ch)

    formatter = logging.Formatter(
        '%(asctime)s %(levelname).1s: %(name)s: %(message)s',
        '%Y-%m-%d %H:%M:%S',
    )
    ch.setFormatter(formatter)
    try:
        yield logger
    finally:
        # restore old logging settings. this helps pytest not spew out errors
        # like "ValueError: I/O operation on closed file", see
        # https://github.com/pytest-dev/pytest/issues/14#issuecomment-272243656
        logger.removeHandler(ch)
        logger.setLevel(oldLevel)

# TODO: test all variations that we support
@pytest.mark.parametrize('captures', list(VARIATIONS.keys()))
def test_variations(virtual_server, captures):
    expected = captures not in TEST_VARIATIONS
    with setup_logging(False):
        check_reproducibility('python3 mock_build.py ' + captures, virtual_server, expected)

@pytest.mark.need_builddeps
def test_self_build(virtual_server):
    # at time of writing (2016-09-23) these are not expected to reproduce;
    # if these start failing then you should change 1 == to 0 == but please
    # figure out which version of setuptools made things reproduce and add a
    # versioned dependency on that one
    assert(1 == subprocess.call(REPROTEST + ['python3 setup.py bdist', 'dist/*.tar.gz'] + virtual_server))
    assert(1 == subprocess.call(REPROTEST + ['python3 setup.py sdist', 'dist/*.tar.gz'] + virtual_server))

def test_command_lines():
    test_args, _, _ = check_command_line(".".split(), 0)
    assert test_args.artifact_pattern is not None
    test_args, _, _ = check_command_line(". -- null -d".split(), 0)
    assert test_args.artifact_pattern is not None
    check_command_line("--dry-run . --verbosity 2 -- null -d".split(), 0)
    assert test_args.artifact_pattern is not None
    check_command_line(". null -d".split(), 2)
    check_command_line(". --verbosity 2 null -d".split(), 2)
    check_command_line("--dry-run . --verbosity 2 null -d".split(), 2)
    check_command_line("--dry-run . null -d".split(), 2)

    test_args, _, _ = check_command_line("auto".split(), 0)
    assert test_args.artifact_pattern is not None
    test_args, _, _ = check_command_line("auto -- null -d".split(), 0)
    assert test_args.artifact_pattern is not None
    check_command_line("--dry-run auto --verbosity 2 -- null -d".split(), 0)
    assert test_args.artifact_pattern is not None
    check_command_line("auto null -d".split(), 2)
    check_command_line("auto --verbosity 2 null -d".split(), 2)
    check_command_line("--dry-run auto --verbosity 2 null -d".split(), 2)
    check_command_line("--dry-run auto null -d".split(), 2)

    _, testbed_args, _ = check_command_line("auto -- schroot unstable-amd64-sbuild".split(), 0)
    assert testbed_args.virtual_server_args == ['schroot', 'unstable-amd64-sbuild']
    _, testbed_args, _ = check_command_line(". -- schroot unstable-amd64-sbuild".split(), 0)
    assert testbed_args.virtual_server_args == ['schroot', 'unstable-amd64-sbuild']

# TODO: don't call it if we don't have debian/, e.g. for other distros
@pytest.mark.need_builddeps
def test_debian_build(virtual_server):
    # This is a bit dirty though it works - when building the debian package,
    # debian/rules will call this, which will call debian/rules, so ../*.deb
    # gets written twice and the second one is the "real" one, but since it
    # should all be reproducible, this should be OK.
    assert(0 == subprocess.call(
        REPROTEST + ['dpkg-buildpackage -b -nc --no-sign', '../*.deb'] + virtual_server,
        # "nocheck" to stop tests recursing into themselves
        env=dict(list(os.environ.items()) + [("DEB_BUILD_OPTIONS", "nocheck")])))
