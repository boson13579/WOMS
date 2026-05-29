"""Build all.html by stitching slide bodies from per-author files.

Layout:
  page 1     <- gemini.html slide-1 (Outline)
  page 2     <- gemini.html slide-2 (工程紀律)
  page 3-6   <- min.html slide-1..4
  page 7-14  <- cerosop.html slide-1..8
  page 15-17 <- gemini.html slide-3..5 (Dashboard / Observability / Audit Log)
  page 18-19 <- xixi.html slide-1..2
  page 20-21 <- ctj.html section[0..1]

Strategy:
- Use gemini.html head as canonical template (Tailwind + fonts + base style).
- Extract each author's slide bodies, tag with data-author="..." for CSS scope.
- Each author's CSS is filtered (template-owned selectors stripped) and prefixed
  with the data-author selector so it cannot leak.
- Author scripts: emit globally (NOT wrapped in IIFE) after removing their own
  deck-nav listeners / `let currentSlide` / `const TotalSlides` declarations.
  This keeps interactive functions like cerosop's resetSearchDemo callable from
  inline onclick handlers (which only see the global scope).
- A single deck-nav script at the bottom handles keyboard navigation and a
  per-slide onEnter hook (page 9 -> kick off batch admission demo).
"""

from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).parent


def read(p: str) -> str:
    return (HERE / p).read_text(encoding="utf-8")


def extract_slide_inners_by_id(html: str) -> list[str]:
    """Return inner HTML of each ``<div class="slide-container..." id="slide-N">``."""
    out: list[str] = []
    open_re = re.compile(
        r'<div\s+class="slide-container[^"]*"\s+id="slide-\d+">',
        flags=re.S,
    )
    tag_re = re.compile(r"<(/?)div\b", flags=re.S)

    for m in open_re.finditer(html):
        start = m.end()
        depth = 1
        for t in tag_re.finditer(html, start):
            if t.group(1) == "/":
                depth -= 1
                if depth == 0:
                    out.append(html[start : t.start()])
                    break
            else:
                depth += 1
    return out


def extract_ctj_sections(html: str) -> list[str]:
    """Return list of <section>...</section> blobs from ctj.html."""
    out = []
    for match in re.finditer(
        r'<section\s+data-label="([^"]+)">(.*?)</section>',
        html,
        flags=re.S,
    ):
        label = match.group(1)
        body = match.group(2)
        out.append(f'<section data-label="{label}">{body}</section>')
    return out


