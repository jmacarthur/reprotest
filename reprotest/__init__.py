# Licensed under the GPL: https://www.gnu.org/licenses/gpl-3.0.en.html
# For details: reprotest/debian/copyright

import argparse
import collections
import configparser
import contextlib
import logging
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import types

import pkg_resources

from reprotest.lib import adtlog
from reprotest.lib import adt_testbed
from reprotest.build import Build, VariationSpec, Variations
from reprotest import presets


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

@contextlib.contextmanager
def start_testbed(args, temp_dir, no_clean_on_error=False, host_distro='debian'):
    '''This is a simple wrapper around adt_testbed that automates the
    initialization and cleanup.'''
    # Find the location of reprotest using setuptools and then get the
    # path for the correct virt-server script.
    server_path = get_server_path(args[0])
    logging.info('STARTING VIRTUAL SERVER %r', [server_path] + args[1:])
    testbed = adt_testbed.Testbed([server_path] + args[1:], temp_dir, None,
            host_distro=host_distro)
    testbed.start()
    testbed.open()
    should_clean = True
    try:
        yield testbed
    except:
        if no_clean_on_error:
            should_clean = False
        raise
    finally:
        if should_clean:
            # TODO: we could probably do *some* level of cleanup even if
            # should_clean is False; investigate this further...
            testbed.stop()


# put build artifacts in ${dist}/source-root, to support tools that put artifacts in ..
VSRC_DIR = "source-root"

class BuildContext(collections.namedtuple('_BuildContext', 'testbed_root local_dist_root local_src build_name')):
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
        return Build.from_command(
            build_command = script,
            env = types.MappingProxyType(env),
            tree = self.testbed_src
        )

    def plan_variations(self, build, is_control, variations): # XXX
        if is_control:
            variations = variations._replace(spec=VariationSpec.empty())
        actions = variations.spec.actions()
        logging.info('build "%s": %s',
            self.build_name,
            ", ".join("%s %s" % ("FIX" if not vary else "vary", v) for v, vary, action in actions))
        for v, vary, action in actions:
            build = action(variations, build, vary)
        return build

    def copydown(self, testbed):
        logging.info("copying %s over to virtual server's %s", self.local_src, self.testbed_src)
        testbed.command('copydown', (os.path.join(self.local_src, ''), self.testbed_src))

    def copyup(self, testbed):
        logging.info("copying %s back from virtual server's %s", self.testbed_dist, self.local_dist)
        testbed.command('copyup', (self.testbed_dist, os.path.join(self.local_dist, '')))

    def run_build(self, testbed, build, artifact_pattern):
        logging.info("starting build with source directory: %s, artifact pattern: %s",
            self.testbed_src, artifact_pattern)
        # remove any existing artifact, in case the build script doesn't overwrite
        # it e.g. like how make(1) sometimes works.
        if re.search(r"""(^| )['"]*/""", artifact_pattern):
            raise ValueError("artifact_pattern is possibly dangerous; maybe use a relative path instead?")
        testbed.check_exec(
            ['sh', '-ec', 'cd "%s" && rm -rf %s' %
            (self.testbed_src, artifact_pattern)])
        # this dance is necessary because the cwd can't be cd'd into during the setup phase under some variations like user_group
        new_script = build.append_setup_exec_raw('export', 'REPROTEST_BUILD_PATH=%s' % build.tree).to_script()
        logging.info("executing: %s", new_script)
        argv = ['sh', '-ec', new_script]
        xenv = ['%s=%s' % (k, v) for k, v in build.env.items()]
        (code, _, _) = testbed.execute(argv, xenv=xenv, kind='build')
        if code != 0:
            testbed.bomb('"%s" failed with status %i' % (' '.join(argv), code), adtlog.AutopkgtestError)
        dist_base = os.path.join(self.testbed_dist, VSRC_DIR)
        testbed.check_exec(
            ['sh', '-ec', """mkdir -p "{0}"
cd "{1}" && cp --parents -a -t "{0}" {2}
cd "{0}" && touch -d@0 . .. {2}
""".format(dist_base, self.testbed_src, artifact_pattern)])


def run_or_tee(progargs, filename, store_dir, *args, **kwargs):
    if store_dir:
        tee = subprocess.Popen(['tee', filename], stdin=subprocess.PIPE, cwd=store_dir)
        r = subprocess.run(progargs, *args, stdout=tee.stdin, **kwargs)
        tee.communicate()
        return r
    else:
        return subprocess.run(progargs, *args, **kwargs)

def run_diff(dist_0, dist_1, diffoscope_args, store_dir):
    if diffoscope_args is None: # don't run diffoscope
        diffprogram = ['diff', '-ru', dist_0, dist_1]
        logging.info("Running diff: %r", diffprogram)
    else:
        diffprogram = ['diffoscope', dist_0, dist_1] + diffoscope_args
        logging.info("Running diffoscope: %r", diffprogram)

    retcode = run_or_tee(diffprogram, 'diffoscope.out', store_dir).returncode
    if retcode == 0:
        logging.info("No differences between %s, %s", dist_0, dist_1)
        if store_dir:
            shutil.rmtree(dist_1)
            os.symlink(os.path.basename(dist_0), dist_1)
    return retcode

