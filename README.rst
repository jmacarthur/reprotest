reprotest
=========

reprotest builds the same source code twice in different environments, and then
checks the binaries produced by each build for differences. If any are found,
then ``diffoscope(1)`` (or if unavailable then ``diff(1)``) is used to display
them in detail for later analysis.

See the ``COMMAND-LINE EXAMPLES`` section further below to get you
started, as well as more detailed explanations of all the command-line
options. The same information is also available in
``/usr/share/doc/reprotest/README.rst`` or similar.

.. raw:: manpage

   .\" the below hack gets rid of the python "usage" message in favour of the
   .\" the synopsis we manually defined in doc/$(PACKAGE).h2m.0
   .SS positional arguments:
   .\" end_of_description_header

Command-line examples
=====================

The easiest way to run reprotest is via our presets::

    # Build the current directory in a null server (/tmp)
    $ reprotest .
    $ reprotest . -vv -- null -d # for very verbose output

    # Build the given Debian source package in an schroot
    # See https://wiki.debian.org/sbuild for instructions on setting that up.
    $ reprotest reprotest_0.3.3.dsc -- schroot unstable-amd64-sbuild

Currently, we only support this for Debian packages, but are keen on
adding more. If we don't have knowledge on how to build your file or
directory, you can send a patch to us on adding this intelligence - see
the reprotest.presets python module, and adapt the existing logic.

In the meantime, you can use other parts of the CLI to build arbitrary things.
You'll need to give two mandatory arguments, the build command to run and the
build artifact file/pattern to test after running the build. For example::

    $ reprotest 'python3 setup.py bdist' 'dist/*.tar.gz'

This runs the command on ``.``, the current working directory. To run it on a
project located elsewhere::

    $ reprotest -s ../path/to/other/project 'python3 setup.py bdist' 'dist/*.tar.gz'
    $ reprotest -c 'python3 setup.py bdist' ../path/to/other/project 'dist/*.tar.gz'

These two invocations are equivalent; you can pick the most convenient one
for your use-case. When using these from a shell:

  * If the build command has spaces, you will need to quote them, e.g.
    ``reprotest "dpkg-buildpackage -b --no-sign" [..]``.

  * If you want to use several build artifact patterns, or if you want to
    use shell wildcards as a pattern, you will also need to quote them, e.g.
    ``reprotest [..] "*.tar.gz *.tar.xz"``.

  * If your build artifacts have spaces in their names, you will need to
    quote these twice, e.g. ``'"a file with spaces.gz"'`` for a single
    artifact or ``'"dir 1"/* "dir 2"/*'`` for multiple patterns.

To get more help for the CLI, including documentation on optional
arguments and what they do, run::

    $ reprotest --help


Running in a virtual server
===========================

You can also run the build inside what is called a "virtual server".
This could be a container, a chroot, etc. You run them like this::

    $ reprotest 'python3 setup.py bdist_wheel'   'dist/*.whl' -- qemu    /path/to/qemu.img
    $ reprotest 'dpkg-buildpackage -b --no-sign' '../*.deb'   -- schroot unstable-amd64

There are different server types available. See ``--help`` for a list of
them, which appears near the top, in the "virtual\_server\_args" part of
the "positional arguments" section.

For each virtual server (e.g. "schroot"), you see which extra arguments
it supports::

    $ reprotest --help schroot

When running builds inside a virtual server, you will probably have to
give extra commands, in order to set up your build dependencies inside
the virtual server. For example, to take you through what the "Debian
directory" preset would look like, if we ran it using the full CLI::

    # "Debian directory" preset
    $ reprotest . -- schroot unstable-amd64-sbuild
    # This is exactly equivalent to this:
    $ reprotest -c auto . -- schroot unstable-amd64-sbuild
    # In the non-preset full CLI, this is roughly similar to:
    $ reprotest \
        --testbed-init 'apt-get -y --no-install-recommends install \
                        disorderfs faketime locales-all sudo util-linux; \
                        test -c /dev/fuse || mknod -m 666 /dev/fuse c 10 229; \
                        test -f /etc/mtab || ln -s ../proc/self/mounts /etc/mtab' \
        --testbed-build-pre 'apt-get -y --no-install-recommends build-dep ./' \
        --build-command 'dpkg-buildpackage --no-sign -b' \
        . \
        '../*.deb' \
        -- \
        schroot unstable-amd64-sbuild

The ``--testbed-init`` argument is needed to set up basic tools, which
reprotest needs in order to make the variations in the first place. This
should be the same regardless of what package is being built, but might
differ depending on what virtual\_server is being used.

Next, we have ``--testbed-build-pre``, then ``--build-command`` (or ``-c``).
For our Debian directory, we install build-dependencies using ``apt-get``,
then we run the actual build command itself using ``dpkg-buildpackage(1)``.

Then, we have the ``source_root`` and the ``artifact_pattern``. For
reproducibility, we're only interested in the binary packages.

Finally, we specify that this is to take place in the "schroot"
virtual\_server with arguments "unstable-amd64-sbuild".

