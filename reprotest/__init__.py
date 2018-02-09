# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import argparse
import collections
import configparser
import contextlib
import getpass
import logging
import os
import random
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
import types

import pkg_resources

from reprotest.lib import adtlog
from reprotest.lib import adt_testbed
from reprotest.build import Build, VariationSpec, Variations, tool_missing
from reprotest import environ, presets, shell_syn

logger = logging.getLogger(__name__)

VIRT_PREFIX = "autopkgtest-virt-"

def get_server_path(server_name):
    return pkg_resources.resource_filename(
        __name__, os.path.join("virt", (VIRT_PREFIX + server_name) if server_name else ""))

def is_executable(parent, fn):
    path = os.path.join(parent, fn)
    return os.path.isfile(path) and os.access(path, os.X_OK)

all_servers = None
def get_all_servers():
    global all_servers
    if all_servers is None:
        server_dir = get_server_path(None)
        all_servers = sorted(fn[len(VIRT_PREFIX):] for fn in os.listdir(server_dir)
                             if is_executable(server_dir, fn) and fn.startswith(VIRT_PREFIX))
    return all_servers


# chroot is the only form of OS virtualization that's available on
# most POSIX OSes.  Linux containers (lxc) and namespaces are specific
# to Linux.  Some versions of BSD have jails (MacOS X?).  There are a
# variety of other options including Docker etc that use different
# approaches.

class Testbed(adt_testbed.Testbed):

    def check_exec2(self, argv, stdout=False, kind='short', xenv=[]):
        """Like check_exec but does not bomb on stderr, and can pass xenv."""
        (code, out, err) = self.execute(argv,
                                        stdout=(stdout and subprocess.PIPE or None),
                                        xenv=xenv, kind=kind)
        if code != 0:
            self.bomb('"%s" failed with status %i' % (' '.join(argv), code),
                      adtlog.AutopkgtestError)
        return out

    def bomb(self, m, _type=adtlog.TestbedFailure):
        adtlog.debug('%s %s' % (_type.__name__, m))
        #self.stop() # don't stop when bombing, so we can control it via no_clean_on_error
        raise _type(m)

@contextlib.contextmanager
def start_testbed(args, temp_dir, no_clean_on_error=False, host_distro=None):
    '''This is a simple wrapper around adt_testbed that automates the
    initialization and cleanup.'''
    # Find the location of reprotest using setuptools and then get the
    # path for the correct virt-server script.
    server_path = get_server_path(args[0])
    logger.info('STARTING VIRTUAL SERVER %r', [server_path] + args[1:])
    if no_clean_on_error:
        os.environ["REPROTEST_NO_CLEAN_ON_ERROR"] = "1"
    # TODO: make the user configurable, like autopkgtest
    username = getpass.getuser()

    if username == "tomjon":
        # Hello BuildStream
        username = str(os.getuid())

    testbed = Testbed([server_path] + args[1:], temp_dir,
                      username, host_distro=host_distro)
    testbed.start()
    testbed.open()
    should_clean = True
    try:
        yield testbed
    except GeneratorExit:
        pass
    except BaseException as e:
        if no_clean_on_error:
            logger.warn("preserving temporary files in: %s", testbed.scratch)
            should_clean = False
        raise
    finally:
        if should_clean:
            testbed.stop()
        else:
            # give VirtSubproc a command it doesn't recognise, to force a non-zero Quit
            # a bit hacky, but it works...
            testbed.send('quit_with_error')


# put build artifacts in ${dist}/source-root, to support tools that put artifacts in ..
VSRC_DIR = "source-root"

def coroutine(func):
    """A decorator to automatically prime coroutines"""
    # https://gist.github.com/dyerw/3d53e7cd94f05cc92c1c
    def start(*args, **kwargs):
        cr = func(*args, **kwargs)
        next(cr)
        return cr
    return start


@contextlib.contextmanager
def empty_or_temp_dir(empty_dir, name):
    if empty_dir:
        empty_dir = str(empty_dir)
        if not os.path.exists(empty_dir):
            os.makedirs(empty_dir, exist_ok=False)
        elif os.listdir(empty_dir):
            raise ValueError("%s must be empty: %s" % (name, empty_dir))
        yield empty_dir
    else:
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir


def shell_copy_pattern(dst, src, globs):
    # assumes globs is already sanitized
    # mkdir -p dst if it doesn't already exist
    return ['sh', '-ec', """mkdir -p "{0}"
cd "{1}" && cp --parents -a -t "{0}" {2}
""".format(dst, src, globs)]


