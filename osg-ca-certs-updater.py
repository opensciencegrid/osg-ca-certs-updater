#!/usr/bin/python
"""OSG Auto-Updater for CA Certificates"""
# pylint: disable=C0103,W0603
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

__version__ = '@VERSION@'
PROGRAM_NAME = "osg-ca-certs-updater"
MAINTAINER_EMAIL = "osg-software@opensciencegrid.org"

LASTRUN_TIMESTAMP_PATH = "/var/lib/osg-ca-certs-updater-lastrun"
PACKAGE_LIST = [
    "osg-ca-certs",
    "osg-ca-certs-compat",
    "igtf-ca-certs",
    "igtf-ca-certs-compat"
    ]

logger = logging.getLogger('updater')
logger_set_up = False


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
    "Class for reporting some failure performing the update"
    def __init__(self, msg=None):
        if msg:
            Error.__init__(self, "Update failure: " + msg + "\n")
        else:
            Error.__init__(self, "Update failure\n")


def setup_logger(loglevel, logfile_path, log_to_syslog, syslog_address, syslog_facility):
    """Set up the global 'logger' object based on where the user wants to log to,
    e.g. syslog, or a file, or console.

    """
    global logger # pylint: disable=W0602
    global logger_set_up
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
            log_handler = logging.FileHandler(logfile_path)

    log_handler.setLevel(loglevel)
    log_handler.setFormatter(log_formatter)
    logger.addHandler(log_handler)
    logger.propagate = False

    logger_set_up = True


def do_random_wait(random_wait_seconds):
    "Sleep for at most 'random_wait_seconds'. Ignore bad arguments."
    try:
        random_wait_seconds = int(random_wait_seconds)
        if random_wait_seconds < 0:
            raise ValueError()
    except (TypeError, ValueError):
        # int() raises TypeError if the arg is None, and ValueError if it
        # cannot be converted to an int.
        logger.debug("Invalid value for random-wait. Not waiting.")
        return
    if random_wait_seconds < 1:
        return
    time_to_wait = random.randint(1, random_wait_seconds)
    logger.debug("Waiting for %d seconds" % time_to_wait)
    time.sleep(time_to_wait)


