# Auto-generated stub for the Unix-only 'resource' module.
# Provides no-op implementations so packages that conditionally use
# resource (e.g. fsspec, angr) don't crash on Windows.

# Common error type
error = OSError

# --- Resource limit constants ---
RLIMIT_AS = 9
RLIMIT_CORE = 4
RLIMIT_CPU = 0
RLIMIT_DATA = 2
RLIMIT_FSIZE = 1
RLIMIT_MEMLOCK = 8
RLIMIT_NOFILE = 7
RLIMIT_NPROC = 6
RLIMIT_RSS = 5
RLIMIT_STACK = 3
RLIM_INFINITY = -1

# --- Resource usage constants ---
RUSAGE_SELF = 0
RUSAGE_CHILDREN = -1


def getrlimit(resource):
    """Return (soft, hard) limits.  On Windows, report unlimited."""
    return (RLIM_INFINITY, RLIM_INFINITY)


def setrlimit(resource, limits):
    """No-op on Windows — resource limits are not supported."""
    pass


class _RUsage:
    """Minimal struct_rusage stand-in."""
    ru_utime = 0.0
    ru_stime = 0.0
    ru_maxrss = 0
    ru_ixrss = 0
    ru_idrss = 0
    ru_isrss = 0
    ru_minflt = 0
    ru_majflt = 0
    ru_nswap = 0
    ru_inblock = 0
    ru_oublock = 0
    ru_msgsnd = 0
    ru_msgrcv = 0
    ru_nsignals = 0
    ru_nvcsw = 0
    ru_nivcsw = 0


def getrusage(who):
    """Return a zero-filled usage struct on Windows."""
    return _RUsage()


def getpagesize():
    """Return a sensible default page size."""
    return 4096