class BuildContext(collections.namedtuple('_BuildContext',
    'testbed_root local_dist_root local_src build_name variations')):
    """

    The idiom os.path.join(x, '') is used here to ensure a trailing directory
    separator, which is needed by some things, notably VirtSubProc.
    """

    @property
    def testbed_src(self):
        return os.path.join(self.testbed_root, 'build-' + self.build_name, '')

    @property
    def testbed_dist(self):
        return os.path.join(self.testbed_root, 'artifacts-' + self.build_name, '')

    @property
    def local_dist(self):
        return os.path.join(self.local_dist_root, self.build_name)

    def make_build_commands(self, script, env):
        # this dance is necessary because the cwd can't be cd'd into during the
        # setup phase under some variations like user_group
        _ = self.plan_variations(Build.from_command(
            build_command =
                'cd "$REPROTEST_BUILD_PATH"; unset REPROTEST_BUILD_PATH; ' +
                'umask "$REPROTEST_UMASK"; unset REPROTEST_UMASK; ' +
                script,
            env = types.MappingProxyType(env),
            tree = self.testbed_src
        ))
        _ = _.append_setup_exec_raw('export', 'REPROTEST_BUILD_PATH=%s' % _.tree)
        _ = _.append_setup_exec_raw('export', 'REPROTEST_UMASK=$(umask)')
        return _

    def plan_variations(self, build):
        actions = self.variations.spec.actions()
        logger.info('build "%s": %s',
            self.build_name,
            ", ".join("%s %s" % ("FIX" if not vary else "vary", v) for v, vary, action in actions))
        for v, vary, action in actions:
            build = action(self.variations, build, vary)
        return build

    def copydown(self, testbed):
        logger.info("copying %s over to virtual server's %s", self.local_src, self.testbed_src)
        testbed.command('copydown', (os.path.join(self.local_src, ''), self.testbed_src))

    def copyup(self, testbed):
        logger.info("copying %s back from virtual server's %s", self.testbed_dist, self.local_dist)
        testbed.command('copyup', (self.testbed_dist, os.path.join(self.local_dist, '')))

    def run_build(self, testbed, build, old_env, artifact_pattern, testbed_build_pre, no_clean_on_error):
        logger.info("starting build with source directory: %s, artifact pattern: %s",
            self.testbed_src, artifact_pattern)
        # we remove existing artifacts in case the build doesn't overwrite it
        # e.g. like how make(1) sometimes works
        testbed.check_exec2(
            ['sh', '-ec', 'cd "%s" && rm -rf %s && %s' %
            (self.testbed_src, artifact_pattern, testbed_build_pre or "true")])
        build_script = build.to_script(no_clean_on_error)
        logger.info("executing build in %s", build.tree)
        logger.debug("#### REPROTEST BUILD ENVVARS ###################################################\n" +
            "\n".join(environ.env_diff(old_env, build.env)))
        logger.debug("#### BEGIN REPROTEST BUILD SCRIPT ##############################################\n" +
            build_script)
        logger.debug("#### END REPROTEST BUILD SCRIPT ################################################")

        if 'root-on-testbed' in testbed.caps:
            testbed.check_exec2(['chown', testbed.user, self.testbed_root])
            fix_path = 'export PATH=%s; ' % shlex.quote(build.env['PATH']) if 'PATH' in build.env else ''
            build_argv = ['su', '-p', '-s', '/bin/sh', testbed.user,
                '-c', 'set -e; ' + fix_path + build_script]
            logger.info("su to user '%s' to run the build", testbed.user)
        else:
            build_argv = ['sh', '-ec', build_script]

        testbed.check_exec2(build_argv,
            xenv=['-i'] + ['%s=%s' % (k, v) for k, v in build.env.items()],
            kind='build')
        logger.info("build successful, copying artifacts")
        dist_base = os.path.join(self.testbed_dist, VSRC_DIR)
        testbed.check_exec2(shell_copy_pattern(dist_base, self.testbed_src, artifact_pattern))
        # FIXME: `touch` is needed because of the FIXME in build.faketime(). we can rm it after that is fixed
        testbed.check_exec2(['sh', '-ec',
            r"""cd "{0}" && touch -d@0 . .. {1}""".format(dist_base, artifact_pattern)])


def run_or_tee(progargs, filename, store_dir, *args, **kwargs):
    if store_dir:
        tee = subprocess.Popen(['tee', filename], stdin=subprocess.PIPE, cwd=store_dir)
        r = subprocess.run(progargs, *args, stdout=tee.stdin, **kwargs)
        tee.communicate()
        return r
    else:
        return subprocess.run(progargs, *args, **kwargs)

