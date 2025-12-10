import asyncio
import json
import sys
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk, filedialog
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from urllib.parse import urlparse, parse_qs, unquote
import re
import queue
import threading
import os

# --- 依赖检查 ---
try:
    from playwright.async_api import async_playwright, BrowserContext
except ImportError:
    PLAYWRIGHT_INSTALLED = False
except Exception:
    PLAYWRIGHT_INSTALLED = False
else:
    PLAYWRIGHT_INSTALLED = True


# --- 0. 核心辅助函数：输入清理和查找 ---

def extract_best_json(text: str) -> Optional[Dict]:
    """
    从混乱的文本中提取最大/最可能的有效 JSON 对象。
    解决了直接正则匹配在包含多个花括号或日志头时失败的问题。
    """
    text = text.strip().replace('\ufeff', '')
    
    # 1. 尝试直接解析
    try:
        return json.loads(text)
    except:
        pass

    # 2. 尝试寻找最外层的 {}
    starts = [m.start() for m in re.finditer(r'\{', text)]
    
    if not starts:
        return None

    # 从最早的起始点开始，尝试寻找能解析的 JSON
    for start in starts:
        # 尝试匹配到字符串末尾的最后一个 }
        end_search = text.rfind('}')
        if end_search == -1 or end_search < start:
            continue
            
        candidate_str = text[start : end_search + 1]
        
        # 优化：尝试去除 JSON 之前的 BOM 或其他非 JSON 字符
        if candidate_str.startswith(')]}\''):
            candidate_str = candidate_str[4:]
        
        try:
            data = json.loads(candidate_str)
            # 确保是字典类型
            if isinstance(data, dict):
                return data 
        except:
            continue
            
    return None

def parse_cookies_from_header_list(headers: List[Dict]) -> Dict[str, str]:
    """从 HAR 格式的 headers 列表中提取 Cookie"""
    cookie_str = ""
    for header in headers:
        # 忽略大小写查找 'Cookie' 头
        if header.get('name', '').lower() == 'cookie':
            cookie_str = header.get('value', '')
            break
    return parse_cookies_from_string(cookie_str)

def parse_cookies_from_string(cookie_string: str) -> Dict[str, str]:
    """从 Cookie 字符串中提取关键 Cookie。"""
    if not cookie_string:
        return {}
        
    # 增加更多相关的 Cookie 名称以提高成功率
    required_names = [
        "__Secure-1PSID", "__Secure-3PSID",
        "__Secure-1PSIDTS", "__Secure-3PSIDTS",
        "SID", "HSID", "SSID", "APISID", "SAPISID",
        "__Secure-1PAPISID", "__Secure-3PAPISID",
        "__Secure-ENID", "AEC", "NID",
        "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
    ]
    
    cookies = {}
    # 处理可能的分隔符：分号后可能跟空格，也可能没有
    parts = cookie_string.split(';')
    for pair in parts:
        if '=' in pair:
            name, value = pair.split('=', 1)
            name = name.strip()
            value = value.strip()
            
            # 只需要包含在 required_names 中的 Cookie
            if name in required_names:
                cookies[name] = value
            # 额外处理：如果用户只粘贴了最重要的 1PSID/3PSID/TS 
            elif name.startswith('__Secure-') and ('PSID' in name or 'TS' in name):
                 cookies[name] = value
                 
    # 仅返回需要的最小集合
    final_cookies = {}
    for name in required_names:
        if name in cookies:
            final_cookies[name] = cookies[name]
            
    # 确保最重要的几个 Cookie 存在
    minimal_required = ["__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS"]
    
    # 再次遍历，确保只包含关键的 PSID/PSIDTS
    final_filtered_cookies = {k: v for k, v in final_cookies.items() if k in minimal_required or ('PSID' in k or 'TS' in k)}

    return final_filtered_cookies

# --- 1. 核心解析逻辑 (在线程池中运行) ---

