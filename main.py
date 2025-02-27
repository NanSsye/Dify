import json
import re
import tomllib
import traceback

import aiohttp
import filetype
from loguru import logger
from typing import Union

from WechatAPI import WechatAPIClient
from database.XYBotDB import XYBotDB
from utils.decorators import *
from utils.plugin_base import PluginBase


class Dify(PluginBase):
    description = "Dify插件"
    author = "HenryXiaoYang/老夏的金库"
    version = "1.2.0"

    # Change Log
    # 1.1.0 2025-02-20 插件优先级，插件阻塞
    # 1.2.0 2025-02-22 有插件阻塞了，other-plugin-cmd可删了

    def __init__(self):
        super().__init__()

        with open("main_config.toml", "rb") as f:
            config = tomllib.load(f)

        self.admins = config["XYBot"]["admins"]

        with open("plugins/Dify/config.toml", "rb") as f:
            config = tomllib.load(f)

        plugin_config = config["Dify"]

        self.enable = plugin_config["enable"]
        self.api_key = plugin_config["api-key"]
        self.base_url = plugin_config["base-url"]

        self.commands = plugin_config["commands"]
        self.command_tip = plugin_config["command-tip"]

        self.price = plugin_config["price"]
        self.admin_ignore = plugin_config["admin_ignore"]
        self.whitelist_ignore = plugin_config["whitelist_ignore"]

        self.http_proxy = plugin_config["http-proxy"]
        
        # 语音配置
        self.tts_enable = plugin_config.get("tts-enable", False)
        self.tts_voice = plugin_config.get("tts-voice", 6)
        self.tts_type = plugin_config.get("tts-type", "baidu")

        self.db = XYBotDB()

    @on_text_message(priority=20)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        command = str(message["Content"]).strip().split(" ")

        if (not command or command[0] not in self.commands) and message["IsGroup"]:  # 不是指令，且是群聊
            return
        elif len(command) == 1 and command[0] in self.commands:  # 只是指令，但没请求内容
            await bot.send_at_message(message["FromWxid"], "\n" + self.command_tip, [message["SenderWxid"]])
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            await self.dify(bot, message, message["Content"])
        return False

    @on_at_message(priority=20)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            await self.dify(bot, message, message["Content"])

        return False

    @on_voice_message(priority=20)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], message["Content"])

            files = [
                {
                    "type": "audio",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    @on_image_message(priority=20)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], bot.base64_to_byte(message["Content"]))

            files = [
                {
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    @on_video_message(priority=20)
    async def handle_video(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], bot.base64_to_byte(message["Video"]))

            files = [
                {
                    "type": "video",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    @on_file_message(priority=20)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        if await self._check_point(bot, message):
            upload_file_id = await self.upload_file(message["FromWxid"], message["Content"])

            files = [
                {
                    "type": "document",
                    "transfer_method": "local_file",
                    "upload_file_id": upload_file_id
                }
            ]

            await self.dify(bot, message, " \n", files)

        return False

    async def dify(self, bot: WechatAPIClient, message: dict, query: str, files=None):
        if files is None:
            files = []
        conversation_id = self.db.get_llm_thread_id(message["FromWxid"],
                                                    namespace="dify")
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        payload = json.dumps({
            "inputs": {},
            "query": query,
            "response_mode": "streaming",
            "conversation_id": conversation_id,
            "user": message["FromWxid"],
            "files": files,
            "auto_generate_name": False,
        })
        url = f"{self.base_url}/chat-messages"

        ai_resp = ""
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.post(url=url, headers=headers, data=payload) as resp:
                if resp.status == 200:
                    # 读取响应
                    async for line in resp.content:  # 流式传输
                        line = line.decode("utf-8").strip()
                        if not line or line == "event: ping":  # 空行或ping
                            continue
                        elif line.startswith("data: "):  # 脑瘫吧，为什么前面要加 "data: " ？？？
                            line = line[6:]

                        try:
                            resp_json = json.loads(line)
                        except json.decoder.JSONDecodeError:
                            logger.error(f"Dify返回的JSON解析错误，请检查格式: {line}")

                        event = resp_json.get("event", "")
                        if event == "message":  # LLM 返回文本块事件
                            ai_resp += resp_json.get("answer", "")
                        elif event == "message_replace":  # 消息内容替换事件
                            ai_resp = resp_json("answer", "")
                        elif event == "message_file":  # 文件事件 目前dify只输出图片
                            await self.dify_handle_image(bot, message, resp_json.get("url", ""))
                        elif event == "tts_message":  # TTS 音频流结束事件
                            await self.dify_handle_audio(bot, message, resp_json.get("audio", ""))
                        elif event == "error":  # 流式输出过程中出现的异常
                            await self.dify_handle_error(bot, message,
                                                         resp_json.get("task_id", ""),
                                                         resp_json.get("message_id", ""),
                                                         resp_json.get("status", ""),
                                                         resp_json.get("code", ""),
                                                         resp_json.get("message", ""))

                    new_con_id = resp_json.get("conversation_id", "")
                    if new_con_id and new_con_id != conversation_id:
                        self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")

                elif resp.status == 404:
                    self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                    return await self.dify(bot, message, query)

                elif resp.status == 400:
                    return await self.handle_400(bot, message, resp)

                elif resp.status == 500:
                    return await self.handle_500(bot, message)

                else:
                    return await self.handle_other_status(bot, message, resp)

        if ai_resp:
            await self.dify_handle_text(bot, message, ai_resp)

    async def upload_file(self, user: str, file: bytes):
        headers = {"Authorization": f"Bearer {self.api_key}"}

        # user multipart/form-data
        kind = filetype.guess(file)
        formdata = aiohttp.FormData()
        formdata.add_field("user", user)
        formdata.add_field("file", file, filename=kind.extension, content_type=kind.mime)

        url = f"{self.base_url}/files/upload"

        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.post(url, headers=headers, data=formdata) as resp:
                resp_json = await resp.json()

        return resp_json.get("id", "")

    async def dify_handle_text(self, bot: WechatAPIClient, message: dict, text: str):
        # 处理媒体文件链接
        pattern = r"\]\((https?:\/\/[^\s\)]+)\)"
        links = re.findall(pattern, text)
        for url in links:
            file = await self.download_file(url)
            extension = filetype.guess_extension(file)
            if extension in ('wav', 'mp3'):
                await bot.send_voice_message(message["FromWxid"], voice=file, format=filetype.guess_extension(file))
            elif extension in ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg'):
                await bot.send_image_message(message["FromWxid"], file)
            elif extension in ('mp4', 'avi', 'mov', 'mkv', 'flv'):
                await bot.send_video_message(message["FromWxid"], video=file, image="None")

        # 清理文本中的链接标记
        pattern = r'\[[^\]]+\]\(https?:\/\/[^\s\)]+\)'
        text = re.sub(pattern, '', text)
        
        if text:
            if self.tts_enable:
                # 语音模式
                logger.info(f"开始处理语音转换，类型: {self.tts_type}, 音色ID: {self.tts_voice}")
                try:
                    # 使用新的语音API
                    tts_api_url = "https://xiaoapi.cn/API/zs_tts.php"
                    params = {
                        "type": self.tts_type,  # 使用配置的语音类型
                        "msg": text,
                        "id": self.tts_voice
                    }
                    
                    async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                        async with session.get(tts_api_url, params=params) as resp:
                            if resp.status == 200:
                                result = await resp.json()
                                if result["code"] == 200:
                                    voice_url = result["tts"]
                                    logger.info(f"成功获取语音URL: {voice_url}")
                                    try:
                                        # 下载语音文件
                                        async with session.get(voice_url) as audio_resp:
                                            if audio_resp.status == 200:
                                                audio_data = await audio_resp.read()
                                                if len(audio_data) > 0:
                                                    try:
                                                        # 发送语音消息
                                                        await bot.send_voice_message(message["FromWxid"], audio_data, format="mp3")
                                                        logger.info("语音消息发送成功")
                                                        return  # 语音发送成功,直接返回
                                                    except Exception as e:
                                                        logger.error(f"语音发送失败: {str(e)}")
                                                else:
                                                    logger.error("获取到的音频数据为空")
                                            else:
                                                logger.error(f"语音下载失败,状态码: {audio_resp.status}")
                                    except Exception as e:
                                        logger.error(f"语音下载过程出错: {str(e)}")
                                else:
                                    logger.error(f"语音合成API返回错误: {result}")
                            else:
                                logger.error(f"语音合成API请求失败,状态码: {resp.status}")
                                
                except Exception as e:
                    logger.error(f"语音转换过程出错: {str(e)}")
                    
                # 如果语音处理失败,发送文字消息作为备选
                await bot.send_at_message(message["FromWxid"], "\n" + text, [message["SenderWxid"]])
            else:
                # 文字模式
                await bot.send_at_message(message["FromWxid"], "\n" + text, [message["SenderWxid"]])

    async def download_file(self, url: str) -> bytes:
        async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
            async with session.get(url) as resp:
                return await resp.read()

    async def dify_handle_image(self, bot: WechatAPIClient, message: dict, image: Union[str, bytes]):
        if isinstance(image, str) and image.startswith("http"):
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.get(image) as resp:
                    image = bot.byte_to_base64(await resp.read())
        elif isinstance(image, bytes):
            image = bot.byte_to_base64(image)

        await bot.send_image_message(message["FromWxid"], image)

    @staticmethod
    async def dify_handle_audio(bot: WechatAPIClient, message: dict, audio: str):

        await bot.send_voice_message(message["FromWxid"], audio)

    @staticmethod
    async def dify_handle_error(bot: WechatAPIClient, message: dict, task_id: str, message_id: str, status: str,
                                code: int, err_message: str):
        output = ("-----XYBot-----\n"
                  "🙅对不起，Dify出现错误！\n"
                  f"任务 ID：{task_id}\n"
                  f"消息唯一 ID：{message_id}\n"
                  f"HTTP 状态码：{status}\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_400(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        output = ("-----XYBot-----\n"
                  "🙅对不起，出现错误！\n"
                  f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = "-----XYBot-----\n🙅对不起，Dify服务内部异常，请稍后再试。"
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = ("-----XYBot-----\n"
                   f"🙅对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_at_message(message["FromWxid"], "\n" + ai_resp, [message["SenderWxid"]])

    @staticmethod
    async def hendle_exceptions(bot: WechatAPIClient, message: dict):
        output = ("-----XYBot-----\n"
                  "🙅对不起，出现错误！\n"
                  f"错误信息：\n"
                  f"{traceback.format_exc()}")
        await bot.send_at_message(message["FromWxid"], "\n" + output, [message["SenderWxid"]])

    async def _check_point(self, bot: WechatAPIClient, message: dict) -> bool:
        wxid = message["SenderWxid"]

        if wxid in self.admins and self.admin_ignore:
            return True
        elif self.db.get_whitelist(wxid) and self.whitelist_ignore:
            return True
        else:
            if self.db.get_points(wxid) < self.price:
                await bot.send_at_message(message["FromWxid"],
                                          f"\n-----XYBot-----\n"
                                          f"😭你的积分不够啦！需要 {self.price} 积分",
                                          [wxid])
                return False

            self.db.add_points(wxid, -self.price)
            return True
