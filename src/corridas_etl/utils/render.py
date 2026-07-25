"""Renderizacao de paginas com Playwright (para conteudo client-side).

Usado pelos passos de enriquecimento quando o dado nao existe no HTML estatico.
"""

from __future__ import annotations

from ..config import settings

# UA de browser real. Algumas fontes (Running Land, TFSports, LIVE! Run) ficam
# atras de WAF/ISR que recusa — ou serve conteudo vazio para — clientes que nao
# se anunciam como navegador. Usado SO nos caminhos que existem para contornar
# isso; a coleta normal continua com o UA honesto do bot (settings.user_agent).
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


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


def page_html(url: str, *, wait_ms: int = 2500) -> str:
    """Renderiza UMA url em Chromium headless e retorna o HTML final.

    Diferente de `pages_inner_text`, preserva a marcacao — necessario quando o
    parser depende de atributos (href, src), nao so do texto. Usado como
    fallback quando o WAF de uma fonte recusa o cliente HTTP puro: o browser
    real passa nas checagens de TLS/JS, ao custo de subir um Chromium.

    Levanta RuntimeError (nao SystemExit) se o Playwright faltar: assim a falha
    fica isolada na fonte que tentou o fallback, sem derrubar o runner diario.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Este fallback precisa do Playwright:\n"
            '  pip install "corridas-etl[browser]" && playwright install chromium'
        ) from exc

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=BROWSER_UA)
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(wait_ms)
            return page.content()
        finally:
            browser.close()
