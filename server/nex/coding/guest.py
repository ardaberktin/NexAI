"""Bootstrap executed by the system Python inside the disposable Linux namespace."""

import ctypes
import errno
import resource
import runpy
import sys
from pathlib import Path

READY = "Nex Python sandbox ready"
MIB = 1024 * 1024

# libseccomp resolves names for the running architecture. The guest needs ordinary
# files, memory, clocks and signal handling. Socket, exec and clone are omitted.
SYSCALLS = ["read", "write", "readv", "writev", "pread64", "pwrite64", "close", "lseek", "fstat", "newfstatat", "stat", "lstat", "statx", "statfs", "fstatfs", "open", "openat", "access", "faccessat", "faccessat2", "getdents64", "getcwd", "chdir", "fchdir", "readlink", "readlinkat", "mkdir", "mkdirat", "rmdir", "unlink", "unlinkat", "rename", "renameat", "renameat2", "link", "linkat", "symlink", "symlinkat", "chmod", "fchmod", "fchmodat", "umask", "utimensat", "ftruncate", "truncate", "fsync", "fdatasync", "flock", "fcntl", "dup", "dup2", "dup3", "mmap", "mremap", "munmap", "mprotect", "madvise", "msync", "brk", "clock_gettime", "clock_nanosleep", "nanosleep", "gettimeofday", "time", "times", "getrusage", "getrlimit", "prlimit64", "getpid", "getppid", "gettid", "getuid", "geteuid", "getgid", "getegid", "getgroups", "uname", "getrandom", "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack", "futex", "sched_yield", "sched_getaffinity", "poll", "ppoll", "select", "pselect6", "exit", "exit_group"]


def restrict_syscalls():
    lib = ctypes.CDLL("libseccomp.so.2")
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    deny = 0x00050000 | errno.EPERM  # SCMP_ACT_ERRNO
    allow = 0x7FFF0000  # SCMP_ACT_ALLOW
    context = lib.seccomp_init(deny)
    if not context:
        raise RuntimeError("Could not initialize seccomp")
    try:
        for name in SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if number >= 0 and lib.seccomp_rule_add(context, allow, number, 0) != 0:
                raise RuntimeError("Could not configure seccomp")
        if lib.seccomp_load(context) != 0:
            raise RuntimeError("Could not load seccomp")
    finally:
        lib.seccomp_release(context)


def main():
    for kind, soft, hard in (
        (resource.RLIMIT_AS, 256 * MIB, 256 * MIB),
        (resource.RLIMIT_FSIZE, 8 * MIB, 8 * MIB),
        (resource.RLIMIT_CPU, 4, 5),
        (resource.RLIMIT_NOFILE, 32, 32),
        (resource.RLIMIT_NPROC, 1, 1),
        (resource.RLIMIT_CORE, 0, 0),
    ):
        resource.setrlimit(kind, (soft, hard))
    restrict_syscalls()
    print(READY, flush=True)
    script = Path("/work/main.py")
    script.write_bytes(sys.stdin.buffer.read())
    sys.argv = [str(script)]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
