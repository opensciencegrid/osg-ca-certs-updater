#!/usr/bin/python3
"""OSG Auto-Updater for CA Certificates"""
# (ignore bad name of script) pylint: disable=C0103
from optparse import OptionParser
import logging
import logging.handlers
import os
import random
import re
import subprocess
import sys
import time
import traceback

__version__            = '@VERSION@'
PROGRAM_NAME           = "osg-ca-certs-updater"
HELP_MAILTO            = "help@opensciencegrid.org"
BUGREPORT_MAILTO       = "help@opensciencegrid.org"
OSG_REPO_ADDR          = "repo.opensciencegrid.org"

LASTRUN_TIMESTAMP_PATH = "/var/lib/osg-ca-certs-updater-lastrun"
PACKAGE_LIST           = ["osg-ca-certs", "igtf-ca-certs"]
UNCHECKED_PACKAGE_LIST = ["osg-ca-certs-compat", "igtf-ca-certs-compat"]
SECONDS_PER_MINUTE     = 60
SECONDS_PER_HOUR       = 3600

ADJUST_MIN_AGE_MESSAGE = "To change update frequency, adjust the -a/--minimum-age argument."
ADJUST_MAX_AGE_MESSAGE = ("To change the maximum time for which update failures are tolerated without a notification, "
                          "adjust the -x/--maximum-age argument.")
GENERIC_HELP_MESSAGE   = "Send email to %s if you are having difficulty diagnosing this error." % HELP_MAILTO

logger                 = logging.getLogger('updater')
logger_set_up          = False


class Error(Exception):
    """Base class for expected exceptions. Caught in main(); may include a
    traceback but will only print it if debugging is enabled.

    """
    def __init__(self, msg, trace=None):
        Exception.__init__(self)
        self.msg = msg
        if trace is None:
            self.traceback = traceback.format_exc()

    def __repr__(self):
        return repr((self.msg, self.traceback))

    def __str__(self):
        return str(self.msg)


class UsageError(Error):
    "Class for reporting incorrect arguments given to the program"
    def __init__(self, msg):
        Error.__init__(self, "Usage error: " + msg + "\n")


class UpdateError(Error):
    """Class for reporting some failure performing the update.
    'helpmsg' var contains a hint to the user for further action.
    'helpmsg' will only be printed if in verbose mode.

    """
    def __init__(self, msg=None, helpmsg=None):
        Error.__init__(self, msg)
        self.helpmsg = helpmsg


