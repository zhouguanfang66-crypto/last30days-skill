#!/usr/bin/env python3
"""Run scheduled last30days research and stage a GitHub Pages static site."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPO_ROOT / "skills" / "last30days" / "scripts" / "last30days.py"
DEFAULT_TOPICS_FILE = REPO_ROOT / ".github" / "research-topics.txt"
LIBRARY_DIR = REPO_ROOT / "_library"
SITE_DIR = REPO_ROOT / "_site"


def parse_topics(raw_override: str, topics_file: Path) -> list[str]:
    """Parse topics from workflow_dispatch input or the repo config file."""
    topics: list[str] = []
    if raw_override.strip():
        for chunk in raw_override.replace("\r\n", "\n").split("\n"):
            for piece in chunk.split(","):
                item = piece.strip()
                if item and not item.startswith("#"):
                    topics.append(item)
        return topics

    if topics_file.is_file():
        for line in topics_file.read_text(encoding="utf-8").splitlines():
            item = line.strip()
            if item and not item.startswith("#"):
                topics.append(item)
    return topics


def run_engine(args: list[str], *, env: dict[str, str], timeout: int = 420) -> tuple[int, str, str]:
    """Invoke last30days.py with bounded timeout and captured output."""
    cmd = [sys.executable, str(ENGINE), *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        return 124, str(exc.stdout or ""), f"Timed out after {timeout}s"


def send_webhook(webhook_url: str, summary_text: str) -> None:
    """Send an optional HTTPS webhook notification (Slack or generic JSON)."""
    parsed = urllib.parse.urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.hostname:
        print("Webhook skipped: WEBHOOK_URL must be a valid https:// URL", file=sys.stderr)
        return

    if parsed.hostname == "hooks.slack.com":
        payload = {"text": summary_text}
    else:
        payload = {
            "source": "last30days-github-actions",
            "message": summary_text,
            "timestamp": time.time(),
        }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "last30days-actions/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"Webhook delivered: HTTP {resp.status}", file=sys.stderr)
    except Exception as exc:
        print(f"Webhook delivery failed: {type(exc).__name__}: {exc}", file=sys.stderr)


def stage_public_site(library_dir: Path, site_dir: Path) -> None:
    """Copy only public HTML/Atom artifacts into _site for GitHub Pages."""
    if site_dir.exists():
        shutil.rmtree(site_dir)
    site_dir.mkdir(parents=True, exist_ok=True)

    for filename in ("index.html", "feed.xml"):
        src = library_dir / filename
        if not src.is_file():
            raise RuntimeError(f"Expected generated file missing: {src}")
        shutil.copy2(src, site_dir / filename)

    briefs_src = library_dir / "briefs"
    if briefs_src.is_dir():
        shutil.copytree(briefs_src, site_dir / "briefs")

    (site_dir / ".nojekyll").write_text("", encoding="utf-8")


def write_step_summary(rows: list[tuple[str, str, str]], pages_url: str, brief_count: int) -> None:
    """Append a readable run summary to GITHUB_STEP_SUMMARY if present."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "## last30days Scheduled Research",
        "",
        f"- **Generated at (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **Total briefs in library:** {brief_count}",
    ]
    if pages_url:
        lines.append(f"- **Site URL:** {pages_url}")
        lines.append(f"- **Atom Feed:** {pages_url.rstrip('/')}/feed.xml")
    lines.extend(["", "| Target | Status | Notes |", "|---|---|---|"])
    for target, status, note in rows:
        safe_note = note.replace("|", "/").replace("\n", " ")[:180]
        lines.append(f"| {target} | {status} | {safe_note} |")
    lines.append("")
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def main() -> int:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)

    child_env = dict(os.environ)
    child_env["LAST30DAYS_SCHEDULED_RUN"] = "1"
    child_env["LAST30DAYS_STORE"] = "1"
    child_env["LAST30DAYS_MEMORY_DIR"] = str(LIBRARY_DIR)
    child_env.setdefault("LAST30DAYS_LIBRARY_OWNER", os.environ.get("GITHUB_REPOSITORY_OWNER", "last30days"))

    raw_topics = os.environ.get("RESEARCH_TOPICS", "")
    discover_domain = os.environ.get("RESEARCH_DISCOVER", "").strip()
    quick_mode = os.environ.get("RESEARCH_QUICK", "true").strip().lower() not in ("false", "0", "no", "off")

    topics = parse_topics(raw_topics, DEFAULT_TOPICS_FILE)
    if not topics and not discover_domain:
        print("No topics configured in .github/research-topics.txt or RESEARCH_TOPICS.", file=sys.stderr)
        return 1

    rows: list[tuple[str, str, str]] = []
    succeeded = 0

    for topic in topics:
        print(f"::group::Researching: {topic}", flush=True)
        args = [
            topic,
            "--research-only",
            "--no-browser-cookies",
            "--save-dir",
            str(LIBRARY_DIR),
            "--emit=md",
        ]
        if quick_mode:
            args.append("--quick")
        rc, _stdout, stderr = run_engine(args, env=child_env, timeout=420)
        if stderr:
            print(stderr, file=sys.stderr)
        print("::endgroup::", flush=True)

        if rc == 0:
            succeeded += 1
            rows.append((topic, "OK", "Saved markdown brief"))
        else:
            last_err = (stderr.strip().splitlines() or [f"exit code {rc}"])[-1]
            rows.append((topic, f"Failed ({rc})", last_err))

    if discover_domain:
        print(f"::group::Discovering trending in: {discover_domain}", flush=True)
        args = [
            "--discover",
            discover_domain,
            "--no-browser-cookies",
            "--save-dir",
            str(LIBRARY_DIR),
            "--emit=md",
        ]
        if quick_mode:
            args.append("--discover-shallow")
        rc, _stdout, stderr = run_engine(args, env=child_env, timeout=420)
        if stderr:
            print(stderr, file=sys.stderr)
        print("::endgroup::", flush=True)

        if rc == 0:
            succeeded += 1
            rows.append((f"discover: {discover_domain}", "OK", "Saved discovery brief"))
        else:
            last_err = (stderr.strip().splitlines() or [f"exit code {rc}"])[-1]
            rows.append((f"discover: {discover_domain}", f"Failed ({rc})", last_err))

    print("::group::Building library index and Atom feed", flush=True)
    feed_rc, feed_out, feed_err = run_engine(
        ["library", "feed", "--save-dir", str(LIBRARY_DIR)],
        env=child_env,
        timeout=120,
    )
    if feed_out:
        print(feed_out)
    if feed_err:
        print(feed_err, file=sys.stderr)
    print("::endgroup::", flush=True)

    if feed_rc != 0:
        print(f"library feed failed with exit code {feed_rc}", file=sys.stderr)
        return feed_rc

    stage_public_site(LIBRARY_DIR, SITE_DIR)

    brief_files = sorted((SITE_DIR / "briefs").glob("*.html")) if (SITE_DIR / "briefs").is_dir() else []
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    pages_url = f"https://{owner}.github.io/{repo}/" if owner and repo else ""

    write_step_summary(rows, pages_url, len(brief_files))

    webhook_url = os.environ.get("WEBHOOK_URL", "").strip()
    if webhook_url:
        status_lines = [f"- {target}: {status}" for target, status in ((r[0], r[1]) for r in rows)]
        msg = (
            f"last30days scheduled research updated ({len(brief_files)} brief(s) published)\n"
            + "\n".join(status_lines)
            + (f"\nSite: {pages_url}\nFeed: {pages_url}feed.xml" if pages_url else "")
        )
        send_webhook(webhook_url, msg)

    if succeeded == 0 and not brief_files:
        print("All research tasks failed and no cached briefs exist.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
