#!/usr/bin/env python3
"""Scaffold a new data-liberation project from the upstream template.

Fetches the [data-liberation-template](https://github.com/brianckeegan/data-liberation-template)
repo (pinned to a tagged release), copies it to a destination path, and
substitutes Jinja-style placeholders. Zero non-stdlib dependencies — uses
`git` from PATH plus the standard library.

Usage
-----
    python scripts/scaffold.py \\
        --dest ~/code/boulder-election-results \\
        --name boulder-election-results \\
        --description "Boulder County election results, 1980–present" \\
        --author "Brian Keegan <bkeegan@example.org>" \\
        --owner BoulderPublicData \\
        --consumers pandas,R

Run with `--dry-run` to see what would be written without touching disk.

To test edits to the template repo locally, pass `--template-repo /path/to/local/clone`
(any local directory works; if it's a git repo we'll skip the network).

The placeholder set is documented in `references/project-template.md`
("Slot-fills used by `scaffold.py`").
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TEMPLATE_REPO = "https://github.com/brianckeegan/data-liberation-template.git"

# Pinned by commit SHA, not by tag — tags on GitHub are mutable, and a force-push
# to a tag on the template repo would silently change scaffolded output for every
# user on this version of the skill. SHA pinning makes the bytes reproducible.
#
# The human-readable tag this corresponds to lives in DEFAULT_TEMPLATE_TAG below
# (for messaging only). See RELEASING.md for the bump procedure when cutting a
# new skill release.
#
# v0.4.0 is a skill-only release (the six-level restructure + reference
# consolidation); the template bytes are unchanged, so the pin still points at
# the v0.3.0 template commit — template v0.4.0 re-tags that same commit to keep
# the skill/template version pair aligned.
DEFAULT_TEMPLATE_VERSION = "72b202020c12056f19c828bff0b619cda5aadf64"
DEFAULT_TEMPLATE_TAG = "v0.4.0"

# File suffixes we treat as text (substitute placeholders). Anything else is
# copied byte-for-byte.
TEXT_SUFFIXES = {
    ".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".ini",
    ".txt", ".csv", ".json", ".disabled", ".gitkeep", ".gitignore",
    ".qmd",            # Quarto source files
    ".gitattributes",  # LFS configuration
}
# Files we always treat as text by exact name (no suffix or unusual case).
TEXT_NAMES = {".gitignore", ".gitkeep", ".gitattributes", "AGENTS.md", "README.md"}

# Files in the template repo that document the template *itself* and should
# not be copied into scaffolded projects.
SKIP_FILES = {"TEMPLATE.md"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dest", required=True, type=Path,
                   help="Destination directory (created if absent; must be empty if it exists).")
    p.add_argument("--name", required=True,
                   help="Project name, kebab-case (e.g. 'boulder-election-results').")
    p.add_argument("--description", required=True,
                   help="One-line description of what the project liberates.")
    p.add_argument("--author", default=None,
                   help="Author string. Falls back to `git config user.name <user.email>` if absent.")
    p.add_argument("--owner", default=None,
                   help="GitHub owner (user or org) the project will live under. "
                        "Used in README badges, Quarto site URL, and BibTeX. "
                        "Falls back to `git config user.name` if absent.")
    p.add_argument("--consumers", default="pandas",
                   help="Comma-separated consumer stacks (e.g. 'pandas,R,polars').")
    p.add_argument("--template-repo", default=DEFAULT_TEMPLATE_REPO,
                   help=f"Git URL or local path of the template. "
                        f"Default: {DEFAULT_TEMPLATE_REPO}")
    p.add_argument("--template-version", default=DEFAULT_TEMPLATE_VERSION,
                   help=f"Tag, branch, or commit of the template to fetch. "
                        f"Default: {DEFAULT_TEMPLATE_TAG} "
                        f"(commit {DEFAULT_TEMPLATE_VERSION[:12]}). "
                        f"Ignored for local paths.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print planned writes without touching disk.")
    return p.parse_args(argv)


def derive_slug(name: str) -> str:
    """`boulder-election-results` → `boulder_election_results`. Strict
    snake_case; lowercase ASCII letters, digits, underscores only.
    """
    cleaned = []
    for ch in name.strip().lower():
        if ch.isalnum():
            cleaned.append(ch)
        elif ch in "-_ ":
            cleaned.append("_")
        # silently drop other characters
    slug = "".join(cleaned).strip("_")
    if not slug:
        raise SystemExit(f"Cannot derive a valid Python identifier from name {name!r}")
    if slug[0].isdigit():
        slug = "_" + slug
    return slug


def _git_config(key: str) -> str:
    try:
        result = subprocess.run(
            ["git", "config", key], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return ""
    return result.stdout.strip()


def detect_git_author() -> str | None:
    name = _git_config("user.name")
    email = _git_config("user.email")
    if name and email:
        return f"{name} <{email}>"
    return name or None


def build_placeholders(args: argparse.Namespace) -> dict[str, str]:
    author = args.author or detect_git_author() or "Anonymous"
    owner = args.owner or _git_config("user.name") or "OWNER"
    return {
        "project_name":   args.name,
        "project_slug":   derive_slug(args.name),
        "description":    args.description,
        "author":         author,
        "owner":          owner,
        "consumer_stack": args.consumers,
    }


def is_text_file(path: Path) -> bool:
    if path.name in TEXT_NAMES:
        return True
    if path.suffix in TEXT_SUFFIXES:
        return True
    return False


def substitute(text: str, placeholders: dict[str, str]) -> str:
    """Replace every `{{ key }}` (with surrounding whitespace flexibility)
    using a simple `str.replace`. No dependencies, no escaping rules to
    learn, no surprises.
    """
    out = text
    for key, value in placeholders.items():
        # Accept either `{{ key }}` (one space) or `{{key}}` for hand-typed
        # variants. Run the wider form first so the substitution is
        # idempotent.
        out = out.replace("{{ " + key + " }}", value)
        out = out.replace("{{" + key + "}}", value)
    return out


_GITHUB_URL_RE = re.compile(
    r"^(?:https?://github\.com/|git@github\.com:)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def _parse_github_repo(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub HTTPS or SSH URL; None if not GitHub."""
    m = _GITHUB_URL_RE.match(url)
    return (m["owner"], m["repo"]) if m else None