def get_options(args):
    "Parse, validate, and transform command-line options."
    parser = OptionParser("""
%prog [options]
""")
    parser.add_option(
        "-a", "--minimum-age", metavar="HOURS", dest="minimum_age_hours", default=0,
        help="The time which must have elapsed since the last successful run before attempting an update. "
        "If absent or 0, always update.")
    parser.add_option(
        "-x", "--maximum-age", metavar="HOURS", dest="maximum_age_hours", default=0,
        help="The time which must have elapsed since the last successful run "
        "before a failure to update the certificates is considered a critical error. "
        "Download failures before this time has elapsed are considered transient errors. "
        "If absent or 0, all unsuccessful update attempts are considered critical errors.")
    parser.add_option(
        "-r", "--random-wait", metavar="MINUTES", dest="random_wait_minutes", default=0,
        help="Delay the update for a random duration betwen 0 and the given number of minutes. "
        "This spreads out update requests to reduce load spikes on update servers. "
        "If absent or 0, update immediately.")
    parser.add_option(
        "--debug", action="store_const", const=logging.DEBUG, dest="loglevel", default=logging.WARNING,
        help="Display debugging information.")
    parser.add_option(
        "-v", "--verbose", action="store_const",
        const=logging.INFO, dest="loglevel", default=logging.WARNING,
        help="Display detailed information.")
    parser.add_option(
        "-q", "--quiet", action="store_const",
        const=logging.ERROR, dest="loglevel", default=logging.WARNING,
        help="Only display errors.")
    parser.add_option(
        "-l", "--logfile", metavar="PATH", default=None,
        help="Write messages to the given file instead of console.")
    parser.add_option(
        "-s", "--log-to-syslog", action="store_true", default=False,
        help="Write messages to syslog instead of console.")
    parser.add_option(
        "--syslog-address", default="/dev/log",
        help="Address to use for syslog. "
        "Can be a file (e.g. /dev/log) or an address:port combination. "
        "Default is '%default'.")
    parser.add_option(
        "--syslog-facility", default="user",
        help="The syslog facility to log to. "
        "Default is '%default'.")
    parser.add_option(
        "--enablerepo", dest="extra_repos", action="append", default=[],
        help="Additional yum repos to enable. May be specified multiple times; wildcards can be used.")

    options, _ = parser.parse_args(args) # raises SystemExit(2) on error

    try:
        options.minimum_age_hours = float(options.minimum_age_hours)
    except ValueError:
        raise UsageError("The value for minimum-age must be a number of hours.")
    try:
        options.maximum_age_hours = float(options.maximum_age_hours)
    except ValueError:
        raise UsageError("The value for maximum-age must be a number of hours.")
    try:
        options.random_wait_minutes = float(options.random_wait_minutes)
        if options.random_wait_minutes < 0:
            raise ValueError()
    except (TypeError, ValueError):
        raise UsageError("The value for random-wait must be a non-negative number of minutes.")

    if options.log_to_syslog:
        # SysLogHandler expects either a (host, port) tuple or a local file
        # for the syslog address.
        if options.syslog_address.find(':') != -1:
            (syslog_host, syslog_port) = options.syslog_address.split(':', 1)
            if not syslog_host:
                raise UsageError("Invalid host specified for syslog-address.")
            try:
                syslog_port = int(syslog_port)
            except ValueError:
                raise UsageError("Invalid port specified for syslog-address. "
                                 "Port must be an integer.")
            options.syslog_address = (syslog_host, syslog_port)
        else:
            if not os.path.exists(options.syslog_address):
                raise UsageError("Invalid path specified for syslog-address.")

    return options


def setup_logger(loglevel, logfile_path, log_to_syslog, syslog_address, syslog_facility):
    """Set up the global 'logger' object based on where the user wants to log to,
    e.g. syslog, or a file, or console.

    """
    global logger_set_up # (ignore usage of 'global') pylint:disable=W0603
    logger.setLevel(loglevel)

    if not logfile_path and not log_to_syslog:
        log_handler = logging.StreamHandler()
        log_formatter = logging.Formatter("%(message)s")
    else:
        if log_to_syslog:
            log_formatter = logging.Formatter(PROGRAM_NAME + ": %(message)s")
            log_handler = logging.handlers.SysLogHandler(address=syslog_address, facility=syslog_facility)
        else:
            log_formatter = logging.Formatter(PROGRAM_NAME + ":%(asctime)s:%(levelname)s:%(message)s")
            try:
                log_handler = logging.FileHandler(logfile_path)
            except IOError as err:
                print("Unable to open %s for writing logs to: %s" % (logfile_path, str(err)), file=sys.stderr)
                sys.exit(4)

    log_handler.setLevel(loglevel)
    log_handler.setFormatter(log_formatter)
    logger.addHandler(log_handler)
    logger.propagate = False

    logger_set_up = True


def get_lastrun_timestamp(timestamp_path):
    """Read a timestamp (seconds since epoch) from a file. Return None if
    the timestamp cannot be read. Assume that a nonexistant file means this
    program has not been run before. Failure to read or parse an existing
    file is a non-fatal error that gets logged.

    """
    if not os.path.exists(timestamp_path):
        logger.debug("No last run recorded")
        return None
    try:
        timestamp_handle = open(timestamp_path)
        try:
            timestamp = timestamp_handle.readline()
            logger.debug("Last run at %s", format_timestamp(timestamp))
            return float(timestamp) # 'finally' happens after this
        finally:
            timestamp_handle.close()
    except (IOError, ValueError) as err:
        logger.error("Unable to load or parse timestamp from %s: %s", timestamp_path, str(err))
        return None