def check(build_command, artifact_pattern, virtual_server_args, source_root,
          no_clean_on_error=False, store_dir=None, diffoscope_args=[],
          variations=Variations.default(),
          testbed_pre=None, testbed_init=None, host_distro='debian'):
    # default argument [] is safe here because we never mutate it.
    if not source_root:
        raise ValueError("invalid source root: %s" % source_root)
    if os.path.isfile(source_root):
        source_root = os.path.normpath(os.path.dirname(source_root))

    if store_dir:
        store_dir = str(store_dir)
        if not os.path.exists(store_dir):
            os.makedirs(store_dir, exist_ok=False)
        elif os.listdir(store_dir):
            raise ValueError("store_dir must be empty: %s" % store_dir)

    logging.debug("virtual_server_args: %r", virtual_server_args)

    source_root = str(source_root)
    with tempfile.TemporaryDirectory() as temp_dir:
        if testbed_pre:
            new_source_root = os.path.join(temp_dir, "testbed_pre")
            shutil.copytree(source_root, new_source_root, symlinks=True)
            subprocess.check_call(["sh", "-ec", testbed_pre], cwd=new_source_root)
            source_root = new_source_root
        logging.debug("source_root: %s", source_root)

        variations = variations._replace(spec=variations.spec.apply_dynamic_defaults(source_root))

        # TODO: an alternative strategy is to run the testbed many times, one for each build
        # not sure if it's worth implementing at this stage, but perhaps in the future.
        with start_testbed(virtual_server_args, temp_dir, no_clean_on_error,
                host_distro=host_distro) as testbed:

            if store_dir:
                result_dir = store_dir
            else:
                result_dir = os.path.join(temp_dir, 'artifacts')
                os.makedirs(result_dir)

            build_contexts = [BuildContext(testbed.scratch, result_dir, source_root, name)
                for name in ('control', 'experiment')]
            builds = [bctx.make_build_commands(
                    'cd "$REPROTEST_BUILD_PATH"; unset REPROTEST_BUILD_PATH; ' + build_command, os.environ)
                for bctx in build_contexts]

            logging.log(5, "builds: %r", builds)
            builds = [c.plan_variations(b, c.build_name == "control", variations)
                for c, b in zip(build_contexts, builds)]
            logging.log(5, "builds: %r", builds)

            try:
                # run the scripts
                if testbed_init:
                    testbed.check_exec(["sh", "-ec", testbed_init])

                for bctx in build_contexts:
                    bctx.copydown(testbed)

                for bctx, build in zip(build_contexts, builds):
                    bctx.run_build(testbed, build, artifact_pattern)

                for bctx in build_contexts:
                    bctx.copyup(testbed)
            except Exception:
                traceback.print_exc()
                return 2

        retcodes = [
            run_diff(build_contexts[0].local_dist, bctx.local_dist, diffoscope_args, store_dir)
            for bctx in build_contexts[1:]]

        retcode = max(retcodes)
        if retcode == 0:
            print("=======================")
            print("Reproduction successful")
            print("=======================")
            print("No differences in %s" % artifact_pattern, flush=True)
            run_or_tee(['sh', '-ec', 'find %s -type f -exec sha256sum "{}" \;' % artifact_pattern],
                'SHA256SUMS', store_dir,
                cwd=os.path.join(build_contexts[0].local_dist, VSRC_DIR))
        else:
            # a slight hack, to trigger no_clean_on_error
            raise SystemExit(retcode)
        return retcode


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
        'some heuristics. Specifically: if neither -c nor -s are given, then: '
        'if this exists as a file or directory and is not "auto", then this is '
        'treated as a source_root, else as a build_command. Otherwise, if one '
        'of -c or -s is given, then this is treated as the other one. If both '
        'are given, then this is a command-line syntax error and we exit code 2.'),
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
    group1.add_argument('--host-distro', default='debian',
        help='The distribution that will run the tests (Default: %(default)s)')
    group1.add_argument('-s', '--source-root', default=None,
        help='Root of the source tree, that is copied to the virtual server '
        'and made available during the build. If a file is given here, then '
        'all files in its parent directory are available during the build. '
        'Default: "." (current working directory).')
    group1.add_argument('-c', '--build-command', default=None,
        help='Build command to execute. If this is "auto" then reprotest will '
        'guess how to build the given source_root, in which case various other '
        'options may be automatically set-if-unset. Default: auto'),
    group1.add_argument('--store-dir', default=None, type=pathlib.Path,
        help='Save the artifacts in this directory, which must be empty or '
        'non-existent. Otherwise, the artifacts will be deleted and you only '
        'see their hashes (if reproducible) or the diff output (if not).')
    group1.add_argument('--variations', default=["+all"], action='append',
        help='Build variations to test as a whitespace-or-comma-separated '
        'list.  Default is to test all available variations: %(default)s.')
    # TODO: remove after reprotest 0.8
    group1.add_argument('--dont-vary', default=[], action='append', help=argparse.SUPPRESS)

    group2 = parser.add_argument_group('diff options')
    group2.add_argument('--diffoscope-arg', default=[], action='append',
        help='Give extra arguments to diffoscope when running it.')
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
        help='Shell commands to run after starting the test bed, but before '
        'applying variations. Used to e.g. install disorderfs in a chroot.')
    group3.add_argument('--auto-preset-expr', default="_", metavar='PYTHON_EXPRESSION',
        help='This may be used to transform the presets returned by the '
        'auto-detection feature. The value should be a python expression '
        'that transforms the _ variable, which is of type reprotest.presets.ReprotestPreset. '
        'See that class\'s documentation for ways you can write this '
        'expression. Default: %(default)s')
    group3.add_argument('--no-clean-on-error', action='store_true', default=False,
        help='Don\'t clean the virtual_server if there was an error. '
        'Useful for debugging, but WARNING: this is currently not '
        'implemented very well and may leave cruft on your system.')
    group3.add_argument('--dry-run', action='store_true', default=False,
        help='Don\'t run the builds, just print what would happen.')

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
        if args.help:
            parser.print_help()
            sys.exit(0)
        else:
            sys.exit(subprocess.call([get_server_path(args.help), "-h"]))

    return args


