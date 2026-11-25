"""On-demand lossless camera preview shared by daemon and setup service."""
from pathlib import Path
import os
import time

ROOT = Path("/run/cozmo-roverd")

def requested(root=ROOT):
    try:
        return 0 <= time.time() - (root / "preview.request").stat().st_mtime < 2
    except OSError:
        return False

def publish_preview(frame, root=ROOT):
    target = root / "preview.png"
    if frame is None or not requested(root):
        target.unlink(missing_ok=True)
        return
    tmp = root / "preview.tmp"
    try:
        with tmp.open("wb") as stream:
            frame.save(stream, format="PNG", compress_level=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)

def request_preview(root=ROOT):
    try:
        (root / "preview.request").touch(mode=0o600)
        with (root / "preview.png").open("rb") as stream:
            if time.time() - os.fstat(stream.fileno()).st_mtime > 1:
                return None
            return stream.read()
    except OSError:
        return None