def get_times(lastrun_timestamp, minimum_age_hours, maximum_age_hours):
    """Return 'next_update_time' (time after which an update is attempted) and
    'expire_time' (time after which a failed update is an error).

    """
    if not lastrun_timestamp:
        next_update_time = time.time()
        expire_time = time.time()
    else:
        next_update_time = lastrun_timestamp + minimum_age_hours * SECONDS_PER_HOUR
        expire_time = lastrun_timestamp + maximum_age_hours * SECONDS_PER_HOUR

    logger.debug("Next update time: %s" % format_timestamp(next_update_time))
    logger.debug("Expire time: %s" % format_timestamp(expire_time))

    return (next_update_time, expire_time)


def wait_random_duration(random_wait_seconds):
    "Sleep for at most 'random_wait_seconds'. Ignore bad arguments."
    random_wait_seconds = int(random_wait_seconds)
    if random_wait_seconds < 1:
        return
    time_to_wait = random.randint(1, random_wait_seconds)
    logger.debug("Waiting for %d seconds" % time_to_wait)
    time.sleep(time_to_wait)


def is_rpm_installed(rpm):
    devnull = open(os.devnull, 'w')
    try:
        proc = subprocess.Popen(["rpm", "-q", rpm], stdout=devnull, stderr=devnull)
        returncode = proc.wait()
    finally:
        devnull.close()

    return returncode == 0


def verify_requirement_available(requirement, extra_repos=None):
    """Use repoquery to ensure that an rpm requirement matching 'requirement'
    is available in an external repository. When trying to update with the osg repos
    disabled, yum will find no updates but return success. We do not actually
    want to consider it a success though. Detect it and raise an error.

    Using 'requirement' instead of 'package' to allow us to rename cert
    packages without breaking this test in the future.

    """
    extra_repos = extra_repos or []
    cmd = ["repoquery"] + ["--enablerepo=" + x for x in extra_repos] + ["--plugins",
                                                                        "--whatprovides",
                                                                        requirement,
                                                                        "--queryformat=%{repoid}"]
    repoquery_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (repoquery_out, repoquery_err) = repoquery_proc.communicate()
    repoquery_ret = repoquery_proc.returncode
    repoquery_out, repoquery_err = (
        repoquery_out.decode("utf-8", errors="ignore"),
        repoquery_err.decode("utf-8", errors="ignore")
    )

    if repoquery_ret != 0:
        raise UpdateError("Unable to query repository. Repoquery error:\n%s" % (repoquery_err))
    # repoquery_out now contains a newline-separated list of repos providing each matching package.
    # 'installed' is a "repo" that contains all installed packages.
    # Only want external repos, so filter that out.
    external_repos_providing = []
    for repo in repoquery_out.split("\n"):
        if re.search(r'\S', repo) and 'installed' not in repo:
            external_repos_providing.append(repo)

    # For now, considering this a fatal error.
    if not external_repos_providing:
        raise UpdateError("No external repos provide %s." % requirement,
                          helpmsg="Ensure that the osg repositories are enabled and accessible. "
                          "Repository definition files are located in '/etc/yum.repos.d' by default.")


def do_yum_update(package_list, extra_repos=None):
    """Use yum to update the packages in 'package_list'. Return a bool
    for success/failure. Output from yum is logged.

    """
    extra_repos = extra_repos or []
    cmd = ["yum", "update"] + ["--enablerepo="+x for x in extra_repos] + ["-y", "-q"] + package_list
    yum_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    yum_outerr = yum_proc.communicate()[0].decode("utf-8", errors="ignore")
    yum_ret = yum_proc.returncode

    if re.search(r'\S', yum_outerr):
        logger.info("Yum output: %s", yum_outerr)
    if yum_ret != 0:
        raise UpdateError()


def save_timestamp(timestamp_path, timestamp):
    """Write the timestamp (seconds since epoch) to a file.
    Failing to write is not fatal but should be logged.
    This is not an atomic write, but is sufficient for this task.
    The consequence of a failed write is that the certs will be updated more often.

    """
    try:
        logger.debug("Writing new timestamp %s", format_timestamp(timestamp))
        timestamp_handle = open(timestamp_path, 'wt')
        try:
            print("%d\n" % timestamp, file=timestamp_handle)
            return True # 'finally' happens after this
        finally:
            timestamp_handle.close() # raises IOError on failure; will be logged
    except IOError as err:
        logger.error("Unable to save timestamp to %s: %s", timestamp_path, str(err))
        return False


