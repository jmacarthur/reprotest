# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import collections
import functools
import getpass
import grp
import logging
import os
import shlex
import shutil
import random
import time
import types

from reprotest import _shell_ast
from reprotest import mdiffconf
from reprotest.utils import AttributeReplacer


def tool_required(*tools):
    def wrap(f):
        @functools.wraps(f)
        def wf(*args, **kwargs):
            return f(*args, **kwargs)
        wf.tool_required = tools
        return wf
    return wrap


def tool_missing(f):
    if not hasattr(f, "tool_required"):
        return []
    return [t for t in f.tool_required if shutil.which(t) is None]


def dirname(p):
    # works more intuitively for paths with a trailing /
    return os.path.normpath(os.path.dirname(os.path.normpath(p)))


def basename(p):
    # works more intuitively for paths with a trailing /
    return os.path.normpath(os.path.basename(os.path.normpath(p)))


class Build(collections.namedtuple('_Build', 'build_command setup cleanup env tree')):
    '''Holds the shell ASTs and various other data, used to execute each build.

    Fields:
        build_command (_shell_ast.Command): The build command itself, including
            all commands that accept other commands as arguments.  Examples:
            setarch.
        setup (_shell_ast.AndList): These are shell commands that change the
            shell environment and need to be run as part of the same script as
            the main build command but don't take other commands as arguments.
            These execute conditionally because if one command fails,
            the whole script should fail.  Examples: cd, umask.
        cleanup (_shell_ast.List): All commands that have to be run to return
            the testbed to its initial state, before the testbed does its own
            cleanup.  These are executed only if the build command fails,
            because otherwise the cleanup has to occur after the build artifact
            is copied out.  These execution unconditionally, one after another,
            because all cleanup commands should be attempted irrespective of
            whether others succeed.  Examples: fileordering.
        env (types.MappingProxyType): Immutable mapping of the environment.
        tree (str): Path to the source root where the build should take place.
    '''

    @classmethod
    def from_command(cls, build_command, env, tree):
        return cls(
            build_command = _shell_ast.SimpleCommand(
                "sh", "-ec", _shell_ast.Quote(build_command)),
            setup = _shell_ast.AndList(),
            cleanup = _shell_ast.List(),
            env = env,
            tree = tree,
        )

    def add_env(self, key, value):
        '''Helper function for adding a key-value pair to an immutable mapping.'''
        new_mapping = self.env.copy()
        new_mapping[key] = value
        return self._replace(env=types.MappingProxyType(new_mapping))

    def append_to_build_command(self, command):
        '''Passes the current build command as the last argument to a given
        _shell_ast.SimpleCommand.

        '''
        new_suffix = (command.cmd_suffix +
                      _shell_ast.CmdSuffix([self.build_command]))
        new_command = _shell_ast.SimpleCommand(command.cmd_prefix,
                                               command.cmd_name,
                                               new_suffix)
        return self._replace(build_command=new_command)

    def append_setup(self, command):
        '''Adds a command to the setup phase.

        '''
        new_setup = self.setup + _shell_ast.AndList([command])
        return self._replace(setup=new_setup)

    def append_setup_exec(self, *args):
        return self.append_setup_exec_raw(*map(_shell_ast.Quote, args))

    def append_setup_exec_raw(self, *args):
        return self.append_setup(_shell_ast.SimpleCommand.make(*args))

    def prepend_cleanup(self, command):
        '''Adds a command to the cleanup phase.

        '''
        # if this command fails, save the exit code but keep executing
        # we run with -e, so it would fail otherwise
        new_cleanup = (_shell_ast.List([_shell_ast.Term(
                            "{0} || __c=$?".format(command), ';')])
                       + self.cleanup)
        return self._replace(cleanup=new_cleanup)

    def prepend_cleanup_exec(self, *args):
        return self.prepend_cleanup_exec_raw(*map(_shell_ast.Quote, args))

    def prepend_cleanup_exec_raw(self, *args):
        return self.prepend_cleanup(_shell_ast.SimpleCommand.make(*args))

    def move_tree(self, source, target, set_tree):
        new_build = self.append_setup_exec(
            'mv', source, target).prepend_cleanup_exec(
            'mv', target, source)
        if set_tree:
            return new_build._replace(tree = os.path.join(target, ''))
        else:
            return new_build

    def to_script(self):
        '''Generates the shell code for the script.

        The build command is only executed if all the setup commands
        finish without errors.  The setup and build commands are
        executed in a subshell so that changes they make to the shell
        don't affect the cleanup commands.  (This avoids the problem
        with the disorderfs mount being kept open as a current working
        directory when the cleanup tries to unmount it.)

        '''
        subshell = _shell_ast.Subshell(self.setup +
                                       _shell_ast.AndList([self.build_command]))

        if self.cleanup:
            cleanup = """( __c=0; {0} exit $__c; )""".format(str(self.cleanup))
            return """\
if {0}; then
    {1};
else
    __x=$?;
    if {1}; then exit $__x; else
        echo >&2; "cleanup failed with exit code $?"; exit $__x;
    fi;
fi
""".format(str(subshell), str(cleanup))
        else:
            return str(subshell)