def run_diff(dist_0, dist_1, diffoscope_args, store_dir):
    name = os.path.basename(dist_1)
    if diffoscope_args is None: # don't run diffoscope
        diffprogram = ['diff', '-ru', dist_0, dist_1]
        logger.info("Running diff: %r", diffprogram)
        output = '%s.diff' % name
    else:
        diffprogram = ([a.format(name, dist_1) for a in diffoscope_args]
            + [dist_0, dist_1])
        logger.info("Running diffoscope: %r", diffprogram)
        output = '%s.diffoscope.out' % name

    retcode = run_or_tee(diffprogram, output, store_dir).returncode
    if retcode == 0:
        logger.info("No differences between %s, %s", dist_0, dist_1)
        if store_dir:
            shutil.rmtree(dist_1)
            os.symlink(os.path.basename(dist_0), dist_1)
    return retcode


class TestbedArgs(collections.namedtuple('_TestbedArgs',
    'virtual_server_args testbed_pre testbed_init testbed_build_pre host_distro')):
    @classmethod
    def of(cls, virtual_server_args=[], testbed_pre=None, testbed_init=None, testbed_build_pre=None, host_distro=None):
        return cls(virtual_server_args, testbed_pre, testbed_init, testbed_build_pre, host_distro)


class TestArgs(collections.namedtuple('_Test',
    'build_command source_root artifact_pattern result_dir source_pattern no_clean_on_error diffoscope_args')):
    @classmethod
    def of(cls, build_command, source_root, artifact_pattern, result_dir=None,
                source_pattern=None, no_clean_on_error=False, diffoscope_args=['diffoscope']):
        artifact_pattern = shell_syn.sanitize_globs(artifact_pattern)
        logger.debug("artifact_pattern sanitized to: %s", artifact_pattern)

        if source_pattern:
            source_pattern = shell_syn.sanitize_globs(source_pattern)
            logger.debug("source_pattern sanitized to: %s", source_pattern)
        return cls(build_command, source_root, artifact_pattern, result_dir,
                   source_pattern, no_clean_on_error, diffoscope_args)

    @coroutine
    def corun_builds(self, testbed_args):
        """A coroutine for running the builds.

        .>>> proc = self.corun_builds(testbed_args)
        .>>> for name, var in variations:
        .>>>     local_dist = proc.send((name, var))
        .>>>     ...
        """
        build_command, source_root, artifact_pattern, result_dir, source_pattern, no_clean_on_error, diffoscope_args = self
        virtual_server_args, testbed_pre, testbed_init, testbed_build_pre, host_distro = testbed_args

        if not source_root:
            raise ValueError("invalid source root: %s" % source_root)
        if os.path.isfile(source_root):
            source_root = os.path.normpath(os.path.dirname(source_root))
        source_root = str(source_root)

        logger.debug("virtual_server_args: %r", virtual_server_args)

        # TODO: if no_clean_on_error then this shouldn't be rm'd
        with tempfile.TemporaryDirectory() as temp_dir:
            if testbed_pre or source_pattern:
                new_source_root = os.path.join(temp_dir, "testbed_pre")
                subprocess.check_call(shell_copy_pattern(new_source_root, source_root, source_pattern or "."))
                source_root = new_source_root
            if testbed_pre:
                subprocess.check_call(["sh", "-ec", testbed_pre], cwd=new_source_root)
            logger.debug("source_root: %s", source_root)

            # TODO: an alternative strategy is to run the testbed many times, one for each build
            # not sure if it's worth implementing at this stage, but perhaps in the future.
            with start_testbed(virtual_server_args, temp_dir, no_clean_on_error,
                               host_distro=host_distro) as testbed:
                if testbed_init:
                    testbed.check_exec2(["sh", "-ec", testbed_init])

                name_variation = yield
                names_seen = set()
                while name_variation:
                    name, var = name_variation
                    if name in names_seen:
                        raise ValueError("already built '%s'" % name)
                    names_seen.add(name)

                    bctx = BuildContext(testbed.scratch, result_dir, source_root, name, var)

                    build = bctx.make_build_commands(build_command, os.environ)
                    bctx.copydown(testbed)
                    bctx.run_build(testbed, build, os.environ, artifact_pattern, testbed_build_pre, no_clean_on_error)
                    bctx.copyup(testbed)

                    name_variation = yield bctx.local_dist

    def check_reproducible(self, proc, dist_control, name, var):
        dist_test = proc.send(("experiment-%s" % name, var))
        # TODO: handle exit codes > 1 correctly, raise a CalledProcessError
        retcode = run_diff(dist_control, dist_test, self.diffoscope_args, self.result_dir)
        if retcode == 0:
            return True
        elif retcode == 1:
            return False
        else:
            raise RuntimeError("diffoscope exited non-boolean %s, can't continue" % retcode)

    def output_reproducible_hashes(self, dist_control):
        print("=======================")
        print("Reproduction successful")
        print("=======================")
        print("No differences in %s" % self.artifact_pattern, flush=True)
        run_or_tee(['sh', '-ec', 'find %s -type f -exec sha256sum "{}" \;' % self.artifact_pattern],
            'SHA256SUMS', self.result_dir,
            cwd=os.path.join(dist_control, VSRC_DIR))


