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

from reprotest import environ
from reprotest import mdiffconf
from reprotest import shell_syn
from reprotest.utils import AttributeReplacer

logger = logging.getLogger(__name__)


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


class Build(collections.namedtuple('_Build', 'build_command setup cleanup env tree aux_tree')):
    '''Holds the shell ASTs and various other data, used to execute each build.

    Fields:
        build_command (shell_syn.Command): The build command itself, including
            wrapper commands like setarch and sudo that never need cleanup.
        setup (shell_syn.AndList): These are shell commands that change the
            shell environment and need to be run as part of the same script as
            the main build command but don't take other commands as arguments.
            These execute conditionally because if one command fails,
            the whole script should fail.  Examples: cd, umask.
        cleanup (shell_syn.List): All commands that have to be run to return
            the testbed to its initial state, before the testbed does its own
            cleanup.  These execute one after another regardless of failure,
            because all cleanup commands should be attempted irrespective of
            whether others succeed.  Examples: fileordering.  This is *not* run
            if no_clean_on_error is given and setup or build_command failed.
        env (types.MappingProxyType): Immutable mapping of the environment.
        tree (str): Path to the source root where the build should take place.
        aux_tree (str): Path where auxilliary files are stored by reprotest.
            When using cls.from_command(), this is automatically created and
            cleaned up by the build script.
    '''

    @classmethod
    def from_command(cls, build_command, env, tree):
        aux_tree = os.path.join(dirname(tree), basename(tree) + '-aux')
        _ = cls(
            build_command = shell_syn.Command.make(
                "sh", "-ec", shlex.quote(str(build_command))),
            setup = shell_syn.AndList(),
            cleanup = shell_syn.List(),
            env = env,
            tree = tree,
            aux_tree = aux_tree,
        )
        _ = _.append_setup_exec('mkdir', '-p', aux_tree)
        _ = _.prepend_cleanup_exec('rm', '-rf', aux_tree)
        return _

    def add_env(self, key, value):
        '''Helper function for adding a key-value pair to an immutable mapping.'''
        new_mapping = self.env.copy()
        new_mapping[key] = value
        return self._replace(env=types.MappingProxyType(new_mapping))

    def modify_env(self, add, rem):
        '''Helper function for adding a key-value pair to an immutable mapping.'''
        new_mapping = self.env.copy()
        for k, v in add:
            new_mapping[k] = v
        for k in rem:
            del new_mapping[k]
        return self._replace(env=types.MappingProxyType(new_mapping))

    def prepend_to_build_command(self, *prefix):
        '''Prepend a wrapper command onto the build_command.'''
        return self.prepend_to_build_command_raw(*map(shlex.quote, prefix))

    def prepend_to_build_command_raw(self, *prefix):
        new_command = shell_syn.Command(
            cmd_prefix=shell_syn.CmdPrefix(prefix),
            cmd_suffix=self.build_command)
        return self._replace(build_command=new_command)

    def append_setup(self, command):
        '''Adds a command to the setup phase.'''
        new_setup = self.setup + shell_syn.AndList([command])
        return self._replace(setup=new_setup)

    def append_setup_exec(self, *args):
        return self.append_setup_exec_raw(*map(shlex.quote, args))

    def append_setup_exec_raw(self, *args):
        return self.append_setup(shell_syn.Command.make(*args))

    def prepend_cleanup(self, command):
        '''Adds a command to the cleanup phase.'''
        # if this command fails, save the exit code but keep executing
        # we run with -e, so it would fail otherwise
        new_cleanup = shell_syn.List.make("{0} || __c=$?".format(command))
        return self._replace(cleanup=new_cleanup + self.cleanup)

    def prepend_cleanup_exec(self, *args):
        return self.prepend_cleanup_exec_raw(*map(shlex.quote, args))

    def prepend_cleanup_exec_raw(self, *args):
        return self.prepend_cleanup(shell_syn.Command.make(*args))

    def move_tree(self, source, target, set_tree):
        new_build = self.append_setup_exec(
            'mv', source, target).prepend_cleanup_exec(
            'mv', target, source)
        if set_tree:
            return new_build._replace(tree = os.path.join(target, ''))
        else:
            return new_build

    def to_script(self, no_clean_on_error):
        '''Generates the shell code for the script.

        The build command is only executed if all the setup commands
        finish without errors.  The setup and build commands are
        executed in a subshell so that changes they make to the shell
        don't affect the cleanup commands.  (This avoids the problem
        with the disorderfs mount being kept open as a current working
        directory when the cleanup tries to unmount it.)

        '''
        subshell = self.setup + shell_syn.AndList([self.build_command])

        if self.cleanup:
            cleanup = shell_syn.List.make("__c=0") + self.cleanup + \
                      shell_syn.List.make("exit $__c")
            # TODO: the below can be extended with a custom command. shell
            # doesn't work yet though; we need to hook into autopkgtest better.
            whether_to_clean = '! ' + str(bool(no_clean_on_error)).lower()
            main_script = """\
trap '( cleanup )' HUP INT QUIT ABRT TERM PIPE # FIXME doesn't quite work reliably yet

if ( run_build ); then ( cleanup ); else
    __x=$?; # save the exit code of run_build
    if ( {0} ); then
        if ( cleanup ); then :; else echo >&2 "cleanup failed with exit code $?"; fi;
    fi
    exit $__x
fi
""".format(whether_to_clean)

            return """\
run_build() {{
    {0}
}}

cleanup() {{
    {1}
}}

{2}
""".format(subshell.__str__(4), cleanup.__str__(4), main_script.rstrip()).rstrip()
        else:
            return str(subshell)


