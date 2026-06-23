from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TextIO

from academic_core.zdbk_client import ZdbkClient, ZdbkError


TERM_BY_NAME = {
    "autumn": 0,
    "winter": 1,
    "spring": 2,
    "summer": 3,
}


def main(
    argv: list[str] | None = None,
    *,
    client_factory=ZdbkClient,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = argparse.ArgumentParser(description="Run a read-only ZDBK smoke check.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--academic-year", required=True)
    parser.add_argument("--term", choices=sorted(TERM_BY_NAME), required=True)

    try:
        args = parser.parse_args(argv)
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        username = _cfg_str(config, "username")
        password = _cfg_str(config, "password")
        if not username or not password:
            raise ZdbkError("auth_credentials", "配置中缺少统一身份认证账号或密码。")

        client = client_factory(username, password)
        client.login()
        classes = client.get_classes(args.academic_year, TERM_BY_NAME[args.term])
        exams = client.get_exams()

        http_status = dict(getattr(client, "last_http_status", {}))
        raw_counts = dict(getattr(client, "last_raw_counts", {}))
        converted_counts = dict(getattr(client, "last_converted_counts", {}))
        converted_counts.setdefault("classes", len(classes))
        converted_counts.setdefault("exams", len(exams))

        _write_json(
            stdout,
            {
                "status": "ok",
                "http_status": http_status,
                "raw_counts": raw_counts,
                "converted_counts": converted_counts,
            },
        )
        return 0
    except ZdbkError as exc:
        _write_json(
            stdout,
            {
                "status": "error",
                "error_code": exc.code,
                "message": exc.user_message,
            },
        )
        return 1
    except SystemExit as exc:
        return int(exc.code or 0)
    except Exception:
        _write_json(
            stdout,
            {
                "status": "error",
                "error_code": "unexpected",
                "message": "ZDBK 只读验证失败。",
            },
        )
        return 1


def _cfg_str(config: dict[str, Any], key: str) -> str:
    value = _cfg_value(config, key, "")
    return str(value).strip() if value is not None else ""


def _cfg_value(config: dict[str, Any], key: str, default: Any) -> Any:
    if key in config:
        return config.get(key, default)
    for group_key in ("basic", "advanced"):
        group = config.get(group_key)
        if isinstance(group, dict) and key in group:
            return group.get(key, default)
    return default


def _write_json(stdout: TextIO, payload: dict[str, Any]) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
