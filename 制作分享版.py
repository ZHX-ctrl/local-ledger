"""Build a clean standalone copy without any local ledger data."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import zipfile


PROJECT = Path(__file__).resolve().parent
OUTPUT_DIR = PROJECT / "dist"
PACKAGE_ROOT = "双币账本-独立版"

# Keep this list explicit.  Never discover files recursively from the project,
# because private/ contains the owner's real ledger, photos and backups.
FILES = (
    "run_ledger.py",
    "启动记账.bat",
    "分享版使用说明.txt",
    ".gitignore",
    ".c2cignore",
    "ledger_app/__init__.py",
    "ledger_app/attachments.py",
    "ledger_app/backup.py",
    "ledger_app/cli.py",
    "ledger_app/fx.py",
    "ledger_app/money.py",
    "ledger_app/schema.sql",
    "ledger_app/service.py",
    "ledger_app/web.py",
    "ledger_app/static/app.js",
    "ledger_app/static/index.html",
    "ledger_app/static/styles.css",
    "ledger_app/static/fonts/InterVariable.woff2",
    "ledger_app/static/fonts/OFL.txt",
)

FORBIDDEN_PARTS = {"private", "data", "photos", "backups", "exports", "work", ".git"}
FORBIDDEN_SUFFIXES = {
    ".sqlite", ".sqlite3", ".db", ".csv", ".zip",
    ".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf",
}


def validate_member(relative: Path) -> None:
    lowered_parts = {part.casefold() for part in relative.parts}
    if lowered_parts & FORBIDDEN_PARTS:
        raise RuntimeError(f"禁止打包私有目录：{relative}")
    if relative.suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise RuntimeError(f"禁止打包数据文件：{relative}")
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"非法打包路径：{relative}")


def build() -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    destination = OUTPUT_DIR / f"双币账本-独立版-{date.today():%Y%m%d}.zip"

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in FILES:
            relative = Path(item)
            validate_member(relative)
            source = PROJECT / relative
            if not source.is_file():
                raise FileNotFoundError(f"缺少分享版文件：{relative}")
            archive.write(source, (Path(PACKAGE_ROOT) / relative).as_posix())

    with zipfile.ZipFile(destination, "r") as archive:
        bad = archive.testzip()
        if bad is not None:
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"压缩包校验失败：{bad}")
        for name in archive.namelist():
            member = Path(name)
            if not member.parts or member.parts[0] != PACKAGE_ROOT:
                raise RuntimeError(f"压缩包包含异常路径：{name}")
            validate_member(Path(*member.parts[1:]))

    return destination


if __name__ == "__main__":
    print(build())
