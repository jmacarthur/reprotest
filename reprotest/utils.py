# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import collections
import functools


class AttributeFunctor(collections.namedtuple('_AttributeFunctor', 'x f')):
    """Functor for applying functions to attributes of namedtuples."""
    def __getattr__(self, name):
        return lambda *args: self.x._replace(**{
            name: self.f(getattr(self.x, name), *args)
        })


class AttributeReplacer(object):
    """Proxy for setting attributes deeply on trees of namedtuples."""
    __slots__ = ['top', 'attrs']

    def __init__(self, top, attrs):
        self.top = top
        self.attrs = attrs

    def rgetattr(self, attrs):
        return functools.reduce(getattr, attrs, self.top)

    def __getattr__(self, name):
        attr = self.rgetattr(self.attrs + [name])
        return self.__class__(self.top, self.attrs + [name])

    def __call__(self, *args, **kwargs):
        f = self.rgetattr(self.attrs)
        if not callable(f):
            raise TypeError("not callable: %s of %s" % (self.attrs, self.top))
        result = f(*args, **kwargs)
        parents = [(self.rgetattr(self.attrs[:i]), self.attrs[i])
            for i in range(len(self.attrs))]
        return functools.reduce(
            lambda v, p: p[0]._replace(**{p[1]: v}),
            reversed(parents[:-1]), result)

