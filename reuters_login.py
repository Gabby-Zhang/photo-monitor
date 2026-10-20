#!/usr/bin/env python3
"""
Reuters Connect 登录助手(仅 Mac 本机)。

Reuters Connect 搜索必须登录,且登录有设备验证,只能人手过。本脚本打开
photo_library_monitor 用的同一个持久浏览器资料夹(~/.reuters_connect_profile),
你手动登录一次,登录状态就存下来供监控复用。

日志里出现 "LOGIN EXPIRED" 时重跑一次本脚本即可。

Usage:
  python3 reuters_login.py           # 登录 + 用监控的抓取函数试搜一次
"""

import asyncio

from playwright.async_api import async_playwright

import photo_library_monitor as m


async def main():
    m.REUTERS_PROFILE.mkdir(exist_ok=True)
    async with async_playwright() as pw:
        ctx = await m.open_reuters_context(pw)
        if not ctx:
            return
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(f"{m.REUTERS_BASE}/login", timeout=60000)
        # input() 放线程里跑,浏览器在等待期间保持响应
        await asyncio.to_thread(input, "\n>>> 在弹出的窗口里登录(含设备验证),看到正常页面后回这里按回车…")

        for person, cfg in m.PERSONS.items():
            for q in cfg["queries"]:
                res = await m.scrape_reuters_connect(page, q)
                print(f"\n=== {q}: {len(res)} 条 ===")
                for r in res[:10]:
                    print(f"-  {r['title'][:90]}\n     {r['url']}")
        await asyncio.to_thread(input, "\n看完后按回车关闭浏览器…")
        await ctx.close()


if __name__ == "__main__":
    asyncio.run(main())
