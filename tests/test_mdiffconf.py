# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import pytest

from reprotest.mdiffconf import *


class LolX(collections.namedtuple("LolX", "x y z")):
    pass

def test_basic():
    one = ImmutableNamespace(lol=LolX(x=strlist_set(";"), y=10, z=True), wtf=True)
    zero = ImmutableNamespace(lol=LolX(x=strlist_set(";", ['less than nothing yo!']), y=0, z=False), wtf=False)
    d = ImmutableNamespace()
    d = parse_all(d, '+lol,wtf,-xxx,lol.x+=4;5;6,lol.y+=123,lol.x-=5,-lol.z', one, zero, sep=",")
    assert d == ImmutableNamespace(lol=LolX(x=['4', '6'], y=133, z=False), wtf=True)
    d = parse_all(d, '@lol,-lol.x,-lol.z,lol.x+=3;4;5', one, zero, sep=",")
    assert d == ImmutableNamespace(lol=LolX(x=['less than nothing yo!', '3', '4', '5'], y=10, z=False), wtf=True)
