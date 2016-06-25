# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import subprocess

import pytest

import reprotest

def check_return_code(command, virtual_server, code):
    try:
        reprotest.check(command, 'artifact', virtual_server, 'tests/')
    except SystemExit as system_exit:
        assert(system_exit.args[0] == code)

@pytest.fixture(scope='module', params=['null']) # , 'chroot'
def virtual_server(request):
    if request.param == 'null':
        return [request.param]
    else:
        return request.param

def test_simple_builds(virtual_server):
    check_return_code(['python', 'mock_build.py'], virtual_server, 0)
    # check_return_code(['python', 'mock_failure.py'], virtual_server, 2)
    check_return_code(['python', 'mock_build.py', 'irreproducible'],
                      virtual_server, 1)

@pytest.mark.parametrize('variation', ['fileordering', 'home', 'kernel', 'locales', 'path', 'timezone']) #, 'umask'
def test_variations(virtual_server, variation):
    check_return_code(['python', 'mock_build.py', variation], virtual_server, 1)

def test_self_build():
    assert(subprocess.call(['reprotest', 'python setup.py bdist', 'dist/reprotest-0.1.linux-x86_64.tar.gz', 'null']) == 1)
    assert(subprocess.call(['reprotest', 'debuild -b -uc -us', '../reprotest_0.1_all.deb', 'null']) == 1)
