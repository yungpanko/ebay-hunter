"""Server mode keeps its data in the GitHub repo: pull before reading, commit and push after writing.

That way searches you build on your laptop reach the server, and finds, chats and learned taste reach the repo.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from pathlib import Path

from .config import Settings

log = logging.getLogger("hunter")
LOCK = threading.RLock()  # one writer at a time: the hunt loop and chat requests share the checkout


class GitSync:
    def __init__(self, settings: Settings, workdir: Path):
        self.s = settings
        self.dir = workdir

    @property
    def enabled(self) -> bool:
        return bool(self.s.git_repo and self.s.github_token)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(self.dir), *args], check=check, capture_output=True, text=True)

    def ensure_clone(self) -> None:
        if (self.dir / ".git").exists():
            return
        url = f"https://x-access-token:{self.s.github_token}@github.com/{self.s.git_repo}.git"
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--depth", "50", url, str(self.dir)], check=True, capture_output=True)
        self._git("config", "user.name", "ebay-hunter")
        self._git("config", "user.email", "ebay-hunter@users.noreply.github.com")

    def pull(self, wait: bool = True) -> None:
        """Bring in changes from GitHub. With wait=False, skip if a hunt is busy (pages shouldn't hang)."""
        if not LOCK.acquire(blocking=wait):
            return
        try:
            r = self._git("pull", "--rebase", "--autostash", "-q", check=False)
            if r.returncode != 0:
                log.warning("git pull failed: %s", r.stderr.strip()[:300])
                self._git("rebase", "--abort", check=False)
        finally:
            LOCK.release()

    def push(self, message: str) -> None:
        with LOCK:
            self._git("add", "data")
            if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
                return
            self._git("commit", "-q", "-m", message)
            for _ in range(3):
                if self._git("push", "-q", check=False).returncode == 0:
                    return
                self.pull()
            log.warning("git push failed after retries; will try again next time")
