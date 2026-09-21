from contextlib import ExitStack, contextmanager
from unittest.mock import patch


@contextmanager
def forbid_live_provider():
    """Block real provider entry points, including aliases and worker threads."""
    targets = ("CopilotBackend.__init__", "CopilotBackend.generate", "find_copilot", "auth_environment")
    with ExitStack() as stack:
        guards = [stack.enter_context(patch("session_spec.backend." + target,
                                           side_effect=AssertionError("Offline tests forbid real provider entry: " + target)))
                  for target in targets]
        yield
        for guard in guards:
            guard.assert_not_called()


def guard_offline_test(test):
    guard = forbid_live_provider()
    guard.__enter__()
    test.addCleanup(guard.__exit__, None, None, None)