_COMPAT_STYLES = """
        /* ctj sections were originally inside <deck-stage>; re-target so they
           fill our slide-container and pick up ctj's padding. */
        .slide-container[data-author="ctj"] > section[data-label] {
            display: flex;
            flex-direction: column;
            background: #fff;
            box-sizing: border-box;
            width: 100%;
            height: 100%;
            padding: var(--pad-top, 36px) var(--pad-x, 64px) var(--pad-bottom, 28px);
            overflow: hidden;
        }

        /* min slides use .slide-header + .htitle bar; restyle to match
           gemini's blue title style. */
        /* Unify title top spacing across every author so the blue title bar
           sits at the same vertical offset on every slide.
           Target: 2.5rem above the title (= gemini's .slide-content
           padding-top). */
        .slide-container[data-author="min"] .slide-header {
            border-bottom: none;
            padding: 2.5rem 4rem 0;
            background: transparent;
        }
        /* cerosop authored some slides with .slide-content.mt-16 (=4rem
           top margin) which pushed titles way lower than the deck baseline.
           Zero it out so cerosop titles line up with gemini's. */
        .slide-container[data-author="cerosop"] .slide-content {
            margin-top: 0 !important;
        }
        /* ctj: --pad-top governs section top padding (=title-top space).
           Bump to ~2.5rem to match the rest. */
        .slide-container[data-author="ctj"] {
            --pad-top: 40px;
        }
        .slide-container[data-author="min"] .slide-header .hbar { display: none; }
        .slide-container[data-author="min"] .slide-header .htitle {
            font-size: 2.5rem;
            font-weight: 700;
            color: #0033A0;
            border-left: 8px solid #0033A0;
            padding-left: 1rem;
            line-height: 1.2;
            margin-bottom: 1rem;
        }
        .slide-container[data-author="min"] .slide-header + .slide-content {
            /* 4rem bottom so content stays clear of page-number. */
            padding: 1rem 4rem 4rem;
        }

        /* min was sized for 1280x720; bump text sizes in our full-viewport deck. */
        .slide-container[data-author="min"] .s1-role-bar {
            font-size: 1.4rem !important; padding: 0.6rem 1.4rem;
        }
        .slide-container[data-author="min"] .s1-role-bar .desc {
            font-size: 1.2rem !important;
        }
        .slide-container[data-author="min"] .s1-num {
            width: 2.2rem; height: 2.2rem; font-size: 1.1rem !important;
        }
        .slide-container[data-author="min"] .s1-lbl { font-size: 1rem !important; }
        .slide-container[data-author="min"] .s1-txt { font-size: 1.3rem !important; }
        .slide-container[data-author="min"] .s1-val { font-size: 1.25rem !important; }
        .slide-container[data-author="min"] .s2-head { font-size: 1.3rem !important; padding: 0.5rem 1rem; }
        .slide-container[data-author="min"] .s2-body { font-size: 1.2rem !important; padding: 0.6rem 1rem; }
        .slide-container[data-author="min"] .s2-story { font-size: 1.25rem !important; }
        .slide-container[data-author="min"] .s2-desc { font-size: 1.15rem !important; }
        .slide-container[data-author="min"] .s3-txt { font-size: 1.25rem !important; }
        .slide-container[data-author="min"] .s3-tbl { font-size: 1.2rem !important; }
        .slide-container[data-author="min"] .s3-tbl-title { font-size: 1.3rem !important; }
        .slide-container[data-author="min"] .s3-title { font-size: 1.5rem !important; }
        .slide-container[data-author="min"] .s3-num { font-size: 1.2rem !important; }
        .slide-container[data-author="min"] .s3-img {
            max-height: 70vh;
        }
        .slide-container[data-author="min"] .s4-frame {
            min-height: 70vh;
        }

        /* xixi: shrink text utilities inside xixi scope. */
        .slide-container[data-author="xixi"] .text-5xl { font-size: 2.5rem !important; }
        .slide-container[data-author="xixi"] .text-4xl { font-size: 2rem !important; }
        .slide-container[data-author="xixi"] .text-3xl { font-size: 1.7rem !important; }
        .slide-container[data-author="xixi"] .text-2xl { font-size: 1.4rem !important; }
        .slide-container[data-author="xixi"] .text-xl { font-size: 1.2rem !important; }

        /* ctj: override --type-* tokens since stage is now 100vw/100vh
           rather than 1920x1080 fixed. */
        .slide-container[data-author="ctj"] {
            --type-title: 38px;
            --type-subtitle: 20px;
            --type-h3: 24px;
            --type-body: 18px;
            --type-small: 16px;
            --type-mono: 16px;
            --pad-top: 40px;
            --pad-bottom: 64px;
            --pad-x: 64px;
        }

        /* gemini 3-5 callout labels: ensure they keep .text-lg / .text-base
           sizes despite cerosop's redefinitions (defensive — cerosop is now
           scoped so this is mostly belt+suspenders). */
        .slide-container[data-author="gemini"] .text-lg { font-size: 1.125rem; }
        .slide-container[data-author="gemini"] .text-base { font-size: 1rem; }
        .slide-container[data-author="gemini"] .text-xl { font-size: 1.25rem; }
"""


_TEMPLATE_SELECTORS = {
    "body",
    "*",
    ".slide-container",
    ".slide-container.active",
    ".slide-content",
    ".slide-title",
    ".slide-title .subtitle",
    ".page-number",
    ".nav-dots",
    ".dot",
    ".dot.active",
    "#stage",
    "deck-stage",
    "deck-stage > section",
    "html",
    ".highlight-box",
    ".tech-font",
}


def extract_style_block(html: str) -> str:
    return "\n".join(m.group(1) for m in re.finditer(r"<style[^>]*>(.*?)</style>", html, flags=re.S))


def _split_css_rules(css: str) -> list[str]:
    """Split CSS into top-level rules (handles nested {} for @keyframes etc.)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(css):
        c = css[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                rules.append(css[start : i + 1])
                start = i + 1
        i += 1
    return [r.strip() for r in rules if r.strip()]


def filter_author_css(css: str, scope: str) -> str:
    """Strip template-owned rules; scope remaining rules under ``scope``."""
    kept: list[str] = []
    for rule in _split_css_rules(css):
        head, _, body = rule.partition("{")
        head = head.strip()
        sels = [s.strip() for s in head.split(",")]
        if any(s in _TEMPLATE_SELECTORS for s in sels):
            continue
        if head.startswith("@keyframes") and "fadeIn" in head:
            continue
        # At-rules: pass through unchanged (no DOM scope).
        if head.startswith("@") or head == ":root":
            # :root tokens shouldn't be promoted into scope; skip — we provide
            # our own --type-* override in _COMPAT_STYLES for ctj.
            if head == ":root":
                continue
            kept.append(rule)
            continue
        scoped = ", ".join(_scope_one(s, scope) for s in sels)
        kept.append(f"{scoped} {{{body}")
    return "\n".join(kept)


def _scope_one(selector: str, scope: str) -> str:
    s = selector.strip()
    if scope in s:
        return s
    if s in ("html", "body"):
        return scope
    return f"{scope} {s}"


def transform_min_script(body: str) -> str:
    """Strip min's deck-nav + scaleStage; keep nothing else needed."""
    # min's script only does deck nav + scaleStage. Both are removed.
    return ""


