# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import pytest

import random
import shlex
import string

from reprotest.shell_syn import *


def test_basic():
    assert (sanitize_globs("""lol \$ *"\$ s" "" '' ' ' " " '   '   "" """)
        == '''./lol ./\$ ./*"\$ s" ./"" ./'' ./' ' ./" " ./'   ' ./""''')
    assert (sanitize_globs("""*"*"'*' ../hm wut???""")
        == '''./*"*"'*' ./../hm ./wut???''')

    with pytest.raises(ValueError):
        sanitize_globs('; sudo rm -rf /')
    with pytest.raises(ValueError):
        sanitize_globs('`sudo rm -rf /`')
    with pytest.raises(ValueError):
        sanitize_globs('<(sudo rm -rf /)')
    with pytest.raises(ValueError):
        sanitize_globs('$(sudo rm -rf /)')
    with pytest.raises(ValueError):
        sanitize_globs('ok; sudo rm -rf /')

    assert sanitize_globs('-rf') == './-rf'
    assert sanitize_globs('/') == './/'

def test_shlex_quote():
    for _ in range(65536):
        x = ''.join(random.choice(string.printable) for _ in range(random.randint(0, 32)))
        assert len(sanitize_globs(shlex.quote(x), False)) == 1
        assert len(shlex.split(sanitize_globs(shlex.quote(x)))) == 1