# time zone, locales, disorderfs, host name, user/group, shell, CPU
# number, architecture for uname (using linux64), umask, HOME, see
# also: https://tests.reproducible-builds.org/index_variations.html
# TODO: the below ideally should *read the current value*, and pick
# something that's different for the experiment.

# FIXME: use taskset(1) and/or dpkg-buildpackage -J1
# def cpu(script, env, tree):
#     return script, env, tree

def environment(ctx, build, vary):
    if not vary:
        return build
    return build.add_env('CAPTURE_ENVIRONMENT', 'i_capture_the_environment')

# FIXME: this requires superuser privileges.
# Probably need to couple with "namespace" UTS unshare when not running in a
# virtual_server, see below for details
# def domain_host(ctx, script, env, tree):
#     return script, env, tree

# Note: this has to go before fileordering because we can't move mountpoints
# TODO: this variation makes it impossible to parallelise the build, for most
# of the current virtual servers. (It's theoretically possible to make it work)
def build_path(ctx, build, vary):
    if vary:
        return build
    const_path = os.path.join(dirname(build.tree), 'const_build_path')
    return build.move_tree(build.tree, const_path, True)

@tool_required("disorderfs")
def fileordering(ctx, build, vary):
    if not vary:
        return build

    old_tree = os.path.join(dirname(build.tree), basename(build.tree) + '-before-disorderfs', '')
    _ = build.move_tree(build.tree, old_tree, False)
    _ = _.append_setup_exec('mkdir', '-p', build.tree)
    _ = _.prepend_cleanup_exec('rmdir', build.tree)
    disorderfs = ['disorderfs'] + ([] if ctx.verbosity else ["-q"])
    _ = _.append_setup_exec(*(disorderfs + ['--shuffle-dirents=yes', old_tree, build.tree]))
    _ = _.prepend_cleanup_exec('fusermount', '-u', build.tree)
    # the "user_group" variation hacks PATH to run "sudo -u XXX" instead of various tools, pick it up here
    binpath = os.path.join(dirname(build.tree), 'bin')
    _ = _.prepend_cleanup_exec_raw('export', 'PATH="%s:$PATH"' % binpath)
    return _

# Note: this has to go after anything that might modify 'tree' e.g. build_path
def home(ctx, build, vary):
    if not vary:
        # choose an existent HOME, see Debian bug #860428
        return build.add_env('HOME', build.tree)
    else:
        return build.add_env('HOME', '/nonexistent/second-build')

# TODO: uname is a POSIX standard.  The related Linux command
# (setarch) only affects uname at the moment according to the docs.
# FreeBSD changes uname with environment variables.  Wikipedia has a
# reference to a setname command on another Unix variant:
# https://en.wikipedia.org/wiki/Uname
def kernel(ctx, build, vary):
    # set these two explicitly different. otherwise, when reprotest is
    # reprotesting itself, then one of the builds will fail its tests, because
    # its two child reprotests will see the same value for "uname" but the
    # tests expect different values.
    if not vary:
        return build.append_to_build_command(_shell_ast.SimpleCommand.make('linux64', '--uname-2.6'))
    else:
        return build.append_to_build_command(_shell_ast.SimpleCommand.make('linux32'))

# TODO: if this locale doesn't exist on the system, Python's
# locales.getlocale() will return (None, None) rather than this
# locale.  I imagine it will also probably cause false positives with
# builds being reproducible when they aren't because of locale-based
# issues if this locale isn't installed.  The right solution here is
# for this locale to be encoded into the dependencies so installing it
# installs the right locale.  A weaker but still reasonable solution
# is to figure out what locales are installed (how?) and use another
# locale if this one isn't installed.

