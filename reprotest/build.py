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
        new_command = shell_syn.Command(
            cmd_prefix=shell_syn.CmdPrefix(map(shlex.quote, prefix)),
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

# FIXME: use taskset(1) and/or dpkg-buildpackage -J1
# def cpu(script, env, tree):
#     return script, env, tree

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
        ns_uts, ns_mnt = ('%s/ns-%s' % (build.aux_tree, ns) for ns in ("uts", "mnt"))
        _ = _.append_setup_exec('touch', ns_mnt, ns_uts)
        # make ns_mnt have propagation=private, required for --mount=$ns_mnt
        _ = _.append_setup_exec('sudo', 'mount', '-B', ns_mnt, ns_mnt)
        _ = _.append_setup_exec('sudo', 'mount', '--make-private', ns_mnt)
        _ = _.prepend_cleanup_exec('sudo', 'umount', ns_mnt)
        # create our unshare
        ns_args = ['--mount=%s' % ns_mnt, '--uts=%s' % ns_uts]
        _ = _.append_setup_exec('sudo', 'unshare', *ns_args, 'true')
        _ = _.prepend_cleanup_exec('sudo', 'umount', ns_mnt)
        _ = _.prepend_cleanup_exec('sudo', 'umount', ns_uts)
        # configure our unshare
        nsenter = ['sudo', 'nsenter'] + ns_args
        _ = _.append_setup_exec(*nsenter, 'hostname', hostname)
        _ = _.append_setup_exec(*nsenter, 'domainname', domainname)
        # the mount -B hack suppresses spurious sudo(1) warnings about "unable to resolve host"
        _ = _.append_setup_exec('sh', '-ec',
            'echo "127.0.0.1 {1}" > {0}/hosts && cat /etc/hosts >> {0}/hosts'.format(build.aux_tree, hostname))
        _ = _.append_setup_exec(*nsenter, 'mount', '-B', '%s/hosts' % build.aux_tree, '/etc/hosts')
        # wrap our build command
        _ = _.prepend_to_build_command('sudo', '-E', 'nsenter', *ns_args, *make_sudo_command(*current_user_group()))
    else:
        logging.warn("Not using sudo for domain_host; it is recommended. Your build may fail.")
        logging.warn("Be sure to `echo 1 > /proc/sys/kernel/unprivileged_userns_clone` if on a Debian system.")
        if "user_group" in ctx.spec and ctx.spec.user_group.available:
            logging.error("Incompatible variations: domain_host.use_sudo False, user_group.available non-empty.")
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
    # set these two explicitly different. otherwise, when reprotest is
    # reprotesting itself, then one of the builds will fail its tests, because
    # its two child reprotests will see the same value for "uname" but the
    # tests expect different values.
    if not vary:
        return build.prepend_to_build_command('linux64', '--uname-2.6')
    else:
        return build.prepend_to_build_command('linux32')

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
        # FIXME: this does not actually fix the time, it just lets the system clock run normally
        return build
    lastmt = random.choice(ctx.spec.time.faketimes)
    now = time.time()
    # FIXME: better way of choosing which faketime to use
    if lastmt.startswith("@") and int(lastmt[1:]) < now - 32253180:
        # if lastmt is far in the past, use that, it's a bit safer
        faket = lastmt
    else:
        # otherwise use a date far in the future
        faket = '+373days+7hours+13minutes'
    # faketime's manpages are stupidly misleading; it also modifies file timestamps.
    # this is only mentioned in the README. we do not want this, it really really
    # messes with GNU make and other buildsystems that look at timestamps.
    return build.add_env('NO_FAKE_STAT', '1').prepend_to_build_command('faketime', faket)

def umask(ctx, build, vary):
    if not vary:
        return build.append_setup_exec('umask', '0022')
    else:
        return build.append_setup_exec('umask', '0002')


def current_user_group():
    return getpass.getuser(), grp.getgrgid(os.getgid()).gr_name


def make_sudo_command(user, group):
    assert user or group
    userarg = ['-u', user] if user else []
    grouparg = ['-g', group] if group else []
    return ['sudo', '-E'] + userarg + grouparg + ['env',
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
        logging.warn("IGNORING user_group variation; supply more usergroups "
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
    ('fileordering', fileordering),
    ('domain_host', domain_host), # needs to run after all other mounts have been set
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
                filemtimes = (os.lstat(os.path.join(root, f)).st_mtime for root, dirs, files in os.walk(source_root) for f in files)
                new_faketimes.append("@%d" % int(max(filemtimes, default=0)))
            else:
                raise ValueError("unrecognized auto_faketime: %s" % a)
        return self.empty()._replace(faketimes=self.faketimes + new_faketimes)


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


def print_sudoers(spec):
    logging.warn("This feature is EXPERIMENTAL, use at your own risk.")
    logging.warn("The output may be out-of-date, please file bugs if it doesn't work...")

    user, group = current_user_group()
    a = "[a-zA-Z0-9]"
    b = "/tmp/reprotest.{0}{0}{0}{0}{0}{0}".format(a)
    bx = os.path.join(b, "build-experiment-[1-9]")
    variables = {
        "user": user,
        "group": group,
        "base": b,
        "base_ex": bx,
    }

    if "user_group" in spec and spec.user_group.available:
        user_groups = [parse_user_group(user_group) for user_group in spec.user_group.available]
        users = sorted(set(user for user, group in user_groups if user))
        for otheruser in users:
            print("""\
# Rules for varying user_group with user %(otheruser)s
%(user)s ALL = (%(otheruser)s) NOPASSWD: ALL
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base)s/const_build_path/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base_ex)s/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(otheruser)s %(user)s %(base_ex)s-before-disorderfs/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base)s/const_build_path/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base_ex)s/
%(user)s ALL = NOPASSWD: /bin/chown -h -R --from=%(user)s %(otheruser)s %(base_ex)s-before-disorderfs/
""" % dict(**variables, **{
        "otheruser": otheruser
    }))

    if "domain_host" in spec and spec.domain_host.use_sudo:
        print("""\
# Rules for varying domain_host
%(user)s ALL = NOPASSWD: /bin/mount -B %(base_ex)s-aux/ns-mnt %(base_ex)s-aux/ns-mnt
%(user)s ALL = NOPASSWD: /bin/mount --make-private %(base_ex)s-aux/ns-mnt
%(user)s ALL = NOPASSWD: /usr/bin/unshare --mount=%(base_ex)s-aux/ns-mnt --uts=%(base_ex)s-aux/ns-uts true
%(user)s ALL = NOPASSWD: /usr/bin/nsenter --mount=%(base_ex)s-aux/ns-mnt --uts=%(base_ex)s-aux/ns-uts hostname reprotest-*
%(user)s ALL = NOPASSWD: /usr/bin/nsenter --mount=%(base_ex)s-aux/ns-mnt --uts=%(base_ex)s-aux/ns-uts domainname reprotest-*
%(user)s ALL = NOPASSWD: /usr/bin/nsenter --mount=%(base_ex)s-aux/ns-mnt --uts=%(base_ex)s-aux/ns-uts mount -B %(base_ex)s-aux/hosts /etc/hosts
%(user)s ALL = NOPASSWD:SETENV: /usr/bin/nsenter --mount=%(base_ex)s-aux/ns-mnt --uts=%(base_ex)s-aux/ns-uts sudo -E -u %(user)s -g %(group)s env *
%(user)s ALL = NOPASSWD: /bin/umount %(base_ex)s-aux/ns-mnt
%(user)s ALL = NOPASSWD: /bin/umount %(base_ex)s-aux/ns-uts
""" % variables)


if __name__ == "__main__":
    import sys
    d = VariationSpec()
    for s in sys.argv[1:]:
        d = d.extend([s])
        print(s)
        print(">>>", d)
    print("result", d.apply_dynamic_defaults("."))
