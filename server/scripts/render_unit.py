#!/usr/bin/env python3
"""Fill the placeholders in a tracked systemd unit / logrotate file and print it.

The tracked files carry no machine paths; this renders them for one box:

    @ANTIGUA_DIR@   repo checkout on this box       (default: this repo)
    @WHISPER_DIR@   whisper.cpp build directory     (default: <repo>/../whisper.cpp)
    @OLLAMA_MODEL@  ollama.model from server.yaml   (falls back to server.example.yaml)
    @USER@, @UID@   service user and its uid        (default: whoever runs this)

Install, e.g.:
    server/scripts/render_unit.py server/antigua-server.service \\
        | sudo tee /etc/systemd/system/antigua-server.service >/dev/null
    sudo systemctl daemon-reload

deploy_fallback.sh renders the backup box's units with --dir set to its DEST.
"""

import argparse
import os
import pwd
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent.parent


def ollama_model() -> str:
    for name in ("server.yaml", "server.example.yaml"):
        path = REPO / "server" / "config" / name
        if path.exists():
            return yaml.safe_load(path.read_text())["ollama"]["model"]
    sys.exit("render_unit: no server/config/server.yaml or server.example.yaml")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("unit", type=Path)
    ap.add_argument("--dir", default=str(REPO), help="@ANTIGUA_DIR@ (default: %(default)s)")
    ap.add_argument("--whisper-dir", help="@WHISPER_DIR@ (default: <dir>/../whisper.cpp)")
    ap.add_argument("--user", default=os.environ.get("SUDO_USER") or pwd.getpwuid(os.getuid()).pw_name,
                    help="@USER@ (default: %(default)s); @UID@ is its uid")
    args = ap.parse_args()

    text = args.unit.read_text()
    whisper = args.whisper_dir or str(Path(args.dir).parent / "whisper.cpp")
    text = text.replace("@ANTIGUA_DIR@", args.dir).replace("@WHISPER_DIR@", whisper)
    if "@USER@" in text or "@UID@" in text:
        text = text.replace("@USER@", args.user).replace("@UID@", str(pwd.getpwnam(args.user).pw_uid))
    if "@OLLAMA_MODEL@" in text:
        text = text.replace("@OLLAMA_MODEL@", ollama_model())
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