# time zone, locales, disorderfs, host name, user/group, shell, CPU
# number, architecture for uname (using linux64), umask, HOME, see
# also: https://tests.reproducible-builds.org/index_variations.html
# TODO: the below ideally should *read the current value*, and pick
# something that's different for the experiment.

def environment(ctx, build, vary):
    if not vary:
        return build
    added, removed = [], []
    for k, v in environ.parse_environ_templates(ctx.spec.environment.variables):
        if v is None:
            removed += [k]
        else:
            added += [(k, v)]
    return build.modify_env(added, removed)

def domain_host(ctx, build, vary):
    if not vary:
        return build
    hostname = "reprotest-capture-hostname"
    domainname = "reprotest-capture-domainname"
    _ = build

    # TODO: below only works on linux, of course..
    if ctx.spec.domain_host.use_sudo:
        ns_uts = '%s/ns-%s' % (build.aux_tree, "uts")
        _ = _.append_setup_exec('touch', ns_uts)
        # create our unshare
        ns_args = ['--uts=%s' % ns_uts]
        _ = _.append_setup_exec(*SUDO, 'unshare', *ns_args, 'true')
        _ = _.prepend_cleanup_exec(*SUDO, 'umount', ns_uts)
        # configure our unshare
        nsenter = SUDO + ['nsenter'] + ns_args
        _ = _.append_setup_exec(*nsenter, 'hostname', hostname)
        _ = _.append_setup_exec(*nsenter, 'domainname', domainname)
        # wrap our build command
        _ = _.prepend_to_build_command(*SUDO, '-E', *(nsenter[len(SUDO):]), *make_sudo_command(*current_user_group()))
    else:
        logger.warn("Not using sudo for domain_host; your build may fail. See man page for other options.")
        logger.warn("Be sure to `echo 1 > /proc/sys/kernel/unprivileged_userns_clone` if on a Debian system.")
        if "user_group" in ctx.spec and ctx.spec.user_group.available:
            logger.error("Incompatible variations: domain_host.use_sudo False, user_group.available non-empty.")
            raise ValueError("Incompatible variations; check the log for details.")
        _ = _.prepend_to_build_command(*"unshare -r --uts".split(),
            "sh", "-ec", r"""
            hostname {1}
            domainname "{2}"
            """.format(build.aux_tree, hostname, domainname) + '"$@"', "-")
    return _

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
    disorderfs = ['disorderfs'] + (['>&2'] if ctx.verbosity >= 2 else ['-q'])
    _ = _.append_setup_exec_raw(*disorderfs, *map(shlex.quote, ['--shuffle-dirents=yes', old_tree, build.tree]))
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
    _ = build
    if not vary:
        _ = _.append_setup_exec_raw('SETARCH_ARCH=$(uname -m)')
    else:
        _ = _.append_setup_exec_raw('SETARCH_ARCH=$(setarch --list | grep -vF "$(uname -m)" | shuf | head -n1)')
        _ = _.append_setup_exec_raw('KERNEL_VERSION=$(uname -r)')
        _ = _.append_setup_exec_raw('if [ ${KERNEL_VERSION#2.6} = $KERNEL_VERSION ]; then SETARCH_OPTS=--uname-2.6; fi')
    return _.prepend_to_build_command_raw('setarch', '$SETARCH_ARCH', '$SETARCH_OPTS')