def transform_cerosop_script(body: str) -> str:
    """Sanitize cerosop's big script for deck embedding.

    - Remove ``const TotalSlides = 8;`` and ``let currentSlide = 0;`` (would
      collide with our deck-nav globals if we had any — we don't, but these
      shadow nothing useful and changeSlide isn't called from anywhere we keep).
    - Remove the keyboard + click window listeners (deck nav handles keys).
    - Remove the body that references missing DOM (#top-counter, speech-draft,
      etc.) by replacing the whole changeSlide function with a no-op stub.
    - Remove the initial init call that touches removed elements.
    - Keep: resetSearchDemo, startAutoPlay, stopAutoPlay, toggleAutoPlay,
      stepNextDemo, updateSearchDemoUI, openControlConsole, toggleControlConsole,
      updatePlayPauseBtnUI. These get exposed as globals automatically since
      they are top-level function declarations.
    """
    # Strip the keyboard listener (window.addEventListener('keydown', ...))
    body = re.sub(
        r"window\.addEventListener\('keydown',\s*\(e\)\s*=>\s*\{.*?\}\s*\);",
        "/* removed keydown listener */",
        body,
        flags=re.S,
    )
    # Strip the click listener
    body = re.sub(
        r"window\.addEventListener\('click',\s*\(e\)\s*=>\s*\{.*?\}\s*\);",
        "/* removed click listener */",
        body,
        flags=re.S,
    )
    # Remove TotalSlides / currentSlide declarations (rename them to scoped
    # alternatives to avoid colliding with anything else). Replace with var.
    body = body.replace(
        "const TotalSlides = 8;",
        "var __cerosop_TotalSlides = 8;",
    )
    body = body.replace(
        "let currentSlide = 0;",
        "var __cerosop_currentSlide = 0;",
    )
    # Remove the initial speech-draft init (it has no element)
    body = re.sub(
        r"const initialSpeechDraftEl = document\.getElementById\('speech-draft'\);\s*"
        r"if \(initialSpeechDraftEl\) \{[^}]*\}",
        "/* removed initial speech draft init */",
        body,
        flags=re.S,
    )
    # Defer the bottom updateSearchDemoUI() call — it needs DOM that exists
    # (slide-3 nodes are in the deck). Wrap in DOMContentLoaded guard.
    body = body.replace(
        "// Run default initialization of search states\n        updateSearchDemoUI();",
        "// initial UI sync deferred to deck-nav onEnter hook for page 9",
    )
    # Guard the targetIds wiring — those inputs don't exist in the merged deck.
    # The forEach already null-checks each pair, so it's fine to leave as is.
    return body


def extract_xixi_script(html: str) -> str:
    """xixi has only deck nav + dot creation. Drop entirely."""
    return ""


def transform_ctj_script(html: str) -> str:
    """ctj uses a custom <deck-stage> element from deck-stage.js. Drop."""
    return ""


def extract_script_bodies(html: str) -> str:
    """Return concatenated bodies of inline scripts (skip src= and tailwind.config)."""
    parts = []
    for m in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, flags=re.S):
        attrs = m.group("attrs")
        if "src=" in attrs:
            continue
        body = m.group("body").strip()
        if not body or "tailwind.config" in body:
            continue
        parts.append(body)
    return "\n\n".join(parts)


