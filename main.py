import io
import json
import re
import subprocess
import tomllib
from typing import Optional, Union, Dict, List, Tuple
import time
from dataclasses import dataclass, field
from datetime import datetime
import asyncio
from collections import defaultdict
from enum import Enum
import urllib.parse
import mimetypes
import base64

import aiohttp
import filetype
from loguru import logger
import speech_recognition as sr
import os
from WechatAPI import WechatAPIClient
from database.XYBotDB import XYBotDB
from utils.decorators import *
from utils.plugin_base import PluginBase
from gtts import gTTS
import traceback
import shutil
from PIL import Image
import xml.etree.ElementTree as ET

# 添加API代理导入
try:
    from api_manager_integrator import has_api_manager_feature
    has_api_proxy = has_api_manager_feature()
    if has_api_proxy:
        logger.info("API管理中心可用，Dify插件将使用API代理")
    else:
        logger.info("API管理中心不可用，Dify插件将使用直接连接")
except ImportError:
    has_api_proxy = False
    logger.warning("未找到API管理中心集成模块，Dify插件将使用直接连接")

# 常量定义
XYBOT_PREFIX = "-----老夏的金库-----\n"
DIFY_ERROR_MESSAGE = "🙅对不起，Dify出现错误！\n"
INSUFFICIENT_POINTS_MESSAGE = "😭你的积分不够啦！需要 {price} 积分"
VOICE_TRANSCRIPTION_FAILED = "\n语音转文字失败"
TEXT_TO_VOICE_FAILED = "\n文本转语音失败"
# 聊天室相关常量已移除

# 聊天室相关类已移除

@dataclass
class ModelConfig:
    api_key: str
    base_url: str
    trigger_words: list[str]
    price: int
    wakeup_words: list[str] = field(default_factory=list)  # 添加唤醒词列表字段

