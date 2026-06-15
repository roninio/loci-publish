"""Whitelist of C/C++ standard-library headers.

Used by the doomed-compile guard: if every non-stdlib #include resolves
to an -I path, we proceed. If none resolve, we fail fast.

Patterns for generated headers (e.g. `ti_*.h`, `syscfg_*.h`) are also
considered "resolved" when build_dir is known, since they're produced
next to the makefile.
"""
from __future__ import annotations

import fnmatch


C_HEADERS = frozenset({
    "assert.h", "complex.h", "ctype.h", "errno.h", "fenv.h", "float.h",
    "inttypes.h", "iso646.h", "limits.h", "locale.h", "math.h", "setjmp.h",
    "signal.h", "stdalign.h", "stdarg.h", "stdatomic.h", "stdbit.h",
    "stdbool.h", "stdckdint.h", "stddef.h", "stdint.h", "stdio.h",
    "stdlib.h", "stdnoreturn.h", "string.h", "tgmath.h", "threads.h",
    "time.h", "uchar.h", "wchar.h", "wctype.h",
    # POSIX and common system
    "unistd.h", "pthread.h", "sys/types.h", "sys/stat.h", "sys/time.h",
    "sys/socket.h", "sys/select.h", "sys/wait.h", "sys/mman.h",
    "arpa/inet.h", "netinet/in.h", "netinet/tcp.h", "netdb.h",
    "fcntl.h", "dirent.h", "dlfcn.h", "grp.h", "pwd.h",
    "syslog.h", "termios.h", "utime.h", "sched.h", "spawn.h",
    "semaphore.h", "strings.h", "poll.h", "sys/epoll.h", "sys/ioctl.h",
    "ifaddrs.h", "net/if.h",
})

CXX_HEADERS = frozenset({
    "algorithm", "any", "array", "atomic", "barrier", "bit", "bitset",
    "cassert", "ccomplex", "cctype", "cerrno", "cfenv", "cfloat",
    "charconv", "chrono", "cinttypes", "ciso646", "climits", "clocale",
    "cmath", "codecvt", "compare", "complex", "concepts", "condition_variable",
    "coroutine", "csetjmp", "csignal", "cstdalign", "cstdarg", "cstdbool",
    "cstddef", "cstdint", "cstdio", "cstdlib", "cstring", "ctgmath",
    "ctime", "cuchar", "cwchar", "cwctype", "deque", "exception",
    "execution", "expected", "filesystem", "flat_map", "flat_set",
    "format", "forward_list", "fstream", "functional", "future",
    "generator", "hazard_pointer", "initializer_list", "iomanip", "ios",
    "iosfwd", "iostream", "istream", "iterator", "latch", "limits",
    "list", "locale", "map", "mdspan", "memory", "memory_resource",
    "mutex", "new", "numbers", "numeric", "optional", "ostream", "print",
    "queue", "random", "ranges", "ratio", "rcu", "regex", "scoped_allocator",
    "semaphore", "set", "shared_mutex", "source_location", "span",
    "spanstream", "sstream", "stack", "stacktrace", "stdexcept",
    "stdfloat", "stop_token", "streambuf", "string", "string_view",
    "strstream", "syncstream", "system_error", "thread", "tuple",
    "type_traits", "typeindex", "typeinfo", "unordered_map", "unordered_set",
    "utility", "valarray", "variant", "vector", "version",
})

ALL_STDLIB = C_HEADERS | CXX_HEADERS

GENERATED_HEADER_PATTERNS = (
    "ti_*.h",
    "syscfg_*.h",
    "generated_*.h",
    "ti_drivers_config.h",
    "ti_*_config.h",
    "FreeRTOSConfig.h",
    "portable.h",
)


def is_stdlib(header: str) -> bool:
    return header in ALL_STDLIB


def is_generated(header: str) -> bool:
    name = header.rsplit("/", 1)[-1]
    for pat in GENERATED_HEADER_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


__all__ = ["is_stdlib", "is_generated", "ALL_STDLIB"]