def check(test_args, testbed_args, build_variations=Variations.of(VariationSpec.default())):
    # default argument [] is safe here because we never mutate it.
    _, _, artifact_pattern, store_dir, _, _, diffoscope_args = test_args
    with empty_or_temp_dir(store_dir, "store_dir") as result_dir:
        assert store_dir == result_dir or store_dir is None
        proc = test_args._replace(result_dir=result_dir).corun_builds(testbed_args)

        bnames = ["control"] + ["experiment-%s" % i for i in range(1, len(build_variations))]
        local_dists = [proc.send(nv) for nv in zip(bnames, build_variations)]

        retcodes = collections.OrderedDict(
            (bname, run_diff(local_dists[0], dist, diffoscope_args, store_dir))
            for bname, dist in zip(bnames, local_dists[1:]))

        retcode = max(retcodes.values())
        if retcode == 0:
            test_args.output_reproducible_hashes(local_dists[0])
            if any(bctx.spec.variations() != VariationSpec.all_names() for bctx in build_variations[1:]):
                print("However, other factors may still make the build unreproducible; try re-running with --vary=+all.")

        elif retcode == 1:
            if 0 in retcodes.values():
                print("Reproduction failed but partially successful: in %s" %
                    ", ".join(name for name, r in retcodes.items() if r == 0))

        else:
            raise RuntimeError("diffoscope exited non-boolean %s" % retcode)

        return not retcode


def check_auto(test_args, testbed_args, build_variations=Variations.of(VariationSpec.default())):
    # default argument [] is safe here because we never mutate it.
    _, _, _, store_dir, _, _, diffoscope_args = test_args
    with empty_or_temp_dir(store_dir, "store_dir") as result_dir:
        assert store_dir == result_dir or store_dir is None
        proc = test_args._replace(result_dir=result_dir).corun_builds(testbed_args)

        var_x0, var_x1 = build_variations
        dist_x0 = proc.send(("control", var_x0))
        is_reproducible = lambda name, var: test_args.check_reproducible(proc, dist_x0, name, var)

        if not is_reproducible("0", var_x0):
            print("Not reproducible, even when fixing as much as reprotest knows how to. :(")
            return False

        if is_reproducible("1", var_x1):
            print("Reproducible, even when varying as much as reprotest knows how to! :)")
            test_args.output_reproducible_hashes(dist_x0)
            return True

        var_cur = var_x0
        unreproducibles = []

        varnames = [v for v in VariationSpec.all_names() if v in var_x1.spec]
        random.shuffle(varnames)
        for v in varnames:
            var_test = var_cur.replace.spec._replace(**{v: var_x1.spec[v]})
            if is_reproducible(v, var_test):
                # vary it for the next test as well, it's OK to vary it
                var_cur = var_test
            else:
                # don't vary it for the next test, continue testing other variations
                unreproducibles.append(v)

        print("Observed unreproducibility when varying each of the following:")
        print(" ".join(unreproducibles))
        print("The build is probably reproducible when varying other things.")
        return False


