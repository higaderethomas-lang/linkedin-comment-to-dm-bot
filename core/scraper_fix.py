import asyncio, json
from pathlib import Path
from playwright.async_api import async_playwright

POST_URL = "https://www.linkedin.com/posts/thomas-higadere_cgp-banquiers-priv%C3%A9s-jai-transform%C3%A9-lemlist-activity-7458025333775261696-ElDW"
COOKIES_PATH = Path("data/linkedin_cookies.json")

async def test():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        cookies = json.loads(COOKIES_PATH.read_text())
        await context.add_cookies(cookies)
        page = await context.new_page()
        await page.goto(POST_URL)
        await asyncio.sleep(5)
        for _ in range(5):
            await page.keyboard.press("End")
            await asyncio.sleep(1)

        seen = set()
        results = []
        links = await page.query_selector_all("a[href*='comment_actor-name']")
        for l in links:
            try:
                href = await l.get_attribute("href")
                slug = href.split("/in/")[1].split("?")[0].rstrip("/")
                profile_url = f"https://www.linkedin.com/in/{slug}"
                if profile_url in seen:
                    continue
                seen.add(profile_url)
                name = (await l.inner_text()).strip()
                first_name = name.split()[0]
                results.append({"profile_url": profile_url, "full_name": name, "first_name": first_name})
                print(f"✅ {name} → {profile_url}")
            except:
                continue

        print(f"\n👥 Total: {len(results)}")
        await browser.close()

asyncio.run(test())
