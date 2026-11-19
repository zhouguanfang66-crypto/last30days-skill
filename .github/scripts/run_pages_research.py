#!/usr/bin/env python3
"""Run scheduled last30days research, translate to Chinese, and stage GitHub Pages."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE = REPO_ROOT / "skills" / "last30days" / "scripts" / "last30days.py"
DEFAULT_TOPICS_FILE = REPO_ROOT / ".github" / "research-topics.txt"
LIBRARY_DIR = REPO_ROOT / "_library"
SITE_DIR = REPO_ROOT / "_site"
ZH_CACHE_FILE = LIBRARY_DIR / ".zh-cache.json"

DEFAULT_GEMINI_MODEL = "gemini-4-hydrogen"
DEFAULT_GEMINI_THINKING_LEVEL = "MEDIUM"
GEMINI_GENERATE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
)

SKIP_TAGS = frozenset({"script", "style", "code", "pre", "noscript", "svg"})
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
URL_ONLY_RE = re.compile(r"^https?://\S+$")

# Deterministic UI dictionary so chrome strings are always crisp Chinese.
STATIC_ZH_UI: dict[str, str] = {
    "RESEARCH LIBRARY": "研报图书馆",
    "What the community is learning": "过去 30 天社区洞察",
    "Saved last30days briefs, newest first. Follow the Atom feed to keep up.": "按最新时间排列的 last30days 研报归档，支持通过 Atom 订阅源持续追踪。",
    "Saved last30days briefs, newest first.": "按最新时间排列的 last30days 研报归档。",
    "Subscribe via Atom": "订阅 Atom RSS",
    "Latest": "最新",
    "No saved briefs yet": "暂无已保存的研报",
    "Run last30days research and this library will fill itself.": "运行 last30days 调研后，这里会自动生成研报列表。",
    "Generated locally by": "本地生成自",
    "← Back to Library": "← 返回研报库",
    "What I learned": "核心发现",
    "Key patterns from the research": "研究关键趋势",
    "Ranked Evidence Clusters": "核心证据聚类",
    "Top Community Comments": "社区高赞评论",
    "Polymarket Markets": "Polymarket 预测市场",
    "Stats": "统计数据",
    "Source Coverage": "来源覆盖情况",
    "Partial Coverage": "部分覆盖提示",
    "Hiring Signals": "招聘暗信号",
    "Jobs Digest": "招聘动态摘要",
    "Patent Digest": "专利动态摘要",
    "Quick Verdict": "快速结论",
    "Head-to-Head": "横向对比",
    "The Bottom Line": "最终结论",
    "The emerging stack": "新兴技术栈",
    "Topics": "话题列表",
    "Daily research briefing": "每日调研简报",
    "Weekly research briefing": "每周调研简报",
}

TOGGLE_CSS = """
.l30d-topbar {
  position: sticky;
  top: 0;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin: -2.25rem 0 1.75rem;
  padding: 0.65rem 0;
  background: color-mix(in srgb, var(--bg) 90%, transparent);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--border);
}
.l30d-topbar-left {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  min-width: 0;
}
.l30d-back {
  font-size: 14px;
  font-weight: 500;
  color: var(--fg-muted);
  border-bottom: 0;
  text-decoration: none;
}
.l30d-back:hover {
  color: var(--accent-soft);
}
.l30d-topbar-right {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-left: auto;
}
.l30d-lang-status {
  font-size: 12px;
  color: var(--fg-subtle);
  font-family: 'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, monospace;
}
.l30d-lang-btn {
  appearance: none;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.35rem 0.85rem;
  border-radius: 999px;
  border: 1px solid var(--border);
  background: var(--bg-elev);
  color: var(--fg);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.3;
  transition: border-color 0.15s ease, background 0.15s ease, color 0.15s ease;
}
.l30d-lang-btn:hover {
  border-color: var(--accent);
  color: var(--accent-soft);
}
.l30d-lang-btn[data-lang="zh"] {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 16%, var(--bg-elev));
}
@media print {
  .l30d-topbar { display: none !important; }
}
""".strip()

TOGGLE_JS = r"""
(function () {
  var STORAGE_KEY = "last30days_ui_lang";
  var btn = document.getElementById("l30d-lang-btn");
  var statusEl = document.getElementById("l30d-lang-status");
  var mapEl = document.getElementById("l30d-zh-map");
  if (!btn || !mapEl) return;

  var zhMap = {};
  try {
    zhMap = JSON.parse(mapEl.textContent || "{}");
  } catch (_) {
    zhMap = {};
  }

  var skipTags = { SCRIPT: 1, STYLE: 1, CODE: 1, PRE: 1, NOSCRIPT: 1, SVG: 1 };
  var originals = new WeakMap();
  var trackedNodes = [];
  var originalTitle = document.title;

  function collectNodes() {
    if (trackedNodes.length > 0) return;
    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var node;
    while ((node = walker.nextNode())) {
      var parent = node.parentElement;
      if (!parent) continue;
      if (parent.closest && parent.closest("#l30d-lang-btn, #l30d-lang-status, pre, code, .engine-footer")) continue;
      if (skipTags[parent.tagName]) continue;
      var raw = node.nodeValue || "";
      if (!raw.trim()) continue;
      originals.set(node, raw);
      trackedNodes.push(node);
    }
  }

  function applyLang(lang) {
    collectNodes();
    var isZh = lang === "zh";
    document.documentElement.lang = isZh ? "zh-CN" : "en";
    btn.setAttribute("data-lang", isZh ? "zh" : "en");
    btn.textContent = isZh ? "🌐 切换英文 / EN" : "🌐 切换中文";

    if (isZh) {
      var titleTrim = originalTitle.trim();
      if (zhMap[titleTrim]) document.title = zhMap[titleTrim];
      var translatedCount = 0;
      for (var i = 0; i < trackedNodes.length; i++) {
        var node = trackedNodes[i];
        var orig = originals.get(node);
        if (!orig) continue;
        var key = orig.trim();
        var zh = zhMap[key];
        if (zh) {
          node.nodeValue = orig.replace(key, zh);
          translatedCount++;
        }
      }
      if (statusEl) {
        statusEl.textContent = translatedCount > 0 ? "中文" : "";
      }
    } else {
      document.title = originalTitle;
      for (var j = 0; j < trackedNodes.length; j++) {
        var n = trackedNodes[j];
        var o = originals.get(n);
        if (o !== undefined) n.nodeValue = o;
      }
      if (statusEl) {
        statusEl.textContent = "";
      }
    }

    try {
      localStorage.setItem(STORAGE_KEY, isZh ? "zh" : "en");
    } catch (_) {}
  }

  btn.addEventListener("click", function () {
    var current = btn.getAttribute("data-lang") === "zh" ? "zh" : "en";
    applyLang(current === "zh" ? "en" : "zh");
  });

  var initial = "en";
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "zh" || saved === "en") initial = saved;
  } catch (_) {}
  if (initial === "zh") {
    applyLang("zh");
  }
})();
""".strip()


class _TextExtractor(HTMLParser):
    """Collect visible text nodes outside code/pre/script/style blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._footer_depth = 0
        self._buf: list[str] = []
        self.segments: list[str] = []

    def _flush(self) -> None:
        if not self._buf:
            return
        text = "".join(self._buf).strip()
        self._buf.clear()
        if not text:
            return
        if not ASCII_LETTER_RE.search(text):
            return
        if URL_ONLY_RE.match(text):
            return
        self.segments.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._flush()
        lower = tag.lower()
        if lower in SKIP_TAGS:
            self._skip_depth += 1
        attr_map = dict(attrs)
        classes = (attr_map.get("class") or "").split()
        if "engine-footer" in classes:
            self._footer_depth += 1

    def handle_endtag(self, tag: str) -> None:
        self._flush()
        lower = tag.lower()
        if lower in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if lower == "div" and self._footer_depth > 0:
            self._footer_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0 or self._footer_depth > 0:
            return
        self._buf.append(data)

    def close(self) -> None:
        self._flush()
        super().close()


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