def do_yum_update(package_list):
    """Use yum to update the packages in 'package_list'. Return a bool
    for success/failure. Output from yum is logged.

    """
    yum_proc = subprocess.Popen(["yum", "update", "-y", "-q"] + package_list,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (yum_outerr, _) = yum_proc.communicate()
    yum_ret = yum_proc.returncode

    if re.search(r'\S', yum_outerr):
        logger.info("Yum output: %s", yum_outerr)
    if yum_ret != 0:
        raise UpdateError()


def verify_requirement_available(requirement):
    """Use repoquery to ensure that an rpm requirement matching 'requirement'
    is available in an external repository. This allows us to detect the case
    when the user's osg repositories are disabled, thus never having updates
    available for their certs. yum does not detect this case.

    Using 'requirement' instead of 'package' to allow us to rename cert
    packages without breaking this test in the future.

    """
    repoquery_proc = subprocess.Popen(
        ["repoquery", "--whatprovides", requirement, "--queryformat=%{repoid}"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    (repoquery_out, repoquery_err) = repoquery_proc.communicate()
    repoquery_ret = repoquery_proc.returncode

    if repoquery_ret != 0:
        raise Error("Unable to query repository. Repoquery error:\n%s" % (repoquery_err))
    # Aside from the case of an error, repoquery_out now contains a newline-
    # separated list of repos providing each matching package.
    # 'installed' is a "repo" that contains all installed packages.
    # Only want external repos, so filter that out. (Also filter blank lines).
    external_repos_providing = []
    for repo in repoquery_out.split("\n"):
        if re.search(r'\S', repo) and -1 == repo.find('installed'):
            external_repos_providing.append(repo)

    # For now, considering this a fatal error.
    if not external_repos_providing:
        raise Error("No external repos provide %s. Ensure required yum repos are enabled." % (requirement))    


def save_timestamp(timestamp_path, timestamp):
    """Write the timestamp (seconds since epoch) to a file.
    Failing to write is not fatal but should be logged.

    """
    try:
        logger.debug("Writing new timestamp %s", format_timestamp(timestamp))
        timestamp_handle = open(timestamp_path, 'w')
        try:
            print >> timestamp_handle, "%d\n" % timestamp
            return True
        finally:
            timestamp_handle.close()
    except IOError, err:
        logger.error("Unable to save timestamp to %s: %s", timestamp_path, str(err))
        return False


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
            return float(timestamp)
        finally:
            timestamp_handle.close()
    except (IOError, ValueError), err:
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
        next_update_time = lastrun_timestamp + minimum_age_hours * 3600
        expire_time = lastrun_timestamp + maximum_age_hours * 3600

    logger.debug("Next update time: %s" % format_timestamp(next_update_time))
    logger.debug("Expire time: %s" % format_timestamp(expire_time))

    return (next_update_time, expire_time)


def format_timestamp(timestamp):
    "The timestamp (seconds since epoch) as a human-readable string."
    return time.strftime("%c", time.localtime(float(timestamp)))


def get_options(args):    
    "Parse, validate, and transform command-line options."
    parser = OptionParser("""
%prog [options]
""")
    parser.add_option("-a", "--minimum-age", metavar="HOURS", dest="minimum_age_hours",
                      help="The time which must have elapsed since the last "
                      "successful run before attempting an update. If not "
                      "supplied or 0, always update.",
                      default=0)
    parser.add_option("-x", "--maximum-age", metavar="HOURS", dest="maximum_age_hours",
                      help="The time after the last successful run after "
                      "which an unsuccessful run is considered an error. "
                      "If not supplied or 0, all unsuccessful runs are "
                      "considered errors.",
                      default=0)
    parser.add_option("-r", "--random-wait", metavar="MINUTES", dest="random_wait_minutes",
                      help="Delay for at most this many minutes before "
                      "running an update, to reduce load spikes on update "
                      "servers. If not supplied or 0, update immediately.",
                      default=0)
    parser.add_option("-v", "--verbose", action="store_const",
                      const=logging.DEBUG, dest="loglevel", default=logging.INFO,
                      help="Display more information.")
    parser.add_option("-q", "--quiet", action="store_const",
                      const=logging.ERROR, dest="loglevel", default=logging.INFO,
                      help="Only display errors.")
    parser.add_option("-l", "--logfile", metavar="PATH", default=None,
                      help="Write messages to the given file instead of console.")
    parser.add_option("-s", "--log-to-syslog", action="store_true", default=False,
                      help="Write messages to syslog instead of console.")
    parser.add_option("--syslog-address", default="/dev/log",
                      help="Address to use for syslog. Can be a file "
                      "(e.g. /dev/log) or an address:port combination. "
                      "Default is '%default'.")
    parser.add_option("--syslog-facility", default="user",
                      help="The syslog facility to log to. "
                      "Default is '%default'.")

    options, _ = parser.parse_args(args)

    try:
        options.minimum_age_hours = int(options.minimum_age_hours)
    except ValueError:
        raise UsageError("The value for minimum-age must be an integer number of hours.")
    try:
        options.maximum_age_hours = int(options.maximum_age_hours)
    except ValueError:
        raise UsageError("The value for maximum-age must be an integer number of hours.")
    try:
        options.random_wait_minutes = int(options.random_wait_minutes)
    except ValueError:
        raise UsageError("The value for random-wait must be an integer number of minutes.")

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
        do_random_wait(options.random_wait_minutes * 60)
        for pkg in PACKAGE_LIST:
            verify_requirement_available(pkg)
        try:
            do_yum_update(PACKAGE_LIST)
            logger.info("Update succeeded")
            save_timestamp(LASTRUN_TIMESTAMP_PATH, time.time())
        except UpdateError, err:
            if time.time() >= expire_time:
                raise UpdateError("Escalated to error")
            else:
                logger.warning("Update error. Considered transient until %s" %
                               (format_timestamp(expire_time)))
    else:
        logger.info("Already updated in the past %d hours. Not updating again until %s." %
                    (options.minimum_age_hours, format_timestamp(next_update_time)))

    return 0


def safe_main(argv=None):
    "Handle exceptions for real main function."
    try:
        exit_code = main(argv or sys.argv)
    except UsageError, err:
        print >> sys.stderr, str(err)
        print >> sys.stderr, "To see usage, run %s --help" % argv[0]
        exit_code = 2
    except SystemExit, err:
        exit_code = err.code
    except KeyboardInterrupt:
        print >> sys.stderr, "Interrupted"
        exit_code = 3
    except UpdateError, err:
        logger.critical(str(err))
        exit_code = 1
    except Error, err:
        logger.critical(str(err))
        logger.debug(err.traceback)
        exit_code = 4
    except Exception, err:
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
                     "circumstances to %s", MAINTAINER_EMAIL)
        exit_code = 99

    return exit_code

if __name__ == "__main__":
    sys.exit(safe_main())

