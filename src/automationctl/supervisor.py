"""Per-execution workload group owner that survives abrupt wrapper death."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys


def main() -> None:
    watch_fd, result_fd = int(sys.argv[1]), int(sys.argv[2])
    # Caught handlers are reset by exec in the workload. Keep watching the
    # wrapper during its TERM grace period, even if the wrapper dies then.
    signal.signal(signal.SIGTERM, lambda *_: None)
    # Popen created this supervisor as a session/group leader. The command
    # inherits its group, but cannot inherit the wrapper-liveness pipe.
    try:
        process = subprocess.Popen(sys.argv[3:], close_fds=True)
        reported = False
        while True:
            ready, _, _ = select.select([watch_fd], [], [], 0.1)
            if ready and not os.read(watch_fd, 1):
                break
            code = process.poll()
            if code is not None and not reported:
                os.write(result_fd, str(code).encode("ascii"))
                reported = True
            # Stay alive even after command exit: descendants still belong to
            # this group until the wrapper cleans it up or its pipe closes.
    finally:
        os.killpg(os.getpgrp(), signal.SIGKILL)


if __name__ == "__main__":
    main()
