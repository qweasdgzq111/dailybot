import asyncio
import threading
import unittest
import sys
import types
from pathlib import Path

# 将仓库根目录加入sys.path，保证在不同pytest启动目录下都能导入项目包。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 测试环境可能未安装loguru，这里提供最小桩避免导入失败。
if 'loguru' not in sys.modules:
    logger_stub = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
        debug=lambda *a, **k: None,
    )
    sys.modules['loguru'] = types.SimpleNamespace(logger=logger_stub)

if 'websocket' not in sys.modules:
    # JS通道在包导入链路中会引用该模块，这里提供最小桩。
    sys.modules['websocket'] = types.SimpleNamespace(WebSocketApp=object)

from channel.channel import Reply, ReplyType
from channel.wcf_channel import WcfChannel


class TestWcfChannel(unittest.TestCase):
    def test_build_context_maps_fields_correctly(self):
        # 使用全量配置路径初始化，验证与ChannelFactory新行为兼容。
        channel = WcfChannel({
            'channel_type': 'wcf',
            'wcf': {
                'single_chat_prefix': ['bot'],
                'group_chat_prefix': ['@bot'],
            }
        })

        # 使用可控桩函数避免依赖真实联系人缓存。
        channel._get_contact_name = lambda wxid: f"name-{wxid}"
        channel._get_room_name = lambda roomid: f"room-{roomid}"
        channel.bot_wxid = 'bot_wxid'
        channel.bot_info = {'name': 'bot'}

        msg = types.SimpleNamespace(
            type=1,
            content='@bot hello',
            roomid='room_001',
            sender='user_001',
        )

        context = channel._build_context(msg)
        self.assertIsNotNone(context)
        self.assertEqual(context.type, 'TEXT')
        self.assertEqual(context.room_id, 'room_001')
        self.assertEqual(context.group_name, 'room-room_001')
        self.assertEqual(context.nick_name, 'name-user_001')
        self.assertTrue(context.kwargs.get('is_at'))

    def test_dispatch_context_runs_async_handler_on_main_loop(self):
        channel = WcfChannel({'single_chat_prefix': ['bot']})

        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
        loop_thread.start()
        channel.set_event_loop(loop)

        called = {'send': False}

        async def async_handler(context):
            return Reply(ReplyType.TEXT, 'ok')

        channel.register_handler('TEXT', async_handler)

        def fake_send(reply, context):
            called['send'] = True
            called['reply'] = reply
            called['context_type'] = context.type

        channel.send = fake_send

        context = types.SimpleNamespace(type='TEXT', reply=None)
        channel._dispatch_context(context)

        self.assertTrue(called['send'])
        self.assertEqual(called['reply'].content, 'ok')
        self.assertEqual(called['context_type'], 'TEXT')

        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)

    def test_dispatch_context_supports_sync_handler(self):
        # 回归测试：确保同步handler不会被误当成协程调度。
        channel = WcfChannel({'single_chat_prefix': ['bot']})

        called = {'send': False}

        def sync_handler(context):
            return Reply(ReplyType.TEXT, 'sync-ok')

        channel.register_handler('TEXT', sync_handler)

        def fake_send(reply, context):
            called['send'] = True
            called['reply'] = reply
            called['context_type'] = context.type

        channel.send = fake_send

        context = types.SimpleNamespace(type='TEXT', reply=None)
        channel._dispatch_context(context)

        self.assertTrue(called['send'])
        self.assertEqual(called['reply'].content, 'sync-ok')
        self.assertEqual(called['context_type'], 'TEXT')


class TestChannelFactoryConfigFlow(unittest.TestCase):
    def test_factory_passes_full_config_into_wcf_channel(self):
        # 延迟导入，避免影响上方桩注入。
        import channel.channel_factory as cf

        captured = {}

        class DummyWcfChannel:
            def __init__(self, config):
                captured['config'] = config

        original_cls = cf.WcfChannel
        original_platform = cf.sys.platform
        try:
            cf.WcfChannel = DummyWcfChannel
            cf.sys.platform = 'win32'

            full_config = {
                'channel_type': 'wcf',
                'system': {'log_level': 'INFO'},
                'wcf': {'single_chat_prefix': ['bot']},
            }
            cf.ChannelFactory.create_channel(full_config)

            self.assertEqual(captured['config'], full_config)
        finally:
            cf.WcfChannel = original_cls
            cf.sys.platform = original_platform


if __name__ == '__main__':
    unittest.main()