# TODO: what exact locales and how to many test is probably a mailing
# list question.
def locales(ctx, build, vary):
    if not vary:
        return build.add_env('LANG', 'C.UTF-8').add_env('LANGUAGE', 'en_US:en')
    else:
        # if there is an issue with this being random, we could instead select it
        # based on a deterministic hash of the inputs
        loc = random.choice(['fr_CH.UTF-8', 'es_ES', 'ru_RU.CP1251', 'kk_KZ.RK1048', 'zh_CN'])
        return build.add_env('LANG', loc).add_env('LC_ALL', loc).add_env('LANGUAGE', '%s:fr' % loc)

# FIXME: Linux-specific.  unshare --uts requires superuser privileges.
# See misc.git/prebuilder/pbuilderhooks/D01_hostname, UTS unshare helps to
# avoid hostname/domainname changes affect the main system.
# def namespace(ctx, script, env, tree):
#     # command1 = ['unshare', '--uts'] + command1
#     # command2 = ['unshare', '--uts'] + command2
#     return script, env, tree

def exec_path(ctx, build, vary):
    if not vary:
        return build
    return build.add_env('PATH', build.env['PATH'] + ':/i_capture_the_path')

# This doesn't require superuser privileges, but the chsh command
# affects all user shells, which would be bad.
# # def shell(ctx, script, env, tree):
#     return script, env, tree
# TODO: also test differences with /bin/sh as bash vs dash

def timezone(ctx, build, vary):
    # These time zones are theoretically in the POSIX time zone format
    # (http://pubs.opengroup.org/onlinepubs/9699919799/basedefs/V1_chap08.html#tag_08),
    # so they should be cross-platform compatible.
    if not vary:
        return build.add_env('TZ', 'GMT+12')
    else:
        return build.add_env('TZ', 'GMT-14')

@tool_required("faketime")
def faketime(ctx, build, vary):
    if not vary:
        # FIXME: this does not actually fix the time, it just lets the system clock run normally
        return build
    lastmt = random.choice(ctx.spec.time.faketimes)
    now = time.time()
    # FIXME: better way of choosing which faketime to use
    if lastmt.startswith("@") and int(lastmt[1:]) < now - 32253180:
        # if lastmt is far in the past, use that, it's a bit safer
        faket = '@%s' % lastmt
    else:
        # otherwise use a date far in the future
        faket = '+373days+7hours+13minutes'
    settime = _shell_ast.SimpleCommand.make('faketime', faket)
    # faketime's manpages are stupidly misleading; it also modifies file timestamps.
    # this is only mentioned in the README. we do not want this, it really really
    # messes with GNU make and other buildsystems that look at timestamps.
    return build.add_env('NO_FAKE_STAT', '1').append_to_build_command(settime)

def umask(ctx, build, vary):
    if not vary:
        return build.append_setup_exec('umask', '0022')
    else:
        return build.append_setup_exec('umask', '0002')

# Note: this needs to go before anything that might need to run setup commands
# as the other user (e.g. due to permissions).
@tool_required("sudo")
def user_group(ctx, build, vary):
    if not vary:
        return build

    if not ctx.spec.user_group.available:
        logging.warn("IGNORING user_group variation; supply more usergroups "
        "with --variations=user_group.available+=USER1:GROUP1;USER2:GROUP2 or "
        "alternatively, suppress this warning with --variations=-user_group")
        return build

    olduser = getpass.getuser()
    oldgroup = grp.getgrgid(os.getgid()).gr_name
    user_group = random.choice(list(set(ctx.spec.user_group.available) - set([(olduser, oldgroup)])))
    if ":" in user_group:
        user, group = user_group.split(":", 1)
        if user:
            sudo_command = ('sudo', '-E', '-u', user, '-g', group)
        else:
            user = olduser
            sudo_command = ('sudo', '-E', '-g', group)
    else:
        user = user_group # "user" is used below
        sudo_command = ('sudo', '-E', '-u', user)
    sudobuild = _shell_ast.SimpleCommand.make(*sudo_command)
    binpath = os.path.join(dirname(build.tree), 'bin')

    _ = build.append_to_build_command(sudobuild)
    # disorderfs needs to run as a different user.
    # we prefer that to running it as root, principle of least-privilege.
    _ = _.append_setup_exec('sh', '-ec', r'''
mkdir -p "{0}"
printf '#!/bin/sh\n{1} /usr/bin/disorderfs "$@"\n' > "{0}"/disorderfs
chmod +x "{0}"/disorderfs
printf '#!/bin/sh\n{1} /bin/mkdir "$@"\n' > "{0}"/mkdir
chmod +x "{0}"/mkdir
printf '#!/bin/sh\n{1} /bin/fusermount "$@"\n' > "{0}"/fusermount
chmod +x "{0}"/fusermount
'''.format(binpath, " ".join(map(shlex.quote, sudo_command))))
    _ = _.prepend_cleanup_exec('sh', '-ec',
        'cd "{0}" && rm -f disorderfs mkdir fusermount'.format(binpath))
    _ = _.append_setup_exec_raw('export', 'PATH="%s:$PATH"' % binpath)
    if user != olduser:
        _ = _.append_setup_exec('sudo', 'chown', '-h', '-R', '--from=%s' % olduser, user, build.tree)
        # TODO: artifacts probably shouldn't be chown'd back
        _ = _.prepend_cleanup_exec('sudo', 'chown', '-h', '-R', '--from=%s' % user, olduser, build.tree)
    return _