def main() -> None:
    gemini = read("gemini.html")
    min_html = read("min.html")
    cero = read("cerosop.html")
    xixi = read("xixi.html")
    ctj = read("ctj.html")

    gemini_slides = extract_slide_inners_by_id(gemini)
    min_slides = extract_slide_inners_by_id(min_html)
    cero_slides = extract_slide_inners_by_id(cero)
    xixi_slides = extract_slide_inners_by_id(xixi)
    ctj_slides = extract_ctj_sections(ctj)

    print(
        f"gemini={len(gemini_slides)} min={len(min_slides)} "
        f"cerosop={len(cero_slides)} xixi={len(xixi_slides)} ctj={len(ctj_slides)}"
    )

    pages: list[tuple[str, str, str]] = []
    pages.append(("gemini 1", "gemini", gemini_slides[0]))
    pages.append(("gemini 2", "gemini", gemini_slides[1]))
    for i, s in enumerate(min_slides[:4]):
        pages.append((f"min {i + 1}", "min", s))
    for i, s in enumerate(cero_slides[:8]):
        pages.append((f"cerosop {i + 1}", "cerosop", s))
    for i, s in enumerate(gemini_slides[2:5]):
        pages.append((f"gemini {i + 3}", "gemini", s))
    for i, s in enumerate(xixi_slides[:2]):
        pages.append((f"xixi {i + 1}", "xixi", s))
    for i, s in enumerate(ctj_slides[:2]):
        pages.append((f"ctj {i + 1}", "ctj", s))

    print(f"total pages composed: {len(pages)}")
    assert len(pages) == 21, "expected 21 pages"

    re_pn = re.compile(r'<div class="page-number">[^<]*</div>')
    rebuilt: list[str] = []
    for idx, (src, author, inner) in enumerate(pages, start=1):
        page_div = f'<div class="page-number">{idx}</div>'
        if re_pn.search(inner):
            inner = re_pn.sub(page_div, inner, count=1)
        else:
            inner = inner.rstrip() + "\n        " + page_div + "\n    "
        # Strip ctj subtitle span (user feedback).
        if author == "ctj":
            inner = re.sub(
                r'<span class="subtitle">.*?</span>',
                "",
                inner,
                flags=re.S,
            )
        active = " active" if idx == 1 else ""
        rebuilt.append(
            f'    <!-- ===== page {idx} :: {src} ===== -->\n'
            f'    <div class="slide-container{active}" id="slide-{idx}" data-author="{author}">\n'
            f"{inner}\n"
            f"    </div>\n"
        )

    # Author CSS (scoped per data-author).
    author_styles = "\n".join(
        f"/* === {name} styles (scoped) === */\n"
        f'{filter_author_css(extract_style_block(src), f""".slide-container[data-author={name!r}]""")}'
        for name, src in (
            ("min", min_html),
            ("cerosop", cero),
            ("xixi", xixi),
            ("ctj", ctj),
        )
    )

    # Author scripts: cerosop only — everyone else has only deck-nav, dropped.
    cero_script = transform_cerosop_script(extract_script_bodies(cero))

    # Build head.
    head_match = re.search(r"(<!DOCTYPE html>.*?</head>\s*<body[^>]*>)", gemini, flags=re.S)
    assert head_match, "gemini.html: failed to find head/body opening"
    head = head_match.group(1)
    head = head.replace(
        "</style>",
        f"\n        /* ===== injected from author files ===== */\n{author_styles}\n{_COMPAT_STYLES}\n        </style>",
        1,
    )

    deck_script = """
    <script>
        (function () {
            const slides = document.querySelectorAll('.slide-container');
            let deckIdx = 0;
            slides.forEach((s, i) => s.classList.toggle('active', i === 0));

            function onEnter(i) {
                const page = i + 1;
                if (page === 9) {
                    // cerosop slide-3 batch admission demo
                    if (typeof window.resetSearchDemo === 'function') {
                        try { window.resetSearchDemo(); } catch (_) {}
                    }
                    if (typeof window.startAutoPlay === 'function') {
                        try { window.startAutoPlay(); } catch (_) {}
                    }
                } else {
                    if (typeof window.stopAutoPlay === 'function') {
                        try { window.stopAutoPlay(); } catch (_) {}
                    }
                }
            }

            function goToSlide(n) {
                slides[deckIdx].classList.remove('active');
                deckIdx = (n + slides.length) % slides.length;
                slides[deckIdx].classList.add('active');
                onEnter(deckIdx);
            }

            document.addEventListener('keydown', (e) => {
                if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'Spacebar' || e.key === 'PageDown') {
                    e.preventDefault();
                    goToSlide(deckIdx + 1);
                } else if (e.key === 'ArrowLeft' || e.key === 'PageUp') {
                    e.preventDefault();
                    goToSlide(deckIdx - 1);
                }
            });

            // Expose for debugging.
            window.deckGoTo = goToSlide;
        })();
    </script>
"""

    # cerosop script must run AFTER the slides exist in DOM so updateSearchDemoUI
    # (if called via onEnter) finds the nodes. We emit slides first, then script.
    author_script_block = ""
    if cero_script.strip():
        author_script_block = (
            "    <script>\n"
            "    // === cerosop interactive demo (sanitized) ===\n"
            f"{cero_script}\n"
            "    </script>\n"
        )

    out = (
        head
        + "\n\n"
        + "".join(rebuilt)
        + "\n"
        + author_script_block
        + deck_script
        + "\n</body>\n</html>\n"
    )
    (HERE / "all.html").write_text(out, encoding="utf-8")
    print(f"wrote all.html ({len(out)} chars)")


if __name__ == "__main__":
    main()
