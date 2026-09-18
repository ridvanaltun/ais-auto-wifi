"""login_item.py — "Open at Login" state handling (SMAppService is faked)."""

import unittest
from unittest import mock

from aiswifi import login_item

try:
    import ServiceManagement  # noqa: F401
    HAVE_SM = True
except ImportError:
    HAVE_SM = False


class _Error:
    def localizedDescription(self):
        return "Operation not permitted"


class LoginItemTests(unittest.TestCase):
    def test_unavailable_when_not_running_as_the_app(self):
        with mock.patch.object(login_item, "running_as_app", return_value=False):
            self.assertEqual(login_item.status(), login_item.UNAVAILABLE)
            self.assertEqual(login_item.set_enabled(True), (login_item.UNAVAILABLE, None))

    @unittest.skipUnless(HAVE_SM, "pyobjc-framework-ServiceManagement not installed")
    def test_status_and_errors(self):
        service = mock.Mock()
        with mock.patch.object(login_item, "_service", return_value=service):
            for raw, expected in ((0, login_item.DISABLED), (1, login_item.ENABLED),
                                  (2, login_item.REQUIRES_APPROVAL), (3, login_item.DISABLED)):
                service.status.return_value = raw
                self.assertEqual(login_item.status(), expected)

            service.status.return_value = 1
            service.registerAndReturnError_.return_value = (True, None)
            self.assertEqual(login_item.set_enabled(True), (login_item.ENABLED, None))

            service.status.return_value = 1
            service.unregisterAndReturnError_.return_value = (False, _Error())
            with self.assertLogs("aiswifi.login_item", level="ERROR"):
                self.assertEqual(login_item.set_enabled(False),
                                 (login_item.ENABLED, "Operation not permitted"))


if __name__ == "__main__":
    unittest.main()
