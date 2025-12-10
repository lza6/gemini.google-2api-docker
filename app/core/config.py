import os
import uuid
# 修正这里的导入，确保只使用正确的名称 SettingsConfigDict
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
from loguru import logger

class Settings(BaseSettings):
    # 关键修复：修正类名拼写错误
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        extra="ignore"
    )

    APP_NAME: str = "gemini-2api"
    APP_VERSION: str = "1.0.0"
    DESCRIPTION: str = "一个将 gemini.google.com 转换为兼容 OpenAI 格式 API 的高性能代理，使用 Playwright 维护会话。"

    API_MASTER_KEY: Optional[str] = None
    NGINX_PORT: int = 8088
    PLAYWRIGHT_POOL_SIZE: int = 3

    API_REQUEST_TIMEOUT: int = 180
    
    API_REQUEST_TIMEOUT: int = 180
    DEFAULT_MODEL: str = "gemini-pro"
    KNOWN_MODELS: List[str] = ["gemini-pro"]

    def __init__(self, **values):
        super().__init__(**values)

settings = Settings()