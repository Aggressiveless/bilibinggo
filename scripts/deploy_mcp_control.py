"""Replace registered program files with a rollback receipt; preserve user-data."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def snapshot_config(home):
    config = home / "config"
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in config.iterdir() if p.is_file()} if config.is_dir() else {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "activate", "rollback", "verify"])
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    args = parser.parse_args()
    install, source, backup = args.install_root.resolve(), args.source.resolve(), args.backup.resolve()
    intended_workspace = Path(__file__).resolve().parents[2]
    for path in (install, source, backup):
        if path == intended_workspace or not path.is_relative_to(intended_workspace):
            raise RuntimeError("Deployment paths must remain inside the intended workspace")
    if backup.is_relative_to(install) or source.is_relative_to(install):
        raise RuntimeError("Source and backup must be outside the install directory")
    names = ["Binggo-RepostFix.exe", "_internal"]
    home = install / "user-data"
    receipt_path = backup / "receipt.json"
    if args.action == "prepare":
        if receipt_path.exists():
            raise RuntimeError("Backup receipt already exists")
        for name in names:
            if not (source / name).exists() or not (install / name).exists():
                raise RuntimeError("Incomplete source or installed program")
            if (install / name).resolve().parent != install:
                raise RuntimeError("Program path escapes the install directory")
        backup.mkdir(parents=True)
        baseline = snapshot_config(home)
        if (home / "config").exists():
            shutil.copytree(home / "config", backup / "config-backup")
        database = home / "data/binggo.db"
        if database.exists():
            with sqlite3.connect(str(database)) as original, sqlite3.connect(str(backup / "binggo.db")) as saved:
                original.backup(saved)
        receipt = {"created_at": datetime.now(timezone.utc).isoformat(), "install_root": str(install), "source": str(source),
                   "baseline_config_hashes": baseline, "state": "prepared"}
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        print(json.dumps({"state": "prepared", "original_config_files": len(baseline), "backup": str(backup)}))
        return
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt["install_root"] != str(install) or receipt["source"] != str(source):
        raise RuntimeError("Receipt does not match deployment paths")
    if args.action == "activate":
        if receipt["state"] != "prepared":
            raise RuntimeError("Receipt is not prepared")
        moved = backup / "program"
        moved.mkdir()
        for name in names:
            shutil.move(str(install / name), str(moved / name))
            if (source / name).is_dir():
                shutil.copytree(source / name, install / name)
            else:
                shutil.copy2(source / name, install / name)
        receipt["state"] = "activated"
    elif args.action == "rollback":
        failed = backup / "failed-program"
        failed.mkdir(exist_ok=True)
        for name in names:
            original = backup / "program" / name
            if original.exists():
                if (install / name).exists():
                    shutil.move(str(install / name), str(failed / name))
                shutil.move(str(original), str(install / name))
        receipt["state"] = "rolled_back"
    current = snapshot_config(home)
    unchanged = all(current.get(name) == value for name, value in receipt["baseline_config_hashes"].items())
    receipt["original_config_unchanged"] = unchanged
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({"state": receipt["state"], "original_config_unchanged": unchanged, "original_config_files": len(receipt["baseline_config_hashes"])}))
    if not unchanged:
        raise RuntimeError("Original configuration changed")


if __name__ == "__main__":
    main()
