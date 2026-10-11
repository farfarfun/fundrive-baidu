import sys
from collections.abc import Callable
from typing import Any

from .keyboard import KeyboardListener


class KeyHandler:
    """保存一个按键与其处理回调的对应关系。"""

    def __init__(self, key: str, callback: Callable[..., Any]):
        """初始化按键处理器。

        :param key: 要监听的按键
        :param callback: 按键触发时调用的回调
        """
        self._key = key
        self._callback = callback

    def handle(self, key: str) -> None:
        """在按键匹配时调用回调。

        :param key: 当前收到的按键
        """
        if self._key == key:
            self._callback(key)


class KeyboardMonitor:
    """集中注册并分发终端按键事件。"""

    KEY_HANDLERS: list[KeyHandler] = []

    @classmethod
    def register(cls, key_handler: KeyHandler) -> None:
        """注册按键处理器。

        :param key_handler: 要注册的按键处理器
        """
        cls.KEY_HANDLERS.append(key_handler)

    @classmethod
    def on(cls, key: str) -> None:
        """向全部处理器分发按键。

        :param key: 当前收到的按键
        """
        for handler in cls.KEY_HANDLERS:
            handler.handle(key)


global _KEYBOARD_LISTENER_STARTED
_KEYBOARD_LISTENER_STARTED = False

global _KEYBOARD_LISTENER
_KEYBOARD_LISTENER = None


def keyboard_listener_start() -> None:
    """在终端环境中启动全局按键监听器。"""
    global _KEYBOARD_LISTENER_STARTED
    if _KEYBOARD_LISTENER_STARTED:
        return

    # KeyboardListener is only available in a terminal
    if sys.stdin.isatty():
        listener = KeyboardListener(on=KeyboardMonitor.on)
        listener.start()

        global _KEYBOARD_LISTENER
        _KEYBOARD_LISTENER = listener

    _KEYBOARD_LISTENER_STARTED = True
