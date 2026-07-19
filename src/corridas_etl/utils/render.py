"""Renderizacao de paginas com Playwright (para conteudo client-side).

Usado pelos passos de enriquecimento quando o dado nao existe no HTML estatico.
"""

from __future__ import annotations

from ..config import settings


def pages_inner_text(urls: list[str], *, wait_ms: int = 2500) -> dict[str, str]:
    """Renderiza cada URL em um Chromium headless e retorna {url: innerText}.

    Reusa um unico browser para toda a lista (barato) e respeita o rate limit
    entre navegacoes.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Este passo precisa do Playwright:\n"
            '  pip install "corridas-etl[browser]" && playwright install chromium'
        )

    texts: dict[str, str] = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(user_agent=settings.user_agent)
        for i, url in enumerate(urls):
            if i > 0:
                page.wait_for_timeout(int(settings.request_delay_seconds * 1000))
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(wait_ms)  # JS hidrata o conteudo
                texts[url] = page.inner_text("body")
            except Exception:
                texts[url] = ""
        browser.close()
    return texts
