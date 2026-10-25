"""采集 `errors.jsonl`：**真实运行**坏代码、原样收 stderr（SPEC §5.14）。

手写 traceback 与真实格式（缩进、`^^^^` 指示、链式异常、Windows 路径）有差异，
会让 `parse_traceback` 的准确率失真。本脚本只做一件事：

    write case.py → `python -I case.py`（cwd=临时目录）→ 收 stderr → 写 JSONL

用 cwd 内的相对文件名运行，于是 traceback 里是 `File "case.py"`，
不含本机临时目录与用户名。

    uv run python experiments/capture_error_dataset.py

输出：`src/pyassist/evals/datasets/errors.jsonl`（20 条，只增不改）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Final

DATASET: Final = (
    Path(__file__).resolve().parents[1] / "src" / "pyassist" / "evals" / "datasets" / "errors.jsonl"
)

# (id, topic, expected_error_type, code)。topic 与 expected 同名 —— 与 SPEC 示例一致。
CASES: Final[list[tuple[str, str, str, str]]] = [
    (
        "attribute-01",
        "AttributeError",
        "AttributeError",
        's = "abc"\ns.push("d")\n',
    ),
    (
        "type-01",
        "TypeError",
        "TypeError",
        'total = "3" + 4\n',
    ),
    (
        "name-01",
        "NameError",
        "NameError",
        "print(missing_count)\n",
    ),
    (
        "key-01",
        "KeyError",
        "KeyError",
        'scores = {"alice": 90}\nprint(scores["bob"])\n',
    ),
    (
        "index-01",
        "IndexError",
        "IndexError",
        "items = [10, 20, 30]\nprint(items[5])\n",
    ),
    (
        "value-01",
        "ValueError",
        "ValueError",
        'number = int("abc")\n',
    ),
    (
        "zero-division-01",
        "ZeroDivisionError",
        "ZeroDivisionError",
        "average = 10 / 0\n",
    ),
    (
        "unbound-local-01",
        "UnboundLocalError",
        "UnboundLocalError",
        "def show_total() -> None:\n    print(total)\n    total = 10\n\n\nshow_total()\n",
    ),
    (
        "import-01",
        "ModuleNotFoundError",
        "ModuleNotFoundError",
        "import requests_cache_helper\n",
    ),
    (
        "indentation-01",
        "IndentationError",
        "IndentationError",
        'def greet():\nprint("hello")\n',
    ),
    (
        "syntax-01",
        "SyntaxError",
        "SyntaxError",
        'print("hello"\n',
    ),
    (
        "file-not-found-01",
        "FileNotFoundError",
        "FileNotFoundError",
        'with open("missing_config.toml") as handle:\n    print(handle.read())\n',
    ),
    (
        "recursion-01",
        "RecursionError",
        "RecursionError",
        "import sys\n\n"
        "sys.setrecursionlimit(50)\n"
        "\n"
        "\n"
        "def countdown() -> None:\n"
        "    countdown()\n"
        "\n"
        "\n"
        "countdown()\n",
    ),
    (
        "json-decode-01",
        "JSONDecodeError",
        "JSONDecodeError",
        'import json\n\npayload = json.loads("{bad json}")\n',
    ),
    (
        "unicode-decode-01",
        "UnicodeDecodeError",
        "UnicodeDecodeError",
        'data = b"\\xff\\xfe"\ntext = data.decode("utf-8")\n',
    ),
    (
        "asyncio-01",
        "RuntimeError",
        "RuntimeError",
        "import asyncio\n\nloop = asyncio.get_running_loop()\n",
    ),
    (
        "stop-iteration-01",
        "StopIteration",
        "StopIteration",
        "values = iter([])\nfirst = next(values)\n",
    ),
    (
        "os-error-01",
        "OSError",
        "OSError",
        "import os\n\nos.close(999999)\n",
    ),
    (
        "permission-01",
        "PermissionError",
        "PermissionError",
        'with open(".", "w", encoding="utf-8") as handle:\n    handle.write("data")\n',
    ),
    (
        "overflow-01",
        "OverflowError",
        "OverflowError",
        "import math\n\nvalue = math.exp(1000)\n",
    ),
]


def _capture(code: str) -> str:
    """跑一次坏代码，返回 stderr（`\n` 换行、无尾随空行）。

    只做一处归一化：**去掉临时目录前缀**。语法错误（SyntaxError / IndentationError）
    由编译器报出，CPython 会写绝对路径；其余帧本来就只有 `File "case.py"`。
    去掉前缀后格式仍与真实 traceback 一致，但不把本机用户名与随机临时目录带进仓库。
    """
    with tempfile.TemporaryDirectory(prefix="pyassist-err-") as tmp:
        case_path = Path(tmp) / "case.py"
        case_path.write_text(code, encoding="utf-8")
        completed = subprocess.run(
            [sys.executable, "-I", "case.py"],
            cwd=tmp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        stderr = completed.stderr
        for prefix in (tmp + "\\", tmp + "/"):
            stderr = stderr.replace(prefix, "")
    return stderr.rstrip("\n")


def main() -> int:
    records: list[dict[str, str]] = []
    for record_id, topic, expected, code in CASES:
        error_text = _capture(code)
        if not error_text:
            print(f"{record_id}: 没有 stderr，用例写错了", file=sys.stderr)
            return 1
        last_line = error_text.splitlines()[-1]
        # `json.decoder.JSONDecodeError` 这类带模块前缀的异常，与 `parse_traceback`
        # 的归一化规则一致：只比最后一段。
        actual_type = last_line.split(":", 1)[0].rsplit(".", 1)[-1]
        if actual_type != expected:
            print(
                f"{record_id}: 期望 {expected}，实际最后一行是 {last_line!r}",
                file=sys.stderr,
            )
            return 1
        records.append(
            {
                "id": record_id,
                "topic": topic,
                "error_text": error_text,
                "broken_code": code.rstrip("\n"),
                "expected_error_type": expected,
            }
        )

    DATASET.parent.mkdir(parents=True, exist_ok=True)
    DATASET.write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(records)} records → {DATASET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