def aslr(ctx, build, vary):
    if vary:
        return build
    return build.append_setup_exec_raw('SETARCH_OPTS="$SETARCH_OPTS -R"')

def num_cpus(ctx, build, vary):
    _ = build
    _ = _.append_setup_exec_raw('CPU_MAX=$(nproc)')
    _ = _.append_setup_exec_raw('CPU_MIN=$({ echo $CPU_MAX; echo %s; } | sort -n | head -n1)' % ctx.min_cpus)
    if ctx.min_cpus <= 0:
        raise ValueError("--min-cpus must be a positive integer: " % ctx.min_cpus)
    if not vary:
        _ = _.append_setup_exec_raw('CPU_NUM=$CPU_MIN')
    else:
        # random number between min_cpus and $(nproc)
        _ = _.append_setup_exec_raw('CPU_NUM=$(if [ $CPU_MIN = $CPU_MAX ]; \
            then echo $CPU_MIN; echo >&2 "only 1 CPU is available; num_cpus is ineffective"; \
            else shuf -i$((CPU_MIN + 1))-$CPU_MAX -n1; fi)')

    # select CPU_NUM random cpus from the range 0..$((CPU_MAX-1))
    cpu_list = "$(echo $(shuf -i0-$((CPU_MAX - 1)) -n$CPU_NUM) | tr ' ' ,)"
    return _.prepend_to_build_command_raw('taskset', '-a', '-c', cpu_list)

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

def exec_path(ctx, build, vary):
    if not vary:
        return build
    return build.add_env('PATH', build.env['PATH'] + ':/i_capture_the_path')

# This doesn't require superuser privileges, but the chsh command
# affects all user shells, which would be bad.
# # def shell(ctx, script, env, tree):
#     return script, env, tree
# FIXME: also test differences with /bin/sh as bash vs dash

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
        # fix the time at base_faketime
        faket = ctx.base_faketime
    elif ctx.spec.time.faketimes:
        faket = random.choice(ctx.spec.time.faketimes)
    else:
        now = time.time()
        base = int(ctx.base_faketime[1:]) if ctx.base_faketime.startswith("@") else now
        # 15552000 = 180 days
        if base < now - 15552000 and not random.randint(0, 1):
            # if ctx.base_faketime is far in the past, with 1/2 probability
            # reuse the current time and don't fake it
            return build
        else:
            # otherwise use a date far in the future
            faket = '+%sdays+%shours+%sminutes' % (
                random.randint(180, 540), random.randint(0, 23), random.randint(0, 59))

    # faketime's manpages are stupidly misleading; it also modifies file timestamps.
    # this is only mentioned in the README. we do not want this, it really really
    # messes with GNU make and other buildsystems that look at timestamps.
    # set NO_FAKE_STAT=1 avoids this.
    return build.add_env('NO_FAKE_STAT', '1').prepend_to_build_command('faketime', faket)

def umask(ctx, build, vary):
    if not vary:
        return build.append_setup_exec('umask', '0022')
    else:
        return build.append_setup_exec('umask', '0002')


def current_user_group():
    return getpass.getuser(), grp.getgrgid(os.getgid()).gr_name


