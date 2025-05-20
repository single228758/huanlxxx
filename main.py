import os
import asyncio
import time
import random
import tomllib # Or tomli for older Python versions
import base64
import aiohttp
import tempfile
from loguru import logger

from WechatAPI import WechatAPIClient
from utils.decorators import on_text_message, on_image_message
from utils.plugin_base import PluginBase

API_HEADERS = {
    "accept": "*/*",
    "accept-language": "zh-CN,zh;q=0.9",
    "origin": "https://beart.ai",
    "priority": "u=1, i",
    "referer": "https://beart.ai/",
    "sec-ch-ua": '"Google Chrome";v="129", "Not=A?Brand";v="8", "Chromium";v="129"', # Example, might need update
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"', # Example
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36" # Example
}

# Supported image MIME types for validation, used by _validate_image
SUPPORTED_IMAGE_SIGNATURES = {
    b'\xFF\xD8\xFF': 'image/jpeg',      # JPEG
    b'\x89PNG\r\n\x1a\n': 'image/png', # PNG
    b'GIF87a': 'image/gif',         # GIF87a
    b'GIF89a': 'image/gif',         # GIF89a
    b'RIFF': 'image/webp',          # WEBP (check for 'WEBP' at offset 8)
    b'BM': 'image/bmp'             # BMP
}

