import json
import time
from typing import Dict, Any, Optional

DONE_CHUNK = b"data: [DONE]\n\n"

def create_sse_data(data: Dict[str, Any]) -> bytes:
    """将字典格式的数据转换为 SSE 格式的字节串"""
    return f"data: {json.dumps(data)}\n\n".encode('utf-8')

def create_chat_completion_chunk(
    request_id: str,
    model: str,
    content: str,
    finish_reason: Optional[str] = None
) -> Dict[str, Any]:
    """创建与 OpenAI 兼容的流式响应数据块"""
    
    # 构造 delta
    delta = {"content": content}
    
    choice = {
        "index": 0,
        "delta": delta,
        "finish_reason": finish_reason
    }

    # 移除 finish_reason 为 None 的键
    if finish_reason is None:
        choice.pop("finish_reason")

    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice]
    }