# -h localhost otherwise we get annoying messages about "can't resolve host"
# especially when doing the domain_host variation
SUDO = ['sudo', '-h', 'localhost']

def make_sudo_command(user, group):
    assert user or group
    userarg = ['-u', user] if user else []
    grouparg = ['-g', group] if group else []
    return SUDO + ['-E'] + userarg + grouparg + ['env',
        '-u', 'SUDO_COMMAND', '-u', 'SUDO_GID', '-u', 'SUDO_UID', '-u', 'SUDO_USER']

def parse_user_group(user_group):
    if not user_group or user_group == ':':
        raise ValueError("user_group is empty: '%s'" % user_group)
    if ":" in user_group:
        user, group = user_group.split(":", 1)
        if user:
            return user, group
        else:
            return None, group
    else:
        return user_group, None

# Note: this needs to go before anything that might need to run setup commands
# as the other user (e.g. due to permissions).
@tool_required("sudo")
def user_group(ctx, build, vary):
    if not vary:
        return build

    if not ctx.spec.user_group.available:
        logger.warn("IGNORING user_group variation; supply more usergroups "
        "with --variations=user_group.available+=USER1:GROUP1;USER2:GROUP2 or "
        "alternatively, suppress this warning with --variations=-user_group")
        return build

    olduser, oldgroup = current_user_group()
    user_group = random.choice(list(set(ctx.spec.user_group.available) - set([(olduser, oldgroup)])))
    user, group = parse_user_group(user_group)
    sudo_command = make_sudo_command(user, group)
    if not user:
        user = olduser
    binpath = os.path.join(dirname(build.tree), 'bin')

    _ = build.prepend_to_build_command(*sudo_command)
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
        _ = _.append_setup_exec(*SUDO, 'chown', '-h', '-R', '--from=%s' % olduser, user, build.tree)
        # TODO: artifacts probably shouldn't be chown'd back
        _ = _.prepend_cleanup_exec(*SUDO, 'chown', '-h', '-R', '--from=%s' % user, olduser, build.tree)
    return _


# The order of the variations *is* important, because the command to
# be executed in the container needs to be built from the inside out.
VARIATIONS = collections.OrderedDict([
    ('environment', environment),
    ('build_path', build_path),
    ('kernel', kernel),
    ('aslr', aslr), # needs to run after kernel which runs "setarch"
                    # but also as close to the build command as possible, (i.e. earlier in this list)
                    # otherwise other variations below can affect the address layout
    ('num_cpus', num_cpus),
    ('time', faketime), # needs to go before sudo (user_group), closer to the build command
    ('user_group', user_group),
    ('fileordering', fileordering),
    ('domain_host', domain_host), # needs to run after all other mounts have been set
    ('home', home),
    ('locales', locales),
    # ('namespace', namespace),
    ('exec_path', exec_path),
    # ('shell', shell),
    ('timezone', timezone),
    ('umask', umask),
])


def auto_source_date_epoch(source_root):
    # Get the latest modification date of all the files in the source root.
    # This tries hard to avoid bad interactions with faketime and make(1) etc.
    # However if you're building this too soon after changing one of the source
    # files then the effect of this variation is not very great.
    filemtimes = (os.lstat(os.path.join(root, f)).st_mtime
                  for root, dirs, files in os.walk(source_root)
                  for f in files)
    return int(max(filemtimes, default=1))


class TimeVariation(collections.namedtuple('_TimeVariation', 'faketimes auto_faketimes')):
    @classmethod
    def default(cls):
        return cls(mdiffconf.strlist_set(";"), mdiffconf.strlist_set(";", ['SOURCE_DATE_EPOCH']))

    @classmethod
    def empty(cls):
        return cls(mdiffconf.strlist_set(";"), mdiffconf.strlist_set(";"))


class EnvironmentVariation(collections.namedtuple("_EnvironmentVariation", "variables")):
    @classmethod
    def default(cls):
        return cls(mdiffconf.strlist_set(";", ["REPROTEST_CAPTURE_ENVIRONMENT"]))

    def extend_variables(self, *ks):
        return self._replace(variables=self.variables + list(ks))


