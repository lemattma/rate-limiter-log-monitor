"""API traffic report: rate-limit violation detection over JSONL request logs."""

import sys

# The single place the interpreter floor is enforced. Both entry points that
# use this package -- report.py and the test modules -- reach it before they
# reach anything that could fail on an old interpreter, so one check here
# covers them with a clear sentence instead of a traceback. (tools/ is a
# stdlib-only dev helper that never imports this package, so it is not
# covered and does not need to be.)
# Kept to syntax that parses back to 2.7 so the message survives the very
# interpreters it exists to turn away. Apple's Command Line Tools ship Python
# 3.9.6, which is the floor this targets.
if sys.version_info < (3, 9):
    sys.stderr.write(
        "api-traffic-report requires Python 3.9 or newer (found %s).\n"
        % ".".join(str(part) for part in sys.version_info[:3])
    )
    raise SystemExit(2)

__version__ = "1.0.0"