def _sync_parse_text_segments(text_content: str) -> Tuple[bool, Optional[Dict], str]:
    """同步解析非标准分段文本，并返回日志。"""
    log_messages = ["-> 尝试使用非标准分段文本/正则解析..."]
    
    # 1. 提取 URL (f.sid)
    url_match = re.search(r'(https?://[^\s]*(?:StreamGenerate|StreamGenerate\?)[^\s]*)', text_content)
    f_sid = None
    
    if url_match:
        full_url = url_match.group(1)
        log_messages.append(f"    [成功] 提取到 URL: {full_url[:60]}...")
        url_parsed = urlparse(full_url)
        query_params = parse_qs(url_parsed.query)
        f_sid = query_params.get('f.sid', [None])[0]
    else:
        # 备用：直接在文本中搜索 f.sid
        sid_match = re.search(r'f\.sid\s*[:=]\s*([-0-9]+)', text_content)
        if sid_match:
             f_sid = sid_match.group(1)
             log_messages.append(f"    [成功] 直接正则提取到 f.sid: {f_sid}")

    # 2. 提取 at 参数
    at_param = None
    at_match = re.search(r'at=([^&\s]+)', text_content)
    if not at_match:
        at_match = re.search(r'at\s*[:=]\s*([^\s"]+)', text_content)
    
    if at_match:
        raw_at = at_match.group(1).strip()
        if '%' in raw_at and raw_at.startswith('A'):
            at_param = unquote(raw_at)
        else:
            at_param = raw_at
    
    # 3. 提取 Cookie
    cookie_header_value = ""
    cookie_match = re.search(r'(?:Cookie|cookie):\s*([^\r\n]+)', text_content, re.IGNORECASE)
    if cookie_match:
        cookie_header_value = cookie_match.group(1).strip()
    elif 'SID=' in text_content and '__Secure-1PSID=' in text_content:
        # 如果用户只粘贴了 Cookie 字符串
         cookie_header_value = text_content 

    extracted_cookies = parse_cookies_from_string(cookie_header_value)


    if not f_sid or not at_param:
        log_messages.append(f"    [失败] 动态参数提取不完整 (fSid found: {bool(f_sid)}, at found: {bool(at_param)})。")
        return (False, None, "\n".join(log_messages))
    
    log_messages.append(f"    [成功] 提取到 f.sid 和 at 动态参数。")
    log_messages.append(f"    [状态] 提取到 {len(extracted_cookies)} 个关键 Cookie。")
    
    if len(extracted_cookies) == 0:
        log_messages.append("    [⚠️ 警告] 未能提取到关键 Cookie。")
    
    return (True, {
        "cookies": extracted_cookies,
        "dynamicParams": {
            "fSid": f_sid,
            "at": at_param
        }
    }, "\n".join(log_messages))


def _sync_parse_har_data(har_content: str) -> Tuple[bool, Optional[Dict], str]:
    """同步解析 HAR 文件内容，并返回日志。"""
    log_messages = ["-> 尝试使用标准 HAR/JSON 解析..."]
    
    data = extract_best_json(har_content)
    if not data:
        log_messages.append("    [失败] 未找到有效的 JSON 结构。")
        return (False, None, "\n".join(log_messages))
        
    target_entry = None
    
    # 递归查找包含特定 URL 的 request 对象
    def find_entry(obj):
        if isinstance(obj, dict):
            if 'url' in obj and ('/StreamGenerate' in obj['url'] or 'f.sid' in obj['url']):
                return obj
            if 'request' in obj:
                res = find_entry(obj['request'])
                if res: return res
            
            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    res = find_entry(value)
                    if res: return res
        elif isinstance(obj, list):
            for item in obj:
                res = find_entry(item)
                if res: return res
        return None

    # 优先检查标准的 log -> entries 结构
    if isinstance(data, dict) and 'log' in data and 'entries' in data['log']:
        for entry in reversed(data['log']['entries']):
            if 'request' in entry and 'url' in entry['request']:
                if '/StreamGenerate' in entry['request']['url'] and entry['request'].get('method') == 'POST':
                    target_entry = entry['request']
                    break
    
    if not target_entry:
        target_entry = find_entry(data)

    if not target_entry:
        log_messages.append("    [失败] 未找到 StreamGenerate API 请求记录。")
        return (False, None, "\n".join(log_messages)) 
    
    log_messages.append("    [成功] 找到目标 API 请求记录。")

    # 1. 提取 f.sid
    url_parsed = urlparse(target_entry.get('url', ''))
    query_params = parse_qs(url_parsed.query)
    f_sid = query_params.get('f.sid', [None])[0]
    
    # 2. 提取 at
    at_param = None
    post_data = target_entry.get('postData', {})
    if post_data.get('text'):
        text_data = post_data.get('text', '')
        if 'application/x-www-form-urlencoded' in post_data.get('mimeType', ''):
             params = parse_qs(text_data)
             at_param_encoded = params.get('at', [None])[0]
             at_param = unquote(at_param_encoded) if at_param_encoded else None
        
        if not at_param:
            at_match = re.search(r'at=([^&]+)', text_data)
            if at_match:
                 at_param = unquote(at_match.group(1))

    # 3. 提取 Cookies
    extracted_cookies = {}
    if 'headers' in target_entry:
        # 使用辅助函数解析 headers 列表
        extracted_cookies = parse_cookies_from_header_list(target_entry['headers'])
    elif 'cookies' in target_entry and isinstance(target_entry['cookies'], list):
        # 处理 HAR 中 cookies 字段是列表的情况
        temp_cookie_str = ""
        for c in target_entry['cookies']:
             temp_cookie_str += f"{c['name']}={c['value']}; "
        extracted_cookies = parse_cookies_from_string(temp_cookie_str)


    if not f_sid or not at_param:
        log_messages.append(f"    [失败] 动态参数提取不完整 (fSid: {f_sid}, at: {at_param})。")
        return (False, None, "\n".join(log_messages)) 
    log_messages.append(f"    [成功] 提取到 f.sid 和 at 动态参数。")
    log_messages.append(f"    [状态] 提取到 {len(extracted_cookies)} 个关键 Cookie。")

    if len(extracted_cookies) == 0:
        log_messages.append("    [⚠️ 警告] 请求头中未发现关键 Cookie！")
        
    return (True, {
        "cookies": extracted_cookies,
        "dynamicParams": {
            "fSid": f_sid,
            "at": at_param
        }
    }, "\n".join(log_messages))