def _load_zh_cache(cache_path: Path) -> dict[str, str]:
    if not cache_path.is_file():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except Exception:
        pass
    return {}


def _save_zh_cache(cache_path: Path, cache: dict[str, str]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    # Keep cache bounded to recent 10,000 entries to stay lightweight.
    if len(cache) > 10000:
        keys = list(cache.keys())[-8000:]
        cache = {k: cache[k] for k in keys}
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _cache_key(text: str, backend: str) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    return f"{backend}:{digest}"


def _translate_batch_gemini(
    texts: list[str],
    *,
    api_key: str,
    model: str,
    thinking_level: str,
) -> dict[str, str]:
    """Translate a list of English UI/report strings via Gemini 4 Hydrogen (Medium)."""
    if not texts:
        return {}

    indexed = {str(i): text for i, text in enumerate(texts)}
    prompt = (
        "Translate each value in the JSON object below from English to natural, concise "
        "Simplified Chinese (简体中文) for a technology research report.\n"
        "Rules:\n"
        "- Return ONLY a valid JSON object with the exact same string keys.\n"
        "- Preserve product names, model names (e.g. Gemini 4 Hydrogen, Claude Code, Reddit, "
        "Polymarket, GitHub, Hacker News), usernames (@handle, r/subreddit), numbers, dates, and URLs.\n"
        "- Keep translations accurate and easy to read.\n\n"
        f"{json.dumps(indexed, ensure_ascii=False)}"
    )
    body: dict[str, object] = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "thinkingConfig": {"thinkingLevel": thinking_level.upper()},
        },
    }
    url = GEMINI_GENERATE_URL.format(model=urllib.parse.quote(model, safe="-_."), api_key=urllib.parse.quote(api_key, safe=""))
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    candidates = payload.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
    raw_text = "".join(
        str(p.get("text") or "")
        for p in parts
        if isinstance(p, dict) and not p.get("thought")
    ).strip()
    if raw_text.startswith("```"):
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.DOTALL).strip()

    parsed = json.loads(raw_text)
    if not isinstance(parsed, dict):
        return {}

    translated: dict[str, str] = {}
    for idx_str, orig in indexed.items():
        val = parsed.get(idx_str)
        if isinstance(val, str) and val.strip():
            translated[orig] = val.strip()
    return translated


