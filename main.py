import sys
from contextlib import asynccontextmanager
from typing import Optional
import time
import traceback
import httpx 

from fastapi import FastAPI, Request, HTTPException, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from app.core.config import settings
from app.providers.gemini_provider import GeminiProvider 

# --- 配置 Loguru ---
logger.remove()
logger.add(
    sys.stdout,
    level="INFO",
    format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>",
    colorize=True
)

provider: Optional[GeminiProvider] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global provider
    logger.info(f"应用启动中... {settings.APP_NAME} v{settings.APP_VERSION}")
    provider = GeminiProvider()
    await provider.initialize()
    num_sessions = len(provider.browser_pool)
    logger.info(f"服务已在 'Headless-Browser-Interaction' 模式下初始化 {num_sessions} 个可用浏览器实例。")
    if num_sessions == 0:
        logger.error("🚫 浏览器实例启动失败！请检查 Playwright 依赖和系统环境。")
    logger.info(f"服务将在 http://localhost:{settings.NGINX_PORT} 上可用")
    yield
    await provider.close()
    logger.info("应用关闭，浏览器实例已清理。")

# Uvicorn 正在寻找的 FastAPI 应用实例
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description=settings.DESCRIPTION,
    lifespan=lifespan
)

async def verify_api_key(authorization: Optional[str] = Header(None)):
    if settings.API_MASTER_KEY and settings.API_MASTER_KEY != "1":
        if not authorization or "bearer" not in authorization.lower():
            raise HTTPException(status_code=401, detail="需要 Bearer Token 认证。")
        token = authorization.split(" ")[-1]
        if token != settings.API_MASTER_KEY:
            raise HTTPException(status_code=403, detail="无效的 API Key。")

@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)], response_model=None, response_class=JSONResponse)
async def chat_completions(request: Request):
    if not provider or not provider.browser_pool:
        raise HTTPException(status_code=503, detail="服务不可用：浏览器实例未启动或初始化失败。")
    try:
        request_data = await request.json()
        return await provider.chat_completion(request_data) 
    except Exception as e:
        logger.error(f"处理聊天请求时发生顶层错误: {e}", exc_info=False)
        logger.error(f"顶层调用栈追踪:\n{traceback.format_exc(limit=5)}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=f"内部服务器错误: {str(e)}")

@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    return JSONResponse(content={
        "object": "list", 
        "data": [{"id": name, "object": "model", "created": int(time.time()), "owned_by": "Google"} for name in settings.KNOWN_MODELS]
    })
        
@app.get("/", summary="根路径", include_in_schema=False)
def root():
    if not provider:
        raise HTTPException(status_code=503, detail="服务初始化失败，请检查应用日志。")
    if not provider.browser_pool:
        raise HTTPException(status_code=503, detail="服务初始化成功，但浏览器实例池为空。请检查日志。")
        
    return {"message": f"欢迎来到 {settings.APP_NAME} v{settings.APP_VERSION}. 服务运行正常。"}