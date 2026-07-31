"""Version derivation.

The release version is computed from git tags plus conventional commits
(ADR-016). A bug here silently ships the wrong version number to devices that
update themselves, so it is worth testing against a real repository rather than
by inspection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "next_version.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _commit(repo: Path, subject: str, body: str = "") -> None:
    message = f"{subject}\n\n{body}" if body else subject
    (repo / "file.txt").write_text(subject)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _run(repo: Path, *args: str) -> dict[str, str]:
    done = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return dict(line.split("=", 1) for line in done.stdout.strip().splitlines() if "=" in line)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@invalid")
    _git(tmp_path, "config", "user.name", "test")
    _commit(tmp_path, "chore: initial")
    return tmp_path


class TestBumps:
    def test_no_tags_starts_at_0_1_0(self, repo):
        """The project exists, which is a feature."""
        result = _run(repo)
        assert result["version"] == "0.1.0"
        assert result["bump"] == "initial"

    def test_feat_bumps_minor(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "feat(allocator): add hysteresis")

        result = _run(repo)
        assert result["version"] == "1.5.0"
        assert result["bump"] == "minor"

    def test_fix_bumps_patch(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "fix(discovery): key on serial")

        result = _run(repo)
        assert result["version"] == "1.4.3"
        assert result["bump"] == "patch"

    def test_bang_bumps_major(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "feat(tunnel)!: new protocol")

        result = _run(repo)
        assert result["version"] == "2.0.0"
        assert result["bump"] == "major"

    def test_breaking_change_footer_bumps_major(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "fix(tunnel): rework handshake", "BREAKING CHANGE: protocol changed")

        result = _run(repo)
        assert result["version"] == "2.0.0"
        assert result["bump"] == "major"

    def test_highest_bump_in_the_range_wins(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "fix: something small")
        _commit(repo, "feat: something new")
        _commit(repo, "docs: a note")

        assert _run(repo)["version"] == "1.5.0"

    def test_non_conventional_commits_fall_back_to_patch(self, repo):
        """CI rejects these on a PR, but the script must not produce nonsense."""
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "wip")

        assert _run(repo)["version"] == "1.4.3"

    def test_only_commits_since_the_last_tag_are_considered(self, repo):
        _commit(repo, "feat: old feature")
        _git(repo, "tag", "-a", "v2.0.0", "-m", "release")
        _commit(repo, "fix: new fix")

        assert _run(repo)["version"] == "2.0.1"


class TestPrereleases:
    def test_pr_build_is_a_semver_prerelease(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "feat: something")

        result = _run(repo, "--pr", "42")
        assert result["version"].startswith("1.5.0-pr42.")
        assert result["publish"] == "false"

    def test_rc_build_is_a_semver_prerelease(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "fix: something")

        result = _run(repo, "--rc")
        assert result["version"].startswith("1.4.3-rc.")
        assert result["publish"] == "false"

    def test_release_build_publishes(self, repo):
        _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
        _commit(repo, "fix: something")

        assert _run(repo)["publish"] == "true"


def test_prereleases_sort_below_their_release(repo):
    """The property that stops an OTA client shipping a PR build.

    SemVer orders 1.5.0-pr42.abc < 1.5.0, so a device comparing versions can
    never mistake a prerelease for something released.
    """
    from wifucked.tunnel import version_tuple

    _git(repo, "tag", "-a", "v1.4.2", "-m", "release")
    _commit(repo, "feat: something")

    release = _run(repo)["version"]
    prerelease = _run(repo, "--pr", "42")["version"]

    assert version_tuple(prerelease) == version_tuple(release)
    assert "-pr42." in prerelease
    assert "-" not in release