def check_env(test_args, testbed_args, build_variations=Variations.of(VariationSpec.default())):
    # default argument [] is safe here because we never mutate it.
    _, _, artifact_pattern, store_dir, _, _, diffoscope_args = test_args
    with empty_or_temp_dir(store_dir, "store_dir") as result_dir:
        assert store_dir == result_dir or store_dir is None
        proc = test_args._replace(result_dir=result_dir).corun_builds(testbed_args)

        var_x0, var_x1 = build_variations
        dist_x0 = proc.send(("control", var_x0))
        is_reproducible = lambda name, var: test_args.check_reproducible(proc, dist_x0, name, var)

        orig_variations = var_x1.spec.variations()
        only_varying_env = (len(orig_variations) == 0 or
            len(orig_variations) == 1 and "environment" in orig_variations)

        blacklist, blacklist_names, non_whitelist, non_whitelist_names = environ.generate_dummy_environ()

        # Test blacklist
        var_x1 = var_x1.replace.spec.extend("environment")
        var_x1 = var_x1.replace.spec.environment.extend_variables(*blacklist)
        if not is_reproducible("blacklist", var_x1):
            print("Unreproducible even when varying blacklisted envvars: ", ", ".join(sorted(blacklist_names)))
            if not only_varying_env:
                print("This may or may not be caused by other factors; try re-running this again with --vary=-all")
            else:
                print("You are highly recommended to make your program reproducible when varying these.")
            return False

        # Test non-whitelist
        var_x2 = var_x1.replace.spec.environment.extend_variables(*non_whitelist)
        if not is_reproducible("non-whitelist", var_x2):
            print("Unreproducible when varying unknown envvars: ", ", ".join(sorted(non_whitelist_names)))
            print("Please file a bug to reprotest to add these to the whitelist or blacklist, to be decided.")
            print("If blacklist, then you should also make your program reproducible when varying them.")
            return False

        print("Reproducible, even when varying known blacklisted and unknown non-whitelisted envvars! :)")
        test_args.output_reproducible_hashes(dist_x0)
        if orig_variations != VariationSpec.all_names():
            print("However, other factors may still make the build unreproducible; try re-running with --vary=+all.")
        return True


def config_to_args(parser, filename):
    if not filename:
        return []
    elif os.path.isdir(filename):
        filename = os.path.join(filename, ".reprotestrc")
    config = configparser.ConfigParser(dict_type=collections.OrderedDict)
    config.read(filename)
    sections = {p.title: p for p in parser._action_groups[2:]}
    args = []
    for sectname, section in config.items():
        if sectname == 'basics':
            sectname = 'basic options'
        elif not sectname.endswith(' options'):
            sectname += ' options'
        items = list(section.items())
        if not items:
            continue
        sectacts = sections[sectname]._option_string_actions
        for key, val in items:
            key = "--" + key.replace("_", "-")
            val = val.strip()
            if key in sectacts.keys():
                if 'Append' in sectacts[key].__class__.__name__:
                    for v in val.split('\n'):
                        args.append('%s=%s' % (key, v))
                else:
                    args.append('%s=%s' % (key, val))
            else:
                raise ValueError("unexpected item in config: %s = %s" % (key, val))
    return args