def _translate_single_gtx(text: str) -> str | None:
    """Fallback free translation via Google Translate public endpoint on CI."""
    params = urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text}
    )
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, list) or not data or not isinstance(data[0], list):
        return None
    joined = "".join(
        str(chunk[0]) for chunk in data[0] if isinstance(chunk, list) and chunk and chunk[0]
    ).strip()
    return joined or None


def _translate_batch_gtx(texts: list[str]) -> dict[str, str]:
    """Translate multiple strings with bounded batching via Google Translate."""
    results: dict[str, str] = {}
    marker = "[[L30D_SEP]]"

    batch: list[str] = []
    batch_len = 0

    def flush(items: list[str]) -> None:
        if not items:
            return
        if len(items) == 1:
            try:
                out = _translate_single_gtx(items[0])
                if out:
                    results[items[0]] = out
            except Exception as exc:
                print(f"Fallback translation skipped for 1 item: {type(exc).__name__}", file=sys.stderr)
            return

        joined_input = f"\n{marker}\n".join(items)
        try:
            joined_out = _translate_single_gtx(joined_input)
            if joined_out:
                parts = [p.strip() for p in re.split(r"\[\[\s*L30D_SEP\s*\]\]", joined_out)]
                if len(parts) == len(items):
                    for src, dst in zip(items, parts):
                        if dst:
                            results[src] = dst
                    return
        except Exception as exc:
            print(f"Batch fallback translation failed ({type(exc).__name__}), retrying individually", file=sys.stderr)

        for item in items:
            try:
                out = _translate_single_gtx(item)
                if out:
                    results[item] = out
            except Exception:
                continue

    for text in texts:
        if len(text) > 1800:
            flush(batch)
            batch, batch_len = [], 0
            flush([text[:3500]])
            continue
        if batch and (batch_len + len(text) > 2600 or len(batch) >= 18):
            flush(batch)
            batch, batch_len = [], 0
            time.sleep(0.15)
        batch.append(text)
        batch_len += len(text) + len(marker) + 2
    flush(batch)
    return results


