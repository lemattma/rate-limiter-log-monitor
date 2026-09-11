# Importing the package under test runs the interpreter-floor check in
# traffic/__init__.py here, during discovery setup, rather than once per test
# module inside unittest's import handler -- which would bury the message under
# a failed-import traceback for every module.
import traffic  # noqa: F401