def cli_parser():
    parser = argparse.ArgumentParser(
        prog='reprotest',
        usage='''%(prog)s --help [<virtual_server_name>]
       %(prog)s [options] [-c <build-command>] <source_root> [<artifact_pattern>]
                 [-- <virtual_server_args> [<virtual_server_args> ...]]
       %(prog)s [options] [-s <source_root>] <build_command> [<artifact_pattern>]
                 [-- <virtual_server_args> [<virtual_server_args> ...]]''',
        description='Build packages and check them for reproducibility.',
        formatter_class=argparse.RawDescriptionHelpFormatter, add_help=False)

    parser.add_argument('source_root|build_command', default=None, nargs='?',
        help='The first argument is treated either as a source_root (see the '
        '-s option) or as a build-command (see the -c option) depending on '
        'what it looks like. Most of the time, this should "just work"; but '
        'specifically: if neither -c nor -s are given, then: if this exists as '
        'a file or directory and is not "auto", then this is treated as a '
        'source_root, else as a build_command. Otherwise, if one of -c or -s '
        'is given, then this is treated as the other one. If both are given, '
        'then this is a syntax error and we exit code 2.'),
    parser.add_argument('artifact_pattern', default=None, nargs='?',
        help='Build artifact to test for reproducibility. May be a shell '
             'pattern such as "*.deb *.changes".'),
    parser.add_argument('virtual_server_args', default=None, nargs='*',
        help='Arguments to pass to the virtual_server, the first argument '
             'being the name of the server. If this itself contains options '
             '(of the form -xxx or --xxx), or if any of the previous arguments '
             'are omitted, you should put a "--" between these arguments and '
             'reprotest\'s own options. Default: "null", to run directly in '
             '/tmp. Choices: %s' %
             ', '.join(get_all_servers()))

    parser.add_argument('--help', default=None, const=True, nargs='?',
        choices=get_all_servers(), metavar='VIRTUAL_SERVER_NAME',
        help='Show this help message and exit. When given an argument, '
        'show instead the help message for that virtual server and exit. ')
    parser.add_argument('-f', '--config-file', default=None,
        help='File to load configuration from. (Default: %(default)s)')

    group1 = parser.add_argument_group('basic options')
    group1.add_argument('--verbosity', type=int, default=0,
        help='An integer.  Control which messages are displayed.')
    group1.add_argument('-v', '--verbose', dest='verbosity', action='count',
        help='Like --verbosity, but given multiple times without arguments.')
    group1.add_argument('--host-distro', default=None,
        help='The distribution that will run the tests (Default: %(default)s)')
    group1.add_argument('-s', '--source-root', default=None, metavar='PATH',
        help='Root of the source tree, that is copied to the virtual server '
        'and made available during the build. If a file is given here, then '
        'its parent directory is used instead. Default: "." (current working '
        'directory).')
    group1.add_argument('--source-pattern', default=None, metavar='PATTERNS',
        help='Shell glob pattern to restrict the files in <source_root> that '
        'are made available during the build. Default: empty, i.e. copy the '
        'whole <source_root> directory with no restrictions.')
    group1.add_argument('-c', '--build-command', default=None, metavar='COMMANDS',
        help='Build command to execute. If this is "auto" then reprotest will '
        'guess how to build the given source_root, in which case various other '
        'options may be automatically set-if-unset. Default: auto'),
    group1.add_argument('--store-dir', default=None, metavar='DIRECTORY',
        help='Save the artifacts in this directory, which must be empty or '
        'non-existent. Otherwise, the artifacts will be deleted and you only '
        'see their hashes (if reproducible) or the diff output (if not).')
    group1.add_argument('--variations', default="+all",
        help='Build variations to test as a comma-separated list of variation '
        'names. Default is "+all", equivalent to "%s", testing all available '
        'variations. See the man page section on VARIATIONS for more advanced '
        'syntax options, including tweaking how certain variations work.' %
        VariationSpec.default_long_string())
    group1.add_argument('--vary', metavar='VARIATIONS', default=[], action='append',
        help='Like --variations, but appends to previous --vary values '
        'instead of overwriting them. The last value set for --variations is '
        'treated implicitly as the zeroth --vary value.')
    group1_0 = group1.add_mutually_exclusive_group()
    group1_0.add_argument('--extra-build', metavar='VARIATIONS', default=[], action='append',
        help='Perform another build with the given VARIATIONS (which may be '
        'empty) to be applied on top of what was given for --variations and '
        '--vary. Each occurence of this flag specifies another build, so e.g. '
        'given twice this will make reprotest perform 4 builds in total.')
    group1_0.add_argument('--auto-build', default=False, action='store_true',
        help='Automatically perform builds to try to determine which specific '
        'variations cause unreproducibility, potentially up to and including '
        'the ones specified by --variations and --vary. Conflicts with '
        '--extra-build.')
    group1_0.add_argument('--env-build', default=False, action='store_true',
        help='Automatically perform builds to try to determine which specific '
        'environment variables cause unreproducibility, based on a hard-coded '
        'whitelist and blacklist. You probably want to set --vary=-all as well '
        'when setting this flag; see the man page for details. Conflicts with '
        '--extra-build and --auto-build.')
    group1.add_argument('--min-cpus', default=None, type=int, metavar='NUM',
        help='Minimum CPUs to use when fixing num_cpus. Default: 1.')
    # TODO: remove after reprotest 0.8
    group1.add_argument('--dont-vary', default=[], action='append', help=argparse.SUPPRESS)

    group2 = parser.add_argument_group('diff options')
    group2.add_argument('--diffoscope-arg', action='append', metavar='ARG',
        help='Give extra arguments to diffoscope when running it. Default: '
        '%(default)s. Arguments are {}-formatted with: {0} the name of each '
        'experiment run, and {1} the path of the experiment output.',
        default=['--exclude-directory-metadata'])
    group2.add_argument('--diffoscope', default='diffoscope', metavar='PATH',
        help='Path to diffoscope(1). Default: %(default)s')
    group2.add_argument('--no-diffoscope', action='store_true', default=False,
        help='Don\'t run diffoscope; instead run diff(1). Useful if you '
        'don\'t want to install diffoscope and/or just want a quick answer '
        'on whether the reproduction was successful or not, without spending '
        'time to compute all the detailed differences.')

    group3 = parser.add_argument_group('advanced options')
    group3.add_argument('--testbed-pre', default=None, metavar='COMMANDS',
        help='Shell commands to run before starting the test bed, in the '
        'context of the current system environment. This may be used to e.g. '
        'compute information needed by the build, where the computation needs '
        'packages you don\'t want installed in the testbed itself.')
    group3.add_argument('--testbed-init', default=None, metavar='COMMANDS',
        help='Shell commands to run after starting the test bed, before '
        'running anything else. Used to e.g. install disorderfs in a chroot.')
    group3.add_argument('--testbed-build-pre', default=None, metavar='COMMANDS',
        help='Shell commands to run before each build, even before applying '
        'variations for that build. Used to e.g. install build-dependencies.')
    group3.add_argument('--auto-preset-expr', default="_", metavar='PYTHON_EXPRESSION',
        help='This may be used to transform the presets returned by the '
        'auto-detection feature. The value should be a python expression '
        'that transforms the _ variable, which is of type reprotest.presets.ReprotestPreset. '
        'See that class\'s documentation for ways you can write this '
        'expression. Default: %(default)s')
    group3.add_argument('--no-clean-on-error', action='store_true', default=False,
        help='Don\'t clean the virtual_server if there was an error. '
        'Useful for debugging but will leave cruft on your system depending on '
        'the virtual_server used; we hint about some but there may be others.')
    group3.add_argument('--dry-run', action='store_true', default=False,
        help='Don\'t run the builds, just print what would happen.')
    group3.add_argument('--print-sudoers', action='store_true', default=False,
        help='Print a sudoers file for passwordless operation using the given '
        '--variations, useful for user_group.available, domain_host.use_sudo.')

    return parser


