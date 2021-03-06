"""Renewable certificates storage."""
import datetime
import os
import re
import time

import configobj
import OpenSSL
import parsedatetime
import pytz
import pyrfc3339

from letsencrypt import constants
from letsencrypt import crypto_util
from letsencrypt import errors
from letsencrypt import le_util

ALL_FOUR = ("cert", "privkey", "chain", "fullchain")


def config_with_defaults(config=None):
    """Merge supplied config, if provided, on top of builtin defaults."""
    defaults_copy = configobj.ConfigObj(constants.RENEWER_DEFAULTS)
    defaults_copy.merge(config if config is not None else configobj.ConfigObj())
    return defaults_copy


def parse_time_interval(interval, textparser=parsedatetime.Calendar()):
    """Parse the time specified time interval.

    The interval can be in the English-language format understood by
    parsedatetime, e.g., '10 days', '3 weeks', '6 months', '9 hours', or
    a sequence of such intervals like '6 months 1 week' or '3 days 12
    hours'. If an integer is found with no associated unit, it is
    interpreted by default as a number of days.

    :param str interval: The time interval to parse.

    :returns: The interpretation of the time interval.
    :rtype: :class:`datetime.timedelta`"""

    if interval.strip().isdigit():
        interval += " days"
    return datetime.timedelta(0, time.mktime(textparser.parse(
        interval, time.localtime(0))[0]))


