import asyncio
import sys
import os
from pathlib import Path
from playwright.async_api import async_playwright, TimeoutError
from loguru import logger

# 配置日志
logger.remove()
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
    colorize=True
)

async def main(user_data_dir: str):
    """
    启动一个带界面的浏览器，用于用户手动登录Google账户。
    登录信息将保存在指定的 user_data_dir 中。
    """
    logger.info("--- 首次登录助手 ---")
    logger.info(f"将为 '{user_data_dir}' 目录启动一个新的浏览器实例...")
    logger.info("请在弹出的浏览器窗口中完成以下操作:")
    logger.info("1. 登录您的 Google 账户。")
    logger.info("2. 确保您已在 Gemini 页面。")
    logger.info("3. 登录成功后，请手动关闭整个浏览器窗口。")
    logger.info("脚本将在此之后自动结束，您的会话将被成功保存并截图。")
    logger.info("-" * 40)
    
    # 截图保存路径 (相对于项目根目录)
    screenshot_dir = Path("debug")
    screenshot_dir.mkdir(exist_ok=True)
    session_name = Path(user_data_dir).name
    screenshot_path = screenshot_dir / f"{session_name}_login_success.png"
    
    try:
        async with async_playwright() as p:
            # 启动一个持久化的上下文
            browser_context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=False,  # 必须为 False 才能显示界面让用户操作
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                # 在 Docker 环境中运行时增加稳定性，注意：在 Docker 中启用非 Headless 可能需要额外的 X server 配置
                args=['--no-sandbox', '--disable-setuid-sandbox'] 
            )
            
            page = await browser_context.new_page()
            await page.goto("https://gemini.google.com", timeout=90000)
            
            logger.info("浏览器已打开，正在等待您登录并关闭窗口...")
            
            try:
                # 等待用户手动关闭浏览器，这是一个阻塞操作
                await browser_context.wait_for_event("close", timeout=300000)
            except TimeoutError:
                logger.warning("等待超时 (5分钟)。请手动检查浏览器状态。")
            
            logger.info("浏览器已关闭。尝试验证会话并截图...")
            
            # --- 截图逻辑 ---
            # 重启一个 HEADLESS 实例来安全截图，防止用户在非无头模式下页面状态不稳
            temp_context = await p.chromium.launch_persistent_context(
                user_data_dir,
                headless=True, 
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
            temp_page = await temp_context.new_page()
            await temp_page.goto("https://gemini.google.com/app", timeout=30000)
            await temp_page.wait_for_load_state('networkidle')

            # 检查是否成功登录（通常是通过检查是否有登录按钮或特定的元素）
            title = await temp_page.title()
            if "Gemini" in title and "Sign in" not in title:
                await temp_page.screenshot(path=screenshot_path)
                logger.success(f"✅ 登录会话成功保存！截图已保存到: {screenshot_path}")
            else:
                 logger.error("❌ 警告: 截图显示可能未成功登录。请重新尝试。")

            await temp_context.close()
            
            logger.success(f"\n会话已保存到 '{user_data_dir}'。您现在可以正常启动 gemini-2api 服务了。")

    except Exception as e:
        logger.error(f"启动浏览器或登录过程中发生错误: {e}")
        logger.error("请确保您的环境中已正确安装 Playwright 驱动 ('playwright install chromium')。")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("用法: python playwright_utils/initial_login.py <path_to_user_data_dir>")
        print("示例: python playwright_utils/initial_login.py ./user_data_1")
        sys.exit(1)
    
    data_dir = sys.argv[1]
    # 在 Windows 上运行，需要特定的事件循环策略
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass
    
    asyncio.run(main(data_dir))