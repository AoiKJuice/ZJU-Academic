import io
import json
import tempfile
import unittest
from pathlib import Path

from academic_core.zdbk_client import ZdbkError
from scripts import zdbk_smoke


class SuccessfulClient:
    def __init__(self, username, password, timeout=20):
        self.username = username
        self.password = password
        self.timeout = timeout
        self.last_http_status = {}
        self.last_raw_counts = {}
        self.last_converted_counts = {}

    def login(self):
        self.last_http_status["login"] = 200

    def get_classes(self, academic_year, term):
        self.academic_year = academic_year
        self.term = term
        self.last_http_status["classes"] = 200
        self.last_raw_counts["classes"] = 3
        self.last_converted_counts["classes"] = 2
        return [{"id": "class-1"}, {"id": "class-2"}]

    def get_exams(self):
        self.last_http_status["exams"] = 200
        self.last_raw_counts["exams"] = 1
        self.last_converted_counts["exams"] = 1
        return [{"id": "exam-1"}]


class ErrorClient(SuccessfulClient):
    def get_classes(self, academic_year, term):
        raise ZdbkError(
            "captcha_required",
            "教务系统要求验证码，当前版本暂不支持自动填写。",
            "username=student password=secret Cookie=abc body=xnm=2025-2026",
        )


class ZdbkSmokeTest(unittest.TestCase):
    def write_config(self, directory, payload):
        path = Path(directory) / "config.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_smoke_success_outputs_only_status_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.write_config(
                tmp,
                {"basic": {"username": "student", "password": "secret"}},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = zdbk_smoke.main(
                [
                    "--config",
                    str(config),
                    "--academic-year",
                    "2025-2026",
                    "--term",
                    "summer",
                ],
                client_factory=SuccessfulClient,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(
            set(payload),
            {"status", "http_status", "raw_counts", "converted_counts"},
        )
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["http_status"]["classes"], 200)
        self.assertEqual(payload["raw_counts"]["classes"], 3)
        self.assertEqual(payload["converted_counts"]["classes"], 2)
        combined_output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("student", combined_output)
        self.assertNotIn("secret", combined_output)
        self.assertNotIn("Cookie", combined_output)
        self.assertNotIn("xnm=2025-2026", combined_output)

    def test_smoke_error_output_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self.write_config(
                tmp,
                {"username": "student", "password": "secret"},
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            exit_code = zdbk_smoke.main(
                [
                    "--config",
                    str(config),
                    "--academic-year",
                    "2025-2026",
                    "--term",
                    "summer",
                ],
                client_factory=ErrorClient,
                stdout=stdout,
                stderr=stderr,
            )

        self.assertNotEqual(exit_code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_code"], "captcha_required")
        self.assertIn("验证码", payload["message"])
        combined_output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("student", combined_output)
        self.assertNotIn("secret", combined_output)
        self.assertNotIn("Cookie", combined_output)
        self.assertNotIn("body=", combined_output)


if __name__ == "__main__":
    unittest.main()