def command_line(parser, argv):
    # parse_known_args does not exactly do what we want - we want everything
    # after '--' to belong to virtual_server_args, but parse_known_args instead
    # treats them as any positional argument (e.g. ones that go before
    # virtual_server_args). so, work around that here.
    if '--' in argv:
        idx = argv.index('--')
        postargv = argv[idx:]
        argv = argv[:idx]
    else:
        postargv = []

    # work around python issue 14191; this allows us to accept command lines like
    # $ reprotest build stuff --option=val --option=val -- schroot unstable-amd64-sbuild
    # where optional args appear in between positional args, but there must be a '--'
    args, remainder = parser.parse_known_args(argv)
    remainder += postargv

    if remainder:
        if remainder[0] != '--':
            # however we disallow split command lines that don't have '--', e.g.:
            # $ reprotest build stuff --option=val --option=val schroot unstable-amd64-sbuild
            # since it's too complex to support that in a way that's not counter-intuitive
            parser.parse_args(argv)
            assert False # previous function should have raised an error
        args.virtual_server_args = (args.virtual_server_args or []) + remainder[1:]
    args.virtual_server_args = args.virtual_server_args or ["null"]

    if args.help:
        if args.help is True:
            parser.print_help()
            sys.exit(0)
        else:
            sys.exit(subprocess.call([get_server_path(args.help), "-h"]))

    return args


def get_main_spec(parsed_args):
    variations = [parsed_args.variations] + parsed_args.vary
    if parsed_args.dont_vary:
        logger.warn("--dont-vary is deprecated; use --vary=-$variation instead")
        variations += ["-%s" % a for x in parsed_args.dont_vary for a in x.split(",")]
    return VariationSpec().extend(variations)