def _sync_parse_manual_json(raw_text: str) -> Tuple[bool, Optional[Dict], str]:
    """尝试作为手动粘贴的会话 JSON 结构解析。"""
    log_messages = ["-> 尝试作为手动会话 JSON 解析..."]
    
    manual_json_data = None
    try:
        temp_data = extract_best_json(raw_text)
        if temp_data and isinstance(temp_data, dict):
            if temp_data.get('cookies') and temp_data.get('dynamicParams') and temp_data['dynamicParams'].get('fSid'):
                manual_json_data = temp_data
                if len(manual_json_data['cookies']) == 0:
                    log_messages.append("    [警告] 手动 JSON 结构完整，但 Cookie 列表为空。")
                
                log_messages.append("    [成功] 识别为有效的会话 JSON 结构。")
            else:
                log_messages.append("    [失败] 结构不完整 (缺少 cookies 或 dynamicParams/fSid)。")
                return (False, None, "\n".join(log_messages))
        else:
            log_messages.append("    [失败] 未找到 JSON 结构。")
            return (False, None, "\n".join(log_messages))
    except Exception as e:
        log_messages.append(f"    [失败] JSON 解析错误: {e}")
        return (False, None, "\n".join(log_messages))
    
    return (True, manual_json_data, "\n".join(log_messages))


def _sync_parse_and_validate(raw_text: str) -> Tuple[bool, Optional[Dict], str]:
    """
    同步函数：尝试所有解析方法，返回结果和详细日志。
    """
    
    # 1. 尝试 HAR 文件/JSON 请求解析 (最优先)
    parsed_from_har = _sync_parse_har_data(raw_text)
    if parsed_from_har[0]:
        return (True, parsed_from_har[1], parsed_from_har[2] + "\n✅ 提取成功! (格式: HAR/JSON)")

    # 2. 尝试手动粘贴的会话 JSON 结构解析
    parsed_from_manual = _sync_parse_manual_json(raw_text)
    if parsed_from_manual[0]:
        return (True, parsed_from_manual[1], parsed_from_manual[2] + "\n✅ 提取成功! (格式: 手动 JSON)")

    # 3. 尝试手动粘贴的分段文本解析 (正则兜底，兼容 cURL/Request Headers 格式)
    parsed_from_segments = _sync_parse_text_segments(raw_text)
    if parsed_from_segments[0]:
        return (True, parsed_from_segments[1], parsed_from_segments[2] + "\n✅ 提取成功! (格式: 正则文本)")
    
    # 全部失败，组合详细日志
    final_log = "\n--- ❌ 提取失败：详细解析日志 ---\n" + \
                "--- 1. HAR/JSON 解析尝试 --- \n" + parsed_from_har[2] + "\n" + \
                "--- 2. 手动 JSON 解析尝试 --- \n" + parsed_from_manual[2] + "\n" + \
                "--- 3. 分段文本解析尝试 --- \n" + parsed_from_segments[2] + "\n"
    
    return (False, None, final_log + "\n❌ 粘贴的内容解析失败。请确保您粘贴了包含 StreamGenerate 请求的完整内容。")


