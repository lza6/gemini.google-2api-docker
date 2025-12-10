import json
import time
import asyncio
import random
import re
from typing import Dict, Any, AsyncGenerator, List, Optional, Tuple
from pathlib import Path
from urllib.parse import parse_qs, urlparse, unquote_plus
import traceback
import httpx# 保持导入，用于客户端初始化

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger
from playwright.async_api import async_playwright, Playwright, BrowserContext, Browser, Error as PlaywrightError, Route 

# 导入 BaseProvider
from app.core.config import settings
from app.providers.base_provider import BaseProvider
from app.utils.sse_utils import create_sse_data, create_chat_completion_chunk, DONE_CHUNK

# 调试目录常量
DEBUG_DIR = Path("debug")
DEBUG_DIR.mkdir(exist_ok=True)

class BrowserInstance:
    """封装 Playwright Browser实例及其锁"""
    def __init__(self, browser: Browser, name: str):
        self.browser = browser
        self.lock = asyncio.Lock()
        self.name = name

class GeminiProvider(BaseProvider):
    def __init__(self):
        self.playwright: Optional[Playwright] = None
        self.browser_pool: List[BrowserInstance] = [] # 浏览器实例池
        self.client = httpx.AsyncClient(timeout=settings.API_REQUEST_TIMEOUT)

    async def initialize(self):
        """初始化 Playwright 和浏览器实例池"""
        self.playwright = await async_playwright().start()
        
        logger.info("注意: 采用 Playwright 提取 + 伪流式返回方案。")

        for i in range(settings.PLAYWRIGHT_POOL_SIZE):
            session_name = f"Browser-Instance-{i+1}"
            try:
                # 启动一个常驻的 Browser 实例
                browser = await self.playwright.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox', 
                        '--disable-setuid-sandbox', 
                        '--disable-features=IsolateOrigins,site-per-process',
                        '--disable-blink-features=AutomationControlled'
                    ],
                )
                self.browser_pool.append(BrowserInstance(browser, session_name))
                logger.success(f"✅ {session_name} 浏览器实例已成功加载。")

            except PlaywrightError as e:
                logger.error(f"❌ Playwright 初始化 {session_name} 失败: {e}")
            except Exception as e:
                logger.error(f"❌ 初始化 {session_name} 发生未知错误: {e}")

        if not self.browser_pool:
            logger.error("🚫 所有浏览器实例初始化失败。服务将无法工作。")
        else:
            logger.success(f"✅ {len(self.browser_pool)} 个浏览器实例已成功加载（纯匿名非持久化模式启动）。")

    async def close(self):
        """清理资源"""
        for instance in self.browser_pool:
            await instance.browser.close()  
        if self.playwright:
            await self.playwright.stop()
        await self.client.aclose()
    
    # 辅助函数：提取用户的最新请求
    def _get_latest_user_message(self, request_data: Dict[str, Any]) -> str:
        messages = request_data.get("messages", [])
        for m in reversed(messages):
            if m.get('role') == 'user':
                return m.get('content') or "Hello" # 确保不为空
        return "Hello" # 默认值


    async def _get_and_extract_answer(self, instance: BrowserInstance, latest_user_message: str) -> Tuple[str, 'page', 'context']:
        """
        核心方法：模拟交互，让浏览器生成答案，并从 DOM 中提取最终的完整回答。
        这个函数包含了参数提取、等待答案完成和最终答案提取的所有逻辑。
        
        :return: (extracted_answer_text, page, context)
        """
        session_name = instance.name
        video_output_dir = DEBUG_DIR.as_posix()
        
        context: BrowserContext = await instance.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            record_video_dir=video_output_dir,
            record_video_size={"width": 1280, "height": 720}
        )
        
        page = await context.new_page()
        
        # ---------------------
        # 步骤 0: 设置网络拦截器 (让请求通过，不再提取参数)
        # ---------------------
        
        # 此时我们不关心参数，只关心请求能正常发出和完成
        await page.route("**/*", lambda route: route.continue_())

        # ---------------------
        # 步骤 1/4: 导航和模拟交互
        # ---------------------
        
        try:
            TIMEOUT_USER_ACTION = 10000 
            
            logger.info(f"  - 会话 {session_name}: [步骤1] 导航到 Gemini 首页...")
            await page.goto("https://gemini.google.com/app", timeout=30000)
                
            TEXT_INPUT_SELECTOR = 'rich-textarea div.ql-editor'
            SEND_BUTTON_SELECTOR = 'button[aria-label*="Send"], button.send-button' 
            ACTIVE_SEND_BUTTON_SELECTOR = 'button[aria-label*="Send"]:not([aria-disabled="true"]), button.send-button:not([aria-disabled="true"])'
            
            # 确保输入框可见
            await page.wait_for_selector(TEXT_INPUT_SELECTOR, timeout=TIMEOUT_USER_ACTION)
            
            # 1. **关键步骤：输入逗号 (,) 激活按钮**
            logger.info("    -> 填充逗号 (,) 激活发送按钮...")
            await page.type(TEXT_INPUT_SELECTOR, ",", delay=50) 
            
            # 2. **填充用户的完整请求**
            full_input = f"{latest_user_message}"
            logger.info(f"    -> 填充用户消息: {full_input[:50]}...")
            await page.fill(TEXT_INPUT_SELECTOR, full_input, timeout=5000)
            
            # 3. **点击发送**
            logger.info("    -> 点击发送按钮，等待回答生成...")
            
            # 触发请求，并等待浏览器完成答案生成
            # 这里我们只等待一个网络响应完成，表示开始生成答案。
            await page.click(ACTIVE_SEND_BUTTON_SELECTOR, timeout=3000)
            
            
            # ---------------------
            # 步骤 5: 等待答案完成并提取文本
            # ---------------------
            
            # 等待发送按钮重新禁用 (表示回答结束)
            ANSWER_FINISHED_SELECTOR = SEND_BUTTON_SELECTOR + '[aria-disabled="true"]'
            
            try:
                # 等待按钮变禁用
                await page.wait_for_selector(ANSWER_FINISHED_SELECTOR, timeout=40000) # 延长超时以适应长回答
                logger.success("    -> 答案生成完毕 (发送按钮重新禁用)。")

            except PlaywrightError as e:
                logger.warning(f"    -> 答案等待超时，尝试提取当前可见答案。错误: {e}")
            
            # 提取最终答案文本
            ANSWER_CONTENT_SELECTOR = 'message-content' 
            
            extracted_answer = "Error: Failed to extract response text."
            try:
                answer_locator = page.locator(ANSWER_CONTENT_SELECTOR)
                last_answer_block = answer_locator.last
                
                # 使用 inner_text() 获取渲染后的文本（包括 Markdown 标记）
                extracted_answer = await last_answer_block.inner_text() 
                
            except Exception as e:
                logger.error(f"提取答案文本失败: {e}")
                
            
            
            # --- 最终检查和返回 ---
            
            if not extracted_answer or extracted_answer.startswith("Error:"):
                 # 如果提取失败，尝试获取 body 的文本，作为最后的调试手段
                 last_resort_text = await page.content()
                 logger.error(f"❌ Playwright 提取失败。HTML 内容片段: {last_resort_text[:500]}...")
                 raise RuntimeError(f"Playwright 提取失败。提取结果: {extracted_answer}")

            logger.success(f"🔑 会话 {session_name} 答案提取成功。")
            
            # 返回提取到的文本和资源，以便在 chat_completion 中处理清理
            return extracted_answer, page, context
            
        except Exception as e:
            logger.error(f"❌ Playwright 模拟交互/提取过程中发生严重错误: {e}")
            
            try:
                await context.close()
            except:
                pass
            raise e

    # -----------------------------------------------
    # 伪流式生成器 (用于模拟流式体验)
    # -----------------------------------------------
    async def _pseudo_stream_generator(self, extracted_text: str, request_id: str, model_name: str) -> AsyncGenerator[bytes, None]:
        
        # 将答案文本分成小块，模拟流式效果
        # 使用正则表达式按空格或标点符号分割，保留 Markdown 格式
        chunks = re.findall(r'(\*\*.*?\*\*|\n\n|\s|[^ \n]+)', extracted_text, re.DOTALL)
        
        if not chunks:
            chunks = [extracted_text] # 如果无法分割，发送整个文本

        for chunk in chunks:
            if chunk:
                # 兼容Markdown，但不转义
                yield create_sse_data(create_chat_completion_chunk(request_id, model_name, chunk))
                # 引入微小延迟来模拟流式传输感
                await asyncio.sleep(0.01) 
        
        # 发送结束标记
        yield DONE_CHUNK


    def _create_openai_json_response(self, text_content: str) -> Dict[str, Any]:
        """将提取的完整答案封装成非流式的 OpenAI JSON 格式。"""
        # 保持原始 Markdown 格式
        cleaned_text = text_content.strip() 
        
        logger.info(f"📝 最终返回内容 (长度: {len(cleaned_text)}): {cleaned_text[:200]}...")

        return {
            "id": f"chatcmpl-{int(time.time())}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": settings.DEFAULT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": cleaned_text
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0
            }
        }


    async def chat_completion(self, request_data: Dict[str, Any]) -> [JSONResponse, StreamingResponse]:
        """
        处理聊天请求，返回伪流式 StreamingResponse 或非流式 JSONResponse。
        """
        if not self.browser_pool:
            raise HTTPException(status_code=503, detail="服务不可用：浏览器实例池为空。")
        
        is_streaming_request = request_data.get("stream") is True
        latest_user_message = self._get_latest_user_message(request_data)
        instance = random.choice(self.browser_pool)
        
        page = None
        context = None
        
        # 锁住实例，执行交互和提取
        async with instance.lock:
            try:
                # 运行 Playwright 交互并提取完整答案
                extracted_text, page, context = await self._get_and_extract_answer(instance, latest_user_message)
            except Exception as e:
                error_msg = f"无法从浏览器获取完整答案。错误: {e}"
                logger.error(f"会话 {instance.name} 失败: {e}")
                raise HTTPException(status_code=502, detail=error_msg)
        
        # --- Playwright 提取成功，处理清理和录屏 ---
        
        video_output_dir = DEBUG_DIR.as_posix()

        async def cleanup_and_save_video(p, c):
            """在后台任务中保存录屏并关闭 Playwright 资源"""
            try:
                video = p.video
                video_filename = Path(await video.path()).name 
                final_video_path = Path(video_output_dir) / video_filename
                
                Path(final_video_path).parent.mkdir(parents=True, exist_ok=True)
                await video.save_as(final_video_path)
                logger.info(f"🎥 录屏已保存到: {final_video_path.as_posix()}")
            except Exception as e:
                logger.warning(f"无法保存录屏: {e}")
            finally:
                if c: await c.close()
        
        # 创建异步任务来处理录屏和清理，确保主线程不阻塞
        asyncio.create_task(cleanup_and_save_video(page, context))
        
        # -----------------------------------------
        # 返回响应 (伪流式或非流式)
        # -----------------------------------------
        
        if is_streaming_request:
            # 客户端请求流式，返回伪流式 StreamingResponse
            logger.info("🟢 客户端请求流式响应，返回伪流式 StreamingResponse。")
            return StreamingResponse(
                self._pseudo_stream_generator(extracted_text, "chatcmpl-pseudo", settings.DEFAULT_MODEL),
                media_type="text/event-stream"
            )

        else:
            # 客户端请求非流式，返回完整 JSONResponse
            response_data = self._create_openai_json_response(extracted_text)
            logger.info(f"✅ 成功返回非流式答案。长度: {len(extracted_text)}")
            return JSONResponse(content=response_data)


    async def get_models(self) -> JSONResponse:
        return JSONResponse(content={
            "object": "list",
            "data": [{"id": name, "object": "model", "created": int(time.time()), "owned_by": "Google"} for name in settings.KNOWN_MODELS]
        }
    )