def run(argv, dry_run=None):
    # Argparse exits with status code 2 if something goes wrong, which
    # is already the right status exit code for reprotest.
    parser = cli_parser()
    parsed_args = command_line(parser, argv)
    config_args = config_to_args(parser, parsed_args.config_file)
    # Command-line arguments override config file settings.
    parsed_args = command_line(parser, config_args + argv)
    dry_run = parsed_args.dry_run or dry_run

    verbosity = parsed_args.verbosity
    adtlog.verbosity = verbosity - 1
    logging.basicConfig(level=30-10*verbosity)
    logger.debug('%r', parsed_args)

    if not dry_run and parsed_args.print_sudoers:
        build.print_sudoers(get_main_spec(parsed_args))
        return 0

    # Decide which form of the CLI we're using
    build_command, source_root = None, None
    first_arg = parsed_args.__dict__['source_root|build_command']
    if parsed_args.build_command:
        if parsed_args.source_root:
            print("Both -c and -s were given; abort")
            sys.exit(2)
        else:
            source_root = first_arg
    else:
        if parsed_args.source_root:
            build_command = first_arg
        elif not first_arg:
            print("No <source_root> or <build_command> provided. See --help for options.")
            sys.exit(2)
        elif first_arg == "auto":
            build_command = first_arg
            if parsed_args.artifact_pattern:
                logger.warn("old CLI form `reprotest auto <source_root>` detected, "
                    "setting source_root to the second argument: %s", parsed_args.artifact_pattern)
                logger.warn("to avoid this warning, use instead `reprotest <source_root>` "
                    "or (if really necessary) `reprotest -s <source_root> auto <artifact>`")
                source_root = parsed_args.artifact_pattern
                parsed_args.artifact_pattern = None
        elif os.path.exists(first_arg):
            source_root = first_arg
        else:
            parts = shlex.split(first_arg)
            if len(parts) == 1:
                if shutil.which(parts[0]) is None:
                    logger.warn("XXX")
                    raise RuntimeError("Not found, neither as a file nor as a command: %s" % first_arg)
            # if len(parts) > 1 then it could be something like '( command )'
            # which is valid despite '(' not existing.
            build_command = first_arg
    build_command = build_command or parsed_args.build_command or "auto"
    source_root = source_root or parsed_args.source_root or '.'

    # Args that might be affected by presets
    virtual_server_args = parsed_args.virtual_server_args
    artifact_pattern = parsed_args.artifact_pattern
    testbed_pre = parsed_args.testbed_pre
    testbed_init = parsed_args.testbed_init
    testbed_build_pre = parsed_args.testbed_build_pre
    diffoscope_args = parsed_args.diffoscope_arg
    source_pattern = parsed_args.source_pattern
    if verbosity >= 3:
        diffoscope_args += ["--debug"]
    elif not verbosity:
        diffoscope_args += ["--no-progress"]

    # Do presets
    if build_command == 'auto':
        auto_preset_expr = parsed_args.auto_preset_expr
        values = presets.get_presets(source_root, virtual_server_args[0])
        values = eval(auto_preset_expr, {'_': values}, {})
        logger.info("preset auto-selected: %r", values)
        build_command = values.build_command
        artifact_pattern = artifact_pattern or values.artifact_pattern
        testbed_pre = testbed_pre or values.testbed_pre
        testbed_init = testbed_init or values.testbed_init
        testbed_build_pre = testbed_build_pre or values.testbed_build_pre
        if values.diffoscope_args is not None:
            diffoscope_args = values.diffoscope_args + diffoscope_args
        if values.source_pattern is not None:
            source_pattern = values.source_pattern + (" " + source_pattern if source_pattern else "")

    # Variations args
    specs = [get_main_spec(parsed_args)]
    if parsed_args.auto_build:
        check_func = check_auto
    elif parsed_args.env_build:
        check_func = check_env
    else:
        for extra_build in parsed_args.extra_build:
            specs.append(spec.extend(extra_build))
        check_func = check
    if parsed_args.min_cpus is None and not dry_run:
        logger.warn("The control build runs on 1 CPU by default, give --min-cpus to increase this.")
    min_cpus = parsed_args.min_cpus or 1
    build_variations = Variations.of(
        *specs,
        verbosity=verbosity,
        min_cpus=min_cpus,
        # TODO: make this configurable via command line
        base_faketime='@%d' % build.auto_source_date_epoch(source_root))

    # Warn about missing programs
    if virtual_server_args[0] == "null" and not dry_run:
        missing = [(var, tool_missing(action))
            for spec in specs
            for var, vary, action in spec.actions()
            if vary]
        missing = [(var, tools) for var, tools in missing if tools]
        for var, tools in missing:
            if tools:
                logger.warn("Varying '%s' requires these program(s): %s", var, ", ".join(tools))
        if missing:
            logger.warn("Your build will probably fail, either install them or disable the variations.")
            logger.warn("(From a system package manager, simply install the 'optional' or 'recommended' "
                         "dependencies of reprotest.)")

    # Remaining args
    host_distro = parsed_args.host_distro
    store_dir = parsed_args.store_dir
    no_clean_on_error = parsed_args.no_clean_on_error
    diffoscope = parsed_args.diffoscope
    if parsed_args.no_diffoscope:
        diffoscope_args = None
    else:
        diffoscope_args = [diffoscope] + diffoscope_args

    if not artifact_pattern:
        print("No <artifact> to test for differences provided. See --help for options.")
        sys.exit(2)

    testbed_args = TestbedArgs.of(virtual_server_args, testbed_pre, testbed_init, testbed_build_pre, host_distro)
    test_args = TestArgs.of(build_command, source_root, artifact_pattern, store_dir,
                            source_pattern, no_clean_on_error, diffoscope_args)

    check_args = (test_args, testbed_args, build_variations)
    if dry_run:
        return check_args
    else:
        try:
            return 0 if check_func(*check_args) else 1
        except Exception:
            traceback.print_exc()
            return 125


def main():
    r = run(sys.argv[1:])
    if not isinstance(r, int):
        import pprint
        pprint.pprint(r, width=80, compact=True)
    else:
        return r
