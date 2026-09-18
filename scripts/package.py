"""只打包发布所需文件，不携带开发环境、登录信息或实际配置。"""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    files = [
        root / name
        for name in (
            "plugin.py",
            "_manifest.json",
            "config.example.toml",
            "pyproject.toml",
            "requirements.txt",
            "uv.lock",
            "README.md",
            "CHANGELOG.md",
            "LICENSE",
            ".gitignore",
        )
    ]
    for directory in ("character_guess", "_locales", "tests", "scripts"):
        files.extend(
            path
            for path in (root / directory).rglob("*")
            if path.is_file() and path.suffix in {".py", ".json"} and "__pycache__" not in path.parts
        )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, Path(root.name) / path.relative_to(root))
    with ZipFile(output) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"打包校验失败：{bad}")
        print(f"已打包 {len(archive.namelist())} 个文件：{output}")


if __name__ == "__main__":
    main()
