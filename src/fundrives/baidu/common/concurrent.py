from collections.abc import Callable
from functools import wraps
from threading import Semaphore
from typing import Any


def sure_release(semaphore: Semaphore, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    finally:
        semaphore.release()


def retry(times: int, except_callback: Callable[..., Any] | None = None):
    """为函数调用增加失败重试。

    :param times: 最大重试次数；小于 0 时持续重试
    :param except_callback: 每次失败后接收异常和当前次数的回调
    :return: 接收原函数并返回带重试逻辑的装饰器
    """

    def wrap(func):
        @wraps(func)
        def retry_it(*args, **kwargs):
            nonlocal times
            if times < 0:  # forever
                times = 1 << 32

            for i in range(1, times + 1):
                try:
                    r = func(*args, **kwargs)
                    return r
                except Exception as err:
                    if except_callback is not None:
                        except_callback(err, i)

                    if i == times:
                        raise err

        return retry_it

    return wrap