def translate_strings(
    segments: list[str],
    cache: dict[str, str],
) -> tuple[dict[str, str], str]:
    """Translate unique English strings into Simplified Chinese with caching."""
    api_key = (
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GOOGLE_GENAI_API_KEY", "").strip()
    )
    gemini_model = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    thinking_level = (
        os.environ.get("GEMINI_THINKING_LEVEL", DEFAULT_GEMINI_THINKING_LEVEL).strip().upper()
        or DEFAULT_GEMINI_THINKING_LEVEL
    )
    backend = f"gemini:{gemini_model}:{thinking_level}" if api_key else "gtx"

    resolved: dict[str, str] = {}
    missing: list[str] = []
    seen: set[str] = set()

    for seg in segments:
        if seg in seen:
            continue
        seen.add(seg)
        if seg in STATIC_ZH_UI:
            resolved[seg] = STATIC_ZH_UI[seg]
            continue
        cached = cache.get(_cache_key(seg, backend))
        if cached:
            resolved[seg] = cached
            continue
        missing.append(seg)

    used_backend = backend
    if missing:
        if api_key:
            print(
                f"Translating {len(missing)} string(s) with {gemini_model} (thinkingLevel={thinking_level})...",
                file=sys.stderr,
            )
            gemini_failed: list[str] = []
            for start in range(0, len(missing), 45):
                chunk = missing[start : start + 45]
                try:
                    batch_out = _translate_batch_gemini(
                        chunk,
                        api_key=api_key,
                        model=gemini_model,
                        thinking_level=thinking_level,
                    )
                    for src, dst in batch_out.items():
                        resolved[src] = dst
                        cache[_cache_key(src, backend)] = dst
                    for src in chunk:
                        if src not in batch_out:
                            gemini_failed.append(src)
                except Exception as exc:
                    print(
                        f"Gemini translation batch failed ({type(exc).__name__}: {exc}); falling back for this batch",
                        file=sys.stderr,
                    )
                    gemini_failed.extend(chunk)
            if gemini_failed:
                fallback_out = _translate_batch_gtx(gemini_failed)
                for src, dst in fallback_out.items():
                    resolved[src] = dst
                    cache[_cache_key(src, "gtx")] = dst
        else:
            print(
                f"GEMINI_API_KEY not set; translating {len(missing)} string(s) via build-time fallback...",
                file=sys.stderr,
            )
            fallback_out = _translate_batch_gtx(missing)
            for src, dst in fallback_out.items():
                resolved[src] = dst
                cache[_cache_key(src, "gtx")] = dst
            used_backend = "gtx (build-time fallback)"

    return resolved, used_backend