# The order of the variations *is* important, because the command to
# be executed in the container needs to be built from the inside out.
VARIATIONS = collections.OrderedDict([
    ('environment', environment),
    ('build_path', build_path),
    ('user_group', user_group),
    # ('cpu', cpu),
    # ('domain_host', domain_host),
    ('fileordering', fileordering),
    ('home', home),
    ('kernel', kernel),
    ('locales', locales),
    # ('namespace', namespace),
    ('exec_path', exec_path),
    # ('shell', shell),
    ('time', faketime),
    ('timezone', timezone),
    ('umask', umask),
])


class TimeVariation(collections.namedtuple('_TimeVariation', 'faketimes auto_faketimes')):
    @classmethod
    def default(cls):
        return cls(mdiffconf.strlist_set(";"), mdiffconf.strlist_set(";", ['SOURCE_DATE_EPOCH']))

    @classmethod
    def empty(cls):
        return cls(mdiffconf.strlist_set(";"), mdiffconf.strlist_set(";"))

    def apply_dynamic_defaults(self, source_root):
        new_faketimes = []
        for a in self.auto_faketimes:
            if a == "SOURCE_DATE_EPOCH":
                # Get the latest modification date of all the files in the source root.
                # This tries hard to avoid bad interactions with faketime and make(1) etc.
                # However if you're building this too soon after changing one of the source
                # files then the effect of this variation is not very great.
                filemtimes = (os.path.getmtime(os.path.join(root, f)) for root, dirs, files in os.walk(source_root) for f in files)
                new_faketimes.append("@%d" % int(max(filemtimes, default=0)))
            else:
                raise ValueError("unrecognized auto_faketime: %s" % a)
        return self.empty()._replace(faketimes=self.faketimes + new_faketimes)


class UserGroupVariation(collections.namedtuple('_UserGroupVariation', 'available')):
    @classmethod
    def default(cls):
        return cls(mdiffconf.strlist_set(";"))


class VariationSpec(mdiffconf.ImmutableNamespace):
    @classmethod
    def default(cls, variations=VARIATIONS):
        default_overrides = {
            "user_group": UserGroupVariation.default(),
            "time": TimeVariation.default(),
        }
        return cls(**{k: default_overrides.get(k, True) for k in variations})

    @classmethod
    def default_long_string(cls):
        actions = cls.default().actions()
        return ", ".join("+" + a[0] for a in actions)

    @classmethod
    def empty(cls):
        return cls()

    @classmethod
    def all_names(cls):
        return list(VARIATIONS.keys())

    def variations(self):
        return [k for k in VARIATIONS.keys() if k in self.__dict__]

    aliases = { ("@+-", "all"): list(VARIATIONS.keys()) }
    def extend(self, actions):
        one = self.default()
        return mdiffconf.parse_all(self, actions, one, one, self.aliases, sep=",")

    def __getitem__(self, k):
        return self.__dict__[k]

    def actions(self):
        return [(k, k in self.__dict__, v) for k, v in VARIATIONS.items()]

    def apply_dynamic_defaults(self, source_root):
        return self.__class__(**{
            k: v.apply_dynamic_defaults(source_root) if hasattr(v, "apply_dynamic_defaults") else v
            for k, v in self.__dict__.items()
        })


class Variations(collections.namedtuple('_Variations', 'spec verbosity')):
    @classmethod
    def of(cls, *specs, zero=VariationSpec.empty(), verbosity=0):
        return [cls(spec, verbosity) for spec in [zero] + list(specs)]

    @property
    def replace(self):
        return AttributeReplacer(self, [])


if __name__ == "__main__":
    import sys
    d = VariationSpec()
    for s in sys.argv[1:]:
        d = d.extend([s])
        print(s)
        print(">>>", d)
    print("result", d.apply_dynamic_defaults("."))