Of course, all of this is a burden to remember, if you must run the same
thing many times. So that is why adding new presets for new package types
would be good.

Here is a more complex example. It tells reprotest to store the build products
into ``./artifacts`` to analyse later; and also tweaks the "Debian dsc" preset
so that it uses our `experimental toolchain
<https://wiki.debian.org/ReproducibleBuilds/ExperimentalToolchain>`__::

    $ reprotest --store-dir=artifacts \
        --auto-preset-expr '_.prepend.testbed_init("apt-get install -y wget; \
            echo deb http://reproducible.alioth.debian.org/debian/ ./ >> /etc/apt/sources.list; \
            wget -q -O- https://reproducible.alioth.debian.org/reproducible.asc | apt-key add -; \
            apt-get update; apt-get upgrade -y; ")' \
        ./bash_4.4-4.0~reproducible1.dsc \
        -- \
        schroot unstable-amd64-sbuild

Alternatively, you can clone your unstable-amd64-sbuild chroot, add our repo to
the cloned chroot, then use this chroot in place of "unstable-amd64-sbuild".
That would allow you to omit the long ``--auto-preset-expr`` flag above.


Config File
===========

You can also give options to reprotest via a config file. This is a
time-saving measure similar to ``auto`` presets; the difference is that
these are more suited for local builds that are suited to your personal
purposes. (You may use both presets and config files in the same build.)

The config file takes exactly the same options as the command-line interface,
but with the additional restriction that the section name must match the ones
given in the --help output. Whitespace is allowed if and only if the same
command-line option allows whitespace. Finally, it is not possible to give
positional arguments via this mechanism.

Reprotest by default does not load any config file. You can tell it to load one
with the ``--config-file`` or ``-f`` command line options. If you give it a
directory such as ``.``, it will load ``.reprotestrc`` within that directory.

A sample config file is below::

    [basics]
    verbosity = 1
    variations =
      environment
      build_path
      user_group.available+=builduser:builduser
      fileordering
      home
      kernel
      locales
      exec_path
      time
      timezone
      umask
    store_dir =
      /home/foo/build/reprotest-artifacts

    [diff]
    diffoscope_arg =
      --debug


Analysing diff output
=====================

Normally when diffoscope compares directories, it also compares the metadata of
files in those directories - file permissions, owners, and so on.

However depending on the circumstance, this filesystem-level metadata may or
may not be intended to be distributed to other systems. For example: (1) for
most distros' package builders, we don't care about the metadata of the output
package files; only the file contents will be distributed to other systems. On
the other hand, (2) when running something like `make install`, we *do* care
about the metadata, because this is what will be recreated on another system.

In developing reprotest, our experience has been that case (1) is more common
and so we pass ``--exclude-directory-metadata`` by default to diffoscope. If
you find that you are using reprotest for case (2) then you should pass
``--diffoscope-args=--no-exclude-directory-metadata`` to reprotest, to tell
diffoscope to not ignore the metadata since it will be distributed and should
therefore be reproducible. Otherwise, you may get a false-positive result.


Variations
==========

The --vary and --variations flags in their simple forms, are a comma-separated
list of variation names that indicate which variations to apply. The full list
of names is given in the --help text for --variations.

| \
| In full detail, the flags are a comma-separated list of actions, as follows:
|
| +$variation (or $variation with no explicit operator)
| -$variation
|    Enable or disable a variation
|
| @$variation
|    Enable a variation, resetting its parameters (see below) to default values.
|
| $variation.$param=$value
| $variation.$param+=$value
| $variation.$param-=$value
|    Set/add/remove $value as/to/from the current value of the $param parameter
     of the $variation.
|
| $variation.$param++
| $variation.$param--
|    Increment/decrement the value of the $param parameter of the $variation.

Most variations do not have parameters, and for them only the + and - operators
are relevant. The variations that accept parameters are:

domain_host.use_sudo
    An integer, whether to use sudo(1) together with unshare(1) to change the
    system hostname and domainname. 0 means don't use sudo; any non-zero value
    means to use sudo. Default is 0, however this is not recommended and make
    may your build fail, see "Varying the domain and host names" for details.
environment.variables
    A semicolon-separated ordered set, specifying environment variables that
    reprotest should try to vary. Default is "REPROTEST_CAPTURE_ENVIRONMENT".
    Supports regex-based syntax e.g.

    - PID=\\d{1,6}
    - HOME=(/\\w{3,12}){1,4}
    - (GO|PYTHON|)PATH=(/\\w{3,12}){1,4}(:(/\\w{3,12}){1,4}){0,4}

    Special cases:

    - $VARNAME= (empty RHS) to tell reprotest to delete the variable
    - $VARNAME=.{0} to tell reprotest to actually set an empty value
    - \\x2c and \\x3b to match or generate , and ; respectively.
