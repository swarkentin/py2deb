# py2deb: Python to Debian package converter.
#
# Authors:
#  - Arjan Verwer
#  - Peter Odding <peter.odding@paylogic.com>
# Last Change: June 7, 2014
# URL: https://py2deb.readthedocs.org

"""
The :py:mod:`py2deb.converter` module contains the high level conversion logic.

This module defines the :py:class:`PackageConverter` class which provides the
intended way for external Python code to interface with `py2deb`. The
separation between the :py:class:`py2deb.converter.PackageConverter` and
:py:class:`py2deb.package.PackageToConvert` classes is somewhat crude (because
neither class can work without the other) but the idea is to separate the high
level conversion logic from the low level conversion logic.
"""

# Standard library modules.
import logging
import os
import shutil
import tempfile

# External dependencies.
from cached_property import cached_property
from executor import execute
from pip.exceptions import DistributionNotFound
from pip_accel import download_source_dists, initialize_directories, unpack_source_dists
from six.moves import configparser

# Modules included in our package.
from py2deb.utils import compact_repeating_words, normalize_package_name, PackageRepository, TemporaryDirectory
from py2deb.package import PackageToConvert

# Initialize a logger.
logger = logging.getLogger(__name__)


class PackageConverter(object):

    """
    The external interface of `py2deb`, the Python to Debian package converter.
    """

    def __init__(self):
        """
        Initialize a Python to Debian package converter.
        """
        self.alternatives = set()
        self.auto_install = False
        self.install_prefix = '/usr'
        self.max_download_attempts = 10
        self.name_mapping = {}
        self.name_prefix = 'python'
        self.repository = PackageRepository(tempfile.gettempdir())
        self.scripts = {}

    def set_repository(self, directory):
        """
        Set pathname of directory where `py2deb` stores converted packages.

        :param directory: The pathname of a directory (a string).
        :raises: :py:exc:`exceptions.ValueError` when the directory doesn't
                 exist.
        """
        directory = os.path.abspath(directory)
        if not os.path.isdir(directory):
            msg = "Repository directory doesn't exist! (%s)"
            raise ValueError(msg % directory)
        self.repository = PackageRepository(directory)

    def set_name_prefix(self, prefix):
        """
        Set package name prefix to use during package conversion.

        :param prefix: The name prefix to use (a string).
        :raises: :py:exc:`exceptions.ValueError` when no name prefix is
                 provided (e.g. an empty string).
        """
        if not prefix:
            raise ValueError("Please provide a nonempty name prefix!")
        self.name_prefix = prefix

    def rename_package(self, python_package_name, debian_package_name):
        """
        Override package name conversion algorithm for given pair of names.

        :param python_package_name: The name of a Python package
                                    as found on PyPI (a string).
        :param debian_package_name: The name of the converted
                                    Debian package (a string).
        :raises: :py:exc:`exceptions.ValueError` when a package name is not
                 provided (e.g. an empty string).
        """
        if not python_package_name:
            raise ValueError("Please provide a nonempty Python package name!")
        if not debian_package_name:
            raise ValueError("Please provide a nonempty Debian package name!")
        self.name_mapping[python_package_name.lower()] = debian_package_name.lower()

    def set_install_prefix(self, directory):
        """
        Set installation prefix to use during package conversion.

        The installation directory doesn't have to exist on the system where
        the package is converted.

        :param directory: The pathname of the directory where the converted
                          packages should be installed (a string).
        :raises: :py:exc:`exceptions.ValueError` when no installation prefix is
                 provided (e.g. an empty string).
        """
        if not directory:
            raise ValueError("Please provide a nonempty installation prefix!")
        self.install_prefix = directory

    def set_auto_install(self, enabled):
        """
        Enable or disable automatic installation of build time dependencies.

        :param enabled: If this evaluates to ``True`` automatic installation is
                        enabled, otherwise it's disabled.
        """
        self.auto_install = bool(enabled)

    def install_alternative(self, link, path):
        r"""
        Install system wide link for program installed in custom installation prefix.

        Use Debian's update-alternatives_ system to add an executable that's
        installed in a custom installation prefix to the system wide executable
        search path using a symbolic link.

        :param link: The generic name for the master link (a string). This is
                     the first argument passed to ``update-alternatives
                     --install``.
        :param path: The alternative being introduced for the master link (a
                     string). This is the third argument passed to
                     ``update-alternatives --install``.
        :raises: :py:exc:`exceptions.ValueError` when one of the paths is not
                 provided (e.g. an empty string).

        If this is a bit vague, consider the following example:

        .. code-block:: sh

           $ py2deb --name-prefix=py2deb \
                    --no-name-prefix=py2deb \
                    --install-prefix=/usr/lib/py2deb \
                    --install-alternative=/usr/bin/py2deb,/usr/lib/py2deb/bin/py2deb \
                    py2deb==0.1

        This example will convert `py2deb` and its dependencies using a custom
        name prefix and a custom installation prefix which means the ``py2deb``
        program is not available on the default executable search path. This is
        why ``update-alternatives`` is used to create a symbolic link
        ``/usr/bin/py2deb`` which points to the program inside the custom
        installation prefix.

        .. _update-alternatives: http://manpages.debian.org/cgi-bin/man.cgi?query=update-alternatives
        """
        if not link:
            raise ValueError("Please provide a nonempty name for the master link!")
        if not path:
            raise ValueError("Please provide a nonempty name for the alternative being introduced!")
        self.alternatives.add((link, path))

    def set_conversion_command(self, python_package_name, command):
        """
        Set shell command to be executed during conversion process.

        The shell command is executed in the directory containing the Python
        module(s) that are to be installed by the converted package.

        :param python_package_name: The name of a Python package
                                    as found on PyPI (a string).
        :param command: The shell command to execute (a string).
        :raises: :py:exc:`exceptions.ValueError` when the package name or
                 command is not provided (e.g. an empty string).

        .. warning:: This functionality allows arbitrary manipulation of the
                     Python modules to be installed by the converted package.
                     It should clearly be considered a last resort, only for
                     for fixing things like packaging issues with Python
                     packages that you can't otherwise change.

        For example old versions of Fabric_ bundle a copy of Paramiko_. Most
        people will never notice this because Python package managers don't
        complain about this, they just blindly overwrite the files... Debian's
        packaging system is much more strict and will consider the converted
        Fabric and Paramiko packages as conflicting and thus broken. In this
        case you have two options:

        1. Switch to a newer version of Fabric that no longer bundles Paramiko;
        2. Use the conversion command ``rm -rf paramiko`` to convert Fabric
           (yes this is somewhat brute force :-).

        .. _Fabric: https://pypi.python.org/pypi/Fabric
        .. _Paramiko: https://pypi.python.org/pypi/paramiko
        """
        if not python_package_name:
            raise ValueError("Please provide a nonempty Python package name!")
        if not command:
            raise ValueError("Please provide a nonempty shell command!")
        self.scripts[python_package_name.lower()] = command

    def load_configuration(self, configuration_file):
        """
        Load configuration defaults from a configuration file.

        :param configuration_file: The pathname of a configuration file (a
                                   string).

        Below is an example of the available options, I assume that the mapping
        between the configuration options and the setters of
        :py:class:`PackageConverter` is fairly obvious (it should be :-).

        .. code-block:: ini

           # The `py2deb' section contains global options.
           [py2deb]
           repository = /tmp
           name-prefix = py2deb
           install-prefix = /usr/lib/py2deb
           auto-install = on

           # The `alternatives' section contains instructions
           # for Debian's `update-alternatives' system.
           [alternatives]
           /usr/bin/py2deb = /usr/lib/py2deb/bin/py2deb

           # Sections starting with `package:' contain conversion options
           # specific to a package.
           [package:py2deb]
           no-name-prefix = true

        Note that the configuration options shown here are just examples, they
        are not the configuration defaults (they are what I use to convert
        `py2deb` itself). Package specific sections support the following
        options:

        **no-name-prefix**:
          A boolean indicating whether the configured name prefix should be
          applied or not. Understands ``true`` and ``false`` (``false`` is the
          default and you only need this option to change the default).

        **rename**:
          Gives an override for the package name conversion algorithm (refer to
          :py:func:`rename_package()` for details).

        **script**:
          Set a shell command to be executed during the conversion process
          (refer to :py:func:`set_conversion_command()` for
          details).
        """
        # Load the configuration file.
        parser = configparser.RawConfigParser()
        files_loaded = parser.read(configuration_file)
        try:
            assert len(files_loaded) == 1
            assert os.path.samefile(configuration_file, files_loaded[0])
        except Exception:
            msg = "Failed to load configuration file! (%s)"
            raise Exception(msg % configuration_file)
        # Apply the global settings in the configuration file.
        if parser.has_option('py2deb', 'repository'):
            self.set_repository(parser.get('py2deb', 'repository'))
        if parser.has_option('py2deb', 'name-prefix'):
            self.set_name_prefix(parser.get('py2deb', 'name-prefix'))
        if parser.has_option('py2deb', 'install-prefix'):
            self.set_install_prefix(parser.get('py2deb', 'install-prefix'))
        if parser.has_option('py2deb', 'auto-install'):
            self.set_auto_install(parser.getboolean('py2deb', 'auto-install'))
        # Apply the defined alternatives.
        if parser.has_section('alternatives'):
            for link, path in parser.items('alternatives'):
                self.install_alternative(link, path)
        # Apply any package specific settings.
        for section in parser.sections():
            tag, _, package = section.partition(':')
            if tag == 'package':
                if parser.has_option(section, 'no-name-prefix'):
                    if parser.getboolean(section, 'no-name-prefix'):
                        self.rename_package(package, package)
                if parser.has_option(section, 'rename'):
                    rename_to = parser.get(section, 'rename')
                    self.rename_package(package, rename_to)
                if parser.has_option(section, 'script'):
                    script = parser.get(section, 'script')
                    self.set_conversion_command(package, script)

    def convert(self, pip_install_arguments):
        """
        Convert one or more Python packages to Debian packages.

        :param pip_install_arguments: The command line arguments to the ``pip
                                      install`` command.
        :returns: A list of strings containing the Debian package relationships
                  required to depend on the converted package(s).

        Here's an example of what's returned:

        >>> from py2deb import PackageConverter
        >>> converter = PackageConverter()
        >>> converter.convert(['py2deb'])
        ['python-py2deb (=0.1)']

        """
        with TemporaryDirectory(prefix='py2deb-sdists-') as sources_directory:
            primary_packages = []
            # Download, unpack and convert no-yet-converted packages.
            for package in self.get_source_distributions(pip_install_arguments, sources_directory):
                if package.requirement.is_direct:
                    primary_packages.append(package)
                if package.existing_archive:
                    logger.info("Package %s (%s) already converted: %s",
                                package.python_name, package.python_version,
                                package.existing_archive.filename)
                else:
                    archive = package.convert()
                    if not os.path.samefile(os.path.dirname(archive), self.repository.directory):
                        shutil.move(archive, self.repository.directory)
            # Tell the caller how to depend on the converted packages.
            dependencies_to_report = []
            for package in primary_packages:
                dependency = '%s (=%s)' % (package.debian_name, package.debian_version)
                dependencies_to_report.append(dependency)
            return sorted(dependencies_to_report)

    def get_source_distributions(self, pip_install_arguments, build_directory):
        """
        Use :py:mod:`pip_accel` to download and unpack Python source distributions.

        Retries several times if a download fails (so it doesn't fail
        immediately when a package index server returns a transient error).

        :param pip_install_arguments: The command line arguments to the ``pip
                                      install`` command.
        :param build_directory: The pathname of a build directory (a string).
        :returns: A generator of :py:class:`py2deb.package.PackageToConvert`
                  objects.
        :raises: When downloading fails even after several retries this
                 function raises :py:exc:`pip.exceptions.DistributionNotFound`.
                 This function can also raise other exceptions raised by pip
                 because it uses :py:mod:`pip_accel` to call pip (as a Python
                 API).
        """
        # Compose the `pip install' command line:
        #  - The command line arguments to `py2deb' are the command line
        #    arguments to `pip install'. Since it doesn't make any sense for
        #    users of `py2deb' to type out commands like `py2deb install ...'
        #    we'll have to fill in the `install' command ourselves.
        #  - We depend on `pip install --ignore-installed ...' so we can
        #    guarantee that all of the packages specified by the caller are
        #    converted, instead of only those not currently installed somewhere
        #    where pip can see them (a poorly defined concept to begin with).
        pip_install_arguments = ['install', '--ignore-installed'] + list(pip_install_arguments)
        # Make sure pip-accel has been properly initialized.
        initialize_directories()
        # Loop to retry downloading source packages a couple of times (so
        # we don't fail immediately when a package index server returns a
        # transient error).
        for i in range(1, self.max_download_attempts + 1):
            try:
                for requirement in unpack_source_dists(pip_install_arguments, build_directory):
                    yield PackageToConvert(self, requirement)
                return
            except DistributionNotFound:
                logger.warning("We don't have all source distributions yet!")
                download_source_dists(pip_install_arguments, build_directory)
        msg = "Failed to download source distribution archive(s)! (tried %i times)"
        raise DistributionNotFound(msg % self.max_download_attempts)

    def transform_name(self, python_package_name):
        """
        Transform Python package name to Debian package name.

        :param python_package_name: The name of a Python package
                                    as found on PyPI (a string).
        :returns: The transformed name (a string).

        Examples:

        >>> from py2deb import PackageConverter
        >>> converter = PackageConverter()
        >>> converter.transform_name('example')
        'python-example'
        >>> converter.set_name_prefix('my-custom-prefix')
        >>> converter.transform_name('example')
        'my-custom-prefix-example'
        """
        # Check for an override by the caller.
        debian_package_name = self.name_mapping.get(python_package_name.lower())
        if not debian_package_name:
            # No override. Make something up :-).
            with_name_prefix = '%s-%s' % (self.name_prefix, python_package_name)
            normalized_words = normalize_package_name(with_name_prefix).split('-')
            debian_package_name = '-'.join(compact_repeating_words(normalized_words))
        # Always normalize the package name (even if it was given to us by the caller).
        return normalize_package_name(debian_package_name)

    @cached_property
    def debian_architecture(self):
        """
        Find Debian architecture of current environment.

        Uses the external command ``uname --machine``.

        :raises: If the output of the command is not recognized
                 :py:exc:`exceptions.Exception` is raised.
        :returns: The Debian architecture (a string like ``i386`` or ``amd64``).
        """
        architecture = execute('uname', '--machine', capture=True, logger=logger)
        if architecture == 'i686':
            return 'i386'
        elif architecture == 'x86_64':
            return 'amd64'
        else:
            msg = "The current architecture is not supported by py2deb! (architecture reported by uname -m: %s)"
            raise Exception(msg % architecture)


# vim: ts=4 sw=4
