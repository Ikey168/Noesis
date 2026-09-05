import asyncio
import os
import pytest
pytest.importorskip("scrapy_playwright")
pytestmark = pytest.mark.skipif(os.environ.get("NOESIS_LIVE_BROWSER") != "1", reason="requires installed Chromium; opt in with NOESIS_LIVE_BROWSER=1")
from types import SimpleNamespace
from src.scraper.spiders.playwright_spider import PlaywrightNewsSpider


def test_repeated_failure_cleanup_releases_page_capacity():
    async def run():
        from playwright.async_api import async_playwright, TimeoutError
        spider=PlaywrightNewsSpider()
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch()
            context=await browser.new_context()
            try:
                for _ in range(6):
                    page=await context.new_page()
                    await page.set_content('<html><body>waiting</body></html>')
                    try:
                        await page.wait_for_selector('article',timeout=30)
                    except TimeoutError as error:
                        await spider.close_failed_page(SimpleNamespace(value=error,request=SimpleNamespace(meta={'playwright_page':page})))
                    assert context.pages==[]
                page=await context.new_page()
                await page.set_content('<article><p>Ready</p></article>')
                await page.wait_for_selector('article',timeout=1000)
                await spider.close_page(page)
                assert context.pages==[]
                delayed=await context.new_page()
                await delayed.set_content('<script>setTimeout(()=>{document.body.innerHTML="<article>Ready</article>"},25)</script>')
                await delayed.wait_for_selector('article',timeout=1000)
                await spider.close_page(delayed)
            finally:
                await context.close()
                await browser.close()
    asyncio.run(run())
