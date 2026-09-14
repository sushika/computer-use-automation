import asyncio, os
from dotenv import load_dotenv
from anthropic import Anthropic
from playwright.async_api import async_playwright

load_dotenv()

def check_llm():
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=50,
        messages=[{"role": "user", "content": "Reply with exactly: OK"}],
    )
    print("LLM:", msg.content[0].text.strip())

async def check_browser():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()
        await page.goto("https://example.com")
        print("BROWSER:", await page.title())
        await asyncio.sleep(2)
        await browser.close()

if __name__ == "__main__":
    check_llm()
    asyncio.run(check_browser())