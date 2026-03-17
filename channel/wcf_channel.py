#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
WeChat-Ferry Channel - 基于wcf的实现
仅支持Windows环境，需要特定版本的微信PC客户端
参考：https://github.com/lich0821/WeChatFerry
"""

import os
import sys
import time
import threading
import asyncio
import inspect
from typing import Dict, Any, Optional
from loguru import logger

# 平台能力标记：允许在非Windows环境导入模块，便于测试与静态检查。
IS_WINDOWS = sys.platform == 'win32'

try:
    from wcferry import Wcf, WxMsg
    WCF_AVAILABLE = IS_WINDOWS
except ImportError as e:
    logger.warning(f"wcferry导入失败: {e}")
    if IS_WINDOWS:
        logger.warning("请确保已安装wcferry: pip install wcferry")
    WCF_AVAILABLE = False
    # 定义占位类
    class Wcf: pass
    class WxMsg: pass

from channel.channel import Channel, Context, Reply, ReplyType


class WcfChannel(Channel):
    """
    WeChat-Ferry (wcf) 微信消息通道
    基于Windows Hook技术实现
    """
    
    def __init__(self, config: Dict[str, Any]):
        """初始化wcf通道"""
        super().__init__(config)
        # 兼容两种输入：全量配置（推荐）或仅wcf子配置（历史行为）。
        self.wcf_config = config.get('wcf', {}) if 'wcf' in config else config
        
        # wcf实例
        self.wcf: Optional[Wcf] = None
        self.running = False
        self.bot_wxid = None
        self.bot_info = None
        
        # 消息处理线程
        self.msg_thread = None
        self.loop = None  # 主事件循环，用于在线程中安全调度异步handler。
        
        # 群组白名单管理
        self.group_white_list = set(self.wcf_config.get('group_name_white_list', []))
        
        # 联系人缓存
        self.contacts_cache = {}
        self.last_cache_update = 0
        self.cache_ttl = 300  # 5分钟缓存
        
        if not WCF_AVAILABLE:
            logger.error("wcferry未正确安装，wcf通道功能将受限")
    
    def set_event_loop(self, loop):
        """设置主事件循环，供消息线程安全提交协程任务。"""
        self.loop = loop

    def _detect_wechat_installation(self) -> Dict[str, str]:
        """在 Windows 上尽力探测微信安装信息。

        说明：
        - 这里是“最佳努力”探测，仅用于在初始化失败前输出更可操作的诊断信息；
        - wcferry 底层仍会自行读取安装信息并初始化 SDK，因此本函数不会替代官方初始化逻辑。
        """
        info: Dict[str, str] = {
            'source': 'unknown',
            'install_path': '',
            'wechat_exe': '',
        }

        if not IS_WINDOWS:
            return info

        # 用户可在环境变量中提供可执行文件路径，方便绕过“注册表缺失/不可读”场景。
        env_wechat_exe = os.environ.get('WCF_WECHAT_EXE', '').strip()
        if env_wechat_exe:
            info['source'] = 'env:WCF_WECHAT_EXE'
            info['wechat_exe'] = env_wechat_exe
            info['install_path'] = os.path.dirname(env_wechat_exe)
            return info

        try:
            import winreg  # type: ignore

            # 注：不同安装来源（官网安装包/企业分发等）注册表路径可能不同，
            # 这里优先尝试 WeChat 常见键值；如不存在则仅记录诊断，不中断主流程。
            reg_candidates = [
                (winreg.HKEY_CURRENT_USER, r"Software\\Tencent\\WeChat", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\\Tencent\\WeChat", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"Software\\WOW6432Node\\Tencent\\WeChat", "InstallPath"),
            ]
            for root, sub_key, value_name in reg_candidates:
                try:
                    with winreg.OpenKey(root, sub_key) as key:
                        install_path, _ = winreg.QueryValueEx(key, value_name)
                        if install_path:
                            info['source'] = f"registry:{sub_key}"
                            info['install_path'] = install_path
                            # 仅用于提示，不强依赖该文件存在。
                            info['wechat_exe'] = os.path.join(install_path, 'WeChat.exe')
                            return info
                except OSError:
                    continue
        except Exception as e:
            logger.debug(f"读取微信注册表信息时出现异常（可忽略）: {e}")

        return info

    def _log_wcf_init_guidance(self, error: Exception):
        """输出 WCF 初始化失败时的排障建议。

        该方法专门针对 Windows 现场问题提供可执行建议，减少用户反复试错。
        """
        err_text = str(error)
        installation = self._detect_wechat_installation()

        logger.error("WCF 初始化失败：无法打开微信或注入 SDK。")
        logger.error(f"底层错误: {err_text}")

        if installation.get('install_path'):
            logger.info(
                "检测到微信安装路径（仅供参考）: "
                f"{installation.get('install_path')} (source={installation.get('source')})"
            )
        else:
            logger.warning(
                "未检测到微信安装路径（注册表可能缺失/不可读，或微信来自商店版安装）。"
            )

        # 下面给出按优先级排序的可执行建议，便于用户逐条排查。
        logger.info("WCF 排障建议：")
        logger.info("1) 确保已安装并登录“微信 PC 桌面版”（避免 Microsoft Store 版本）。")
        logger.info("2) 先手动打开微信并保持登录，再启动 python app.py。")
        logger.info("3) 使用“管理员权限”运行 PyCharm/PowerShell 后再次启动。")
        logger.info("4) 安装/切换到 WeChatFerry 支持的微信版本（版本不匹配会初始化失败）。")
        logger.info("5) 如注册表缺失，可手动设置环境变量 WCF_WECHAT_EXE 指向 WeChat.exe 后重试。")

    def _update_contacts_cache(self):
        """更新联系人缓存"""
        try:
            current_time = time.time()
            if current_time - self.last_cache_update > self.cache_ttl:
                contacts = self.wcf.get_contacts()
                self.contacts_cache = {c['wxid']: c for c in contacts}
                self.last_cache_update = current_time
                logger.debug(f"更新联系人缓存，共 {len(self.contacts_cache)} 个联系人")
        except Exception as e:
            logger.error(f"更新联系人缓存失败: {e}")
    
    def _get_contact_name(self, wxid: str) -> str:
        """获取联系人名称"""
        self._update_contacts_cache()
        contact = self.contacts_cache.get(wxid, {})
        return contact.get('name', wxid)
    
    def _get_room_name(self, room_id: str) -> str:
        """获取群名称"""
        self._update_contacts_cache()
        room = self.contacts_cache.get(room_id, {})
        return room.get('name', room_id)
    
    def process_message(self, msg: WxMsg):
        """处理接收到的消息"""
        try:
            # 构建消息上下文
            context = self._build_context(msg)
            if not context:
                return
            
            # 检查白名单
            if not self.check_white_list(context):
                return
            
            # 检查是否触发机器人
            if not self._should_handle(context):
                return
            
            # 处理消息：wcf在独立线程收消息，需将协程处理器提交回主事件循环。
            self._dispatch_context(context)
            
        except Exception as e:
            logger.error(f"处理消息时出错: {e}", exc_info=True)
    
    def _build_context(self, msg: WxMsg) -> Optional[Context]:
        """构建消息上下文"""
        try:
            # 获取消息类型
            msg_type = self._get_msg_type(msg.type)
            
            # 获取消息内容
            content = msg.content
            
            # 判断是否为群消息
            is_group = bool(msg.roomid)
            
            # 获取发送者信息
            sender_id = msg.sender
            sender_name = self._get_contact_name(sender_id)
            
            # 获取群信息
            group_id = msg.roomid if is_group else ''
            group_name = self._get_room_name(group_id) if is_group else ''
            
            # 检查是否被@
            is_at = False
            if is_group and self.bot_wxid:
                # 简单判断是否包含@机器人
                is_at = f"@{self.bot_info.get('name', '')}" in content if self.bot_info else False
            
            context = Context(
                type=msg_type,
                content=content,
                msg=msg,
                is_group=is_group,
                nick_name=sender_name,
                user_id=sender_id,
                group_name=group_name,
                room_id=group_id,
                is_at=is_at
            )
            
            return context
            
        except Exception as e:
            logger.error(f"构建消息上下文失败: {e}", exc_info=True)
            return None
    
    def _get_msg_type(self, type_code: int) -> str:
        """转换消息类型"""
        type_map = {
            1: 'TEXT',      # 文本
            3: 'IMAGE',     # 图片
            34: 'VOICE',    # 语音
            43: 'VIDEO',    # 视频
            49: 'FILE',     # 文件
            47: 'EMOJI',    # 表情
            48: 'LOCATION', # 位置
        }
        return type_map.get(type_code, 'UNKNOWN')
    
    def _should_handle(self, context: Context) -> bool:
        """判断是否应该处理该消息"""
        # 不处理自己发送的消息
        if self.bot_wxid and context.user_id == self.bot_wxid:
            return False
        
        if context.is_group:
            # 群聊消息
            if context.is_at:
                # 被@了
                return True
            # 检查群聊前缀
            prefix_list = self.wcf_config.get('group_chat_prefix', [])
            matched, _ = self.check_prefix(context.content, prefix_list)
            return matched
        else:
            # 私聊消息
            prefix_list = self.wcf_config.get('single_chat_prefix', [])
            matched, _ = self.check_prefix(context.content, prefix_list)
            return matched
    
    def _dispatch_context(self, context: Context):
        """在线程环境下安全分发消息到已注册处理器。

        关键假设：
        1) wcf 通道通常在独立线程里收消息；
        2) 业务 handler 可能是 async，也可能是 sync（测试桩或历史实现）。
        因此这里统一做“可等待对象”判断，避免把普通对象误当协程提交导致异常。
        """
        handler = self.handlers.get(context.type)
        if not handler:
            logger.warning(f"未找到消息类型 {context.type} 的处理器")
            return

        try:
            result = handler(context)

            if inspect.isawaitable(result):
                if self.loop:
                    # 在主事件循环中执行协程，避免线程内重复创建事件循环。
                    future = asyncio.run_coroutine_threadsafe(result, self.loop)
                    reply = future.result(timeout=60)
                else:
                    # 回退路径：无主循环时直接阻塞执行，确保功能最小可用（主要用于测试场景）。
                    reply = asyncio.run(result)
            else:
                # 同步处理器直接返回结果。
                reply = result

            if reply:
                context.reply = reply
                self.send(reply, context)
        except Exception as e:
            logger.error(f"分发wcf消息失败: {e}", exc_info=True)

    def send(self, reply: Reply, context: Context):
        """发送回复消息"""
        try:
            if not self.wcf or not WCF_AVAILABLE:
                logger.error("wcf未初始化或不可用")
                return
            
            # 获取消息对象
            msg: WxMsg = context.msg
            receiver = msg.roomid if msg.roomid else msg.sender
            
            # 根据回复类型发送不同内容
            if reply.type == ReplyType.TEXT:
                # 如果是群消息且需要@发送者
                at_list = []
                if msg.roomid and self.wcf_config.get('group_at_sender', True):
                    at_list = [msg.sender]
                
                self.wcf.send_text(reply.content, receiver, at_list)
                
            elif reply.type == ReplyType.IMAGE:
                if isinstance(reply.content, str) and os.path.exists(reply.content):
                    self.wcf.send_image(reply.content, receiver)
                else:
                    logger.warning(f"图片文件不存在: {reply.content}")
                    
            elif reply.type == ReplyType.FILE:
                if isinstance(reply.content, str) and os.path.exists(reply.content):
                    self.wcf.send_file(reply.content, receiver)
                else:
                    logger.warning(f"文件不存在: {reply.content}")
                    
            else:
                logger.warning(f"不支持的回复类型: {reply.type}")
                
        except Exception as e:
            logger.error(f"发送消息失败: {e}", exc_info=True)
    
    def message_loop(self):
        """消息接收循环"""
        while self.running:
            try:
                msg = self.wcf.get_msg(timeout=1)
                if msg:
                    self.process_message(msg)
            except TimeoutError:
                continue
            except Exception as e:
                logger.error(f"消息循环出错: {e}", exc_info=True)
                time.sleep(1)
    
    def startup(self):
        """启动通道"""
        try:
            if not IS_WINDOWS:
                logger.error("wcf通道仅支持Windows系统，当前环境无法启动。")
                return

            if not WCF_AVAILABLE:
                logger.error("wcferry未安装，无法启动wcf通道")
                logger.info("请运行: pip install wcferry")
                return
            
            logger.info("正在启动wcf...")

            # 在真正初始化前输出一次安装探测结果，便于现场排障。
            installation = self._detect_wechat_installation()
            if installation.get('wechat_exe'):
                logger.info(
                    "微信可执行文件探测结果（仅诊断用途）: "
                    f"{installation.get('wechat_exe')} (source={installation.get('source')})"
                )
            else:
                logger.warning("未探测到微信可执行文件路径，后续若初始化失败请按日志中的排障建议检查。")
            
            # 创建wcf实例
            self.wcf = Wcf()
            
            # 检查登录状态
            if not self.wcf.is_login():
                logger.warning("微信未登录，请先登录微信PC客户端")
                # 获取二维码
                qrcode = self.wcf.get_qrcode()
                if qrcode:
                    logger.info(f"请扫描二维码登录: {qrcode}")
                # 等待登录
                while not self.wcf.is_login():
                    time.sleep(2)
            
            # 获取登录信息
            self.bot_info = self.wcf.get_user_info()
            self.bot_wxid = self.bot_info.get('wxid')
            logger.info(f"登录成功: {self.bot_info.get('name')} ({self.bot_wxid})")
            
            # 启用消息接收
            self.wcf.enable_receiving_msg()
            self.running = True
            
            # 启动消息处理线程
            self.msg_thread = threading.Thread(
                target=self.message_loop,
                daemon=True
            )
            self.msg_thread.start()
            
            logger.info("="*50)
            logger.info("DailyBot (wcf) 已启动，等待消息...")
            logger.info("="*50)
            
        except Exception as e:
            self._log_wcf_init_guidance(e)
            logger.error(f"启动wcf通道失败: {e}", exc_info=True)
            raise
    
    def shutdown(self):
        """关闭通道"""
        self.running = False
        if self.wcf and WCF_AVAILABLE:
            try:
                self.wcf.disable_recv_msg()
                self.wcf = None
            except Exception as e:
                logger.warning(f"关闭wcf消息接收时出现异常: {e}")
        logger.info("wcf通道已关闭") 