# --- 2. Playwright 注入逻辑 (I/O 密集型) ---

def normalize_path(path_str: str) -> str:
    """标准化路径，去除冗余的 ./，并转换为正斜杠"""
    return Path(path_str).resolve().as_posix()

def get_next_available_dir(base_path: Path) -> str:
    """检测下一个可用的 user_data_X 目录，从 1 开始。"""
    i = 1
    while True:
        target_dir = base_path / f"user_data_{i}"
        # 如果目录不存在，或者目录是空的，或者不包含 Playwright/Chrome 的默认配置文件，则认为可用
        if not target_dir.exists() or not (target_dir / "Default").exists():
            return f"./user_data_{i}" 
        else:
            i += 1
            if i > 50: 
                raise RuntimeError("检测到超过 50 个会话目录，请手动清理。")

async def inject_cookies_to_context(
    user_data_dir: str,
    session_data: Dict[str, Any],
    log_queue: queue.Queue 
) -> Tuple[bool, str]:
    """
    执行 Playwright 注入操作。
    :return: (是否成功, 最终日志)
    """
    final_logs = []
    
    def log_async(message, is_error=False):
        """将日志推送到队列，以便主线程安全打印"""
        log_queue.put((message, is_error))
        final_logs.append(message) 

    if not PLAYWRIGHT_INSTALLED:
        log_async("❌ Playwright 依赖缺失或启动失败。请先安装依赖。", is_error=True)
        return (False, "\n".join(final_logs))
        
    normalized_dir = normalize_path(user_data_dir)
    log_async(f"\n--- 注入会话开始 ({normalized_dir}) ---", is_error=False)
    
    Path(normalized_dir).mkdir(parents=True, exist_ok=True)

    domain = session_data.get('cookieDomain', ".google.com")
    path = session_data.get('cookiePath', "/")
    
    cookies_to_inject = []
    current_cookie_count = len(session_data['data']['cookies'])
    
    # 强制检查最重要的四个
    minimal_cookies_found = [k for k in session_data['data']['cookies'].keys() if k in ["__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS"]]
    
    if len(minimal_cookies_found) == 0:
        log_async("⚠️ 严重警告: 未提取到任何 **关键** Cookie！", is_error=True)
        log_async("⚠️ 注入将继续，但没有关键 Cookie，Gemini 服务极大概率无法工作。", is_error=True)
        log_async("⚠️ 请重新导出 HAR 或请求头，确保包含 **PSID** 和 **PSIDTS** Cookie。", is_error=True)
    else:
        log_async(f"  - 发现 {current_cookie_count} 个 Cookie (包含 {len(minimal_cookies_found)} 个关键 Cookie)，准备写入...", is_error=False)
    
    for name, value in session_data['data']['cookies'].items():
        cookies_to_inject.append({
            'name': name,
            'value': value,
            'domain': domain,
            'path': path,
            'secure': True,
            'httpOnly': True,
            'expires': -1 
        })
        log_async(f"  - 准备 Cookie: {name}", is_error=False)
    
    fSid = session_data['data']['dynamicParams'].get('fSid')
    at_param = session_data['data']['dynamicParams'].get('at')
    
    if not fSid or not at_param:
          log_async(f"⚠️ 警告: 动态参数 (fSid/at) 缺失。服务可能无法工作。", is_error=True)
    else:
        log_async(f"  - 动态参数完整: f.sid={fSid}, at={at_param[:10]}...", is_error=False)


    try:
        async with async_playwright() as p:
            log_async("  - 启动 Playwright 浏览器上下文...", is_error=False)
            context: BrowserContext = await p.chromium.launch_persistent_context(
                user_data_dir=normalized_dir,
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-features=IsolateOrigins,site-per-process'] 
            )

            if cookies_to_inject:
                log_async("  - 写入 Cookie 到持久化会话...", is_error=False)
                await context.add_cookies(cookies_to_inject)
            else:
                log_async("  - 跳过 Cookie 写入 (列表为空)。", is_error=False)
                
            await context.close()
            
            log_message = f"✅ 会话数据处理完成。目录: '{normalized_dir}'"
            log_async(log_message, is_error=False)
            return (True, "\n".join(final_logs))

    except Exception as e:
        log_async(f"❌ 注入过程中发生致命错误: {e}", is_error=True)
        log_async("请确保 Playwright 驱动已正确安装 (playwright install chromium)。", is_error=True)
        return (False, "\n".join(final_logs))


