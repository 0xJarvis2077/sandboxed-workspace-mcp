from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    writer = (
        "import time\n"
        "from pathlib import Path\n"
        "p=Path('/artifacts/heartbeat')\n"
        "for i in range(200):\n"
        "    with p.open('ab') as h: h.write(b'x')\n"
        "    time.sleep(0.05)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", writer],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path("/artifacts/child.pid").write_text(f"{child.pid}\n", encoding="utf-8")
    try:
        time.sleep(30.0)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1.0)


if __name__ == "__main__":
    main()