class HuanlxxxPlugin(PluginBase):
    description = "BeArt AI 换脸插件 (XXXBot 框架)"
    author = "xxxbot团伙"
    version = "1.0.0"
    
    # MIME type mapping for sending to API
    MIME_MAP = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.bmp': 'image/bmp'
    }

    def __init__(self):
        super().__init__()
        self.enable = False
        self.trigger_prefix = "换脸"
        self.beart_product_serial = ""
        self.http_session = None # Will be initialized in async_init
        self.waiting_for_images = {}  # Stores user state: {session_id: "source" or "target"}
        self.image_data = {}          # Stores image bytes: {session_id: {"source": bytes, "target": bytes}}

        config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        try:
            with open(config_path, "rb") as f:
                config = tomllib.load(f)
            
            basic_config = config.get("basic", {})
            self.enable = basic_config.get("enable", False)
            self.trigger_prefix = basic_config.get("trigger_prefix", "换脸")
            self.beart_product_serial = basic_config.get("beart_product_serial", "7ccd9ec0944184501659484ed36d6550")
            
            if not self.beart_product_serial:
                logger.warning("[Huanlxxx] BeArt product serial is not configured in config.toml. API calls might fail.")

            logger.info(f"[Huanlxxx] Plugin initialized. Enabled: {self.enable}, Trigger: '{self.trigger_prefix}'")
        except FileNotFoundError:
            logger.error(f"[Huanlxxx] config.toml not found at {config_path}. Plugin will be disabled.")
            self.enable = False
        except Exception as e:
            logger.error(f"[Huanlxxx] Error loading config.toml: {e}. Plugin will be disabled.")
            self.enable = False

    async def async_init(self):
        """Perform async initialization."""
        if self.enable:
            self.http_session = aiohttp.ClientSession()
            logger.info("[Huanlxxx] aiohttp.ClientSession initialized.")
        return await super().async_init()

    async def on_disable(self):
        """Called when the plugin is disabled."""
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()
            logger.info("[Huanlxxx] aiohttp.ClientSession closed.")
        self.waiting_for_images.clear()
        self.image_data.clear()
        logger.info("[Huanlxxx] Plugin disabled and states cleared.")
        return await super().on_disable()

    def _get_session_id(self, message: dict) -> str:
        """Generates a unique session ID for user state management."""
        is_group = message.get("is_group", False)
        if is_group:
            group_id = message.get("ChatRoomWxid") # Group chat ID
            user_id = message.get("SenderWxid")    # Actual sender in group
            if group_id and user_id:
                return f"group_{group_id}_user_{user_id}"
            logger.warning(f"[Huanlxxx] Could not determine group session ID from message: {message}")
            # Fallback, though not ideal for group context if IDs are missing
            return message.get("FromWxid", message.get("MsgId", "unknown_session")) 
        else:
            # For private chats, FromWxid is the other user.
            # SenderWxid might be our bot's ID or the user's, depending on message direction.
            # Using FromWxid should be consistent for user identification.
            return message.get("FromWxid", message.get("MsgId", "unknown_session"))


    async def _send_reply(self, bot: WechatAPIClient, recipient_wxid: str, text: str):
        """Helper to send text messages, potentially splitting long ones."""
        try:
            
            await bot.send_text_message(recipient_wxid, text)
        except Exception as e:
            logger.error(f"[Huanlxxx] Failed to send message to {recipient_wxid}: {e}")
            
    @on_text_message(priority=60) # Mid-priority
    async def handle_text_message(self, bot: WechatAPIClient, message: dict):
        if not self.enable or not self.http_session:
            return True # Allow other plugins

        content = message.get("Content", "").strip()
        from_wxid = message.get("FromWxid") # The user who sent the message or group ID

        if not from_wxid:
            logger.warning("[Huanlxxx] No FromWxid in text message, cannot process.")
            return True

        session_id = self._get_session_id(message)

        if content == self.trigger_prefix:
            self.waiting_for_images[session_id] = "source"
            self.image_data[session_id] = {}
            logger.info(f"[Huanlxxx] User {session_id} triggered face swap. Waiting for source image.")
            await self._send_reply(bot, from_wxid, "请发送一张带有人脸的源图片 (模板图，脸部清晰).")
            return False # Block other plugins

        # Optional: Add a cancel command
        if content.lower() in ["取消换脸", "取消操作", "stop swap"]:
            if session_id in self.waiting_for_images or session_id in self.image_data:
                self.waiting_for_images.pop(session_id, None)
                self.image_data.pop(session_id, None)
                logger.info(f"[Huanlxxx] User {session_id} cancelled face swap process.")
                await self._send_reply(bot, from_wxid, "换脸操作已取消。")
                return False
        
        return True

    async def _get_image_bytes_from_message(self, bot: WechatAPIClient, message: dict) -> bytes | None:
        """
        Extracts image data from a message.
        XXXBot appears to provide image data as a Base64 string in message['Content']
        after automatically downloading it.
        """
        content = message.get("Content")
        msg_id = message.get("MsgId")

        if isinstance(content, bytes) and len(content) > 100: # Arbitrary check for some data
             logger.debug(f"[Huanlxxx] Image data received directly as bytes (len: {len(content)}).")
             return content

        if isinstance(content, str):
            # Priority 1: Try direct Base64 decoding from content string
            if len(content) > 200: # Heuristic: Base64 strings are usually long
                try:
                    # Ensure no data URI prefix is mistakenly included if it's raw base64
                    str_to_decode = content
                    if content.startswith("data:image") and ";base64," in content:
                         # This case will be handled by the next block if direct decode fails
                         # or if we want to be more specific for data URIs.
                         # For now, if it has the prefix, let the dedicated data URI logic handle it.
                         pass # Let it fall through to data URI check or refine later
                    else: # Assumed to be raw Base64
                        image_bytes = base64.b64decode(str_to_decode)
                        if len(image_bytes) > 100: # Basic validation of decoded data
                            logger.info(f"[Huanlxxx] Successfully decoded raw Base64 image from message.Content (len: {len(image_bytes)}).")
                            return image_bytes
                        else:
                            logger.warning(f"[Huanlxxx] Decoded raw Base64 from message.Content but data is too small (len: {len(image_bytes)}). Might be invalid.")
                except base64.binascii.Error as e:
                    logger.debug(f"[Huanlxxx] Failed to decode message.Content as raw Base64: {e}. Will try other methods.")
                except Exception as e_direct_b64:
                    logger.warning(f"[Huanlxxx] Unexpected error during direct Base64 decoding from message.Content: {e_direct_b64}. Will try other methods.")
            
            # Priority 2: Check if it's a data URI (e.g., "data:image/jpeg;base64,...")
            if content.startswith("data:image") and ";base64," in content:
                try:
                    base64_data = content.split(";base64,", 1)[1]
                    image_bytes = base64.b64decode(base64_data)
                    logger.info(f"[Huanlxxx] Successfully decoded Base64 image from data URI (len: {len(image_bytes)}).")
                    return image_bytes
                except Exception as e:
                    logger.warning(f"[Huanlxxx] Failed to decode Base64 data URI from message.Content: {e}")
            
            # Priority 3: Check if it's a file path
            # This is less likely for incoming messages from a bot framework but kept for robustness.
            elif os.path.isfile(content):
                try:
                    with open(content, "rb") as f:
                        image_bytes = f.read()
                    logger.info(f"[Huanlxxx] Successfully read image from file path in message.Content: {content} (len: {len(image_bytes)}).")
                    return image_bytes
                except Exception as e:
                    logger.error(f"[Huanlxxx] Error reading image from file path {content}: {e}")
            
            # Priority 4: Check if it's an HTTP/HTTPS URL
            elif content.startswith(("http://", "https://")) and self.http_session:
                try:
                    logger.info(f"[Huanlxxx] message.Content appears to be a URL. Attempting download: {content}")
                    async with self.http_session.get(content, timeout=30) as response:
                        if response.status == 200:
                            image_bytes = await response.read()
                            logger.info(f"[Huanlxxx] Successfully downloaded image from URL in message.Content: {content} (len: {len(image_bytes)}).")
                            return image_bytes
                        else:
                            logger.error(f"[Huanlxxx] Failed to download image from URL {content}, status: {response.status}")
                except Exception as e:
                    logger.error(f"[Huanlxxx] Error downloading image from URL {content}: {e}")
        
        # Fallback information if all methods fail:
        logger.error(f"[Huanlxxx] All attempts to extract image bytes from message (MsgId: {msg_id}) failed. Content type: {type(content)}. Content snippet: {str(content)[:200] if content else 'None'}")
        logger.error("[Huanlxxx] Review how XXXBot framework provides image data. "
                     "Commonly, message['Content'] should contain Base64 after framework download, "
                     "or a specific bot API like 'await bot.download_image(message)' might be needed.")
        return None

    def _validate_image(self, image_data: bytes) -> str | None:
        """Validates image format by checking its signature and returns MIME type or None."""
        if not image_data or len(image_data) < 12: # Basic check
            return None
        
        for signature, mime_type in SUPPORTED_IMAGE_SIGNATURES.items():
            if image_data.startswith(signature):
                if mime_type == 'image/webp': # WEBP needs an additional check
                    if len(image_data) >= 12 and image_data[8:12] == b'WEBP':
                        return mime_type
                else:
                    return mime_type
        logger.warning("[Huanlxxx] Image validation failed: Unknown format.")
        return None

    def _get_api_mime_type(self, image_data: bytes) -> str:
        """Gets MIME type for API upload, falling back if _validate_image fails (should not happen if validated before)."""
        mime = self._validate_image(image_data)
        if mime:
            return mime
        # Fallback, though ideally validation should prevent this.
        # BeArt API might be flexible, but sending correct MIME is better.
        logger.warning("[Huanlxxx] _get_api_mime_type: Could not determine specific MIME, defaulting to image/jpeg.")
        return 'image/jpeg'


    @on_image_message(priority=60) # Same priority as text to catch image replies
    async def handle_image_message(self, bot: WechatAPIClient, message: dict):
        if not self.enable or not self.http_session:
            return True

        from_wxid = message.get("FromWxid")
        if not from_wxid:
            logger.warning("[Huanlxxx] No FromWxid in image message, cannot process.")
            return True
            
        session_id = self._get_session_id(message)

        if session_id not in self.waiting_for_images:
            logger.debug(f"[Huanlxxx] Received image from {session_id} but not waiting for one. Ignoring.")
            return True # Not part of an active swap process for this user

        logger.info(f"[Huanlxxx] Received image from {session_id}. Current state: {self.waiting_for_images[session_id]}")
        
        # Attempt to clear any previous error messages for this user if they send a new image
        # (This is a simple way, could be more sophisticated)
        # await bot.clear_last_error_message_for_user(from_wxid) # Fictional method
        
        image_bytes = await self._get_image_bytes_from_message(bot, message)

        if not image_bytes:
            await self._send_reply(bot, from_wxid, "无法获取图片数据，请换一张图片或稍后再试。")
            # Do not clear state here, user might try sending another image immediately.
            return False

        validated_mime = self._validate_image(image_bytes)
        if not validated_mime:
            await self._send_reply(bot, from_wxid, "图片格式不支持或已损坏。请发送 JPG, PNG, GIF, WEBP, 或 BMP 格式的图片。")
            return False

        current_stage = self.waiting_for_images[session_id]
        user_image_data = self.image_data.setdefault(session_id, {})

        if current_stage == "source":
            user_image_data["source"] = image_bytes
            self.waiting_for_images[session_id] = "target"
            logger.info(f"[Huanlxxx] Source image received for {session_id}. Waiting for target image.")
            await self._send_reply(bot, from_wxid, "源图片 (模板图) 已收到。现在请发送您想将脸换到上面的目标图片。")
        
        elif current_stage == "target":
            user_image_data["target"] = image_bytes
            logger.info(f"[Huanlxxx] Target image received for {session_id}. Starting face swap process.")
            await self._send_reply(bot, from_wxid, "目标图片已收到，正在处理换脸，请稍候...")
            
            # Perform the face swap
            await self._process_face_swap_and_reply(bot, from_wxid, session_id)
            
            # Clean up state for this session
            self.waiting_for_images.pop(session_id, None)
            self.image_data.pop(session_id, None)
            logger.info(f"[Huanlxxx] Face swap process finished for {session_id}. State cleared.")
        
        else:
            logger.warning(f"[Huanlxxx] Unknown state '{current_stage}' for session {session_id}. Resetting.")
            self.waiting_for_images.pop(session_id, None)
            self.image_data.pop(session_id, None)
            await self._send_reply(bot, from_wxid, "发生内部状态错误，请重新使用触发词开始。")

        return False # Block other plugins as we've handled it


    async def _process_face_swap_and_reply(self, bot: WechatAPIClient, recipient_wxid: str, session_id: str):
        if not self.http_session or self.http_session.closed:
            logger.error("[Huanlxxx] HTTP session is not available for face swap.")
            await self._send_reply(bot, recipient_wxid, "处理失败：网络服务未初始化。")
            return

        source_image_bytes = self.image_data[session_id].get("source")
        target_image_bytes = self.image_data[session_id].get("target")

        if not source_image_bytes or not target_image_bytes:
            logger.error(f"[Huanlxxx] Missing source or target image for session {session_id}.")
            await self._send_reply(bot, recipient_wxid, "处理失败：图片数据不完整，请重新开始。")
            return

        job_id = await self._create_face_swap_job(source_image_bytes, target_image_bytes)
        if not job_id:
            await self._send_reply(bot, recipient_wxid, "创建换脸任务失败，请稍后再试或检查日志。")
            return

        result_url = await self._get_face_swap_result(job_id)
        if not result_url:
            await self._send_reply(bot, recipient_wxid, "获取换脸结果失败或超时，请稍后再试。")
            return

        # Download the result image
        try:
            logger.info(f"[Huanlxxx] Downloading result image from: {result_url}")
            async with self.http_session.get(result_url, timeout=60) as response:
                if response.status == 200:
                    result_image_bytes = await response.read()
                    # Save to a temporary file to send with WechatAPIClient
                    # WechatAPIClient.send_image_message likely takes a file path.
                    # with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp_file: # Suffix can be from result MIME if available
                    #     tmp_file.write(result_image_bytes)
                    #     tmp_file_path = tmp_file.name
                    
                    # logger.info(f"[Huanlxxx] Result image downloaded (size: {len(result_image_bytes)}B). Sending to {recipient_wxid} from {tmp_file_path}")
                    # await bot.send_image_message(recipient_wxid, tmp_file_path)
                    
                    logger.info(f"[Huanlxxx] Result image downloaded (size: {len(result_image_bytes)}B). Sending bytes to {recipient_wxid}")
                    await bot.send_image_message(recipient_wxid, result_image_bytes)
                    logger.info(f"[Huanlxxx] Result image sent successfully to {recipient_wxid}.")
                    
                    # Clean up the temporary file
                    # try:
                    #     os.remove(tmp_file_path)
                    #     logger.debug(f"[Huanlxxx] Temporary file {tmp_file_path} removed.")
                    # except OSError as e_remove:
                    #     logger.warning(f"[Huanlxxx] Could not remove temporary file {tmp_file_path}: {e_remove}")
                else:
                    logger.error(f"[Huanlxxx] Failed to download result image, status: {response.status}, URL: {result_url}")
                    await self._send_reply(bot, recipient_wxid, f"下载结果图片失败 (HTTP {response.status})。")
        except Exception as e:
            logger.error(f"[Huanlxxx] Exception during result image download or sending: {e}")
            await self._send_reply(bot, recipient_wxid, f"处理结果图片时发生错误: {e}")


    async def _create_face_swap_job(self, source_image_bytes: bytes, target_image_bytes: bytes) -> str | None:
        if not self.http_session or self.http_session.closed:
            logger.error("[Huanlxxx] _create_face_swap_job: HTTP session not available.")
            return None
            
        url = "https://api.beart.ai/api/beart/face-swap/create-job"
        headers = API_HEADERS.copy()
        # The 'product-code' is in API_HEADERS, 'product-serial' is specific
        headers["product-serial"] = self.beart_product_serial 
        # Content-Type for multipart/form-data is set automatically by aiohttp.FormData

        source_mime = self._get_api_mime_type(source_image_bytes)
        target_mime = self._get_api_mime_type(target_image_bytes)
        
        # Generate random enough filenames
        source_filename = f"source_{random.getrandbits(32):08x}.{source_mime.split('/')[-1]}"
        target_filename = f"target_{random.getrandbits(32):08x}.{target_mime.split('/')[-1]}"

        form_data = aiohttp.FormData()
        # BeArt API expects 'swap_image' as the source/template face, and 'target_image' as the image to put the face onto.
        # Original huanl.py: "target_image": (target_name, source_image, target_mime), "swap_image": (source_name, target_image, source_mime)
        # This seems swapped. Let's follow original huanl.py for field names carefully:
        #   'target_image' API field gets the ORIGINAL source_image_bytes (template face image)
        #   'swap_image' API field gets the ORIGINAL target_image_bytes (image to change)
        form_data.add_field('target_image', source_image_bytes, filename=source_filename, content_type=source_mime)
        form_data.add_field('swap_image', target_image_bytes, filename=target_filename, content_type=target_mime)
        
        logger.info(f"[Huanlxxx] Creating face swap job. Source: {len(source_image_bytes)}B ({source_mime}), Target: {len(target_image_bytes)}B ({target_mime})")

        try:
            async with self.http_session.post(url, headers=headers, data=form_data, timeout=60) as response:
                if response.status == 200:
                    resp_json = await response.json()
                    if resp_json.get("code") == 100000: # Success code from BeArt
                        job_id = resp_json.get("result", {}).get("job_id")
                        if job_id:
                            logger.info(f"[Huanlxxx] Face swap job created successfully: {job_id}")
                            return job_id
                        else:
                            logger.error(f"[Huanlxxx] BeArt API success but no job_id in response: {resp_json}")
                    else:
                        error_msg = resp_json.get("message", {}).get("zh", str(resp_json))
                        logger.error(f"[Huanlxxx] BeArt API error (code {resp_json.get('code')}): {error_msg}")
                else:
                    response_text = await response.text()
                    logger.error(f"[Huanlxxx] Failed to create face swap job, HTTP status: {response.status}. Response: {response_text[:500]}")
            return None
        except asyncio.TimeoutError:
            logger.error("[Huanlxxx] Timeout during face swap job creation.")
            return None
        except Exception as e:
            logger.error(f"[Huanlxxx] Exception during face swap job creation: {e}")
            return None

    async def _get_face_swap_result(self, job_id: str, max_retries: int = 30, interval_seconds: int = 3) -> str | None:
        if not self.http_session or self.http_session.closed:
            logger.error("[Huanlxxx] _get_face_swap_result: HTTP session not available.")
            return None

        url = f"https://api.beart.ai/api/beart/face-swap/get-job/{job_id}"
        headers = API_HEADERS.copy()
        headers["content-type"] = "application/json; charset=UTF-8" # As in original plugin
        # product-serial might not be needed for GET job status, but can be kept
        headers["product-serial"] = self.beart_product_serial 


        logger.info(f"[Huanlxxx] Polling for result of job_id: {job_id} (max {max_retries} retries, {interval_seconds}s interval)")
        for attempt in range(max_retries):
            try:
                async with self.http_session.get(url, headers=headers, timeout=20) as response:
                    if response.status == 200:
                        resp_json = await response.json()
                        api_code = resp_json.get("code")
                        
                        if api_code == 100000: # Success
                            outputs = resp_json.get("result", {}).get("output", [])
                            if outputs and isinstance(outputs, list) and len(outputs) > 0:
                                result_url = outputs[0] # Assuming first URL is the desired one
                                logger.info(f"[Huanlxxx] Job {job_id} completed. Result URL: {result_url}")
                                return result_url
                            else:
                                logger.error(f"[Huanlxxx] Job {job_id} successful but no output URL found: {resp_json}")
                                return None
                        elif api_code == 300001: # Processing
                            logger.info(f"[Huanlxxx] Job {job_id} still processing... (Attempt {attempt + 1}/{max_retries})")
                        else: # Other error codes
                            error_msg = resp_json.get("message", {}).get("zh", str(resp_json))
                            logger.error(f"[Huanlxxx] BeArt API error polling job {job_id} (code {api_code}): {error_msg}")
                            return None # Stop polling on definitive API error
                    else: # HTTP error
                        response_text = await response.text()
                        logger.error(f"[Huanlxxx] HTTP error polling job {job_id}: Status {response.status}. Response: {response_text[:500]}")
                        # Depending on error, might retry or return None. For now, retry.
                
                await asyncio.sleep(interval_seconds) # Wait before next poll

            except asyncio.TimeoutError:
                logger.warning(f"[Huanlxxx] Timeout polling job {job_id} (Attempt {attempt + 1}/{max_retries}). Retrying...")
                await asyncio.sleep(interval_seconds) # Wait after timeout before retrying
            except Exception as e:
                logger.error(f"[Huanlxxx] Exception polling job {job_id}: {e}. Retrying... (Attempt {attempt + 1}/{max_retries})")
                await asyncio.sleep(interval_seconds) # Wait after exception

        logger.error(f"[Huanlxxx] Max retries reached for job {job_id}. Could not get result.")
        return None

# To ensure the plugin is discoverable by the XXXBot plugin manager (if it uses a direct load)
# This is usually handled by the __init__.py in the plugin directory.
if __name__ == "__main__":
    # This part is for testing purposes if you run the file directly,
    # it won't be executed when loaded by XXXBot.
    logger.info("Huanlxxx main.py executed directly (for testing/dev).")
    # Example: Test config loading
    # plugin = HuanlxxxPlugin()
    # print(f"Plugin enabled: {plugin.enable}")
    # print(f"Trigger prefix: {plugin.trigger_prefix}")
    # print(f"Product Serial: {plugin.beart_product_serial}")

    # To test async methods, you'd need an event loop:
    # async def test_run():
    #     plugin_instance = HuanlxxxPlugin()
    #     if plugin_instance.enable:
    #         await plugin_instance.async_init()
    #         # ... further test calls ...
    #         await plugin_instance.on_disable()
    #
    # asyncio.run(test_run())
    pass 