class UserGroupVariation(collections.namedtuple('_UserGroupVariation', 'available')):
    @classmethod
    def default(cls):
        return cls(mdiffconf.strlist_set(";"))


class DomainHostVariation(collections.namedtuple('_DomainHostVariation', 'use_sudo')):
    @classmethod
    def default(cls):
        return cls(0)


class VariationSpec(mdiffconf.ImmutableNamespace):
    @classmethod
    def default(cls, variations=VARIATIONS):
        default_overrides = {
            "environment": EnvironmentVariation.default(),
            "user_group": UserGroupVariation.default(),
            "time": TimeVariation.default(),
            "domain_host": DomainHostVariation.default(),
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

    def __contains__(self, k):
        return k in self.__dict__

    def __getitem__(self, k):
        return self.__dict__[k]

    def actions(self):
        return [(k, k in self.__dict__, v) for k, v in VARIATIONS.items()]


class Variations(collections.namedtuple('_Variations', 'spec verbosity min_cpus base_faketime')):
    @classmethod
    def of(cls, *specs, zero=VariationSpec.empty(), verbosity=0, min_cpus=1, base_faketime="@0"):
        return [cls(spec, verbosity, min_cpus, base_faketime) for spec in [zero] + list(specs)]

    @property
    def replace(self):
        return AttributeReplacer(self, [])


def print_sudoers(spec):
    logger.warn("This feature is EXPERIMENTAL, use at your own risk.")
    logger.warn("The output may be out-of-date, please file bugs if it doesn't work...")

    user, group = current_user_group()
    a = "[a-zA-Z0-9]"
    b = "/tmp/reprotest.{0}{0}{0}{0}{0}{0}".format(a)
    variables = {
        "user": user,
        "group": group,
        "base": b,
    }
    experiments = [os.path.join(b, x) for x in [
        "build-experiment-[1-9]",
        "build-experiment-[1-9][0-9]",
        "build-experiment-blacklist",
        "build-experiment-non-whitelist",
    ] + ["build-experiment-%s" % k for k in VariationSpec.all_names()]]

    if "user_group" in spec and spec.user_group.available:
        user_groups = [parse_user_group(user_group) for user_group in spec.user_group.available]
        users = sorted(set(user for user, group in user_groups if user))
        for otheruser in users:
            newvars = dict(**variables, otheruser=otheruser)
            print("""\
# Rules for varying user_group with user %(otheruser)s
%(user)s ALL = (%(otheruser)s) NOPASSWD: ALL
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base)s/const_build_path/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base)s/const_build_path/
""".rstrip() % newvars)
            for base_ex in experiments:
                print("""\
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base_ex)s/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base_ex)s-before-disorderfs/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base_ex)s/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base_ex)s-before-disorderfs/
""".rstrip() % dict(**newvars, base_ex=base_ex))
            print()

    if "domain_host" in spec and spec.domain_host.use_sudo:
        print("""# Rules for varying domain_host""")
        for base_ex in experiments:
            print("""\
%(user)s ALL = NOPASSWD: /usr/bin/unshare --uts=%(base_ex)s-aux/ns-uts true
%(user)s ALL = NOPASSWD: /usr/bin/nsenter --uts=%(base_ex)s-aux/ns-uts hostname reprotest-*
%(user)s ALL = NOPASSWD: /usr/bin/nsenter --uts=%(base_ex)s-aux/ns-uts domainname reprotest-*
%(user)s ALL = NOPASSWD:SETENV: /usr/bin/nsenter --uts=%(base_ex)s-aux/ns-uts sudo -h localhost -E -u %(user)s -g %(group)s env *
%(user)s ALL = NOPASSWD: /bin/umount %(base_ex)s-aux/ns-uts
""".rstrip() % dict(**variables, base_ex=base_ex))
        print()


if __name__ == "__main__":
    import sys
    d = VariationSpec()
    for s in sys.argv[1:]:
        d = d.extend([s])
        print(s)
        print(">>>", d)
    print("result", d)