def _safe_inline_json(obj: dict[str, str]) -> str:
    raw = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def inject_zh_toggle(html_path: Path, zh_map: dict[str, str], *, is_brief: bool) -> None:
    """Inject a sticky English/Chinese switcher and pre-translated dictionary into an HTML page."""
    content = html_path.read_text(encoding="utf-8")
    if 'id="l30d-lang-btn"' in content:
        return

    back_html = (
        '<a class="l30d-back" href="../index.html">← Back to Library</a>' if is_brief else ""
    )
    topbar_html = (
        '<nav class="l30d-topbar" aria-label="Language switcher">'
        f'<div class="l30d-topbar-left">{back_html}</div>'
        '<div class="l30d-topbar-right">'
        '<span id="l30d-lang-status" class="l30d-lang-status" aria-live="polite"></span>'
        '<button type="button" id="l30d-lang-btn" class="l30d-lang-btn" data-lang="en">🌐 切换中文</button>'
        "</div>"
        "</nav>\n"
    )

    # Filter map to strings actually present on this page + static UI strings.
    extractor = _TextExtractor()
    extractor.feed(content)
    extractor.close()
    page_keys = set(extractor.segments) | set(STATIC_ZH_UI.keys())
    page_map = {k: zh_map[k] for k in page_keys if k in zh_map}

    script_block = (
        f'<script type="application/json" id="l30d-zh-map">{_safe_inline_json(page_map)}</script>\n'
        f"<script>\n{TOGGLE_JS}\n</script>\n"
    )

    if "</style>" in content:
        content = content.replace("</style>", f"\n{TOGGLE_CSS}\n</style>", 1)
    if "<body>" in content:
        content = content.replace("<body>", f"<body>\n{topbar_html}", 1)
    if "</body>" in content:
        content = content.replace("</body>", f"{script_block}</body>", 1)

    html_path.write_text(content, encoding="utf-8")


DRIVE_DOC_CSS = """
body {
  font-family: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", Arial, sans-serif;
  color: #1f2328;
  background: #ffffff;
  max-width: 820px;
  margin: 0 auto;
  padding: 24px;
  line-height: 1.7;
  font-size: 14px;
}
h1 { font-size: 24px; color: #111827; margin: 0 0 12px; border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; }
h2 { font-size: 18px; color: #1d4ed8; margin: 24px 0 10px; }
h3 { font-size: 15px; color: #111827; margin: 16px 0 6px; }
p { margin: 8px 0; }
a { color: #1a73e8; text-decoration: underline; }
ul, ol { margin: 8px 0 12px; padding-left: 22px; }
li { margin: 4px 0; }
blockquote { margin: 10px 0; padding: 8px 14px; border-left: 3px solid #93c5fd; background: #f8fafc; color: #334155; }
code, pre { font-family: ui-monospace, Menlo, Consolas, monospace; background: #f3f4f6; color: #111827; }
code { padding: 1px 4px; border-radius: 3px; }
pre { padding: 10px 12px; border-radius: 6px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 12px 0; }
th, td { border: 1px solid #d1d5db; padding: 6px 10px; text-align: left; }
th { background: #f3f4f6; font-weight: 600; }
.meta-banner { font-size: 12px; color: #6b7280; margin-bottom: 16px; }
""".strip()

_VOID_TAGS = frozenset({"br", "hr", "img", "meta", "link", "input"})
_DROP_TAGS = frozenset({"script", "style", "noscript", "svg"})
_DATE_IN_SLUG_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


class _ZhDocRenderer(HTMLParser):
    """Render a clean light-mode pure-Chinese HTML body suitable for Google Docs import."""

    def __init__(self, zh_map: dict[str, str]) -> None:
        super().__init__(convert_charrefs=True)
        self._zh_map = zh_map
        self._in_body = False
        self._in_title = False
        self._drop_depth = 0
        self._code_depth = 0
        self._footer_depth = 0
        self._buf: list[str] = []
        self._title_buf: list[str] = []
        self.out: list[str] = []
        self.title: str = ""

    def _flush(self) -> None:
        if not self._buf:
            return
        raw = "".join(self._buf)
        self._buf.clear()
        if self._code_depth > 0 or self._footer_depth > 0:
            self.out.append(html.escape(raw))
            return
        key = raw.strip()
        if key and key in self._zh_map:
            rendered = raw.replace(key, self._zh_map[key], 1)
        else:
            rendered = raw
        self.out.append(html.escape(rendered))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower == "title":
            self._in_title = True
            return
        if lower == "body":
            self._in_body = True
            return
        if not self._in_body:
            return
        self._flush()
        if lower in _DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth > 0:
            return
        attr_map = dict(attrs)
        classes = (attr_map.get("class") or "").split()
        if "engine-footer" in classes:
            self._footer_depth += 1
        if lower in ("code", "pre"):
            self._code_depth += 1
        kept_attrs: list[str] = []
        if lower == "a" and attr_map.get("href"):
            href = attr_map["href"] or ""
            if href.startswith(("http://", "https://", "mailto:")):
                kept_attrs.append(f'href="{html.escape(href, quote=True)}"')
        attr_str = (" " + " ".join(kept_attrs)) if kept_attrs else ""
        self.out.append(f"<{lower}{attr_str}>")

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower == "title":
            self._in_title = False
            self.title = "".join(self._title_buf).strip()
            return
        if lower == "body":
            self._flush()
            self._in_body = False
            return
        if not self._in_body:
            return
        if lower in _DROP_TAGS:
            if self._drop_depth > 0:
                self._drop_depth -= 1
            return
        if self._drop_depth > 0:
            return
        self._flush()
        if lower not in _VOID_TAGS:
            self.out.append(f"</{lower}>")
        if lower in ("code", "pre") and self._code_depth > 0:
            self._code_depth -= 1
        if lower == "div" and self._footer_depth > 0:
            self._footer_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_buf.append(data)
            return
        if not self._in_body or self._drop_depth > 0:
            return
        self._buf.append(data)

    def close(self) -> None:
        self._flush()
        super().close()