def _git_clone(repo: str, version: str, target: Path) -> None:
    """Fallback for non-GitHub remotes. Requires `git` on PATH."""
    if shutil.which("git") is None:
        raise SystemExit(
            f"--template-repo {repo} is not a GitHub URL, and git is not on PATH "
            f"to fall back to. Install git, or pass --template-repo with a local "
            f"path to a checked-out template."
        )
    # For non-GitHub remotes we can't reliably fetch a SHA via shallow clone,
    # so do a full clone and check out. Tradeoff: slower; acceptable for the
    # uncommon non-GitHub case.
    result = subprocess.run(
        ["git", "clone", repo, str(target)], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"git clone failed (exit {result.returncode}):\n{result.stderr}")
    co = subprocess.run(
        ["git", "-C", str(target), "checkout", version],
        capture_output=True, text=True, check=False,
    )
    if co.returncode != 0:
        raise SystemExit(f"git checkout {version} failed:\n{co.stderr}")
    shutil.rmtree(target / ".git", ignore_errors=True)


def fetch_template(repo: str, version: str, scratch: Path) -> Path:
    """Materialize the template into `scratch` and return the root path.

    Three paths:
      * `repo` is a local directory → use it in place, no fetch.
      * `repo` is a GitHub URL → download a tarball of `version` from
        `codeload.github.com`. Works with tag, branch, OR commit SHA. No
        git dependency.
      * `repo` is any other Git URL → fall back to `git clone` + checkout.
    """
    local = Path(repo).expanduser()
    if local.exists() and local.is_dir():
        if not (local / "scripts").exists():
            raise SystemExit(
                f"--template-repo {repo} exists but doesn't look like a template "
                f"(no scripts/ directory). Wrong path?"
            )
        return local

    gh = _parse_github_repo(repo)
    if gh is None:
        target = scratch / "template"
        print(f"Fetching template {repo} @ {version} (git clone)…")
        _git_clone(repo, version, target)
        return target

    owner, repo_name = gh
    tarball_url = f"https://codeload.github.com/{owner}/{repo_name}/tar.gz/{version}"
    print(f"Fetching template {owner}/{repo_name} @ {version[:12]}…")

    tarball_path = scratch / "template.tar.gz"
    try:
        with urllib.request.urlopen(tarball_url, timeout=60) as resp:
            tarball_path.write_bytes(resp.read())
    except urllib.error.HTTPError as exc:
        raise SystemExit(
            f"Tarball fetch failed (HTTP {exc.code}): {tarball_url}\n"
            f"Check that the ref `{version}` exists on {owner}/{repo_name}."
        ) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Tarball fetch failed (network): {exc.reason}") from exc

    extract_dir = scratch / "extracted"
    extract_dir.mkdir()
    with tarfile.open(tarball_path) as tf:
        # PEP 706 safe extraction (Python 3.12+); fall back to plain extractall
        # on 3.11. The risk profile (we know the source) makes either acceptable.
        try:
            tf.extractall(extract_dir, filter="data")
        except TypeError:
            tf.extractall(extract_dir)

    children = [c for c in extract_dir.iterdir() if c.is_dir()]
    if len(children) != 1:
        raise SystemExit(
            f"Unexpected tarball layout under {extract_dir}: {[c.name for c in children]}"
        )
    return children[0]