def format_timestamp(timestamp):
    "The timestamp (seconds since epoch) as a human-readable string."
    return time.strftime("%F %T", time.localtime(float(timestamp)))


def main(argv):
    "Main function"
    options = get_options(argv[1:])

    setup_logger(options.loglevel,
                 options.logfile,
                 options.log_to_syslog,
                 options.syslog_address,
                 options.syslog_facility)

    next_update_time, expire_time = get_times(get_lastrun_timestamp(LASTRUN_TIMESTAMP_PATH),
                                              options.minimum_age_hours,
                                              options.maximum_age_hours)

    if time.time() >= next_update_time:
        wait_random_duration(options.random_wait_minutes * SECONDS_PER_MINUTE)
        packages = PACKAGE_LIST
        for pkg in packages:
            verify_requirement_available(pkg, options.extra_repos)
        packages += UNCHECKED_PACKAGE_LIST
        installed_packages = [pkg for pkg in packages if is_rpm_installed(pkg)]
        try:
            do_yum_update(installed_packages, options.extra_repos)
            logger.info("Update succeeded")
            save_timestamp(LASTRUN_TIMESTAMP_PATH, time.time())
        except UpdateError:
            logger.warning("Update failed")
            logger.info("Verify that this machine can reach the OSG repositories at %s." % OSG_REPO_ADDR)
            logger.info("Also try clearing the yum cache with the following commands:")
            logger.info("'yum --enablerepo=\\* clean all; yum --enablerepo=\\* clean expire-cache'")
            logger.info("This may also be a transient error on the remote side.")
            logger.info("Send email to %s if you are having persistent trouble." % HELP_MAILTO)
            if time.time() >= expire_time:
                logger.warning("Cert updates have failed for the past %g hours." % options.maximum_age_hours)
                logger.info("Updates have failed for a long enough time that the failure is no "
                            "longer considered transient.")
                logger.info("This script will now exit unsuccessfully, triggering a notification.")
                logger.info(ADJUST_MAX_AGE_MESSAGE)
                raise
            else:
                logger.info("Updates have not failed for longer than %g hours." % options.maximum_age_hours)
                logger.info("Since updates have succeeded recently, this failure will be considered transient, ")
                logger.info("and will not trigger a notification for the admin.")
                logger.info("An update failure after %s will be considered a persistant error, "
                            "triggering a notification."
                            % (format_timestamp(expire_time)))
                logger.info(ADJUST_MAX_AGE_MESSAGE)
    else:
        logger.warning("Not updating until %s." % format_timestamp(next_update_time))
        logger.info("Since an update was performed in the past %g hours, "
                    "another update will not be performed at this time." % options.minimum_age_hours)
        logger.info("This is normal behavior.")
        logger.info(ADJUST_MIN_AGE_MESSAGE)

    return 0


def safe_main(argv=None):
    "Handle exceptions for real main function."
    try:
        exit_code = main(argv or sys.argv)
    except UsageError as err:
        print(str(err), file=sys.stderr)
        print("To see usage, run %s --help" % os.path.basename(sys.argv[0]), file=sys.stderr)
        exit_code = 2
    except SystemExit as err:
        if err.code == 2: # parser.error raises this
            print("To see usage, run %s --help" % os.path.basename(sys.argv[0]), file=sys.stderr)
        exit_code = err.code
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        exit_code = 3
    except UpdateError as err:
        logger.critical(str(err))
        if err.helpmsg:
            logger.info(err.helpmsg)
        logger.info(GENERIC_HELP_MESSAGE)
        exit_code = 1
    except Error as err:
        logger.critical(str(err))
        logger.debug(err.traceback)
        logger.info(GENERIC_HELP_MESSAGE)
        exit_code = 4
    except Exception as err:
        # We have to worry about the logger possibly not being initialized at
        # this point. Use the root logger if that's the case.
        if not logger_set_up:
            log = logging
        else:
            log = logger
        log.critical("Unhandled exception: %s", str(err))
        log.critical(traceback.format_exc())
        log.critical("Please send a bug report regarding this error with as "
                     "much information as you can provide about the "
                     "circumstances to %s", BUGREPORT_MAILTO)
        exit_code = 99

    return exit_code

if __name__ == "__main__":
    sys.exit(safe_main())

