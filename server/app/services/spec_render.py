"""Render a structured spec dict → branded HTML → PDF.

HTML: Jinja2 over `templates/spec.html` (the More-Than branded template).
PDF:  headless Chromium via Playwright. Chromium is used (not WeasyPrint) because
      the template relies on flexbox and CSS paged-media footers, which Chromium
      reproduces faithfully and WeasyPrint does not.

Requires, once per environment:
    pip install playwright && python -m playwright install --with-deps chromium
"""

import logging
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


log = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def render_spec_html(spec: dict) -> str:
    return _env().get_template("spec.html").render(spec=spec)


def render_pdf_from_html(html: str) -> bytes:
    """HTML string → PDF bytes via headless Chromium (Playwright, sync API)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            page = browser.new_page()
            # `networkidle` lets the Google-Fonts @import settle before printing.
            page.set_content(html, wait_until="networkidle")
            pdf = page.pdf(
                format="A4",
                print_background=True,
                prefer_css_page_size=True,
            )
        finally:
            browser.close()
    return pdf


def render_spec_pdf(spec: dict) -> bytes:
    """Convenience: structured spec dict → PDF bytes."""
    html = render_spec_html(spec)
    pdf = render_pdf_from_html(html)
    log.info("spec pdf rendered", extra={"bytes": len(pdf), "sections": len(spec.get("sections", []))})
    return pdf
