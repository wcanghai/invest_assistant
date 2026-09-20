"""检查本项目 Python 行长与函数说明，并报告语法错误。"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    # 只检查交付源码、测试和新增维护工具，历史工具保持原有验证范围。
    files = list((ROOT / "src").rglob("*.py")) + list((ROOT / "tests").rglob("*.py"))
    files += [ROOT / "scripts" / name for name in (
        "check_python_style.py", "relocate_modules.py", "verify_module_cutover.py")]
    issues = []
    for path in files:
        source = path.read_text("utf-8-sig")
        lines = source.splitlines()
        for number, line in enumerate(lines, 1):
            if len(line) > 100:
                issues.append(f"{path.relative_to(ROOT)}:{number}: 超过 100 字符")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            header = lines[node.lineno:node.body[0].lineno - 1]
            if not any(line.strip().startswith("#") for line in header):
                issues.append(f"{path.relative_to(ROOT)}:{node.lineno}: 缺少函数说明")
    if issues:
        raise SystemExit("\n".join(issues))
    print(f"Python 编码规范检查通过：{len(files)} 个文件")


if __name__ == "__main__":
    main()