class Dify(PluginBase):
    description = "Dify插件"
    author = "老夏的金库"
    version = "1.4.0"  # 更新版本号 - 移除聊天室功能
    is_ai_platform = True  # 标记为 AI 平台插件

    def __init__(self):
        super().__init__()
        self.user_models = {}  # 存储用户当前使用的模型
        try:
            with open("main_config.toml", "rb") as f:
                config = tomllib.load(f)
            self.admins = config["XYBot"]["admins"]
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.error(f"加载主配置文件失败: {e}")
            raise

        try:
            with open("plugins/Dify/config.toml", "rb") as f:
                config = tomllib.load(f)
            plugin_config = config["Dify"]
            self.enable = plugin_config["enable"]
            self.default_model = plugin_config["default-model"]
            self.command_tip = plugin_config["command-tip"]
            self.commands = plugin_config["commands"]
            self.admin_ignore = plugin_config["admin_ignore"]
            self.whitelist_ignore = plugin_config["whitelist_ignore"]
            self.http_proxy = plugin_config["http-proxy"]
            self.voice_reply_all = plugin_config["voice_reply_all"]
            self.robot_names = plugin_config.get("robot-names", [])
            # 移除单独的 URL 配置，改为动态构建
            self.remember_user_model = plugin_config.get("remember_user_model", True)
            # 聊天室功能已移除
            self.support_agent_mode = plugin_config.get("support_agent_mode", True)  # 添加Agent模式支持开关

            # 加载所有模型配置
            self.models = {}
            for model_name, model_config in plugin_config.get("models", {}).items():
                self.models[model_name] = ModelConfig(
                    api_key=model_config["api-key"],
                    base_url=model_config["base-url"],
                    trigger_words=model_config["trigger-words"],
                    price=model_config["price"],
                    # 如果有唤醒词配置则加载,否则使用空列表
                    wakeup_words=model_config.get("wakeup-words", [])
                )

            # 设置当前使用的模型
            self.current_model = self.models[self.default_model]
        except (FileNotFoundError, tomllib.TOMLDecodeError) as e:
            logger.error(f"加载Dify插件配置文件失败: {e}")
            raise

        self.db = XYBotDB()
        self.image_cache = {}
        self.image_cache_timeout = 60
        # 添加文件缓存
        self.file_cache = {}
        self.file_cache_timeout = 300  # 5分钟文件缓存超时
        # 添加文件存储目录配置
        self.files_dir = "files"
        # 创建文件存储目录
        os.makedirs(self.files_dir, exist_ok=True)

        # 添加Agent模式相关属性
        self.current_agent_thoughts = {}  # 存储当前Agent思考过程，格式: {conversation_id: [thought1, thought2, ...]}
        self.agent_files = {}  # 存储Agent生成的文件，格式: {file_id: {url: "", type: "", belongs_to: ""}}

        # 创建唤醒词到模型的映射
        self.wakeup_word_to_model = {}
        logger.info("开始加载唤醒词配置:")
        for model_name, model_config in self.models.items():
            logger.info(f"处理模型 '{model_name}' 的唤醒词列表: {model_config.wakeup_words}")
            for wakeup_word in model_config.wakeup_words:
                if wakeup_word in self.wakeup_word_to_model:
                    old_model = next((name for name, config in self.models.items()
                                     if config == self.wakeup_word_to_model[wakeup_word]), '未知')
                    logger.warning(f"唤醒词冲突! '{wakeup_word}' 已绑定到模型 '{old_model}'，"
                                  f"现在被覆盖绑定到 '{model_name}'")
                self.wakeup_word_to_model[wakeup_word] = model_config
                logger.info(f"唤醒词 '{wakeup_word}' 成功绑定到模型 '{model_name}'")

        logger.info(f"唤醒词映射完成，共加载 {len(self.wakeup_word_to_model)} 个唤醒词")

        # 加载配置文件
        self.config_path = os.path.join(os.path.dirname(__file__), "config.toml")
        logger.info(f"加载Dify插件配置文件：{self.config_path}")

        # 尝试获取API代理实例
        self.api_proxy = None
        if has_api_proxy:
            try:
                import sys
                # 导入api_proxy实例
                sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))
                from admin.server import get_api_proxy
                self.api_proxy = get_api_proxy()
                if self.api_proxy:
                    logger.info("成功获取API代理实例")
                else:
                    logger.warning("API代理实例获取失败，将使用直接连接")
            except Exception as e:
                logger.error(f"获取API代理实例失败: {e}")
                logger.error(traceback.format_exc())

    def get_user_model(self, user_id: str) -> ModelConfig:
        """获取用户当前使用的模型"""
        if self.remember_user_model and user_id in self.user_models:
            return self.user_models[user_id]
        return self.current_model

    def set_user_model(self, user_id: str, model: ModelConfig):
        """设置用户当前使用的模型"""
        if self.remember_user_model:
            self.user_models[user_id] = model

    def get_model_from_message(self, content: str, user_id: str) -> tuple[ModelConfig, str, bool]:
        """根据消息内容判断使用哪个模型，并返回是否是切换模型的命令"""
        original_content = content  # 保留原始内容
        content = content.lower()  # 只在检测时使用小写版本

        # 检查是否是切换模型的命令
        if content.endswith("切换"):
            for model_name, model_config in self.models.items():
                for trigger in model_config.trigger_words:
                    if content.startswith(trigger.lower()):
                        self.set_user_model(user_id, model_config)
                        logger.info(f"用户 {user_id} 切换模型到 {model_name}")
                        return model_config, "", True
            return self.get_user_model(user_id), original_content, False

        # 检查是否使用了唤醒词
        logger.debug(f"检查消息 '{content}' 是否包含唤醒词")
        for wakeup_word, model_config in self.wakeup_word_to_model.items():
            wakeup_lower = wakeup_word.lower()
            content_lower = content.lower()
            if content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in content_lower:
                model_name = next((name for name, config in self.models.items() if config == model_config), '未知')
                logger.info(f"消息中检测到唤醒词 '{wakeup_word}'，临时使用模型 '{model_name}'")

                # 更精确地替换唤醒词
                # 先找到原文中唤醒词的实际位置和形式
                original_wakeup = None
                if content_lower.startswith(wakeup_lower):
                    # 如果以唤醒词开头，直接取对应长度的原始文本
                    original_wakeup = original_content[:len(wakeup_lower)]
                else:
                    # 如果唤醒词在中间，找到它的位置并获取原始形式
                    wakeup_pos = content_lower.find(f" {wakeup_lower}") + 1  # +1 是因为包含了前面的空格
                    if wakeup_pos > 0:
                        original_wakeup = original_content[wakeup_pos:wakeup_pos+len(wakeup_lower)]

                if original_wakeup:
                    # 使用原始形式进行替换，保留大小写
                    query = original_content.replace(original_wakeup, "", 1).strip()
                    logger.debug(f"唤醒词处理后的查询: '{query}'")
                    return model_config, query, False

        # 检查是否是临时使用其他模型
        for model_name, model_config in self.models.items():
            for trigger in model_config.trigger_words:
                if trigger.lower() in content:
                    logger.info(f"消息中包含触发词 '{trigger}'，临时使用模型 '{model_name}'")
                    query = original_content.replace(trigger, "", 1).strip()  # 使用原始内容替换原始触发词
                    return model_config, query, False

        # 使用用户当前的模型
        current_model = self.get_user_model(user_id)
        model_name = next((name for name, config in self.models.items() if config == current_model), '默认')
        logger.debug(f"未检测到特定模型指示，使用用户 {user_id} 当前默认模型 '{model_name}'")
        return current_model, original_content, False

    async def check_and_notify_inactive_users(self, bot: WechatAPIClient):
        # 聊天室功能已移除
        return

    # 聊天室相关方法已移除

    @on_text_message(priority=20)
    async def handle_text(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        content = message["Content"].strip()
        command = content.split(" ")[0] if content else ""

        await self.check_and_notify_inactive_users(bot)

        if not message["IsGroup"]:
            # 先检查唤醒词或触发词，获取对应模型
            model, processed_query, is_switch = self.get_model_from_message(content, message["SenderWxid"])

            # 检查是否有最近的图片
            image_content = await self.get_cached_image(message["FromWxid"])
            files = []
            if image_content:
                try:
                    logger.debug("发现最近的图片，准备上传到 Dify")
                    file_id = await self.upload_file_to_dify(
                        image_content,
                        f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                        "image/jpeg",  # 根据实际图片类型调整
                        message["FromWxid"],
                        model_config=model  # 传递正确的模型配置
                    )
                    if file_id:
                        logger.debug(f"图片上传成功，文件ID: {file_id}")
                        files = [file_id]
                    else:
                        logger.error("图片上传失败")
                except Exception as e:
                    logger.error(f"处理图片失败: {e}")

            if command in self.commands:
                query = content[len(command):].strip()
            else:
                query = content

            # 检查API密钥是否可用 - 使用检测到的模型，而非默认模型
            if query and model.api_key:
                if await self._check_point(bot, message, model):  # 传递模型到_check_point
                    if is_switch:
                        model_name = next(name for name, config in self.models.items() if config == model)
                        await bot.send_text_message(
                            message["FromWxid"],
                            f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                        )
                        return
                    # 使用获取到的模型处理请求
                    await self.dify(bot, message, processed_query, files=files, specific_model=model)
                else:
                    logger.info(f"积分检查失败或模型API密钥无效，无法处理请求")
            else:
                if not query:
                    logger.debug("查询内容为空，不处理")
                elif not model.api_key:
                    logger.error(f"模型 {next((name for name, config in self.models.items() if config == model), '未知')} 的API密钥未配置")
                    await bot.send_text_message(message["FromWxid"], "所选模型的API密钥未配置，请联系管理员")
            return

        # 以下是群聊处理逻辑
        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]

        # 添加对切换模型命令的特殊处理
        if content.endswith("切换"):
            for model_name, model_config in self.models.items():
                for trigger in model_config.trigger_words:
                    if content.lower().startswith(trigger.lower()):
                        self.set_user_model(user_wxid, model_config)
                        await bot.send_at_message(
                            group_id,
                            f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                            [user_wxid]
                        )
                        return

        is_at = self.is_at_message(message)
        is_command = command in self.commands

        # 先检查是否有唤醒词
        wakeup_detected = False
        wakeup_model = None
        processed_wakeup_query = ""

        for wakeup_word, model_config in self.wakeup_word_to_model.items():
            # 改用更精确的匹配方式，避免错误识别
            wakeup_lower = wakeup_word.lower()
            content_lower = content.lower()
            if content_lower.startswith(wakeup_lower) or f" {wakeup_lower}" in content_lower:
                wakeup_detected = True
                wakeup_model = model_config
                model_name = next((name for name, config in self.models.items() if config == model_config), '未知')
                logger.info(f"检测到唤醒词 '{wakeup_word}'，触发模型 '{model_name}'，原始内容: '{content}'")

                # 更精确地替换唤醒词
                original_wakeup = None
                if content_lower.startswith(wakeup_lower):
                    original_wakeup = content[:len(wakeup_lower)]
                else:
                    wakeup_pos = content_lower.find(f" {wakeup_lower}") + 1
                    if wakeup_pos > 0:
                        original_wakeup = content[wakeup_pos:wakeup_pos+len(wakeup_lower)]

                if original_wakeup:
                    processed_wakeup_query = content.replace(original_wakeup, "", 1).strip()
                    logger.info(f"处理后的查询内容: '{processed_wakeup_query}'")
                break

        # 检查是否有最近的图片 - 无论聊天室功能是否启用都获取图片
        files = []
        image_content = await self.get_cached_image(group_id)
        if image_content:
            try:
                logger.debug("发现最近的图片，准备上传到 Dify")
                # 如果检测到唤醒词，使用对应模型；否则使用用户当前模型
                model_config = wakeup_model or self.get_user_model(user_wxid)

                file_id = await self.upload_file_to_dify(
                    image_content,
                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                    "image/jpeg",
                    group_id,
                    model_config=model_config  # 传递正确的模型配置
                )
                if file_id:
                    logger.debug(f"图片上传成功，文件ID: {file_id}")
                    files = [file_id]
                else:
                    logger.error("图片上传失败")
            except Exception as e:
                logger.error(f"处理图片失败: {e}")

        # 如果检测到唤醒词，处理唤醒词请求
        if wakeup_detected and wakeup_model and processed_wakeup_query:
            if wakeup_model.api_key:  # 检查唤醒词对应模型的API密钥
                if await self._check_point(bot, message, wakeup_model):  # 传递模型到_check_point
                    logger.info(f"使用唤醒词对应模型处理请求")
                    await self.dify(bot, message, processed_wakeup_query, files=files, specific_model=wakeup_model)
                    return
                else:
                    logger.info(f"积分检查失败，无法处理唤醒词请求")
            else:
                model_name = next((name for name, config in self.models.items() if config == wakeup_model), '未知')
                logger.error(f"唤醒词对应模型 '{model_name}' 的API密钥未配置")
                await bot.send_at_message(group_id, f"\n此模型API密钥未配置，请联系管理员", [user_wxid])
            return

        # 继续处理@或命令的情况
        if is_at or is_command:
            # 群聊处理逻辑
            query = content
            for robot_name in self.robot_names:
                query = query.replace(f"@{robot_name}", "").strip()
            if command in self.commands:
                query = query[len(command):].strip()
            if query:
                if await self._check_point(bot, message, model):
                    # 检查是否有唤醒词或触发词
                    model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
                    await self.dify(bot, message, processed_query, files=files, specific_model=model)
            return

        # 聊天室功能已移除，所有消息都需要@或命令触发
        if is_at or is_command:
            query = content
            for robot_name in self.robot_names:
                query = query.replace(f"@{robot_name}", "").strip()
            if command in self.commands:
                query = query[len(command):].strip()
            if query:
                if await self._check_point(bot, message):
                    await self.dify(bot, message, query, files=files)
        return

        if content:
            if is_at or is_command:
                query = content

                # 检查是否以@开头，如果是，则移除@部分
                if content.startswith('@'):
                    # 先检查是否是@机器人
                    at_bot_prefix = None
                    for robot_name in self.robot_names:
                        if content.startswith(f'@{robot_name}'):
                            at_bot_prefix = f'@{robot_name}'
                            break

                    if at_bot_prefix:
                        # 如果是@机器人，移除@机器人部分
                        query = content[len(at_bot_prefix):].strip()
                        logger.debug(f"移除@{at_bot_prefix}后的查询内容: {query}")
                    else:
                        # 如果不是@机器人，则尝试找空格
                        space_index = content.find(' ')
                        if space_index > 0:
                            # 只保留空格后面的内容
                            query = content[space_index+1:].strip()
                            logger.debug(f"移除@前缀后的查询内容: {query}")
                        else:
                            # 如果没有空格，尝试提取@后面的内容
                            # 找到第一个非空格字符的位置
                            for i in range(1, len(content)):
                                if content[i] != '@' and content[i] != ' ':
                                    query = content[i:].strip()
                                    logger.debug(f"提取@后面的内容: {query}")
                                    break
                            else:
                                # 如果整个内容都是@，将query设为空
                                query = ""
                else:
                    # 如果不是以@开头，则尝试移除@机器人名称
                    for robot_name in self.robot_names:
                        query = query.replace(f"@{robot_name}", "").strip()
                if command in self.commands:
                    query = query[len(command):].strip()
                if query:
                    if await self._check_point(bot, message):
                        # 检查是否有唤醒词或触发词
                        model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
                        if is_switch:
                            model_name = next(name for name, config in self.models.items() if config == model)
                            await bot.send_at_message(
                                message["FromWxid"],
                                f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                                [message["SenderWxid"]]
                            )
                            return
                        await self.dify(bot, message, processed_query, files=files, specific_model=model)
            else:
                # 只有在聊天室功能开启时，才缓冲普通消息
                if self.chatroom_enable:
                    await self.chat_manager.add_message_to_buffer(group_id, user_wxid, content, files)
                    await self.schedule_message_processing(bot, group_id, user_wxid)
        return

    @on_at_message(priority=20)
    async def handle_at(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if not self.current_model.api_key:
            await bot.send_at_message(message["FromWxid"], "\n你还没配置Dify API密钥！", [message["SenderWxid"]])
            return False

        await self.check_and_notify_inactive_users(bot)

        content = message["Content"].strip()
        query = content

        # 检查是否以@开头，如果是，则移除@部分
        if content.startswith('@'):
            # 先检查是否是@机器人
            at_bot_prefix = None
            for robot_name in self.robot_names:
                if content.startswith(f'@{robot_name}'):
                    at_bot_prefix = f'@{robot_name}'
                    break

            if at_bot_prefix:
                # 如果是@机器人，移除@机器人部分
                query = content[len(at_bot_prefix):].strip()
                logger.debug(f"移除@{at_bot_prefix}后的查询内容: {query}")
            else:
                # 如果不是@机器人，则尝试找空格
                space_index = content.find(' ')
                if space_index > 0:
                    # 只保留空格后面的内容
                    query = content[space_index+1:].strip()
                    logger.debug(f"移除@前缀后的查询内容: {query}")
                else:
                    # 如果没有空格，尝试提取@后面的内容
                    # 找到第一个非空格字符的位置
                    for i in range(1, len(content)):
                        if content[i] != '@' and content[i] != ' ':
                            query = content[i:].strip()
                            logger.debug(f"提取@后面的内容: {query}")
                            break
                    else:
                        # 如果整个内容都是@，将query设为空
                        query = ""
        else:
            # 如果不是以@开头，则尝试移除@机器人名称
            for robot_name in self.robot_names:
                query = query.replace(f"@{robot_name}", "").strip()

        group_id = message["FromWxid"]
        user_wxid = message["SenderWxid"]

        # 聊天室功能已移除

        logger.debug(f"提取到的 query: {query}")

        if not query:
            await bot.send_at_message(message["FromWxid"], "\n请输入你的问题或指令。", [message["SenderWxid"]])
            return False

        # 检查唤醒词或触发词，在图片上传前获取对应模型
        model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
        if is_switch:
            model_name = next(name for name, config in self.models.items() if config == model)
            await bot.send_at_message(
                message["FromWxid"],
                f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                [message["SenderWxid"]]
            )
            return False

        # 检查模型API密钥是否可用
        if not model.api_key:
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.error(f"所选模型 '{model_name}' 的API密钥未配置")
            await bot.send_at_message(message["FromWxid"], f"\n此模型API密钥未配置，请联系管理员", [message["SenderWxid"]])
            return False

        # 检查是否有最近的图片
        files = []
        image_content = await self.get_cached_image(group_id)
        if image_content:
            try:
                logger.debug("@消息中发现最近的图片，准备上传到 Dify")
                file_id = await self.upload_file_to_dify(
                    image_content,
                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                    "image/jpeg",
                    group_id,
                    model_config=model  # 传递正确的模型配置
                )
                if file_id:
                    logger.debug(f"图片上传成功，文件ID: {file_id}")
                    files = [file_id]
                else:
                    logger.error("图片上传失败")
            except Exception as e:
                logger.error(f"处理图片失败: {e}")

        if await self._check_point(bot, message, model):  # 传递正确的模型参数
            # 使用上面已经获取的模型和处理过的查询
            logger.info(f"@消息使用模型 '{next((name for name, config in self.models.items() if config == model), '未知')}' 处理请求")
            await self.dify(bot, message, processed_query, files=files, specific_model=model)
        else:
            logger.info(f"积分检查失败，无法处理@消息请求")
        return False

    @on_quote_message(priority=20)
    async def handle_quote(self, bot: WechatAPIClient, message: dict):
        """处理引用消息"""
        if not self.enable:
            return

        # 提取引用消息的内容
        content = message["Content"].strip()
        quote_info = message.get("Quote", {})
        quoted_content = quote_info.get("Content", "")
        quoted_sender = quote_info.get("Nickname", "")

        # 处理群聊和私聊的情况
        if message["IsGroup"]:
            group_id = message["FromWxid"]
            user_wxid = message["SenderWxid"]

            # 检查是否是@机器人
            is_at = self.is_at_message(message)

            # 检查是否在引用消息中@了机器人
            is_at_bot = False
            if content.startswith('@'):
                # 检查@的是否是机器人
                for robot_name in self.robot_names:
                    if content.startswith(f'@{robot_name}'):
                        is_at_bot = True
                        break

            # 只有当用户@了机器人时，才处理引用消息
            if is_at and is_at_bot:
                # 处理@机器人的引用消息
                query = content

                # 检查是否以@开头，如果是，则移除@部分
                if content.startswith('@'):
                    # 先检查是否是@机器人
                    at_bot_prefix = None
                    for robot_name in self.robot_names:
                        if content.startswith(f'@{robot_name}'):
                            at_bot_prefix = f'@{robot_name}'
                            break

                    if at_bot_prefix:
                        # 如果是@机器人，移除@机器人部分
                        query = content[len(at_bot_prefix):].strip()
                        logger.debug(f"移除@{at_bot_prefix}后的查询内容: {query}")
                    else:
                        # 如果不是@机器人，则尝试找空格
                        space_index = content.find(' ')
                        if space_index > 0:
                            # 只保留空格后面的内容
                            query = content[space_index+1:].strip()
                            logger.debug(f"移除@前缀后的查询内容: {query}")
                        else:
                            # 如果没有空格，尝试提取@后面的内容
                            # 找到第一个非空格字符的位置
                            for i in range(1, len(content)):
                                if content[i] != '@' and content[i] != ' ':
                                    query = content[i:].strip()
                                    logger.debug(f"提取@后面的内容: {query}")
                                    break
                            else:
                                # 如果整个内容都是@，将query设为空
                                query = ""
                else:
                    # 如果不是以@开头，则尝试移除@机器人名称
                    for robot_name in self.robot_names:
                        query = query.replace(f"@{robot_name}", "").strip()

                # 如果没有内容，则使用引用的内容
                if not query:
                    query = f"请回复这条消息: '{quoted_content}'"
                else:
                    query = f"{query} (引用消息: '{quoted_content}')"

                # 检查是否有唤醒词或触发词
                model, processed_query, is_switch = self.get_model_from_message(query, user_wxid)

                if is_switch:
                    model_name = next(name for name, config in self.models.items() if config == model)
                    await bot.send_at_message(
                        message["FromWxid"],
                        f"\n已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。",
                        [user_wxid]
                    )
                    return False

                # 检查模型API密钥是否可用
                if not model.api_key:
                    model_name = next((name for name, config in self.models.items() if config == model), '未知')
                    logger.error(f"所选模型 '{model_name}' 的API密钥未配置")
                    await bot.send_at_message(message["FromWxid"], f"\n此模型API密钥未配置，请联系管理员", [user_wxid])
                    return False

                # 检查是否有最近的图片
                files = []
                image_content = await self.get_cached_image(group_id)
                if image_content:
                    try:
                        logger.debug("引用消息中发现最近的图片，准备上传到 Dify")
                        file_id = await self.upload_file_to_dify(
                            image_content,
                            f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                            "image/jpeg",
                            group_id,
                            model_config=model
                        )
                        if file_id:
                            logger.debug(f"图片上传成功，文件ID: {file_id}")
                            files = [file_id]
                        else:
                            logger.error("图片上传失败")
                    except Exception as e:
                        logger.error(f"处理图片失败: {e}")

                if await self._check_point(bot, message, model):
                    logger.info(f"引用消息使用模型 '{next((name for name, config in self.models.items() if config == model), '未知')}' 处理请求")
                    await self.dify(bot, message, processed_query, files=files, specific_model=model)
                else:
                    logger.info(f"积分检查失败，无法处理引用消息请求")
        else:
            # 私聊引用消息处理
            user_wxid = message["SenderWxid"]

            # 如果没有内容，则使用引用的内容
            if not content:
                query = f"请回复这条消息: '{quoted_content}'"
            else:
                query = f"{content} (引用消息: '{quoted_content}')"

            # 检查是否有唤醒词或触发词
            model, processed_query, is_switch = self.get_model_from_message(query, user_wxid)

            if is_switch:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    message["FromWxid"],
                    f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                )
                return False

            # 检查模型API密钥是否可用
            if not model.api_key:
                model_name = next((name for name, config in self.models.items() if config == model), '未知')
                logger.error(f"所选模型 '{model_name}' 的API密钥未配置")
                await bot.send_text_message(message["FromWxid"], "此模型API密钥未配置，请联系管理员")
                return False

            # 检查是否有最近的图片
            files = []
            image_content = await self.get_cached_image(message["FromWxid"])
            if image_content:
                try:
                    logger.debug("引用消息中发现最近的图片，准备上传到 Dify")
                    file_id = await self.upload_file_to_dify(
                        image_content,
                        f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                        "image/jpeg",
                        message["FromWxid"],
                        model_config=model
                    )
                    if file_id:
                        logger.debug(f"图片上传成功，文件ID: {file_id}")
                        files = [file_id]
                    else:
                        logger.error("图片上传失败")
                except Exception as e:
                    logger.error(f"处理图片失败: {e}")

            if await self._check_point(bot, message, model):
                logger.info(f"私聊引用消息使用模型 '{next((name for name, config in self.models.items() if config == model), '未知')}' 处理请求")
                await self.dify(bot, message, processed_query, files=files, specific_model=model)
            else:
                logger.info(f"积分检查失败，无法处理引用消息请求")

        return False

    @on_voice_message(priority=20)
    async def handle_voice(self, bot: WechatAPIClient, message: dict):
        if not self.enable:
            return

        if message["IsGroup"]:
            return

        if not self.current_model.api_key:
            await bot.send_text_message(message["FromWxid"], "你还没配置Dify API密钥！")
            return False

        query = await self.audio_to_text(bot, message)
        if not query:
            await bot.send_text_message(message["FromWxid"], VOICE_TRANSCRIPTION_FAILED)
            return False

        logger.debug(f"语音转文字结果: {query}")

        # 识别可能的唤醒词
        model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
        if is_switch:
            model_name = next(name for name, config in self.models.items() if config == model)
            await bot.send_text_message(
                message["FromWxid"],
                f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
            )
            return False

        # 检查识别到的模型API密钥是否可用
        if not model.api_key:
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.error(f"语音消息选择的模型 '{model_name}' 的API密钥未配置")
            await bot.send_text_message(message["FromWxid"], "所选模型的API密钥未配置，请联系管理员")
            return False

        # 积分检查
        if await self._check_point(bot, message, model):
            logger.info(f"语音消息使用模型 '{next((name for name, config in self.models.items() if config == model), '未知')}' 处理请求")
            await self.dify(bot, message, processed_query, specific_model=model)
        else:
            logger.info(f"积分检查失败，无法处理语音消息请求")
        return False

    def is_at_message(self, message: dict) -> bool:
        """检查消息是否@了机器人

        支持检测普通消息和引用消息中的@
        """
        if not message["IsGroup"]:
            return False

        # 获取消息内容
        content = message["Content"]

        # 记录原始消息信息便于调试
        logger.debug(f"检查消息是否@机器人: {content[:50]}...")

        # 检查消息类型
        msg_type = message.get("MsgType")
        logger.debug(f"消息类型: {msg_type}, 是否有Quote字段: {'Quote' in message}")

        # 如果消息内容以@开头，这是一个强烈的信号，表明用户@了某人
        if content.startswith('@'):
            logger.debug(f"消息内容以@开头: {content[:20]}")
            # 检查@的是否是机器人
            for robot_name in self.robot_names:
                if content.startswith(f'@{robot_name}'):
                    logger.debug(f"消息内容以@{robot_name}开头")
                    return True
            # 如果@的不是机器人，继续检查其他条件

        # 检查普通消息中的@
        for robot_name in self.robot_names:
            if f"@{robot_name}" in content:
                logger.debug(f"在消息内容中发现@{robot_name}")
                return True

        # 如果是引用消息，检查消息类型
        if msg_type == 49 or msg_type == 57 or "Quote" in message:  # 引用消息类型
            logger.debug(f"检测到引用消息: {msg_type}, Quote字段: {'Quote' in message}")

            # 如果有Quote字段，检查引用的消息内容
            if "Quote" in message:
                quote_info = message.get("Quote", {})
                quote_from = quote_info.get("Nickname", "")

                # 检查被引用的消息是否来自机器人
                for robot_name in self.robot_names:
                    if robot_name == quote_from:
                        logger.debug(f"引用了机器人 '{robot_name}' 的消息")
                        return True

                # 检查引用消息的内容中是否有@机器人
                quote_content = quote_info.get("Content", "")
                for robot_name in self.robot_names:
                    if f"@{robot_name}" in quote_content:
                        logger.debug(f"在引用的消息内容中发现@{robot_name}")
                        return True

            # 如果有OriginalContent，尝试解析XML
            if "OriginalContent" in message:
                try:
                    root = ET.fromstring(message.get("OriginalContent", ""))
                    title = root.find("appmsg/title")
                    if title is not None and title.text:
                        # 检查引用消息的标题中是否包含@机器人
                        for robot_name in self.robot_names:
                            if f"@{robot_name}" in title.text:
                                logger.debug(f"在引用消息标题中发现@{robot_name}")
                                return True
                except Exception as e:
                    logger.debug(f"解析引用消息 XML 失败: {e}")

            # 特殊处理：如果消息内容中包含机器人名称（不带@符号）
            for robot_name in self.robot_names:
                if robot_name in content:
                    logger.debug(f"在引用消息内容中发现机器人名称: {robot_name}")
                    return True

        # 检查消息的Ats字段，这是一个直接的@标记
        if "Ats" in message and message["Ats"]:
            logger.debug(f"消息包含Ats字段: {message['Ats']}")
            # 如果机器人的wxid在Ats列表中，则返回True
            if "wxid_uz9za1pqr3ea22" in message["Ats"]:
                logger.debug("在Ats字段中发现机器人的wxid")
                return True

        return False

    async def dify(self, bot: WechatAPIClient, message: dict, query: str, files=None, specific_model=None):
        """发送消息到Dify API"""
        if files is None:
            files = []

        # 如果提供了specific_model，直接使用；否则根据消息内容选择模型
        if specific_model:
            model = specific_model
            processed_query = query
            is_switch = False
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.info(f"使用指定的模型 '{model_name}'")
        else:
            # 根据消息内容选择模型
            model, processed_query, is_switch = self.get_model_from_message(query, message["SenderWxid"])
            model_name = next((name for name, config in self.models.items() if config == model), '默认')
            logger.info(f"从消息内容选择模型 '{model_name}'")

            # 如果是切换模型的命令
            if is_switch:
                model_name = next(name for name, config in self.models.items() if config == model)
                await bot.send_text_message(
                    message["FromWxid"],
                    f"已切换到{model_name.upper()}模型，将一直使用该模型直到下次切换。"
                )
                return

        # 记录将要使用的模型配置
        logger.info(f"模型API密钥: {model.api_key[:5]}...{model.api_key[-5:] if len(model.api_key) > 10 else ''}")
        logger.info(f"模型API端点: {model.base_url}")

        # 处理文件上传
        formatted_files = []
        for file_info in files:
            if isinstance(file_info, dict) and "id" in file_info and "type" in file_info:
                # 新格式，已包含类型信息
                formatted_files.append({
                    "type": file_info["type"],
                    "transfer_method": "local_file",
                    "upload_file_id": file_info["id"]
                })
            else:
                # 兼容旧格式，假设是图片ID
                formatted_files.append({
                    "type": "image",
                    "transfer_method": "local_file",
                    "upload_file_id": file_info
                })

        # 检查是否有缓存的文件
        cached_file = await self.get_cached_file(message["SenderWxid"])
        if cached_file:
            file_content, file_name, mime_type = cached_file
            logger.info(f"发现缓存文件，准备上传到 Dify: {file_name}, 大小: {len(file_content)} 字节")

            # 上传文件到 Dify
            file_info = await self.upload_file_to_dify(file_content, file_name, mime_type, message["SenderWxid"], model_config=model)
            if file_info:
                logger.info(f"成功上传缓存文件到 Dify，文件ID: {file_info['id']}, 类型: {file_info['type']}")
                formatted_files.append({
                    "type": file_info["type"],
                    "transfer_method": "local_file",
                    "upload_file_id": file_info["id"]
                })

        try:
            logger.debug(f"开始调用 Dify API - 用户消息: {processed_query}")
            logger.debug(f"文件列表: {formatted_files}")
            conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="dify")

            user_wxid = message["SenderWxid"]
            try:
                user_username = await bot.get_nickname(user_wxid) or "未知用户"
            except:
                user_username = "未知用户"

            inputs = {
                "user_wxid": user_wxid,
                "user_username": user_username
            }

            # 根据是否支持Agent模式，设置不同的请求参数
            payload = {
                "inputs": inputs,
                "query": processed_query,
                "response_mode": "streaming",  # 始终使用流式响应
                "conversation_id": conversation_id,
                "user": message["FromWxid"],
                "files": formatted_files,
                "auto_generate_name": False,
            }

            # 决定是使用API代理还是直接连接
            use_api_proxy = self.api_proxy is not None and has_api_proxy
            logger.debug(f"发送请求到 Dify - URL: {model.base_url}/chat-messages, Payload: {json.dumps(payload)}")

            if use_api_proxy:
                # 使用API代理调用
                logger.info(f"通过API代理调用Dify")
                try:
                    # 检查是否有对应的注册API
                    base_url_without_v1 = model.base_url.rstrip("/v1")
                    endpoint = model.base_url.replace(base_url_without_v1, "")
                    endpoint = endpoint + "/chat-messages"

                    # 准备请求
                    api_response = await self.api_proxy.call_api(
                        api_type="dify",
                        endpoint=endpoint,
                        data=payload,
                        method="POST",
                        headers={"Authorization": f"Bearer {model.api_key}"}
                    )

                    if api_response.get("success") is False:
                        logger.error(f"API代理调用失败: {api_response.get('error')}")
                        # 失败时回退到直接调用
                        use_api_proxy = False
                    else:
                        # API代理不支持流式响应，处理非流式返回的结果
                        ai_resp = api_response.get("data", {}).get("answer", "")
                        new_con_id = api_response.get("data", {}).get("conversation_id", "")
                        if new_con_id and new_con_id != conversation_id:
                            self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")

                        # 过滤掉思考标签
                        think_pattern = r'<think>.*?</think>'
                        ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                        logger.debug(f"API代理返回(过滤思考标签后): {ai_resp[:100]}...")

                        if ai_resp:
                            await self.dify_handle_text(bot, message, ai_resp, model)
                        else:
                            logger.warning("API代理未返回有效响应")
                            # 回退到直接调用
                            use_api_proxy = False
                except Exception as e:
                    logger.error(f"API代理调用异常: {e}")
                    logger.error(traceback.format_exc())
                    # 出错时回退到直接调用
                    use_api_proxy = False

            # 如果API代理不可用或调用失败，使用直接连接
            if not use_api_proxy:
                headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
                ai_resp = ""
                async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                    async with session.post(url=f"{model.base_url}/chat-messages", headers=headers, data=json.dumps(payload)) as resp:
                        if resp.status in (200, 201):
                            async for line in resp.content:
                                line = line.decode("utf-8").strip()
                                if not line or line == "event: ping":
                                    continue
                                elif line.startswith("data: "):
                                    line = line[6:]
                                try:
                                    resp_json = json.loads(line)
                                except json.JSONDecodeError:
                                    logger.error(f"Dify返回的JSON解析错误: {line}")
                                    continue

                                event = resp_json.get("event", "")
                                if event == "message":
                                    ai_resp += resp_json.get("answer", "")
                                elif event == "message_replace":
                                    ai_resp = resp_json.get("answer", "")
                                elif event == "message_end":
                                    # 在消息结束时过滤掉思考标签
                                    think_pattern = r'<think>.*?</think>'
                                    ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                                    logger.debug(f"消息结束时过滤思考标签")
                                elif event == "message_file":
                                    file_url = resp_json.get("url", "")
                                    file_id = resp_json.get("id", "")
                                    file_type = resp_json.get("type", "image")
                                    belongs_to = resp_json.get("belongs_to", "assistant")

                                    # 存储文件信息
                                    self.agent_files[file_id] = {
                                        "url": file_url,
                                        "type": file_type,
                                        "belongs_to": belongs_to
                                    }

                                    # 处理文件
                                    if file_type == "image":
                                        await self.dify_handle_image(bot, message, file_url, model_config=model)
                                    else:
                                        logger.info(f"收到非图片类型文件: {file_type}, ID: {file_id}, URL: {file_url}")
                                elif event == "agent_thought":
                                    # 处理Agent思考过程
                                    if self.support_agent_mode:
                                        thought_id = resp_json.get("id", "")
                                        message_id = resp_json.get("message_id", "")
                                        conversation_id = resp_json.get("conversation_id", "")
                                        position = resp_json.get("position", 0)
                                        thought = resp_json.get("thought", "")
                                        observation = resp_json.get("observation", "")
                                        tool = resp_json.get("tool", "")
                                        tool_input = resp_json.get("tool_input", "")
                                        message_files = resp_json.get("message_files", [])

                                        # 记录思考过程
                                        if conversation_id not in self.current_agent_thoughts:
                                            self.current_agent_thoughts[conversation_id] = []

                                        self.current_agent_thoughts[conversation_id].append({
                                            "id": thought_id,
                                            "message_id": message_id,
                                            "position": position,
                                            "thought": thought,
                                            "observation": observation,
                                            "tool": tool,
                                            "tool_input": tool_input,
                                            "files": message_files
                                        })

                                        logger.debug(f"Agent思考: {thought[:100]}...")
                                        if tool:
                                            logger.debug(f"使用工具: {tool}, 输入: {tool_input}")
                                        if observation:
                                            logger.debug(f"观察结果: {observation[:100]}...")
                                elif event == "agent_message":
                                    # 处理Agent消息
                                    if self.support_agent_mode:
                                        answer = resp_json.get("answer", "")
                                        ai_resp += answer
                                        logger.debug(f"Agent消息: {answer}")
                                elif event == "error":
                                    await self.dify_handle_error(bot, message,
                                                                resp_json.get("task_id", ""),
                                                                resp_json.get("message_id", ""),
                                                                resp_json.get("status", ""),
                                                                resp_json.get("code", ""),
                                                                resp_json.get("message", ""))

                            new_con_id = resp_json.get("conversation_id", "")
                            if new_con_id and new_con_id != conversation_id:
                                self.db.save_llm_thread_id(message["FromWxid"], new_con_id, "dify")
                            ai_resp = ai_resp.rstrip()

                            # 最后再次过滤思考标签，确保完全移除
                            think_pattern = r'<think>.*?</think>'
                            ai_resp = re.sub(think_pattern, '', ai_resp, flags=re.DOTALL)
                            logger.debug(f"Dify响应(过滤思考标签后): {ai_resp[:100]}...")
                        elif resp.status == 404:
                            logger.warning("会话ID不存在，重置会话ID并重试")
                            self.db.save_llm_thread_id(message["FromWxid"], "", "dify")
                            # 重要：在递归调用时必须传递原始模型，不要重新选择
                            return await self.dify(bot, message, processed_query, files=files, specific_model=model)
                        elif resp.status == 400:
                            return await self.handle_400(bot, message, resp)
                        elif resp.status == 500:
                            return await self.handle_500(bot, message)
                        else:
                            return await self.handle_other_status(bot, message, resp)

                if ai_resp:
                    await self.dify_handle_text(bot, message, ai_resp, model)
                else:
                    logger.warning("Dify未返回有效响应")
        except Exception as e:
            logger.error(f"Dify API 调用失败: {e}")
            await self.hendle_exceptions(bot, message, model_config=model)

    async def download_file(self, url: str) -> bytes:
        """
        下载文件并返回文件内容
        """
        try:
            logger.info(f"开始下载文件: {url}")
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        logger.info(f"文件下载成功，大小: {len(content)} 字节")
                        return content
                    else:
                        logger.error(f"文件下载失败: HTTP {resp.status}")
                        return None
        except Exception as e:
            logger.error(f"下载文件时发生错误: {e}")
            logger.error(traceback.format_exc())
            return None

    async def upload_file_to_dify(self, file_content: bytes, file_name: str, mime_type: str, user: str, model_config=None) -> Optional[dict]:
        """
        上传文件到Dify并返回文件信息
        返回格式: {"id": "uuid", "type": "image|document|audio|video"}
        """
        logger.info(f"开始上传文件到Dify, 用户: {user}, 文件名: {file_name}, 文件大小: {len(file_content)} 字节, MIME类型: {mime_type}")

        if not file_content or len(file_content) == 0:
            logger.error("文件内容为空，无法上传")
            return None

        try:
            # 判断文件类型
            file_extension = os.path.splitext(file_name)[1].lower().lstrip('.')
            if not file_extension:
                # 如果文件名没有扩展名，尝试从 MIME 类型推断
                file_extension = mime_type.split('/')[-1].lower()

            # 确定文件类型
            # 根据 Dify 文档，支持的文件类型如下：
            # document: 'TXT', 'MD', 'MARKDOWN', 'PDF', 'HTML', 'XLSX', 'XLS', 'DOCX', 'CSV', 'EML', 'MSG', 'PPTX', 'PPT', 'XML', 'EPUB'
            # image: 'JPG', 'JPEG', 'PNG', 'GIF', 'WEBP', 'SVG'
            # audio: 'MP3', 'M4A', 'WAV', 'WEBM', 'AMR'
            # video: 'MP4', 'MOV', 'MPEG', 'MPGA'
            # custom: 其他文件类型

            # 文档类型列表 - 根据 Dify 文档
            document_extensions = ['txt', 'md', 'markdown', 'pdf', 'html', 'xlsx', 'xls', 'docx', 'csv', 'eml', 'msg', 'pptx', 'ppt', 'xml', 'epub']
            # 根据文档，Dify 确实支持 'ppt' 格式
            # 图片类型列表
            image_extensions = ['jpg', 'jpeg', 'png', 'gif', 'webp', 'svg']
            # 音频类型列表
            audio_extensions = ['mp3', 'm4a', 'wav', 'webm', 'amr']
            # 视频类型列表
            video_extensions = ['mp4', 'mov', 'mpeg', 'mpga']

            # 默认使用 custom 类型
            file_type = "custom"

            # 根据文件扩展名判断类型
            if file_extension in document_extensions or mime_type.startswith('application/') or mime_type.startswith('text/'):
                file_type = "document"
                # 特殊处理 PPT 文件
                if file_extension == 'ppt' or file_name.lower().endswith('.ppt') or mime_type == 'application/vnd.ms-powerpoint':
                    logger.info(f"检测到 PPT 文件，使用 document 类型上传")
            elif file_extension in image_extensions or mime_type.startswith('image/'):
                file_type = "image"
                # 处理图片文件
                try:
                    # 尝试打开图片数据
                    # 特别处理截断的图片文件
                    from PIL import ImageFile
                    ImageFile.LOAD_TRUNCATED_IMAGES = True  # 允许加载截断的图片

                    # 使用BytesIO确保完整读取图片数据
                    image_io = io.BytesIO(file_content)
                    image = Image.open(image_io)
                    logger.debug(f"原始图片格式: {image.format}, 大小: {image.size}, 模式: {image.mode}")

                    # 转换为RGB模式(去除alpha通道)
                    if image.mode in ('RGBA', 'LA'):
                        logger.debug(f"图片包含alpha通道，转换为RGB模式")
                        background = Image.new('RGB', image.size, (255, 255, 255))
                        background.paste(image, mask=image.split()[-1])
                        image = background

                    # 检查图片大小，如果太大则调整大小
                    max_dimension = 1600  # 最大尺寸限制
                    max_file_size = 1024 * 1024 * 2  # 2MB大小限制

                    # 调整图片尺寸
                    width, height = image.size
                    if width > max_dimension or height > max_dimension:
                        # 计算缩放比例
                        ratio = min(max_dimension / width, max_dimension / height)
                        new_width = int(width * ratio)
                        new_height = int(height * ratio)
                        logger.info(f"图片尺寸过大，调整大小从 {width}x{height} 到 {new_width}x{new_height}")
                        image = image.resize((new_width, new_height), Image.LANCZOS)

                    # 保存为JPEG，尝试不同的质量级别以满足大小限制
                    quality = 95
                    output = io.BytesIO()
                    image.save(output, format='JPEG', quality=quality, optimize=True)
                    output.seek(0)
                    resized_content = output.getvalue()

                    # 如果文件仍然太大，逐步降低质量
                    while len(resized_content) > max_file_size and quality > 50:
                        quality -= 10
                        output = io.BytesIO()
                        image.save(output, format='JPEG', quality=quality, optimize=True)
                        output.seek(0)
                        resized_content = output.getvalue()
                        logger.debug(f"降低图片质量到 {quality}，新大小: {len(resized_content)} 字节")

                    file_content = resized_content
                    mime_type = 'image/jpeg'
                    file_extension = 'jpg'
                    logger.info(f"图片处理成功，质量: {quality}，新大小: {len(file_content)} 字节")

                    # 验证处理后的图片
                    try:
                        Image.open(io.BytesIO(file_content))
                        logger.debug("处理后的图片验证成功")
                    except Exception as e:
                        logger.error(f"处理后的图片验证失败: {e}")
                        # 如果处理后的图片无效，尝试使用原始图片数据
                        file_content = image_io.getvalue()
                        logger.warning(f"使用原始图片数据上传，大小: {len(file_content)} 字节")
                except Exception as e:
                    logger.error(f"图片格式转换失败: {e}")
                    logger.error(traceback.format_exc())
                    # 尝试使用原始数据上传，但先验证原始数据是否为有效图片
                    try:
                        Image.open(io.BytesIO(file_content))
                        logger.warning("原始图片数据有效，将直接使用原始数据上传")
                    except Exception as img_error:
                        logger.error(f"原始图片数据无效: {img_error}")
                        # 如果原始数据也无效，返回None
                        return None
            elif file_extension in audio_extensions or mime_type.startswith('audio/'):
                file_type = "audio"
            elif file_extension in video_extensions or mime_type.startswith('video/'):
                file_type = "video"

            logger.info(f"文件类型判断: {file_type}, 扩展名: {file_extension}")

            # 使用传入的model_config，如果没有则使用默认模型
            model = model_config or self.current_model
            model_name = next((name for name, config in self.models.items() if config == model), '未知')
            logger.debug(f"使用模型 '{model_name}' 上传文件")

            # 检查API密钥
            if not model.api_key:
                logger.error(f"模型 '{model_name}' 的API密钥未配置，无法上传文件")
                return None

            # 决定是使用API代理还是直接连接
            use_api_proxy = self.api_proxy is not None and has_api_proxy and False  # 文件上传暂不使用API代理

            if use_api_proxy:
                # API代理目前不支持文件上传，使用直接连接
                logger.info("文件上传目前不支持API代理，使用直接连接")
                use_api_proxy = False

            # 处理文件名，确保有正确的扩展名
            if file_type == "image" and not file_name.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg')):
                processed_file_name = f"image_{int(time.time())}.jpg"
                logger.info(f"更新图片文件名为: {processed_file_name}")
            else:
                # 处理文件名，避免重复的扩展名
                processed_file_name = file_name
                file_extension = os.path.splitext(file_name)[1].lower().lstrip('.')
                base_name = os.path.splitext(file_name)[0]

                # 检查基本名称是否已经包含扩展名
                if base_name.lower().endswith(f".{file_extension}"):
                    # 如果基本名称已经包含扩展名，则去除重复的扩展名
                    processed_file_name = f"{base_name}.{file_extension}"
                    logger.info(f"去除重复的文件扩展名，处理后的文件名: {processed_file_name}")

            # 确保MIME类型与文件类型匹配
            if file_type == "image" and not mime_type.startswith('image/'):
                mime_type = 'image/jpeg'
                logger.info(f"更新MIME类型为: {mime_type}")

            # 使用直接连接上传文件
            headers = {"Authorization": f"Bearer {model.api_key}"}
            formdata = aiohttp.FormData()
            # 使用处理后的文件名
            formdata.add_field("file", file_content,
                            filename=processed_file_name,
                            content_type=mime_type)
            formdata.add_field("user", user)

            url = f"{model.base_url}/files/upload"
            logger.debug(f"开始请求Dify文件上传API: {url}")

            # 设置较长的超时时间
            timeout = aiohttp.ClientTimeout(total=60)  # 60秒超时

            try:
                async with aiohttp.ClientSession(proxy=self.http_proxy, timeout=timeout) as session:
                    async with session.post(url, headers=headers, data=formdata) as resp:
                        if resp.status in (200, 201):
                            result = await resp.json()
                            file_id = result.get("id")
                            if file_id:
                                logger.info(f"文件上传成功，文件ID: {file_id}, 类型: {file_type}")
                                # 上传成功后删除缓存
                                if user in self.file_cache:
                                    del self.file_cache[user]
                                    logger.debug(f"已清除用户 {user} 的文件缓存")
                                # 清除图片缓存
                                if file_type == "image" and user in self.image_cache:
                                    del self.image_cache[user]
                                    logger.debug(f"已清除用户 {user} 的图片缓存")
                                return {
                                    "id": file_id,
                                    "type": file_type
                                }
                            else:
                                logger.error(f"文件上传成功但未返回文件ID: {result}")
                        else:
                            error_text = await resp.text()
                            logger.error(f"文件上传失败: HTTP {resp.status} - {error_text}")
                            return None
            except aiohttp.ClientError as e:
                logger.error(f"HTTP请求失败: {e}")
                return None
        except Exception as e:
            logger.error(f"上传文件时发生错误: {e}")
            logger.error(traceback.format_exc())
            return None

    async def dify_handle_text(self, bot: WechatAPIClient, message: dict, text: str, model_config=None):
        # 使用传入的model_config，如果没有则使用默认模型
        model = model_config or self.current_model

        # 先过滤掉<think>...</think>标签中的内容
        think_pattern = r'<think>.*?</think>'
        text = re.sub(think_pattern, '', text, flags=re.DOTALL)
        logger.debug(f"过滤思考标签后的文本: {text[:100]}...")

        # 获取会话ID，用于查找Agent思考过程
        conversation_id = self.db.get_llm_thread_id(message["FromWxid"], namespace="dify")

        # 如果启用了Agent模式且有思考过程，可以在这里处理
        if self.support_agent_mode and conversation_id in self.current_agent_thoughts:
            thoughts = self.current_agent_thoughts[conversation_id]
            if thoughts:
                logger.debug(f"发现Agent思考过程，共{len(thoughts)}条")
                # 这里可以根据需要处理思考过程，例如添加到消息中
                # 例如：添加使用的工具信息
                tools_used = []
                for thought in thoughts:
                    if thought.get("tool") and thought["tool"] not in tools_used:
                        tools_used.append(thought["tool"])

                if tools_used:
                    logger.info(f"Agent使用了以下工具: {', '.join(tools_used)}")

                # 清除已处理的思考过程
                self.current_agent_thoughts[conversation_id] = []

        # 匹配Dify返回的图片引用格式
        image_pattern = r'\[(.*?)\]\((.*?)\)'
        matches = re.findall(image_pattern, text)

        # 移除所有图片引用文本
        text = re.sub(image_pattern, '', text)

        # 先发送文字内容
        if text:
            if message["MsgType"] == 34 or self.voice_reply_all:
                await self.text_to_voice_message(bot, message, text)
            else:
                paragraphs = text.split("//n")
                for paragraph in paragraphs:
                    if paragraph.strip():
                        await bot.send_text_message(message["FromWxid"], paragraph.strip())

        # 如果有图片引用，只处理最后一个
        if matches:
            filename, url = matches[-1]  # 只取最后一个图片
            try:
                # 如果URL是相对路径,添加base_url
                if url.startswith('/files'):
                    # 移除base_url中可能的v1路径
                    base_url = model.base_url.replace('/v1', '')
                    url = f"{base_url}{url}"

                logger.debug(f"处理图片链接: {url}")
                headers = {"Authorization": f"Bearer {model.api_key}"}
                async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            image_data = await resp.read()
                            await bot.send_image_message(message["FromWxid"], image_data)
                        else:
                            logger.error(f"下载图片失败: HTTP {resp.status}")
                            await bot.send_text_message(message["FromWxid"], f"下载图片失败: HTTP {resp.status}")
            except Exception as e:
                logger.error(f"处理图片 {url} 失败: {e}")
                await bot.send_text_message(message["FromWxid"], f"处理图片失败: {str(e)}")

        # 处理其他类型的链接
        pattern = r"\]$$(https?:\/\/[^\s$$]+)\)"
        links = re.findall(pattern, text)
        for url in links:
            try:
                file_content = await self.download_file(url)
                if file_content:
                    # 检测文件类型
                    kind = filetype.guess(file_content)
                    if kind is None:
                        # 如果无法检测文件类型，尝试从URL获取
                        ext = os.path.splitext(url)[1].lower().lstrip('.')
                        if not ext:
                            logger.warning(f"无法识别文件类型: {url}")
                            continue
                    else:
                        ext = kind.extension

                    # 根据文件类型发送不同类型的消息
                    if ext in ('wav', 'mp3', 'ogg', 'm4a'):
                        await bot.send_voice_message(message["FromWxid"], voice=file_content, format=ext)
                        logger.info(f"发送语音消息成功，大小: {len(file_content)} 字节")
                    elif ext in ('jpg', 'jpeg', 'png', 'gif', 'bmp', 'svg'):
                        await bot.send_image_message(message["FromWxid"], file_content)
                        logger.info(f"发送图片消息成功，大小: {len(file_content)} 字节")
                    elif ext in ('mp4', 'avi', 'mov', 'mkv', 'flv'):
                        await bot.send_video_message(message["FromWxid"], video=file_content, image="None")
                        logger.info(f"发送视频消息成功，大小: {len(file_content)} 字节")
                    else:
                        # 其他类型文件，发送文本通知
                        file_name = os.path.basename(url)
                        await bot.send_text_message(message["FromWxid"], f"下载了文件: {file_name}\n类型: {ext}\n大小: {len(file_content)/1024:.2f} KB")
                        logger.info(f"发送文件通知成功，文件名: {file_name}, 类型: {ext}, 大小: {len(file_content)} 字节")
            except Exception as e:
                logger.error(f"处理链接文件 {url} 失败: {e}")
                logger.error(traceback.format_exc())
                await bot.send_text_message(message["FromWxid"], f"下载文件 {url} 失败")

        # 识别普通文件链接
        file_pattern = r'https?://[^\s<>"]+?/[^\s<>"]+\.(?:pdf|doc|docx|xls|xlsx|txt|zip|rar|7z|tar|gz)'
        file_links = re.findall(file_pattern, text)
        for url in file_links:
            await self.download_and_send_file(bot, message, url)

        pattern = r'\$\$[^$$]+\]\$\$https?:\/\/[^\s$$]+\)'
        text = re.sub(pattern, '', text)

    async def dify_handle_image(self, bot: WechatAPIClient, message: dict, image: Union[str, bytes], model_config=None):
        try:
            image_content = None

            if isinstance(image, str) and image.startswith("http"):
                try:
                    logger.info(f"从URL下载图片: {image}")
                    async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                        async with session.get(image) as resp:
                            if resp.status == 200:
                                image_content = await resp.read()
                                logger.info(f"成功从URL下载图片，大小: {len(image_content)} 字节")

                                # 上传到 Dify
                                file_info = await self.upload_file_to_dify(
                                    image_content,
                                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                                    "image/jpeg",  # 根据实际图片类型调整
                                    message["FromWxid"],
                                    model_config=model_config  # 传递模型配置
                                )
                                if file_info:
                                    logger.info(f"图片上传成功，文件ID: {file_info['id']}, 类型: {file_info['type']}")
                            else:
                                logger.error(f"下载图片失败: HTTP {resp.status}")
                                await bot.send_text_message(message["FromWxid"], f"下载图片失败: HTTP {resp.status}")
                                return
                except Exception as e:
                    logger.error(f"下载图片 {image} 失败: {e}")
                    logger.error(traceback.format_exc())
                    await bot.send_text_message(message["FromWxid"], f"下载图片 {image} 失败: {str(e)}")
                    return
            elif isinstance(image, bytes):
                logger.info(f"处理二进制图片数据，大小: {len(image)} 字节")
                image_content = image

                # 上传到 Dify
                file_info = await self.upload_file_to_dify(
                    image_content,
                    f"image_{int(time.time())}.jpg",  # 生成一个有效的文件名
                    "image/jpeg",  # 根据实际图片类型调整
                    message["FromWxid"],
                    model_config=model_config  # 传递模型配置
                )
                if file_info:
                    logger.info(f"图片上传成功，文件ID: {file_info['id']}, 类型: {file_info['type']}")
            else:
                logger.error(f"不支持的图片类型: {type(image)}")
                await bot.send_text_message(message["FromWxid"], f"不支持的图片类型: {type(image)}")
                return

            # 确保我们有图片内容
            if not image_content:
                logger.error("图片内容为空，无法发送")
                await bot.send_text_message(message["FromWxid"], "图片内容为空，无法发送")
                return

            # 验证图片数据
            try:
                # 允许加载截断的图片
                from PIL import ImageFile
                ImageFile.LOAD_TRUNCATED_IMAGES = True

                # 验证图片数据
                img = Image.open(io.BytesIO(image_content))
                logger.info(f"图片验证成功，格式: {img.format}, 大小: {img.size}, 模式: {img.mode}")

                # 检查图片大小，如果太大则调整大小
                width, height = img.size
                max_dimension = 1600  # 最大尺寸限制

                if width > max_dimension or height > max_dimension:
                    # 计算缩放比例
                    ratio = min(max_dimension / width, max_dimension / height)
                    new_width = int(width * ratio)
                    new_height = int(height * ratio)
                    logger.info(f"图片尺寸过大，调整大小从 {width}x{height} 到 {new_width}x{new_height}")
                    img = img.resize((new_width, new_height), Image.LANCZOS)

                    # 转换为RGB模式(去除alpha通道)
                    if img.mode in ('RGBA', 'LA'):
                        logger.debug(f"图片包含alpha通道，转换为RGB模式")
                        background = Image.new('RGB', img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[-1])
                        img = background

                    # 保存为JPEG
                    output = io.BytesIO()
                    img.save(output, format='JPEG', quality=95, optimize=True)
                    output.seek(0)
                    image_content = output.getvalue()
                    logger.info(f"图片处理成功，新大小: {len(image_content)} 字节")
            except Exception as e:
                logger.error(f"图片验证或处理失败: {e}")
                logger.error(traceback.format_exc())
                # 继续使用原始图片数据

            # 直接发送图片数据，不进行base64转换
            logger.info(f"发送图片给用户，大小: {len(image_content)} 字节")
            await bot.send_image_message(message["FromWxid"], image_content)
            logger.info("图片发送成功")
        except Exception as e:
            logger.error(f"处理图片失败: {e}")
            logger.error(traceback.format_exc())
            await bot.send_text_message(message["FromWxid"], f"处理图片失败: {str(e)}")

    @staticmethod
    async def dify_handle_error(bot: WechatAPIClient, message: dict, task_id: str, message_id: str, status: str,
                                code: int, err_message: str):
        output = (XYBOT_PREFIX +
                  DIFY_ERROR_MESSAGE +
                  f"任务 ID：{task_id}\n"
                  f"消息唯一 ID：{message_id}\n"
                  f"HTTP 状态码：{status}\n"
                  f"错误码：{code}\n"
                  f"错误信息：{err_message}")
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_400(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        output = (XYBOT_PREFIX +
                  "🙅对不起，出现错误！\n"
                  f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_500(bot: WechatAPIClient, message: dict):
        output = XYBOT_PREFIX + "🙅对不起，Dify服务内部异常，请稍后再试。"
        await bot.send_text_message(message["FromWxid"], output)

    @staticmethod
    async def handle_other_status(bot: WechatAPIClient, message: dict, resp: aiohttp.ClientResponse):
        ai_resp = (XYBOT_PREFIX +
                   f"🙅对不起，出现错误！\n"
                   f"状态码：{resp.status}\n"
                   f"错误信息：{(await resp.content.read()).decode('utf-8')}")
        await bot.send_text_message(message["FromWxid"], ai_resp)

    @staticmethod
    async def hendle_exceptions(bot: WechatAPIClient, message: dict, model_config=None):
        output = (XYBOT_PREFIX +
                  "🙅对不起，出现错误！\n"
                  f"错误信息：\n"
                  f"{traceback.format_exc()}")
        await bot.send_text_message(message["FromWxid"], output)

    async def _check_point(self, bot: WechatAPIClient, message: dict, model_config=None) -> bool:
        wxid = message["SenderWxid"]
        if wxid in self.admins and self.admin_ignore:
            return True
        elif self.db.get_whitelist(wxid) and self.whitelist_ignore:
            return True
        else:
            if self.db.get_points(wxid) < (model_config or self.current_model).price:
                await bot.send_text_message(message["FromWxid"],
                                            XYBOT_PREFIX +
                                            INSUFFICIENT_POINTS_MESSAGE.format(price=(model_config or self.current_model).price))
                return False
            self.db.add_points(wxid, -((model_config or self.current_model).price))
            return True

    async def audio_to_text(self, bot: WechatAPIClient, message: dict) -> str:
        if not shutil.which("ffmpeg"):
            logger.error("未找到ffmpeg，请安装并配置到环境变量")
            await bot.send_text_message(message["FromWxid"], "服务器缺少ffmpeg，无法处理语音")
            return ""

        silk_file = "temp_audio.silk"
        mp3_file = "temp_audio.mp3"
        try:
            with open(silk_file, "wb") as f:
                f.write(message["Content"])

            command = f"ffmpeg -y -i {silk_file} -ar 16000 -ac 1 -f mp3 {mp3_file}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 执行失败: {process.stderr}")
                return ""

            # 使用当前模型的 base-url 构建音频转文本 URL
            model = self.get_user_model(message["SenderWxid"])
            audio_to_text_url = f"{model.base_url}/audio-to-text"
            logger.debug(f"使用音频转文本 URL: {audio_to_text_url}")

            headers = {"Authorization": f"Bearer {model.api_key}"}
            formdata = aiohttp.FormData()
            with open(mp3_file, "rb") as f:
                mp3_data = f.read()
            formdata.add_field("file", mp3_data, filename="audio.mp3", content_type="audio/mp3")
            formdata.add_field("user", message["SenderWxid"])
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(audio_to_text_url, headers=headers, data=formdata) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        text = result.get("text", "")
                        if "failed" in text.lower() or "code" in text.lower():
                            logger.error(f"Dify API 返回错误: {text}")
                        else:
                            logger.info(f"语音转文字结果 (Dify API): {text}")
                            return text
                    else:
                        logger.error(f"audio-to-text 接口调用失败: {resp.status} - {await resp.text()})")

            command = f"ffmpeg -y -i {mp3_file} {silk_file.replace('.silk', '.wav')}"
            process = subprocess.run(command, shell=True, check=True, capture_output=True, text=True)
            if process.returncode != 0:
                logger.error(f"ffmpeg 转为 WAV 失败: {process.stderr}")
                return ""

            r = sr.Recognizer()
            with sr.AudioFile(silk_file.replace('.silk', '.wav')) as source:
                audio = r.record(source)
            text = r.recognize_google(audio, language="zh-CN")
            logger.info(f"语音转文字结果 (Google): {text}")
            return text
        except Exception as e:
            logger.error(f"语音处理失败: {e}")
            return ""
        finally:
            for temp_file in [silk_file, mp3_file, silk_file.replace('.silk', '.wav')]:
                if os.path.exists(temp_file):
                    os.remove(temp_file)

    async def text_to_voice_message(self, bot: WechatAPIClient, message: dict, text: str):
        try:
            # 使用当前模型的 base-url 构建文本转音频 URL
            model = self.get_user_model(message["SenderWxid"])
            text_to_audio_url = f"{model.base_url}/text-to-audio"
            logger.debug(f"使用文本转音频 URL: {text_to_audio_url}")

            headers = {"Authorization": f"Bearer {model.api_key}", "Content-Type": "application/json"}
            data = {"text": text, "user": message["SenderWxid"]}
            async with aiohttp.ClientSession(proxy=self.http_proxy) as session:
                async with session.post(text_to_audio_url, headers=headers, json=data) as resp:
                    if resp.status == 200:
                        audio = await resp.read()
                        await bot.send_voice_message(message["FromWxid"], voice=audio, format="mp3")
                    else:
                        logger.error(f"text-to-audio 接口调用失败: {resp.status} - {await resp.text()}")
                        await bot.send_text_message(message["FromWxid"], TEXT_TO_VOICE_FAILED)
        except Exception as e:
            logger.error(f"text-to-audio 接口调用异常: {e}")
            await bot.send_text_message(message["FromWxid"], f"{TEXT_TO_VOICE_FAILED}: {str(e)}")

    @on_image_message(priority=20)
    async def handle_image(self, bot: WechatAPIClient, message: dict):
        """处理图片消息"""
        if not self.enable:
            return

        try:
            # 获取图片消息的关键信息
            msg_id = message.get("MsgId")
            from_wxid = message.get("FromWxid")
            sender_wxid = message.get("SenderWxid")

            logger.info(f"收到图片消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}")

            # 直接从消息中获取图片内容
            image_content = None
            xml_content = message.get("Content")

            # 如果是二进制数据，直接使用
            if isinstance(xml_content, bytes):
                logger.debug("图片内容是二进制数据，尝试直接处理")
                try:
                    # 验证是否为有效的图片数据
                    Image.open(io.BytesIO(xml_content))
                    image_content = xml_content
                    logger.info(f"二进制图片数据验证成功，大小: {len(xml_content)} 字节")
                except Exception as e:
                    logger.error(f"二进制图片数据无效: {e}")

            # 如果是字符串，尝试解析XML或处理base64图片数据
            elif isinstance(xml_content, str):
                # 检查是否是base64编码的图片数据
                if xml_content.startswith('/9j/') or xml_content.startswith('iVBOR'):
                    logger.debug("检测到base64编码的图片数据，直接解码")
                    try:
                        import base64
                        # 处理可能的填充字符
                        xml_content = xml_content.strip()
                        # 处理可能的换行符
                        xml_content = xml_content.replace('\n', '').replace('\r', '')

                        try:
                            # 先尝试直接解码
                            image_data = base64.b64decode(xml_content)
                        except Exception as base64_error:
                            logger.warning(f"直接解码失败: {base64_error}")
                            # 尝试修复可能的base64编码问题
                            try:
                                # 添加可能缺失的填充
                                padding_needed = len(xml_content) % 4
                                if padding_needed:
                                    xml_content += '=' * (4 - padding_needed)
                                image_data = base64.b64decode(xml_content)
                                logger.debug("添加填充后成功解码base64数据")
                            except Exception as padding_error:
                                logger.error(f"添加填充后仍然无法解码: {padding_error}")
                                # 尝试使用更宽松的解码方式
                                try:
                                    image_data = base64.b64decode(xml_content + '==', validate=False)
                                    logger.debug("使用宽松模式成功解码base64数据")
                                except Exception as e:
                                    logger.error(f"所有base64解码方法均失败: {e}")
                                    return

                        # 验证图片数据
                        try:
                            # 允许加载截断的图片
                            from PIL import ImageFile
                            ImageFile.LOAD_TRUNCATED_IMAGES = True

                            Image.open(io.BytesIO(image_data))
                            image_content = image_data
                            logger.info(f"base64图片数据解码成功，大小: {len(image_data)} 字节")
                        except Exception as img_error:
                            logger.error(f"base64图片数据无效: {img_error}")
                    except Exception as base64_error:
                        logger.error(f"base64解码失败: {base64_error}")
                        logger.debug(f"base64数据前100字符: {xml_content[:100]}")
                else:
                    # 尝试解析XML
                    logger.debug("图片内容是字符串，尝试解析XML")
                    try:
                        # 尝试解析XML获取图片信息
                        root = ET.fromstring(xml_content)
                        img_element = root.find('img')

                        if img_element is not None:
                            # 提取图片元数据
                            md5 = img_element.get('md5')
                            aeskey = img_element.get('aeskey')
                            length = img_element.get('length')
                            cdnmidimgurl = img_element.get('cdnmidimgurl')
                            cdnthumburl = img_element.get('cdnthumburl')

                            logger.info(f"从XML解析到图片信息: md5={md5}, aeskey={aeskey}, length={length}")

                            # 尝试使用PAD API下载图片
                            try:
                                # 从 XML 中提取图片大小
                                img_length = int(length) if length and length.isdigit() else 0

                                # 使用消息 ID 下载图片 - 实现分段下载
                                logger.debug(f"尝试使用消息 ID {msg_id} 下载图片，图片大小: {img_length}")

                                # 创建一个字节数组来存储完整的图片数据
                                full_image_data = bytearray()

                                # 分段下载大图片
                                chunk_size = 64 * 1024  # 64KB
                                chunks = (img_length + chunk_size - 1) // chunk_size  # 向上取整

                                logger.info(f"开始分段下载图片，总大小: {img_length} 字节，分 {chunks} 段下载")

                                download_success = True
                                for i in range(chunks):
                                    try:
                                        # 下载当前段
                                        chunk_data = await bot.get_msg_image(msg_id, from_wxid, img_length, start_pos=i*chunk_size)
                                        if chunk_data and len(chunk_data) > 0:
                                            full_image_data.extend(chunk_data)
                                            logger.debug(f"第 {i+1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                        else:
                                            logger.error(f"第 {i+1}/{chunks} 段下载失败，数据为空")
                                            download_success = False
                                            break
                                    except Exception as e:
                                        logger.error(f"下载第 {i+1}/{chunks} 段时出错: {e}")
                                        download_success = False
                                        break

                                if download_success and len(full_image_data) > 0:
                                    # 验证图片数据
                                    try:
                                        image_data = bytes(full_image_data)
                                        Image.open(io.BytesIO(image_data))
                                        image_content = image_data
                                        logger.info(f"使用消息 ID下载图片成功，总大小: {len(image_data)} 字节")
                                    except Exception as img_error:
                                        logger.error(f"下载的图片数据无效: {img_error}")
                                else:
                                    logger.error(f"图片分段下载失败，已下载: {len(full_image_data)}/{img_length} 字节")
                            except Exception as download_error:
                                logger.error(f"使用消息 ID下载图片失败: {download_error}")
                                logger.error(traceback.format_exc())
                    except Exception as xml_error:
                        logger.error(f"XML解析失败: {xml_error}")
                        logger.debug(f"XML内容前100字符: {xml_content[:100]}")
            else:
                logger.error(f"图片消息内容格式未知: {type(xml_content)}")

            # 如果成功获取图片内容，则缓存
            if image_content:
                # 缓存图片到发送者和收件人的ID
                self.image_cache[sender_wxid] = {
                    "content": image_content,
                    "timestamp": time.time()
                }
                logger.info(f"已缓存用户 {sender_wxid} 的图片")

                # 如果是私聊，也缓存到聊天对象的ID
                if from_wxid != sender_wxid:
                    self.image_cache[from_wxid] = {
                        "content": image_content,
                        "timestamp": time.time()
                    }
                    logger.info(f"已缓存聊天对象 {from_wxid} 的图片")
            else:
                logger.warning(f"未能获取图片内容，无法缓存")

        except Exception as e:
            logger.error(f"处理图片消息失败: {e}")
            logger.error(f"错误详情: {traceback.format_exc()}")

    async def get_cached_image(self, user_wxid: str) -> Optional[bytes]:
        """获取用户最近的图片"""
        logger.debug(f"尝试获取用户 {user_wxid} 的缓存图片")
        if user_wxid in self.image_cache:
            cache_data = self.image_cache[user_wxid]
            current_time = time.time()
            cache_age = current_time - cache_data["timestamp"]
            logger.debug(f"找到缓存图片，年龄: {cache_age:.2f}秒, 超时时间: {self.image_cache_timeout}秒")

            if cache_age <= self.image_cache_timeout:
                try:
                    # 确保我们有有效的二进制数据
                    image_content = cache_data["content"]
                    if not isinstance(image_content, bytes):
                        logger.error("缓存的图片内容不是二进制格式")
                        del self.image_cache[user_wxid]
                        return None

                    # 尝试验证图片数据
                    try:
                        img = Image.open(io.BytesIO(image_content))
                        logger.debug(f"缓存图片验证成功，格式: {img.format}, 大小: {len(image_content)} 字节")
                    except Exception as e:
                        logger.error(f"缓存的图片数据无效: {e}")
                        del self.image_cache[user_wxid]
                        return None

                    # 不再删除缓存，而是在上传成功后删除
                    # 更新时间戳，避免过早超时
                    self.image_cache[user_wxid]["timestamp"] = current_time
                    logger.info(f"成功获取用户 {user_wxid} 的缓存图片")
                    return image_content
                except Exception as e:
                    logger.error(f"处理缓存图片失败: {e}")
                    del self.image_cache[user_wxid]
                    return None
            else:
                # 超时清除
                logger.debug(f"缓存图片超时，已清除")
                del self.image_cache[user_wxid]
        else:
            logger.debug(f"未找到用户 {user_wxid} 的缓存图片")
        return None

    async def get_cached_file(self, user_wxid: str) -> Optional[tuple[bytes, str, str]]:
        """获取用户最近的文件，返回 (文件内容, 文件名, MIME类型)"""
        logger.debug(f"尝试获取用户 {user_wxid} 的缓存文件")
        if user_wxid in self.file_cache:
            cache_data = self.file_cache[user_wxid]
            current_time = time.time()
            cache_age = current_time - cache_data["timestamp"]
            logger.debug(f"找到缓存文件，年龄: {cache_age:.2f}秒, 超时时间: {self.file_cache_timeout}秒")

            if cache_age <= self.file_cache_timeout:
                try:
                    # 确保我们有有效的二进制数据
                    file_content = cache_data["content"]
                    file_name = cache_data["name"]
                    mime_type = cache_data["mime_type"]

                    # 处理不同类型的文件内容
                    if isinstance(file_content, bytearray):
                        # 将 bytearray 转换为 bytes
                        file_content = bytes(file_content)
                        logger.info(f"将 bytearray 转换为 bytes，大小: {len(file_content)} 字节")
                    elif isinstance(file_content, str):
                        # 尝试将字符串解析为 base64
                        try:
                            file_content = base64.b64decode(file_content)
                            logger.info(f"将 base64 字符串转换为 bytes，大小: {len(file_content)} 字节")
                        except Exception as e:
                            logger.error(f"Base64 解码失败: {e}")
                            file_content = file_content.encode('utf-8')
                            logger.info(f"将普通字符串转换为 bytes，大小: {len(file_content)} 字节")
                    elif not isinstance(file_content, bytes):
                        logger.error(f"缓存的文件内容不是支持的格式: {type(file_content)}")
                        del self.file_cache[user_wxid]
                        return None

                    # 更新缓存中的文件内容
                    self.file_cache[user_wxid]["content"] = file_content

                    # 更新时间戳，避免过早超时
                    self.file_cache[user_wxid]["timestamp"] = current_time
                    logger.info(f"成功获取用户 {user_wxid} 的缓存文件: {file_name}, 大小: {len(file_content)} 字节")
                    return (file_content, file_name, mime_type)
                except Exception as e:
                    logger.error(f"处理缓存文件失败: {e}")
                    del self.file_cache[user_wxid]
                    return None
            else:
                # 超时清除
                logger.debug(f"缓存文件超时，已清除")
                del self.file_cache[user_wxid]
        else:
            logger.debug(f"未找到用户 {user_wxid} 的缓存文件")
        return None

    def cache_file(self, user_wxid: str, file_content: bytes, file_name: str, mime_type: str) -> None:
        """缓存用户文件"""
        self.file_cache[user_wxid] = {
            "content": file_content,
            "name": file_name,
            "mime_type": mime_type,
            "timestamp": time.time()
        }
        logger.info(f"已缓存用户 {user_wxid} 的文件: {file_name}, 大小: {len(file_content)} 字节")

    async def download_and_send_file(self, bot: WechatAPIClient, message: dict, url: str):
        """下载并发送文件"""
        try:
            # 从URL中获取文件名
            parsed_url = urllib.parse.urlparse(url)
            filename = os.path.basename(parsed_url.path)
            if not filename:
                filename = f"downloaded_file_{int(time.time())}"

            logger.info(f"开始下载文件: {url}")

            # 使用改进后的download_file方法
            content = await self.download_file(url)
            if not content:
                await bot.send_text_message(message["FromWxid"], f"下载文件失败: {url}")
                return

            # 检测文件类型
            kind = filetype.guess(content)
            if kind is None:
                # 如果无法检测文件类型,尝试从URL获取
                ext = os.path.splitext(filename)[1].lower()
                if not ext:
                    # 如果没有扩展名，使用默认扩展名
                    ext = ".txt"
                    logger.warning(f"无法识别文件类型，使用默认扩展名: {ext}")
            else:
                ext = f".{kind.extension}"
                logger.info(f"检测到文件类型: {kind.mime}, 扩展名: {ext}")

            # 确保文件名有扩展名
            if not os.path.splitext(filename)[1]:
                filename = f"{filename}{ext}"

            # 根据文件类型发送不同类型的消息
            if ext.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg']:
                await bot.send_image_message(message["FromWxid"], content)
                logger.info(f"发送图片消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            elif ext.lower() in ['.mp3', '.wav', '.ogg', '.m4a']:
                await bot.send_voice_message(message["FromWxid"], voice=content, format=ext[1:])
                logger.info(f"发送语音消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            elif ext.lower() in ['.mp4', '.avi', '.mov', '.mkv', '.flv']:
                await bot.send_video_message(message["FromWxid"], video=content, image="None")
                logger.info(f"发送视频消息成功，文件名: {filename}, 大小: {len(content)} 字节")
            else:
                # 其他类型文件，发送文件信息
                await bot.send_text_message(message["FromWxid"], f"文件名: {filename}\n类型: {ext[1:]}\n大小: {len(content)/1024:.2f} KB")
                logger.info(f"发送文件信息成功，文件名: {filename}, 大小: {len(content)} 字节")

            # 缓存文件，便于后续使用
            mime_type = kind.mime if kind else f"application/{ext[1:]}"
            self.cache_file(message["SenderWxid"], content, filename, mime_type)
            logger.info(f"文件已缓存，用户: {message['SenderWxid']}, 文件名: {filename}")

            # 如果是私聊，也缓存到聊天对象的ID
            if message["FromWxid"] != message.get("SenderWxid", message["FromWxid"]):
                self.cache_file(message["FromWxid"], content, filename, mime_type)
                logger.info(f"文件已缓存到聊天对象: {message['FromWxid']}, 文件名: {filename}")

        except Exception as e:
            logger.error(f"下载或发送文件失败: {e}")
            logger.error(traceback.format_exc())

    @on_xml_message(priority=98)  # 使用高优先级确保先处理
    async def handle_xml_file(self, bot: WechatAPIClient, message: dict):
        """处理XML格式的文件消息"""
        if not self.enable:
            return True

        try:
            # 检查消息内容是否是XML格式
            content = message.get("Content", "")
            if not content or not isinstance(content, str) or not content.strip().startswith("<"):
                logger.warning(f"Dify: 消息内容不是XML格式: {content[:100]}")
                return True

            # 如果是引用消息，检查是否有Quote字段
            if message.get("Quote"):
                logger.info("Dify: 检测到引用消息，使用普通文本处理")
                return True

            # 解析XML内容
            root = ET.fromstring(message["Content"])
            appmsg = root.find("appmsg")
            if appmsg is None:
                return True

            type_element = appmsg.find("type")
            if type_element is None:
                return True

            type_value = int(type_element.text)
            logger.info(f"Dify: XML消息类型: {type_value}")

            # 检测是否是文件消息（类型6）
            if type_value == 6:
                logger.info("Dify: 检测到文件消息")

                # 提取文件信息
                title = appmsg.find("title").text
                appattach = appmsg.find("appattach")
                attach_id = appattach.find("attachid").text
                file_extend = appattach.find("fileext").text
                total_len = int(appattach.find("totallen").text)

                logger.info(f"Dify: 文件名: {title}")
                logger.info(f"Dify: 文件扩展名: {file_extend}")
                logger.info(f"Dify: 附件ID: {attach_id}")
                logger.info(f"Dify: 文件大小: {total_len}")

                # 不发送下载提示
                logger.info(f"开始下载文件: {title}, 大小: {total_len} 字节")

                # 使用 /Tools/DownloadFile API 下载文件
                logger.info("Dify: 开始下载文件...")
                # 分段下载大文件
                # 每次下载 64KB
                chunk_size = 64 * 1024  # 64KB
                app_id = appmsg.get("appid", "")

                # 创建一个字节数组来存储完整的文件数据
                file_data = bytearray()

                # 计算需要下载的分段数量
                chunks = (total_len + chunk_size - 1) // chunk_size  # 向上取整

                logger.info(f"Dify: 开始分段下载文件，总大小: {total_len} 字节，分 {chunks} 段下载")

                # 尝试两个不同的API端点
                urls = [
                    f'http://127.0.0.1:9011/api/Tools/DownloadFile',
                    f'http://127.0.0.1:9011/VXAPI/Tools/DownloadFile'
                ]

                download_success = False

                for url in urls:
                    if download_success:
                        break

                    file_data.clear()  # 清空之前的数据
                    logger.info(f"Dify: 尝试使用 {url} 下载文件")

                    # 分段下载
                    for i in range(chunks):
                        start_pos = i * chunk_size
                        # 最后一段可能不足 chunk_size
                        current_chunk_size = min(chunk_size, total_len - start_pos)

                        logger.info(f"Dify: 下载第 {i+1}/{chunks} 段，起始位置: {start_pos}，大小: {current_chunk_size} 字节")

                        async with aiohttp.ClientSession() as session:
                            # 设置较长的超时时间
                            timeout = aiohttp.ClientTimeout(total=60)  # 1分钟

                            # 构造请求参数
                            json_param = {
                                "AppID": app_id,
                                "AttachId": attach_id,
                                "DataLen": total_len,
                                "Section": {
                                    "DataLen": current_chunk_size,
                                    "StartPos": start_pos
                                },
                                "UserName": "",  # 可选参数
                                "Wxid": bot.wxid
                            }

                            logger.info(f"Dify: 调用下载文件API: AttachId={attach_id}, 起始位置: {start_pos}, 大小: {current_chunk_size}")
                            response = await session.post(
                                url,
                                json=json_param,
                                timeout=timeout
                            )

                            # 处理响应
                            try:
                                json_resp = await response.json()

                                if json_resp.get("Success"):
                                    data = json_resp.get("Data")

                                    # 尝试从不同的响应格式中获取文件数据
                                    chunk_data = None
                                    if isinstance(data, dict):
                                        if "buffer" in data:
                                            chunk_data = base64.b64decode(data["buffer"])
                                        elif "data" in data and isinstance(data["data"], dict) and "buffer" in data["data"]:
                                            chunk_data = base64.b64decode(data["data"]["buffer"])
                                        else:
                                            try:
                                                chunk_data = base64.b64decode(str(data))
                                            except:
                                                logger.error(f"Dify: 无法解析文件数据: {data}")
                                    elif isinstance(data, str):
                                        try:
                                            chunk_data = base64.b64decode(data)
                                        except:
                                            logger.error(f"Dify: 无法解析文件数据字符串")

                                    if chunk_data:
                                        # 将分段数据添加到完整文件中
                                        file_data.extend(chunk_data)
                                        logger.info(f"Dify: 第 {i+1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                    else:
                                        logger.warning(f"Dify: 第 {i+1}/{chunks} 段数据为空")
                                        break
                                else:
                                    error_msg = json_resp.get("Message", "Unknown error")
                                    logger.error(f"Dify: 第 {i+1}/{chunks} 段下载失败: {error_msg}")
                                    break
                            except Exception as e:
                                logger.error(f"Dify: 解析第 {i+1}/{chunks} 段响应失败: {e}")
                                break

                    # 检查文件是否下载完整
                    if len(file_data) > 0:
                        logger.info(f"Dify: 文件下载成功: AttachId={attach_id}, 实际大小: {len(file_data)} 字节")
                        download_success = True
                        break
                    else:
                        logger.warning("Dify: 文件数据为空，尝试下一个API端点")

                # 如果文件下载成功
                if download_success:
                    # 确定文件类型
                    mime_type = mimetypes.guess_type(f"{title}.{file_extend}")[0] or "application/octet-stream"

                    # 确保文件数据是二进制格式
                    if isinstance(file_data, str):
                        try:
                            binary_file_data = base64.b64decode(file_data)
                            logger.info(f"Dify: 将base64字符串转换为二进制数据，大小: {len(binary_file_data)} 字节")
                        except Exception as e:
                            logger.error(f"Dify: Base64解码失败: {e}")
                            binary_file_data = file_data.encode('utf-8')
                    elif isinstance(file_data, bytearray):
                        binary_file_data = bytes(file_data)
                        logger.info(f"Dify: 将bytearray转换为二进制数据，大小: {len(binary_file_data)} 字节")
                    else:
                        binary_file_data = file_data

                    # 处理文件名，避免重复的扩展名
                    if title.lower().endswith(f".{file_extend.lower()}"):
                        file_name = title  # 如果标题已经包含扩展名，直接使用
                    else:
                        file_name = f"{title}.{file_extend}"  # 否则添加扩展名

                    logger.info(f"Dify: 处理后的文件名: {file_name}")

                    # 缓存文件
                    from_wxid = message["FromWxid"]
                    sender_wxid = message.get("SenderWxid", from_wxid)
                    self.cache_file(sender_wxid, binary_file_data, file_name, mime_type)

                    # 如果是私聊，也缓存到聊天对象的ID
                    if from_wxid != sender_wxid:
                        self.cache_file(from_wxid, binary_file_data, file_name, mime_type)

                    logger.info(f"文件下载成功并已缓存: {file_name}, 大小: {len(binary_file_data)/1024:.2f} KB")
                else:
                    logger.warning("Dify: 所有API端点尝试失败")
        except Exception as e:
            logger.error(f"Dify: 处理XML消息时发生错误: {str(e)}")
            logger.error(traceback.format_exc())

        return True  # 允许后续插件处理

    @on_file_message(priority=20)
    async def handle_file(self, bot: WechatAPIClient, message: dict):
        """处理文件消息"""
        if not self.enable:
            return

        try:
            # 获取文件消息的关键信息
            msg_id = message.get("MsgId")
            from_wxid = message.get("FromWxid")
            sender_wxid = message.get("SenderWxid")
            file_content = message.get("Content")

            logger.info(f"收到文件消息: MsgId={msg_id}, FromWxid={from_wxid}, SenderWxid={sender_wxid}")

            # 如果Content是二进制数据，直接使用
            if isinstance(file_content, bytes) and len(file_content) > 0:
                logger.info(f"文件内容是二进制数据，大小: {len(file_content)} 字节")

                # 获取文件名和类型
                file_name = message.get("FileName", f"file_{int(time.time())}")

                # 检测文件类型
                mime_type = "application/octet-stream"  # 默认类型
                try:
                    kind = filetype.guess(file_content)
                    if kind is not None:
                        mime_type = kind.mime
                        # 如果文件名没有后缀，添加正确的后缀
                        if not os.path.splitext(file_name)[1]:
                            file_name = f"{file_name}.{kind.extension}"
                except Exception as e:
                    logger.error(f"检测文件类型失败: {e}")

            # 如果Content是XML字符串，解析并下载文件
            elif isinstance(file_content, str) and ("<appmsg" in file_content or "<msg>" in file_content):
                logger.info("文件内容是XML格式，尝试解析并下载文件")
                try:
                    # 解析XML
                    import xml.etree.ElementTree as ET
                    import mimetypes
                    import base64

                    # 处理可能的XML格式差异
                    if "<msg>" in file_content and "<appmsg" in file_content:
                        # 提取<appmsg>部分
                        start = file_content.find("<appmsg")
                        end = file_content.find("</appmsg>") + 9
                        appmsg_xml = file_content[start:end]
                        root = ET.fromstring(f"<root>{appmsg_xml}</root>")
                        appmsg = root.find('appmsg')
                    else:
                        root = ET.fromstring(file_content)
                        appmsg = root.find('.//appmsg')

                    if appmsg is not None:
                        # 获取文件名
                        title = appmsg.find('.//title')
                        file_name = title.text if title is not None and title.text else f"file_{int(time.time())}"

                        # 获取文件类型
                        fileext = appmsg.find('.//fileext')
                        if fileext is not None and fileext.text:
                            ext = fileext.text.lower()
                            if not file_name.lower().endswith(f".{ext}"):
                                file_name = f"{file_name}.{ext}"
                            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
                        else:
                            mime_type = "application/octet-stream"

                        # 获取下载所需信息
                        appattach = appmsg.find('.//appattach')
                        if appattach is not None:
                            attachid = appattach.find('.//attachid')
                            aeskey = appattach.find('.//aeskey')
                            totallen = appattach.find('.//totallen')

                            # 获取文件大小
                            total_len = int(totallen.text) if totallen is not None and totallen.text and totallen.text.isdigit() else 0

                            # 获取附件ID和其他下载所需信息
                            attach_id = None
                            cdn_url = None
                            aes_key = None

                            if attachid is not None and attachid.text:
                                attach_id = attachid.text.strip()
                                logger.info(f"找到附件ID: {attach_id}")

                            # 获取CDN URL和AES密钥（用于方法3）
                            cdnattachurl = appattach.find('.//cdnattachurl')
                            if cdnattachurl is not None and cdnattachurl.text:
                                cdn_url = cdnattachurl.text.strip()
                                logger.info(f"找到CDN URL: {cdn_url}")

                            if aeskey is not None and aeskey.text:
                                aes_key = aeskey.text.strip()
                                logger.info(f"找到AES密钥: {aes_key}")

                                # 开始下载文件
                                logger.info(f"开始下载文件: {file_name}, 大小: {total_len} 字节")

                                # 尝试不同的下载方法
                                try:
                                    file_data = None

                                    # 方法1: 如果有附件ID，使用download_attach方法
                                    if attach_id:
                                        logger.debug(f"方法1: 尝试使用download_attach方法下载文件，附件ID: {attach_id}")
                                        file_data = await bot.download_attach(attach_id)

                                    # 方法3: 如果有CDN URL和AES密钥，使用download_image方法
                                    if not file_data and cdn_url and aes_key:
                                        logger.debug(f"方法3: 尝试使用download_image方法下载文件，CDN URL: {cdn_url}")
                                        try:
                                            image_data = await bot.download_image(aes_key, cdn_url)
                                            if image_data:
                                                if isinstance(image_data, str):
                                                    try:
                                                        file_data = base64.b64decode(image_data)
                                                        logger.info(f"使用download_image成功下载文件，大小: {len(file_data)} 字节")
                                                    except Exception as e:
                                                        logger.error(f"Base64解码失败: {e}")
                                        except Exception as e:
                                            logger.error(f"download_image方法失败: {e}")
                                    if not file_data:
                                        # 方法2: 使用Tools/DownloadFile API分段下载文件
                                        logger.debug(f"尝试使用Tools/DownloadFile API分段下载文件")

                                        # 分段下载大文件
                                        chunk_size = 64 * 1024  # 64KB
                                        chunks = (total_len + chunk_size - 1) // chunk_size  # 向上取整
                                        file_data_bytes = bytearray()
                                        download_success = False

                                        # 尝试两个不同的API端点
                                        urls = [
                                            f'http://{bot.ip}:{bot.port}/api/Tools/DownloadFile',
                                            f'http://{bot.ip}:{bot.port}/VXAPI/Tools/DownloadFile'
                                        ]

                                        # 尝试每个API端点
                                        for url in urls:
                                            if download_success:
                                                break

                                            logger.info(f"尝试使用 {url} 分段下载文件，总大小: {total_len} 字节，分 {chunks} 段下载")
                                            file_data_bytes.clear()  # 清空之前的数据

                                            try:
                                                async with aiohttp.ClientSession() as session:
                                                    # 分段下载
                                                    for i in range(chunks):
                                                        start_pos = i * chunk_size
                                                        # 最后一段可能不足 chunk_size
                                                        current_chunk_size = min(chunk_size, total_len - start_pos)

                                                        logger.debug(f"下载第 {i+1}/{chunks} 段，起始位置: {start_pos}，大小: {current_chunk_size} 字节")

                                                        # 构造请求参数
                                                        json_param = {
                                                            "AppID": "",  # 可选参数
                                                            "AttachId": attach_id,
                                                            "DataLen": total_len,
                                                            "Section": {
                                                                "DataLen": current_chunk_size,
                                                                "StartPos": start_pos
                                                            },
                                                            "UserName": "",  # 可选参数
                                                            "Wxid": bot.wxid
                                                        }

                                                        # 设置较长的超时时间
                                                        timeout = aiohttp.ClientTimeout(total=60)  # 1分钟

                                                        # 发送请求
                                                        try:
                                                            async with session.post(url, json=json_param, timeout=timeout) as resp:
                                                                if resp.status == 200:
                                                                    resp_json = await resp.json()
                                                                    if resp_json.get("Success"):
                                                                        data = resp_json.get("Data")
                                                                        if isinstance(data, str):
                                                                            try:
                                                                                chunk_data = base64.b64decode(data)
                                                                                file_data_bytes.extend(chunk_data)
                                                                                logger.debug(f"第 {i+1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                                                            except Exception as e:
                                                                                logger.error(f"Base64解码失败: {e}")
                                                                                break
                                                                        elif isinstance(data, dict) and "buffer" in data:
                                                                            try:
                                                                                chunk_data = base64.b64decode(data["buffer"])
                                                                                file_data_bytes.extend(chunk_data)
                                                                                logger.debug(f"第 {i+1}/{chunks} 段下载成功，大小: {len(chunk_data)} 字节")
                                                                            except Exception as e:
                                                                                logger.error(f"Buffer Base64解码失败: {e}")
                                                                                break
                                                                        else:
                                                                            logger.warning(f"无法解析响应数据: {data}")
                                                                            break
                                                                    else:
                                                                        logger.warning(f"API返回错误: {resp_json}")
                                                                        break
                                                                else:
                                                                    logger.warning(f"API请求失败: {resp.status}")
                                                                    break
                                                        except Exception as e:
                                                            logger.error(f"下载第 {i+1}/{chunks} 段时出错: {e}")
                                                            break

                                                    # 检查文件是否下载完整
                                                    if len(file_data_bytes) > 0:
                                                        logger.info(f"文件分段下载成功，实际大小: {len(file_data_bytes)} 字节")
                                                        file_data = base64.b64encode(file_data_bytes).decode('utf-8')
                                                        download_success = True
                                                        break
                                                    else:
                                                        logger.warning(f"文件下载失败，数据为空")
                                            except Exception as e:
                                                logger.error(f"尝试使用 {url} 分段下载文件时出错: {e}")
                                                logger.error(traceback.format_exc())

                                        # 如果所有尝试都失败
                                        if not download_success:
                                            logger.error("所有API端点尝试失败")
                                except Exception as e:
                                    logger.error(f"下载文件异常: {e}")
                                    logger.error(traceback.format_exc())
                                    file_data = None

                                if file_data:
                                    # 如果返回的是base64字符串，解码为二进制
                                    if isinstance(file_data, str):
                                        try:
                                            file_content = base64.b64decode(file_data)
                                        except Exception as e:
                                            logger.error(f"Base64解码失败: {e}")
                                            file_content = file_data.encode('utf-8')
                                    elif isinstance(file_data, dict) and "buffer" in file_data:
                                        try:
                                            file_content = base64.b64decode(file_data["buffer"])
                                        except Exception as e:
                                            logger.error(f"Buffer Base64解码失败: {e}")
                                            file_content = str(file_data).encode('utf-8')
                                    else:
                                        file_content = str(file_data).encode('utf-8')

                                    logger.info(f"文件下载成功，大小: {len(file_content)} 字节")
                                else:
                                    logger.error("文件下载失败或内容为空")
                                    await bot.send_text_message(from_wxid, "文件下载失败，请重新发送。")
                                    return
                            else:
                                logger.error("XML中缺少必要的附件ID")
                                await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                                return
                        else:
                            logger.error("XML中缺少appattach节点")
                            await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                            return
                    else:
                        logger.error("XML格式不正确，无法解析appmsg节点")
                        await bot.send_text_message(from_wxid, "无法解析文件信息，请重新发送。")
                        return
                except Exception as e:
                    logger.error(f"解析XML或下载文件失败: {e}")
                    logger.error(traceback.format_exc())
                    await bot.send_text_message(from_wxid, f"处理文件失败: {str(e)}")
                    return
            else:
                logger.warning(f"文件内容格式不支持: {type(file_content)}")
                await bot.send_text_message(from_wxid, "不支持的文件格式，请重新发送。")
                return

            # 缓存文件
            self.cache_file(sender_wxid, file_content, file_name, mime_type)

            # 如果是私聊，也缓存到聊天对象的ID
            if from_wxid != sender_wxid:
                self.cache_file(from_wxid, file_content, file_name, mime_type)

            logger.info(f"文件已缓存: {file_name}, 大小: {len(file_content)/1024:.2f} KB, 类型: {mime_type}")

        except Exception as e:
            logger.error(f"处理文件消息失败: {e}")
            logger.error(traceback.format_exc())