# --- 3. Tkinter GUI 界面 ---

class SessionInjectorApp:
    def __init__(self, master, loop):
        self.master = master
        self.loop = loop 
        master.title("Gemini 会话注入工具 (增强版)")
        master.geometry("850x950") # 增加高度以容纳新的输入框
        
        self.log_queue = queue.Queue() # 日志队列
        self.default_base_dir = Path("./")

        # 1. 标题和说明
        tk.Label(master, text="Gemini 会话注入工具 (增强版)", font=("Arial", 16, "bold")).pack(pady=10)
        
        tk.Label(master, 
                      text="步骤: 1. F12 找到 StreamGenerate 请求; 2. 复制 HAR/JSON/请求头粘贴到下方或手动输入 Cookie。", 
                      fg="#333").pack(fill="x", padx=10)
        tk.Label(master, 
                      text="关键 Cookie 位于 'Request Headers' 的 'Cookie' 字段，包含 __Secure-1PSID、__Secure-3PSID 等。", 
                      fg="#0056b3", font=("Arial", 10, "italic")).pack(fill="x", padx=10, pady=(0, 5))

        # 2. 目录选择区域
        dir_frame = ttk.Frame(master)
        dir_frame.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Label(dir_frame, text="目标目录:", anchor="w", font=("Arial", 10, "bold")).pack(side=tk.LEFT, padx=(0, 5))
        self.dir_var = tk.StringVar(value="")
        self.dir_entry = ttk.Entry(dir_frame, textvariable=self.dir_var, width=60)
        self.dir_entry.pack(side=tk.LEFT, expand=True, fill=tk.X)
        self.dir_button = tk.Button(dir_frame, text="选择目录", command=self.select_directory)
        self.dir_button.pack(side=tk.LEFT, padx=(5, 0))
        
        self.auto_dir_button = tk.Button(dir_frame, text="自动创建新目录", command=self.set_auto_new_directory, bg="#2196F3", fg="white")
        self.auto_dir_button.pack(side=tk.LEFT, padx=(5, 0))
        
        # 3. JSON/HAR 输入框
        tk.Label(master, text="粘贴 StreamGenerate 请求内容 (HAR/JSON/文本):", anchor="w", font=("Arial", 10, "bold")).pack(fill="x", padx=10, pady=(5, 0))
        self.json_input = scrolledtext.ScrolledText(master, height=10, width=90, wrap=tk.WORD, font=("Consolas", 9))
        self.json_input.pack(pady=5, padx=10)

        # 4. 手动 Cookie 输入框 (新增)
        tk.Label(master, text="或：手动粘贴关键 Cookie 字符串（SID=...;__Secure-1PSID=...）:", anchor="w", font=("Arial", 10, "bold")).pack(fill="x", padx=10, pady=(5, 0))
        self.cookie_input = scrolledtext.ScrolledText(master, height=3, width=90, wrap=tk.WORD, font=("Consolas", 9))
        self.cookie_input.pack(pady=5, padx=10)
        
        # 5. 注入按钮和进度条框架
        btn_frame = ttk.Frame(master)
        btn_frame.pack(fill=tk.X, padx=10, pady=10)
        
        self.inject_button = tk.Button(btn_frame, text="🚀 开始注入会话", command=self.run_injection, height=2, bg="#4CAF50", fg="white", font=("Arial", 11, "bold"))
        self.inject_button.pack(side=tk.LEFT, expand=True, fill=tk.X)
        
        # 进度条
        self.progress = ttk.Progressbar(btn_frame, orient='horizontal', length=200, mode='indeterminate')
        self.progress.pack(side=tk.RIGHT, padx=10)

        # 6. 结果/日志输出框
        tk.Label(master, text="运行日志:", anchor="w", font=("Arial", 10, "bold")).pack(fill="x", padx=10)
        self.log_output = scrolledtext.ScrolledText(master, height=15, width=90, state=tk.DISABLED, wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9))
        self.log_output.pack(pady=5, padx=10, expand=True, fill=tk.BOTH)
        
        # 配置日志颜色标签
        self.log_output.tag_config('error', foreground='#ff6b6b')
        self.log_output.tag_config('warn', foreground='#feca57')
        self.log_output.tag_config('success', foreground='#1dd1a1')
        self.log_output.tag_config('normal', foreground='#d4d4d4')
        
        # 启动日志轮询器
        master.after(100, self.poll_log_queue)
        
        if not PLAYWRIGHT_INSTALLED:
             self.log("⚠️ 警告: Playwright 依赖可能缺失。请运行 'pip install playwright' 和 'playwright install chromium'。", is_warning=True)

    def select_directory(self):
        """打开对话框让用户选择目标目录"""
        initial_dir = self.dir_var.get() or str(self.default_base_dir)
        directory = filedialog.askdirectory(initialdir=initial_dir, title="选择 Playwright 用户数据目录")
        if directory:
            self.dir_var.set(directory)
            self.log(f"📝 目标目录已设置为: {directory}", is_warning=True)
        
    def set_auto_new_directory(self):
        """自动检测并设置下一个可用的新目录"""
        try:
            new_dir = get_next_available_dir(self.default_base_dir)
            self.dir_var.set(new_dir)
            self.log(f"📝 已自动选择新目录: {new_dir}", is_warning=False)
        except RuntimeError as e:
            self.log(f"❌ 自动创建目录失败: {e}", is_error=True)
            messagebox.showerror("错误", str(e))


    def log(self, message, is_error=False, is_warning=False, is_success=False):
        """将信息安全地打印到 GUI 日志区域，并强制刷新。"""
        self.log_output.config(state=tk.NORMAL)
        
        tag = "normal"
        if is_error or "❌" in message: tag = "error"
        elif is_warning or "⚠️" in message: tag = "warn"
        elif is_success or "成功" in message or "✅" in message or "✨" in message: tag = "success"
            
        self.log_output.insert(tk.END, message + "\n", tag)
        self.log_output.see(tk.END)
        self.log_output.config(state=tk.DISABLED)
        self.master.update_idletasks()


    def poll_log_queue(self):
        """Tkinter 主线程定期检查日志队列并安全更新 GUI。"""
        while not self.log_queue.empty():
            message, is_error = self.log_queue.get()
            is_warn = "警告" in message or "⚠️" in message
            self.log(message, is_error=is_error, is_warning=is_warn)
        
        self.master.after(100, self.poll_log_queue)


    def run_injection(self):
        """处理按钮点击事件，启动异步任务（非阻塞）"""
        self.log_output.config(state=tk.NORMAL)
        self.log_output.delete(1.0, tk.END)
        self.log_output.config(state=tk.DISABLED)
        
        raw_text = self.json_input.get(1.0, tk.END).strip()
        manual_cookie_text = self.cookie_input.get(1.0, tk.END).strip()
        target_dir = self.dir_var.get().strip()

        if not raw_text and not manual_cookie_text:
            self.log("❌ 请先粘贴请求内容或手动输入 Cookie！", is_error=True)
            return

        if not target_dir:
            try:
                target_dir = get_next_available_dir(self.default_base_dir)
                self.dir_var.set(target_dir)
                self.log(f"📝 未指定目录，自动创建到: {target_dir}", is_warning=True)
            except RuntimeError as e:
                self.log(f"❌ 目录错误: {e}", is_error=True)
                return

        self.inject_button.config(state=tk.DISABLED, text="⏳ 处理中...")
        self.progress.start()
        
        # 启动异步任务
        task = self.loop.create_task(self.full_injection_task(raw_text, manual_cookie_text, target_dir))
        task.add_done_callback(self.on_injection_done)
        
    async def full_injection_task(self, raw_text: str, manual_cookie_text: str, target_dir: str) -> Tuple[bool, str]:
        """异步任务协调器"""
        
        def log_safe(message, is_error=False):
            self.log_queue.put((message, is_error))

        # --- 1. 解析 ---
        log_safe("🔍 [1/2] 正在解析内容...", is_error=False)
        
        # 使用 run_in_executor 在单独的线程中运行同步解析函数
        future = self.loop.run_in_executor(
            None, 
            _sync_parse_and_validate,
            raw_text
        )
        
        try:
            success, session_data_inner, logs = await future
            log_safe(logs)
        except Exception as e:
            log_safe(f"❌ 解析线程异常: {e}", is_error=True)
            return (False, "解析线程失败。")

        # --- 1.1 Cookie 补充/覆盖逻辑 (新增) ---
        if success:
            extracted_cookies = session_data_inner.get('cookies', {})
            
            if manual_cookie_text:
                manual_cookies = parse_cookies_from_string(manual_cookie_text)
                if manual_cookies:
                    log_safe(f"🔗 [补充] 发现手动输入的 {len(manual_cookies)} 个关键 Cookie。")
                    # 使用手动 Cookie 覆盖和补充自动解析的结果
                    extracted_cookies.update(manual_cookies)
                else:
                    log_safe("⚠️ [警告] 无法解析手动输入的 Cookie，请检查格式。", is_error=True)

            
            # 最终检查 Cookie
            if not extracted_cookies and session_data_inner.get('dynamicParams'):
                 # 如果动态参数提取成功，但 Cookie 仍然为空，则判定为 Cookie 缺失
                 log_safe("❌ [致命] 提取到动态参数，但最终 Cookie 仍为空。注入将失败。", is_error=True)
                 return (False, "Cookie 缺失。")

            session_data_inner['cookies'] = extracted_cookies
            
        elif manual_cookie_text:
             # 如果自动解析失败，但用户提供了手动 Cookie，我们尝试从 Cookie 中提取 fSid/at
             # 但由于 fSid/at 无法从 Cookie 中提取，这里只能要求用户确保主输入框包含动态参数
             log_safe("⚠️ [警告] 自动解析失败，但发现手动 Cookie。请确保主输入框包含 URL 和 POST 参数以便提取 fSid 和 at。", is_error=True)
             return (False, "自动解析失败且无法提取动态参数。")
        else:
             # 自动解析失败且没有手动 Cookie 补充
             return (False, "解析失败。")

        # --- 2. 注入 ---
        full_session_data = {
            "data": session_data_inner,
            "cookieDomain": ".google.com",
            "cookiePath": "/"
        }
        
        log_safe(f"🔨 [2/2] 启动 Playwright 注入 -> {target_dir}", is_error=False)
        
        return await inject_cookies_to_context(target_dir, full_session_data, self.log_queue)
            

    def on_injection_done(self, task):
        """回调函数，处理任务结果并更新 GUI"""
        self.inject_button.config(state=tk.NORMAL, text="🚀 开始注入会话")
        self.progress.stop()
        
        try:
            success, full_logs = task.result()
            
            # 尝试从日志中提取目录名
            match = re.search(r"目录: '(.*?)'", full_logs)
            target_dir = match.group(1) if match else self.dir_var.get()
            
            # 清理路径以获取索引
            dir_name = Path(target_dir).name
            dir_index = dir_name.split('_')[-1] if 'user_data_' in dir_name else "?"
            
            if success:
                self.log_queue.put(("\n" + "=" * 60, False))
                self.log_queue.put(("✨ 注入流程结束。请检查上方是否有警告，特别是 Cookie 数量。", True))
                self.log_queue.put((f"1. .env 配置: PLAYWRIGHT_USER_DATA_DIR_{dir_index}={target_dir}", False))
                self.log_queue.put((f"2. Docker 挂载: - {target_dir}:/app/{target_dir}", False))
                self.log_queue.put(("=" * 60, False))
                messagebox.showinfo("完成", f"处理完成。\n目录: {target_dir}\n请查看日志确认 Cookie 是否成功写入。")
            else:
                self.log_queue.put((f"\n❌ 流程失败，请查看日志！", True))
                messagebox.showerror("失败", "流程失败，请查看日志。")

        except asyncio.CancelledError:
            self.log_queue.put(("⚠️ 任务取消。", True))
        except Exception as e:
            self.log_queue.put((f"❌ 未知错误: {e}", True))
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    # Windows 兼容性设置
    if sys.platform == "win32":
        try:
            # 确保 Windows 下使用 ProactorEventLoop
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    root = tk.Tk()
    app = SessionInjectorApp(root, loop)
    
    # 将 asyncio loop 驱动到 Tkinter 的主循环中
    def run_asyncio_loop_driver():
        try:
            # 运行已准备好的 Future/Task
            loop.run_until_complete(asyncio.sleep(0))
        except Exception:
            # 捕获异常，防止主循环中断
            pass
        root.after(10, run_asyncio_loop_driver)

    root.after(10, run_asyncio_loop_driver)
    root.mainloop()