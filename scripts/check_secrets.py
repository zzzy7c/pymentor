#!/usr/bin/env python
"""提交前的密钥自检：扫描即将提交的文件，发现疑似密钥就拦截提交。

用法：
    python scripts/check_secrets.py            # 检查 git 暂存区
    python scripts/check_secrets.py --all      # 检查整个工作区（排除 .gitignore 命中的文件）

作为提交钩子安装（一次即可）：
    cp scripts/check_secrets.py .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit

退出码 0 = 未发现密钥，1 = 发现疑似密钥。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 敏感文件名：一旦出现在待提交列表中就报错
FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", "secrets.toml", "credentials.json"}

# 疑似密钥内容特征
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("OpenAI/DeepSeek 风格 sk- 密钥", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("Anthropic 密钥", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("私钥文件头", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "硬编码赋值（key/token/secret/password）",
        re.compile(
            r"""(?i)\b(api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|password)\b"""
            r"""\s*[:=]\s*["']([^"'\s]{12,})["']"""
        ),
    ),
]

# 允许的占位值：命中这些不算泄密
PLACEHOLDER = re.compile(r"(?i)(xxx+|your[_-]?|example|placeholder|changeme|<[^>]+>|\$\{|\{\{|os\.environ|getenv)")

# 不扫描的路径片段
SKIP_PARTS = ("scripts/check_secrets.py", ".env.example", ".env.template", ".gitignore")


def staged_files() -> list[Path]:
    """返回本次提交涉及的文件（暂存区 + 新增但未跟踪的文件）。"""
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=False,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=False,
    ).stdout
    names = {line.strip() for line in (out + "\n" + untracked).splitlines() if line.strip()}
    return [Path(n) for n in sorted(names)]


def scan(path: Path) -> list[str]:
    problems: list[str] = []
    name = path.as_posix()

    if path.name in FORBIDDEN_NAMES or path.name.startswith(".env") and path.name != ".env.example":
        problems.append("敏感文件本身不应提交")
        return problems

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except (OSError, UnicodeDecodeError):
        return problems

    for lineno, line in enumerate(text.splitlines(), start=1):
        if PLACEHOLDER.search(line):
            continue
        for label, pattern in PATTERNS:
            match = pattern.search(line)
            if match:
                shown = match.group(0)[:40]
                problems.append(f"第 {lineno} 行疑似{label}：{shown}…")
    return problems

def main() -> int:
    # Windows 控制台默认 GBK，中文提示会乱码
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    targets = staged_files()
    if not targets:
        print("没有待提交的文件，跳过检查。")
        return 0

    failed = False
    for path in targets:
        if any(part in path.as_posix() for part in SKIP_PARTS):
            continue
        if not path.exists():
            continue
        for problem in scan(path):
            failed = True
            print(f"[!] {path} -> {problem}")

    if failed:
        print("\n发现疑似密钥，已阻止提交。")
        print("如确为误报，请用 git commit --no-verify 跳过（请谨慎）。")
        return 1

    print(f"已检查 {len(targets)} 个文件，未发现密钥。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