def _clean_topic_label(raw_title: str) -> str:
    """Strip common last30days title suffixes to produce a clean Google Doc name."""
    cleaned = raw_title.strip()
    for sep in (" — ", " - last30days", " | last30days"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0].strip()
    return cleaned or "Research Brief"


def export_drive_sync_bundle(
    site_dir: Path,
    raw_brief_htmls: dict[str, str],
    zh_map: dict[str, str],
    pages_url: str,
) -> int:
    """Export pre-rendered Chinese HTML docs and manifest.json for Google Drive sync."""
    sync_dir = site_dir / "drive-sync"
    sync_dir.mkdir(parents=True, exist_ok=True)
    base_url = pages_url.rstrip("/") if pages_url else ""

    entries: list[dict[str, str]] = []
    for filename in sorted(raw_brief_htmls.keys(), reverse=True):
        raw_html = raw_brief_htmls[filename]
        slug = filename[:-5] if filename.endswith(".html") else filename

        renderer = _ZhDocRenderer(zh_map)
        renderer.feed(raw_html)
        renderer.close()

        raw_title = renderer.title or slug
        title_zh = zh_map.get(raw_title, raw_title)
        date_match = _DATE_IN_SLUG_RE.search(slug)
        date_str = date_match.group(1) if date_match else datetime.now(timezone.utc).strftime("%Y-%m-%d")
        topic_label = _clean_topic_label(raw_title)
        doc_name = f"{date_str} - {topic_label} 研报（中文版）"

        html_url = f"{base_url}/briefs/{filename}" if base_url else f"briefs/{filename}"
        zh_filename = f"{slug}-zh.html"
        zh_doc_url = f"{base_url}/drive-sync/{zh_filename}" if base_url else f"drive-sync/{zh_filename}"

        banner_html = (
            f'<div class="meta-banner">来源：last30days 自动研报 · 日期：{html.escape(date_str)} · '
            f'<a href="{html.escape(html_url, quote=True)}">查看网页原版（支持中英切换）</a></div>'
        )
        doc_html = (
            '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            f"<title>{html.escape(doc_name)}</title>\n"
            f"<style>\n{DRIVE_DOC_CSS}\n</style>\n</head>\n<body>\n"
            f"{banner_html}\n{''.join(renderer.out)}\n</body>\n</html>\n"
        )
        (sync_dir / zh_filename).write_text(doc_html, encoding="utf-8")

        entries.append({
            "slug": slug,
            "filename": filename,
            "date": date_str,
            "title": raw_title,
            "title_zh": title_zh,
            "doc_name": doc_name,
            "html_url": html_url,
            "zh_doc_html_url": zh_doc_url,
        })

    manifest = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "site_url": f"{base_url}/" if base_url else "",
        "index_html_url": f"{base_url}/index.html" if base_url else "index.html",
        "feed_xml_url": f"{base_url}/feed.xml" if base_url else "feed.xml",
        "briefs": entries,
    }
    (sync_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return len(entries)


def enhance_site_with_zh_toggle(site_dir: Path, cache_path: Path) -> tuple[str, dict[str, str], dict[str, str]]:
    """Pre-translate all generated HTML pages and inject the Chinese toggle button."""
    html_files: list[tuple[Path, bool]] = []
    raw_brief_htmls: dict[str, str] = {}
    index_path = site_dir / "index.html"
    if index_path.is_file():
        html_files.append((index_path, False))
    briefs_dir = site_dir / "briefs"
    if briefs_dir.is_dir():
        for brief_path in sorted(briefs_dir.glob("*.html")):
            html_files.append((brief_path, True))
            raw_brief_htmls[brief_path.name] = brief_path.read_text(encoding="utf-8")

    all_segments: list[str] = list(STATIC_ZH_UI.keys())
    for html_path, _ in html_files:
        extractor = _TextExtractor()
        extractor.feed(html_path.read_text(encoding="utf-8"))
        extractor.close()
        all_segments.extend(extractor.segments)

    cache = _load_zh_cache(cache_path)
    zh_map, backend = translate_strings(all_segments, cache)
    _save_zh_cache(cache_path, cache)

    for html_path, is_brief in html_files:
        inject_zh_toggle(html_path, zh_map, is_brief=is_brief)

    return backend, zh_map, raw_brief_htmls


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


def collect_active_sources(env: dict[str, str]) -> list[str]:
    """Run --diagnose to log active free/configured sources without network calls."""
    rc, stdout, stderr = run_engine(["--diagnose", "--no-browser-cookies"], env=env, timeout=60)
    if rc != 0 or not stdout.strip():
        if stderr:
            print(stderr, file=sys.stderr)
        return []
    try:
        payload = json.loads(stdout)
    except Exception:
        return []
    sources = payload.get("available_sources")
    ext_cmds = payload.get("external_commands") or {}
    has_github = payload.get("has_github")
    print(f"Available sources: {', '.join(sources) if isinstance(sources, list) else 'unknown'}", flush=True)
    print(f"External CLIs: {json.dumps(ext_cmds, sort_keys=True)} | has_github_token={has_github}", flush=True)
    return [str(s) for s in sources] if isinstance(sources, list) else []


def write_step_summary(
    rows: list[tuple[str, str, str]],
    pages_url: str,
    brief_count: int,
    translation_backend: str,
    active_sources: list[str] | None = None,
) -> None:
    """Append a readable run summary to GITHUB_STEP_SUMMARY if present."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "## last30days Scheduled Research",
        "",
        f"- **Generated at (UTC):** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **Total briefs in library:** {brief_count}",
        f"- **Chinese translation backend:** `{translation_backend}`",
    ]
    if active_sources:
        lines.append(f"- **Active sources ({len(active_sources)}):** `{', '.join(active_sources)}`")
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

    print("::group::Engine source diagnostics", flush=True)
    active_sources = collect_active_sources(child_env)
    print("::endgroup::", flush=True)

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

    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    pages_url = f"https://{owner}.github.io/{repo}/" if owner and repo else ""

    print("::group::Building Chinese/English toggle and Google Drive sync bundle", flush=True)
    translation_backend, zh_map, raw_brief_htmls = enhance_site_with_zh_toggle(SITE_DIR, ZH_CACHE_FILE)
    drive_count = export_drive_sync_bundle(SITE_DIR, raw_brief_htmls, zh_map, pages_url)
    print(
        f"Chinese toggle embedded (backend: {translation_backend}); exported {drive_count} Drive sync doc(s)",
        file=sys.stderr,
    )
    print("::endgroup::", flush=True)

    brief_files = sorted((SITE_DIR / "briefs").glob("*.html")) if (SITE_DIR / "briefs").is_dir() else []

    write_step_summary(rows, pages_url, len(brief_files), translation_backend, active_sources=active_sources)

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
