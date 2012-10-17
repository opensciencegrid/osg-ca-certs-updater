#!/usr/bin/python
"""OSG Auto-Updater for CA Certificates"""
# pylint: disable=C0103
from optparse import OptionParser
import logging
import logging.handlers
import os
import random
import subprocess
import sys
import time
import traceback

__version__ = '@VERSION@'
MAINTAINER_EMAIL = "osg-software@opensciencegrid.org"

LASTRUN_TIMESTAMP_PATH = "/var/lib/osg-ca-certs-updater-lastrun"
PACKAGE_LIST = [
    "osg-ca-certs",
    "osg-ca-certs-compat",
    "igtf-ca-certs",
    "igtf-ca-certs-compat"
    ]

logger = logging.getLogger('updater')


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


class UpdateFailureError(Error):
    "Class for reporting a failure running yum update"
    def __init__(self, msg):
        if msg:
            Error.__init__(self, "Update failure: " + msg + "\n")
        else:
            Error.__init__(self, "Update failure\n")


def setup_logging(loglevel, logfile_path, log_to_syslog=False):
    """Set up the global 'log' object based on where the user wants to log to,
    e.g. syslog, or a file, or console.

    """
    global logger # pylint: disable=W0602
    logger.setLevel(loglevel)

    simple_formatter = logging.Formatter("%(message)s")
    detailed_formatter = logging.Formatter("osg-ca-certs-updater:%(asctime)s:%(levelname)s:%(message)s")
    if not logfile_path and not log_to_syslog:
        log_handler = logging.StreamHandler()
        log_formatter = simple_formatter
    else:
        log_formatter = detailed_formatter
        if log_to_syslog:
            log_handler = logging.handlers.SysLogHandler()
        else:
            log_handler = logging.FileHandler(logfile_path)

    log_handler.setLevel(loglevel)
    log_handler.setFormatter(log_formatter)
    logger.addHandler(log_handler)
    logger.propagate = False


def do_random_wait(random_wait_seconds):
    "Sleep for at most 'random_wait_seconds'. Ignore bad arguments."
    try:
        random_wait_seconds = int(random_wait_seconds)
        if random_wait_seconds < 0:
            raise ValueError()
    except (TypeError, ValueError):
        # int() raises TypeError if the arg is None, and ValueError if it cannot be converted to an int.
        logger.debug("Invalid value for random-wait. Not waiting.")
        return
    time_to_wait = random.randint(0, random_wait_seconds)
    logger.debug("Sleeping for %d seconds" % time_to_wait)
    time.sleep(time_to_wait)


def do_yum_update():
    """Use yum to update the packages in 'PACKAGE_LIST'. Return a bool
    for success/failure. Output from yum is logged.

    """
    yum_proc = subprocess.Popen(["yum", "update", "-y", "-q"] + PACKAGE_LIST, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    (yum_outerr, _) = yum_proc.communicate()
    yum_ret = yum_proc.returncode

    logger.info("Yum output: %s", yum_outerr)
    if yum_ret != 0:
        return False
    return True


def save_timestamp(timestamp_path, timestamp):
    """Write the timestamp (seconds since epoch) to a file.
    Log errors.

    """
    try:
        timestamp_handle = open(timestamp_path)
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
    the timestamp cannot be read. Ignore the file not existing, log other
    errors.

    """
    if not os.path.exists(timestamp_path):
        return None
    try:
        timestamp_handle = open(timestamp_path)
        try:
            timestamp = timestamp_handle.readline()
            return timestamp
        finally:
            timestamp_handle.close()
    except IOError, err:
        logger.error("Unable to load timestamp from %s: %s", timestamp_path, str(err))
        return None


def timestamp_to_str(timestamp):
    "The timestamp (seconds since epoch) as a human-readable string."
    return time.strftime("%c", time.localtime(timestamp))

def main(argv=None): # pylint:disable=R0911,R0912,R0915
    "Main function"
    if argv is None:
        argv = sys.argv
    try:
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
        parser.add_option("-v", "--verbose", action="store_const", const=logging.DEBUG, dest="loglevel", default=logging.INFO,
                          help="Display more information.")
        parser.add_option("-q", "--quiet", action="store_const", const=logging.ERROR, dest="loglevel", default=logging.INFO,
                          help="Only display errors.")
        parser.add_option("-l", "--logfile", metavar="PATH", default=None,
                          help="Write messages to this file instead of console.")
        parser.add_option("-s", "--syslog", action="store_true", dest="log_to_syslog", default=False,
                          help="Write messages to syslog instead of console.")

        options, _ = parser.parse_args(argv[1:])

        setup_logging(options.loglevel, options.logfile, options.log_to_syslog)

        lastrun_timestamp = get_lastrun_timestamp(LASTRUN_TIMESTAMP_PATH)
        if not lastrun_timestamp:
            logger.debug("No record of script having been run before")
        else:
            logger.debug("Last run %s" % timestamp_to_str(lastrun_timestamp))

        if not lastrun_timestamp or lastrun_timestamp > time.time() - options.minimum_age_hours * 3600:
            do_random_wait(options.random_wait_minutes * 60)
            ret = do_yum_update()
            if ret:
                new_timestamp = time.time()
                logger.info("Update succeeded at %s" % timestamp_to_str(new_timestamp))
                save_timestamp(LASTRUN_TIMESTAMP_PATH, new_timestamp)
            else:
                logger.info("Update failed at %s" % timestamp_to_str(time.time()))
                if not lastrun_timestamp or lastrun_timestamp > time.time() - options.maximum_age_hours * 3600:
                    raise UpdateFailureError()
                else:
                    logger.info("Error considered transient until %s" %
                             (timestamp_to_str(lastrun_timestamp + options.maximum_age_hours * 3600)))
        else:
            logger.info("Already updated in the past %d hours. Not updating again until %s." %
                     (options.minimum_age_hours, timestamp_to_str(lastrun_timestamp + options.minimum_age_hours)))
            
                         

    except UsageError, err:
        parser.print_help()
        print >> sys.stderr, str(err)
        return 2
    except SystemExit, err:
        return err.code
    except KeyboardInterrupt:
        print >> sys.stderr, "Interrupted"
        return 3
    except UpdateFailureError, err:
        logging.error(str(err))
        return 1
    except Error, err:
        logging.critical(str(err))
        logging.debug(err.traceback)
        return 1
    except Exception, err:
        logging.critical("Unhandled exception: %s", str(err))
        logging.critical(traceback.format_exc())
        logging.critical("Please send a bug report regarding this error with as "
                         "much information as you can provide about the "
                         "circumstances to %s", MAINTAINER_EMAIL)
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())