user_group.available
    A semicolon-separated ordered set, specifying the available user+group
    combinations that reprotest can ``sudo(1)`` to. Default is empty, in which
    case the variation is a no-op, and you'll see a warning about this. Each
    user+group should be given in the form $user:$group where either component
    can be omitted, or else if there is no colon then it is interpreted as only
    a $user, with no $group variation.
time.faketimes
    A semicolon-separated ordered set, specifying possible ``faketime(1)`` time
    descriptors to use. Default is empty, in which case we randomly choose a
    time: either now (if the latest file-modtime in ``source_root`` is older
    than about half-a-year) or more than half-a-year in the future.

    Note that the clock continues to run during the build. It is possible for
    ``faketime(1)`` to freeze it, but we don't yet support that yet; it has a
    higher chance of causing your build to fail or misbehave.

The difference between --vary and --variations is that the former appends onto
previous values but the latter resets them. Furthermore, the last value set for
--variations is treated as the zeroth --vary argument. For example::

    reprotest --vary=-user_group

means to vary +all (the default value for --variations) and -user_group (the
given value for --vary), whereas::

    reprotest --variations=-all,locales --variations=home,time --vary=timezone --vary=-time

means to vary home, time (the last given value for --variations), timezone, and
-time (the given multiple values for --vary), i.e. home and timezone.


Notes on variations
===================

reprotest tries hard to perform variations without assuming it has full root
access to the system. It also assumes other software may be running on the same
system, so it does not perform system-level modifications that would affect
other processes. Due to these assumptions, some variations are implemented
using hacks at various levels of dirtiness, which are documented below.

We will hopefully lift these assumptions for certain virtual_server contexts,
in future. That would likely allow for smoother operation in those contexts.
The assumptions will remain for the "null" (default) virtual_server however.

Number of CPUs
--------------

The control build uses only 1 CPU in order to try to reduce nondeterminism that
might exist due to multithreading or multiprocessing. If you are sure your
build is not affected by this (and good builds ought not to be), you can give
--min-cpus=99999 to use all available cores for both builds.

Domain or host
--------------

Doing this without sudo *may* result in your build failing.

Failure is likely if your build must do system-related things - as opposed to
only processing bits and bytes. This is because it runs in a separate namespace
where your non-privileged user looks like it is "root", but this prevents the
filesystem from recognising files owned by the real "root" user, amongst other
things. This is a limitation of unshare(1) and it is not possible work around
this in reprotest without heavy effort.

Therefore, it is recommended to run this variation with use_sudo=1. To avoid
password prompts, see the section "Avoid sudo(1) password prompts" below.

When running inside a virtual-server:

The non-sudo method fails with "Operation not permitted", even if you edited
``/proc/sys/kernel/unprivileged_userns_clone``. The cause is currently unknown.

The sudo method works only if you take measures to avoid sudo password prompts,
since containers don't have a method to input this.

User or group
-------------

If you also vary fileordering at the same time (this is the case by default),
each user you use needs to be in the "fuse" group. Do that by running `usermod
-aG fuse $OTHERUSER` as root.

To avoid sudo(1) password prompts, see the section "Avoid sudo(1) password
prompts" below.

Time
----

The "time" variation uses ``faketime(1)`` which *sometimes* causes weird and
hard-to-diagnose problems. In the past, this has included:

- builds taking an infinite amount of time; though this should be fixed in
  recent versions of reprotest.

- builds with implausibly huge differences caused by ./configure scripts
  producing different results with and without faketime. This still affects
  bash and probably certain other packages using autotools.

- builds accessing the network failing due to certificate expiration errors
  and/or other time-related security errors. (Transparent builds of FOSS should
  not access the network in the first place, but it's outside of reprotest's
  scope to audit or prevent this.)

If you see a difference that you really think should not be there, try passing
``--variations=-time`` to reprotest, and/or check our results on
https://tests.reproducible-builds.org/ which use a different (more reliable)
mechanism to vary the system time.


Avoid sudo(1) password prompts
==============================

There is currently no good way to do this. The following is an EXPERIMENTAL
solution and is brittle and unclean. You will have to decide for yourself if
it's worth it for your use-case::

    $ reprotest --print-sudoers \
        --variations=user_group.available+=guest-builder,domain_host.use_sudo=1 \
        | sudo EDITOR=tee visudo -f /etc/sudoers.d/local-reprotest

Make sure you set the variations you actually want to use. Obviously, don't
pick privileged users for this purpose, such as root.

(Simplifying the output using wildcards, would open up passwordless access to
chown anything on your system, because wildcards here match whitespace. I don't
know what the sudo authors were thinking.)

No, this is not nice at all - suggestions and patches welcome.

If you want to use this in a virtual server such as a chroot, you'll need to
copy (or mount or otherwise map) the resulting sudoers file into your chroot.

For example, for an schroot, you should (1) login to the source schroot and
create an empty file `/etc/sudoers.d/local-reprotest` (this is important) and
then (2) add the line:

    /etc/sudoers.d/local-reprotest  /etc/sudoers.d/local-reprotest  none bind 0 0

to your schroot's fstab.