def run(argv, check):
    # Argparse exits with status code 2 if something goes wrong, which
    # is already the right status exit code for reprotest.
    parser = cli_parser()
    parsed_args = command_line(parser, argv)
    config_args = config_to_args(parser, parsed_args.config_file)
    # Command-line arguments override config file settings.
    parsed_args = command_line(parser, config_args + argv)

    verbosity = parsed_args.verbosity
    adtlog.verbosity = verbosity
    logging.basicConfig(
        format='%(message)s', level=30-10*verbosity, stream=sys.stdout)
    logging.debug('%r', parsed_args)

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
                logging.warn("old CLI form `reprotest auto <source_root>` detected, "
                    "setting source_root to the second argument: %s", parsed_args.artifact_pattern)
                logging.warn("to avoid this warning, use instead `reprotest <source_root>` "
                    "or (if really necessary) `reprotest -s <source_root> auto <artifact>`")
                source_root = parsed_args.artifact_pattern
                parsed_args.artifact_pattern = None
        elif os.path.exists(first_arg):
            source_root = first_arg
        else:
            build_command = first_arg
    build_command = build_command or parsed_args.build_command or "auto"
    source_root = source_root or parsed_args.source_root or '.'

    # Args that might be affected by presets
    virtual_server_args = parsed_args.virtual_server_args
    artifact_pattern = parsed_args.artifact_pattern
    testbed_pre = parsed_args.testbed_pre
    testbed_init = parsed_args.testbed_init
    diffoscope_args = parsed_args.diffoscope_arg

    # Do presets
    if build_command == 'auto':
        auto_preset_expr = parsed_args.auto_preset_expr
        values = presets.get_presets(source_root, virtual_server_args[0])
        values = eval(auto_preset_expr, {'_': values}, {})
        logging.info("preset auto-selected: %r", values)
        build_command = values.build_command
        artifact_pattern = artifact_pattern or values.artifact_pattern
        testbed_pre = testbed_pre or values.testbed_pre
        testbed_init = testbed_init or values.testbed_init
        if diffoscope_args is not None:
            diffoscope_args = values.diffoscope_args + diffoscope_args

    # Variations args
    variations = parsed_args.variations
    if parsed_args.dont_vary:
        logging.warn("--dont-vary is deprecated; use --variations=-$variation instead")
        variations += ["-%s" % a for x in parsed_args.dont_vary for a in x.split(",")]
    spec = VariationSpec().extend(variations)
    variations = Variations(verbosity, spec)

    # Remaining args
    host_distro = parsed_args.host_distro
    store_dir = parsed_args.store_dir
    no_clean_on_error = parsed_args.no_clean_on_error
    if parsed_args.no_diffoscope:
        diffoscope_args = None

    if not artifact_pattern:
        print("No <artifact> to test for differences provided. See --help for options.")
        sys.exit(2)

    check_args_keys = (
        "build_command", "artifact_pattern", "virtual_server_args", "source_root",
        "no_clean_on_error", "store_dir", "diffoscope_args",
        "variations",
        "testbed_pre", "testbed_init", "host_distro")
    l = locals()
    check_args = collections.OrderedDict([(k, l[k]) for k in check_args_keys])
    if parsed_args.dry_run:
        return check_args
    else:
        return check(**check_args)

def main():
    r = run(sys.argv[1:], check)
    if isinstance(r, collections.OrderedDict):
        print("check(%s)" % ", ".join("%s=%r" % (k, v) for k, v in r.items()))
    else:
        return r