def walk_and_write(
    src: Path,
    dst: Path,
    placeholders: dict[str, str],
    dry_run: bool,
) -> list[Path]:
    """Copy `src` tree to `dst`, substituting placeholders in text files.

    Returns a list of destination paths written (or that would be).
    """
    written: list[Path] = []
    for entry in src.rglob("*"):
        # Skip the .git directory if --template-repo is a live git checkout.
        if ".git" in entry.parts:
            continue
        if entry.name in SKIP_FILES:
            continue
        rel = entry.relative_to(src)
        # Substitute placeholders in path components too — lets the
        # template support filenames like `{{ project_slug }}.csv`.
        rel_str = substitute(str(rel), placeholders)
        target = dst / rel_str

        if entry.is_dir():
            if not dry_run:
                target.mkdir(parents=True, exist_ok=True)
            continue

        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)

        if is_text_file(entry):
            text = entry.read_text(encoding="utf-8")
            rendered = substitute(text, placeholders)
            if dry_run:
                print(f"  [text]   {target}")
            else:
                target.write_text(rendered, encoding="utf-8")
        else:
            if dry_run:
                print(f"  [binary] {target}")
            else:
                shutil.copy2(entry, target)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    placeholders = build_placeholders(args)
    dest = args.dest.expanduser().resolve()

    if dest.exists():
        if any(dest.iterdir()):
            sys.stderr.write(
                f"Destination {dest} exists and is not empty. "
                f"Refusing to overwrite. Pick a fresh directory.\n"
            )
            return 1
    else:
        if not args.dry_run:
            dest.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="data-liberation-template-") as scratch_str:
        scratch = Path(scratch_str)
        template_root = fetch_template(args.template_repo, args.template_version, scratch)

        print(f"Scaffolding {args.name}")
        print(f"  → {dest}")
        print(f"  slug:      {placeholders['project_slug']}")
        print(f"  author:    {placeholders['author']}")
        print(f"  owner:     {placeholders['owner']}")
        print(f"  consumers: {placeholders['consumer_stack']}")
        print(f"  template:  {args.template_repo} @ {args.template_version}")
        if args.dry_run:
            print("  (dry-run; no files written)")
        print()

        written = walk_and_write(template_root, dest, placeholders, args.dry_run)

    if args.dry_run:
        print(f"\nWould write {len(written)} files.")
        return 0

    print(f"Wrote {len(written)} files.")
    print()
    print("Next steps:")
    print(f"  cd {dest}")
    print("  uv sync")
    print("  uv run pytest")
    print("  # Edit scripts/config.py to register your first source,")
    print("  # then `uv run python -m scripts.pipeline run`.")
    print("  # For Datasette publishing: `uv sync --extra publish` then")
    print("  # `uv run python -m scripts.publish build` and `serve` or `deploy`.")
    print("  # For the Quarto site: run `quarto publish gh-pages` once locally,")
    print("  # then rename .github/workflows/gh-pages.yml.disabled to enable CI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