class RenewableCert(object):  # pylint: disable=too-many-instance-attributes
    """Renewable certificate.

    Represents a lineage of certificates that is under the management
    of the Let's Encrypt client, indicated by the existence of an
    associated renewal configuration file.

    Note that the notion of "current version" for a lineage is
    maintained on disk in the structure of symbolic links, and is not
    explicitly stored in any instance variable in this object. The
    RenewableCert object is able to determine information about the
    current (or other) version by accessing data on disk, but does not
    inherently know any of this information except by examining the
    symbolic links as needed. The instance variables mentioned below
    point to symlinks that reflect the notion of "current version" of
    each managed object, and it is these paths that should be used when
    configuring servers to use the certificate managed in a lineage.
    These paths are normally within the "live" directory, and their
    symlink targets -- the actual cert files -- are normally found
    within the "archive" directory.

    :ivar str cert: The path to the symlink representing the current
        version of the certificate managed by this lineage.
    :ivar str privkey: The path to the symlink representing the current
        version of the private key managed by this lineage.
    :ivar str chain: The path to the symlink representing the current version
        of the chain managed by this lineage.
    :ivar str fullchain: The path to the symlink representing the
        current version of the fullchain (combined chain and cert)
        managed by this lineage.
    :ivar configobj.ConfigObj configuration: The renewal configuration
        options associated with this lineage, obtained from parsing the
        renewal configuration file and/or systemwide defaults.

    """
    def __init__(self, configfile, config_opts=None, cli_config=None):
        """Instantiate a RenewableCert object from an existing lineage.

        :param configobj.ConfigObj configfile: an already-parsed
            ConfigObj object made from reading the renewal config file
            that defines this lineage.

        :param configobj.ConfigObj config_opts: systemwide defaults for
            renewal properties not otherwise specified in the individual
            renewal config file.
        :param .RenewerConfiguration cli_config:

        :raises .CertStorageError: if the configuration file's name didn't end
            in ".conf", or the file is missing or broken.
        :raises TypeError: if the provided renewal configuration isn't a
            ConfigObj object.

        """
        self.cli_config = cli_config
        if isinstance(configfile, configobj.ConfigObj):
            if not os.path.basename(configfile.filename).endswith(".conf"):
                raise errors.CertStorageError(
                    "renewal config file name must end in .conf")
            self.lineagename = os.path.basename(
                configfile.filename)[:-len(".conf")]
        else:
            raise TypeError("RenewableCert config must be ConfigObj object")

        # self.configuration should be used to read parameters that
        # may have been chosen based on default values from the
        # systemwide renewal configuration; self.configfile should be
        # used to make and save changes.
        self.configfile = configfile
        # TODO: Do we actually use anything from defaults and do we want to
        #       read further defaults from the systemwide renewal configuration
        #       file at this stage?
        self.configuration = config_with_defaults(config_opts)
        self.configuration.merge(self.configfile)

        if not all(x in self.configuration for x in ALL_FOUR):
            raise errors.CertStorageError(
                "renewal config file {0} is missing a required "
                "file reference".format(configfile))

        self.cert = self.configuration["cert"]
        self.privkey = self.configuration["privkey"]
        self.chain = self.configuration["chain"]
        self.fullchain = self.configuration["fullchain"]

    def consistent(self):
        """Are the files associated with this lineage self-consistent?

        :returns: Whether the files stored in connection with this
            lineage appear to be correct and consistent with one
            another.
        :rtype: bool

        """
        # Each element must be referenced with an absolute path
        if any(not os.path.isabs(x) for x in
               (self.cert, self.privkey, self.chain, self.fullchain)):
            return False

        # Each element must exist and be a symbolic link
        if any(not os.path.islink(x) for x in
               (self.cert, self.privkey, self.chain, self.fullchain)):
            return False
        for kind in ALL_FOUR:
            link = getattr(self, kind)
            where = os.path.dirname(link)
            target = os.readlink(link)
            if not os.path.isabs(target):
                target = os.path.join(where, target)

            # Each element's link must point within the cert lineage's
            # directory within the official archive directory
            desired_directory = os.path.join(
                self.cli_config.archive_dir, self.lineagename)
            if not os.path.samefile(os.path.dirname(target),
                                    desired_directory):
                return False

            # The link must point to a file that exists
            if not os.path.exists(target):
                return False

            # The link must point to a file that follows the archive
            # naming convention
            pattern = re.compile(r"^{0}([0-9]+)\.pem$".format(kind))
            if not pattern.match(os.path.basename(target)):
                return False

            # It is NOT required that the link's target be a regular
            # file (it may itself be a symlink). But we should probably
            # do a recursive check that ultimately the target does
            # exist?
        # XXX: Additional possible consistency checks (e.g.
        #      cryptographic validation of the chain being a chain,
        #      the chain matching the cert, and the cert matching
        #      the subject key)
        # XXX: All four of the targets are in the same directory
        #      (This check is redundant with the check that they
        #      are all in the desired directory!)
        #      len(set(os.path.basename(self.current_target(x)
        #      for x in ALL_FOUR))) == 1
        return True

    def fix(self):
        """Attempt to fix defects or inconsistencies in this lineage.

        .. todo:: Currently unimplemented.

        """
        # TODO: Figure out what kinds of fixes are possible.  For
        #       example, checking if there is a valid version that
        #       we can update the symlinks to.  (Maybe involve
        #       parsing keys and certs to see if they exist and
        #       if a key corresponds to the subject key of a cert?)

    # TODO: In general, the symlink-reading functions below are not
    #       cautious enough about the possibility that links or their
    #       targets may not exist.  (This shouldn't happen, but might
    #       happen as a result of random tampering by a sysadmin, or
    #       filesystem errors, or crashes.)

    def current_target(self, kind):
        """Returns full path to which the specified item currently points.

        :param str kind: the lineage member item ("cert", "privkey",
            "chain", or "fullchain")

        :returns: The path to the current version of the specified
            member.
        :rtype: str

        """
        if kind not in ALL_FOUR:
            raise errors.CertStorageError("unknown kind of item")
        link = getattr(self, kind)
        if not os.path.exists(link):
            return None
        target = os.readlink(link)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(link), target)
        return os.path.abspath(target)

    def current_version(self, kind):
        """Returns numerical version of the specified item.

        For example, if kind is "chain" and the current chain link
        points to a file named "chain7.pem", returns the integer 7.

        :param str kind: the lineage member item ("cert", "privkey",
            "chain", or "fullchain")

        :returns: the current version of the specified member.
        :rtype: int

        """
        if kind not in ALL_FOUR:
            raise errors.CertStorageError("unknown kind of item")
        pattern = re.compile(r"^{0}([0-9]+)\.pem$".format(kind))
        target = self.current_target(kind)
        if target is None or not os.path.exists(target):
            target = ""
        matches = pattern.match(os.path.basename(target))
        if matches:
            return int(matches.groups()[0])
        else:
            return None

    def version(self, kind, version):
        """The filename that corresponds to the specified version and kind.

        .. warning:: The specified version may not exist in this
           lineage. There is no guarantee that the file path returned
           by this method actually exists.

        :param str kind: the lineage member item ("cert", "privkey",
            "chain", or "fullchain")
        :param int version: the desired version

        :returns: The path to the specified version of the specified member.
        :rtype: str

        """
        if kind not in ALL_FOUR:
            raise errors.CertStorageError("unknown kind of item")
        where = os.path.dirname(self.current_target(kind))
        return os.path.join(where, "{0}{1}.pem".format(kind, version))

    def available_versions(self, kind):
        """Which alternative versions of the specified kind of item exist?

        The archive directory where the current version is stored is
        consulted to obtain the list of alternatives.

        :param str kind: the lineage member item (
            ``cert``, ``privkey``, ``chain``, or ``fullchain``)

        :returns: all of the version numbers that currently exist
        :rtype: `list` of `int`

        """
        if kind not in ALL_FOUR:
            raise errors.CertStorageError("unknown kind of item")
        where = os.path.dirname(self.current_target(kind))
        files = os.listdir(where)
        pattern = re.compile(r"^{0}([0-9]+)\.pem$".format(kind))
        matches = [pattern.match(f) for f in files]
        return sorted([int(m.groups()[0]) for m in matches if m])

    def newest_available_version(self, kind):
        """Newest available version of the specified kind of item?

        :param str kind: the lineage member item (``cert``,
            ``privkey``, ``chain``, or ``fullchain``)

        :returns: the newest available version of this member
        :rtype: int

        """
        return max(self.available_versions(kind))

    def latest_common_version(self):
        """Newest version for which all items are available?

        :returns: the newest available version for which all members
            (``cert, ``privkey``, ``chain``, and ``fullchain``) exist
        :rtype: int

        """
        # TODO: this can raise CertStorageError if there is no version overlap
        #       (it should probably return None instead)
        # TODO: this can raise a spurious AttributeError if the current
        #       link for any kind is missing (it should probably return None)
        versions = [self.available_versions(x) for x in ALL_FOUR]
        return max(n for n in versions[0] if all(n in v for v in versions[1:]))

    def next_free_version(self):
        """Smallest version newer than all full or partial versions?

        :returns: the smallest version number that is larger than any
            version of any item currently stored in this lineage
        :rtype: int

        """
        # TODO: consider locking/mutual exclusion between updating processes
        # This isn't self.latest_common_version() + 1 because we don't want
        # collide with a version that might exist for one file type but not
        # for the others.
        return max(self.newest_available_version(x) for x in ALL_FOUR) + 1

    def has_pending_deployment(self):
        """Is there a later version of all of the managed items?

        :returns: ``True`` if there is a complete version of this
            lineage with a larger version number than the current
            version, and ``False`` otherwis
        :rtype: bool

        """
        # TODO: consider whether to assume consistency or treat
        #       inconsistent/consistent versions differently
        smallest_current = min(self.current_version(x) for x in ALL_FOUR)
        return smallest_current < self.latest_common_version()

    def update_link_to(self, kind, version):
        """Make the specified item point at the specified version.

        (Note that this method doesn't verify that the specified version
        exists.)

        :param str kind: the lineage member item ("cert", "privkey",
            "chain", or "fullchain")
        :param int version: the desired version

        """
        if kind not in ALL_FOUR:
            raise errors.CertStorageError("unknown kind of item")
        link = getattr(self, kind)
        filename = "{0}{1}.pem".format(kind, version)
        # Relative rather than absolute target directory
        target_directory = os.path.dirname(os.readlink(link))
        # TODO: it could be safer to make the link first under a temporary
        #       filename, then unlink the old link, then rename the new link
        #       to the old link; this ensures that this process is able to
        #       create symlinks.
        # TODO: we might also want to check consistency of related links
        #       for the other corresponding items
        os.unlink(link)
        os.symlink(os.path.join(target_directory, filename), link)

    def update_all_links_to(self, version):
        """Change all member objects to point to the specified version.

        :param int version: the desired version"""

        for kind in ALL_FOUR:
            self.update_link_to(kind, version)

    def _notafterbefore(self, method, version):
        """Internal helper function for finding notbefore/notafter."""
        if version is None:
            target = self.current_target("cert")
        else:
            target = self.version("cert", version)
        pem = open(target).read()
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM,
                                               pem)
        i = method(x509)
        return pyrfc3339.parse(i[0:4] + "-" + i[4:6] + "-" + i[6:8] + "T" +
                               i[8:10] + ":" + i[10:12] + ":" + i[12:])

    def notbefore(self, version=None):
        """When does the specified cert version start being valid?

        (If no version is specified, use the current version.)

        :param int version: the desired version number

        :returns: the notBefore value from the specified cert version in
            this lineage
        :rtype: :class:`datetime.datetime`

        """
        return self._notafterbefore(lambda x509: x509.get_notBefore(), version)

    def notafter(self, version=None):
        """When does the specified cert version stop being valid?

        (If no version is specified, use the current version.)

        :param int version: the desired version number

        :returns: the notAfter value from the specified cert version in
            this lineage
        :rtype: :class:`datetime.datetime`

        """
        return self._notafterbefore(lambda x509: x509.get_notAfter(), version)

    def names(self, version=None):
        """What are the subject names of this certificate?

        (If no version is specified, use the current version.)

        :param int version: the desired version number
        :returns: the subject names
        :rtype: `list` of `str`

        """
        if version is None:
            target = self.current_target("cert")
        else:
            target = self.version("cert", version)
        with open(target) as f:
            return crypto_util.get_sans_from_cert(f.read())

    def should_autodeploy(self):
        """Should this lineage now automatically deploy a newer version?

        This is a policy question and does not only depend on whether
        there is a newer version of the cert. (This considers whether
        autodeployment is enabled, whether a relevant newer version
        exists, and whether the time interval for autodeployment has
        been reached.)

        :returns: whether the lineage now ought to autodeploy an
            existing newer cert version
        :rtype: bool

        """
        if ("autodeploy" not in self.configuration or
                self.configuration.as_bool("autodeploy")):
            if self.has_pending_deployment():
                interval = self.configuration.get("deploy_before_expiry",
                                                  "5 days")
                autodeploy_interval = parse_time_interval(interval)
                expiry = self.notafter()
                now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
                remaining = expiry - now
                if remaining < autodeploy_interval:
                    return True
        return False

    def ocsp_revoked(self, version=None):
        # pylint: disable=no-self-use,unused-argument
        """Is the specified cert version revoked according to OCSP?

        Also returns True if the cert version is declared as intended
        to be revoked according to Let's Encrypt OCSP extensions.
        (If no version is specified, uses the current version.)

        This method is not yet implemented and currently always returns
        False.

        :param int version: the desired version number

        :returns: whether the certificate is or will be revoked
        :rtype: bool

        """
        # XXX: This query and its associated network service aren't
        # implemented yet, so we currently return False (indicating that the
        # certificate is not revoked).
        return False

    def should_autorenew(self):
        """Should we now try to autorenew the most recent cert version?

        This is a policy question and does not only depend on whether
        the cert is expired. (This considers whether autorenewal is
        enabled, whether the cert is revoked, and whether the time
        interval for autorenewal has been reached.)

        Note that this examines the numerically most recent cert version,
        not the currently deployed version.

        :returns: whether an attempt should now be made to autorenew the
            most current cert version in this lineage
        :rtype: bool

        """
        if ("autorenew" not in self.configuration or
                self.configuration.as_bool("autorenew")):
            # Consider whether to attempt to autorenew this cert now

            # Renewals on the basis of revocation
            if self.ocsp_revoked(self.latest_common_version()):
                return True

            # Renewals on the basis of expiry time
            interval = self.configuration.get("renew_before_expiry", "10 days")
            autorenew_interval = parse_time_interval(interval)
            expiry = self.notafter(self.latest_common_version())
            now = datetime.datetime.utcnow().replace(tzinfo=pytz.UTC)
            remaining = expiry - now
            if remaining < autorenew_interval:
                return True
            return False

    @classmethod
    def new_lineage(cls, lineagename, cert, privkey, chain,
                    renewalparams=None, config=None, cli_config=None):
        # pylint: disable=too-many-locals,too-many-arguments
        """Create a new certificate lineage.

        Attempts to create a certificate lineage -- enrolled for
        potential future renewal -- with the (suggested) lineage name
        lineagename, and the associated cert, privkey, and chain (the
        associated fullchain will be created automatically). Optional
        configurator and renewalparams record the configuration that was
        originally used to obtain this cert, so that it can be reused
        later during automated renewal.

        Returns a new RenewableCert object referring to the created
        lineage. (The actual lineage name, as well as all the relevant
        file paths, will be available within this object.)

        :param str lineagename: the suggested name for this lineage
            (normally the current cert's first subject DNS name)
        :param str cert: the initial certificate version in PEM format
        :param str privkey: the private key in PEM format
        :param str chain: the certificate chain in PEM format
        :param configobj.ConfigObj renewalparams: parameters that
            should be used when instantiating authenticator and installer
            objects in the future to attempt to renew this cert or deploy
            new versions of it
        :param configobj.ConfigObj config: renewal configuration
            defaults, affecting, for example, the locations of the
            directories where the associated files will be saved

        :returns: the newly-created RenewalCert object
        :rtype: :class:`storage.renewableCert`"""

        config = config_with_defaults(config)
        # This attempts to read the renewer config file and augment or replace
        # the renewer defaults with any options contained in that file.  If
        # renewer_config_file is undefined or if the file is nonexistent or
        # empty, this .merge() will have no effect.
        config.merge(configobj.ConfigObj(cli_config.renewer_config_file))

        # Examine the configuration and find the new lineage's name
        for i in (cli_config.renewal_configs_dir, cli_config.archive_dir,
                  cli_config.live_dir):
            if not os.path.exists(i):
                os.makedirs(i, 0700)
        config_file, config_filename = le_util.unique_lineage_name(
            cli_config.renewal_configs_dir, lineagename)
        if not config_filename.endswith(".conf"):
            raise errors.CertStorageError(
                "renewal config file name must end in .conf")

        # Determine where on disk everything will go
        # lineagename will now potentially be modified based on which
        # renewal configuration file could actually be created
        lineagename = os.path.basename(config_filename)[:-len(".conf")]
        archive = os.path.join(cli_config.archive_dir, lineagename)
        live_dir = os.path.join(cli_config.live_dir, lineagename)
        if os.path.exists(archive):
            raise errors.CertStorageError(
                "archive directory exists for " + lineagename)
        if os.path.exists(live_dir):
            raise errors.CertStorageError(
                "live directory exists for " + lineagename)
        os.mkdir(archive)
        os.mkdir(live_dir)
        relative_archive = os.path.join("..", "..", "archive", lineagename)

        # Put the data into the appropriate files on disk
        target = dict([(kind, os.path.join(live_dir, kind + ".pem"))
                       for kind in ALL_FOUR])
        for kind in ALL_FOUR:
            os.symlink(os.path.join(relative_archive, kind + "1.pem"),
                       target[kind])
        with open(target["cert"], "w") as f:
            f.write(cert)
        with open(target["privkey"], "w") as f:
            f.write(privkey)
            # XXX: Let's make sure to get the file permissions right here
        with open(target["chain"], "w") as f:
            f.write(chain)
        with open(target["fullchain"], "w") as f:
            # assumes that OpenSSL.crypto.dump_certificate includes
            # ending newline character
            f.write(cert + chain)

        # Document what we've done in a new renewal config file
        config_file.close()
        new_config = configobj.ConfigObj(config_filename, create_empty=True)
        for kind in ALL_FOUR:
            new_config[kind] = target[kind]
        if renewalparams:
            new_config["renewalparams"] = renewalparams
            new_config.comments["renewalparams"] = ["",
                                                    "Options and defaults used"
                                                    " in the renewal process"]
        # TODO: add human-readable comments explaining other available
        #       parameters
        new_config.write()
        return cls(new_config, config, cli_config)

    def save_successor(self, prior_version, new_cert, new_privkey, new_chain):
        """Save new cert and chain as a successor of a prior version.

        Returns the new version number that was created.

        .. note:: this function does NOT update links to deploy this
                  version

        :param int prior_version: the old version to which this version
            is regarded as a successor (used to choose a privkey, if the
            key has not changed, but otherwise this information is not
            permanently recorded anywhere)
        :param str new_cert: the new certificate, in PEM format
        :param str new_privkey: the new private key, in PEM format,
            or ``None``, if the private key has not changed
        :param str new_chain: the new chain, in PEM format

        :returns: the new version number that was created
        :rtype: int

        """
        # XXX: assumes official archive location rather than examining links
        # XXX: consider using os.open for availability of os.O_EXCL
        # XXX: ensure file permissions are correct; also create directories
        #      if needed (ensuring their permissions are correct)
        # Figure out what the new version is and hence where to save things

        target_version = self.next_free_version()
        archive = self.cli_config.archive_dir
        prefix = os.path.join(archive, self.lineagename)
        target = dict(
            [(kind,
              os.path.join(prefix, "{0}{1}.pem".format(kind, target_version)))
             for kind in ALL_FOUR])

        # Distinguish the cases where the privkey has changed and where it
        # has not changed (in the latter case, making an appropriate symlink
        # to an earlier privkey version)
        if new_privkey is None:
            # The behavior below keeps the prior key by creating a new
            # symlink to the old key or the target of the old key symlink.
            old_privkey = os.path.join(
                prefix, "privkey{0}.pem".format(prior_version))
            if os.path.islink(old_privkey):
                old_privkey = os.readlink(old_privkey)
            else:
                old_privkey = "privkey{0}.pem".format(prior_version)
            os.symlink(old_privkey, target["privkey"])
        else:
            with open(target["privkey"], "w") as f:
                f.write(new_privkey)

        # Save everything else
        with open(target["cert"], "w") as f:
            f.write(new_cert)
        with open(target["chain"], "w") as f:
            f.write(new_chain)
        with open(target["fullchain"], "w") as f:
            f.write(new_cert + new_chain)
        return target_version
