# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import re
import rstr
import os


xe_small = {
    "path": "(/\w{1,12}){1,4}",
    "port": "([1-9]\d{0,3}|[1-5]\d{4})",
    "domain": "\w{1,10}(\.\w{1,10}){0,3}",
    "password": "\w{1,40}",
    "username": "\w{2,20}",
}

xe_medium = dict(**{
    "proxy_url" : "%(username)s:%(password)s@%(domain)s:%(port)s" % xe_small,
    "pathlist": "%(path)s(:%(path)s){0,4}" % xe_small,
}, **xe_small)


"""
Variables intended to control the behaviour of general run-time programs that
include non-build and non-developer programs.

See also:
- http://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap08.html
"""
# TODO: TMPDIR needs special treatment, make it a proper variation
BLACKLIST = (r"""
HOME LOGNAME USER USERNAME
_ LANG LANGUAGE LC_\w+ MSGVERB OLDPWD PWD SHELL SHLVL TZ
TMPDIR=(/tmp|/var/tmp|/dev/shm)
COLORTERM COLUMNS DATEMSK EDITOR LINES LS_COLORS TERM VISUAL VTE_VERSION
PAGER MAIL BROWSER
ftp_proxy=ftp://%(proxy_url)s http_proxy=http://%(proxy_url)s https_proxy=https://%(proxy_url)s
MANPATH=%(pathlist)s INFODIR=%(pathlist)s
DISPLAY WINDOWID XAUTHORITY XMODIFIERS
DBUS_SESSION_\w+ DESKTOP_SESSION GDMSESSION ICEAUTHORITY SESSION_MANAGER XDG_\w+ \w+_SOCKET
QT_\w+ GTK_\w+ \w+_IM_MODULE
SSH_\w+ GNUPG\w+ GPG_\w+
DEBEMAIL DEBFULLNAME
SUDO_COMMAND SUDO_GID SUDO_UID SUDO_USER
""" % xe_medium).split()


"""
Variables intended to control the output of build processes, or interpreter
settings that "normal users" aren't expected to customise in most situations.

Notes:

- Path variables are subtle, we keep many of them here to avoid false-positives
  and breaking builds, but ideally they would be "in the blacklist if contents
  are the same, else in the whitelist if contents differ".
"""
WHITELIST = r"""
CC CPP CXX FC F GCJ LD OBJC OBJCXX RUSTC LEX YACC
CFLAGS CPPFLAGS CXXFLAGS FCFLAGS FFLAGS GCJFLAGS LDFLAGS OBJCFLAGS OBJCXXFLAGS RUSTFLAGS
DEB_\w+ DPKG_\w+
PATH JAVA_HOME GOPATH LD_PRELOAD LD_LIBRARY_PATH PERL5LIB PYTHONPATH
SOURCE_DATE_EPOCH BUILD_PATH_PREFIX_MAP
""".split()


"""
Some stuff breaks when you unset certain vars, e.g. diffoscope breaks if PATH
is unset. technically these are bugs, but they are so prevalent and we'd like
to focus on more important things first.

TODO: make it possible to clear this list on the command line.
"""
NEVER_UNSET = "HOME PATH USER LOGNAME PWD".split()


def parse_environ_templates(variables):
    for tmpl in variables:
        k, sep, v = tmpl.partition("=")
        if not v and sep:
            yield (k, None)
        else:
            yield (rstr.xeger(k), rstr.xeger(v) or "i_capture_the_environment")


def generate_dummy_environ(env=None, blacklist=BLACKLIST, whitelist=WHITELIST, never_unset=NEVER_UNSET):
    if env is None:
        env = os.environ
    env = set(env.keys()) - set(never_unset)

    def generate(name, variables):
        for tmpl in variables:
            k, sep, v = tmpl.partition("=")
            if re.match(k, name):
                # unset (if v empty), or generate random value matching v
                yield (name, "%s=%s" % (name, v))

    blacklist_matches = [m for n in env for m in generate(n, blacklist)]
    # generate overrides for existing vars, and possibly generate new vars
    b = [m[1] for m in blacklist_matches] + blacklist
    bn = sorted(set([m[0] for m in blacklist_matches] + blacklist))

    def matches(name, pp):
        return any(re.match(p, name) for p in pp)
    blacklist_names = [t.partition("=")[0] for t in blacklist]
    whitelist_names = [t.partition("=")[0] for t in whitelist]

    unrecognized = sorted(n for n in env
        if (not matches(n, blacklist_names)
        and not matches(n, whitelist_names)))
    extra_unknown = ["[A-Z]{2,5}(_[A-Z]{2,5}){1,3}",
                     "[A-Z]{2,5}(_[A-Z]{2,5}){1,3}",
                     "REPROTEST_CAPTURE_ENVIRONMENT_UNKNOWN_\w+"]

    # unset unrecognized stuff in the current env that doesn't match the
    # whitelist or blacklist, which we set earlier
    nw = ["%s=" % k for k in unrecognized] + extra_unknown
    nwn = unrecognized + extra_unknown

    return b, bn, nw, nwn


def env_diff(old, new):
    diff = ["-%s" % k for k in old.keys() - new.keys()] + \
           ["+%s=%s" % (k, v) for k, v in new.items() if k in old and v != old[k]]
    return sorted(diff, key=lambda x: x[1:])
