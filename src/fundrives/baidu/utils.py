"""百度网盘模块用到的小工具函数。"""

import json
import math
import string
import time


def dump_json(obj) -> str:
    """序列化成百度接口要求的紧凑 JSON（无多余空格、不转义非 ASCII）。

    :param obj: 可被 :mod:`json` 序列化的对象
    :return: JSON 字符串
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def format_date(timestramp: int) -> str:
    """把 Unix 时间戳格式化成本地时区的 ``YYYY-MM-DD HH:MM:SS``。

    :param timestramp: Unix 时间戳（秒）
    :return: 格式化后的日期时间字符串
    """
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestramp))


def format_time(seconds: int) -> str:
    """把秒数格式化成 ``Ndays HH:MM:SS``。

    :param seconds: 时长（秒）
    :return: 格式化后的时长字符串
    """
    tm = seconds
    ft = ""
    for unit in [60, 60, 24]:
        ft = f"{tm % unit:0>2}" + ft
        if unit != 24:
            ft = ":" + ft
        tm //= unit

    ft = f"{tm}days {ft}"
    return ft


def human_size(size: int) -> str:
    """把字节数格式化成人类可读的大小，如 ``1.5 MB``。

    :param size: 字节数
    :return: 带单位的大小字符串，单位最大到 ``TB``
    """
    s = float(size)
    v = ""
    t = ""
    for t in ["B", "KB", "MB", "GB", "TB"]:
        if s < 1024.0:
            v = f"{s:3.1f}"
            break
        s /= 1024.0
    # 超过 1024TB 时循环不会 break，这里兜底，避免返回 " TB" 这种缺数字的结果
    if not v:
        v = f"{s:3.1f}"
    v = v.removesuffix(".0")
    return f"{v} {t}"


_nums_set = set(string.digits + ".")


def human_size_to_int(size_str: str) -> int:
    """把 :func:`human_size` 那种带单位的大小字符串解析回字节数。

    单位大小写不敏感，只看首字母，因此 ``"1G"``、``"1GB"``、``"1 GiB"`` 等价；
    不带单位时按字节处理。

    :param size_str: 带单位的大小字符串
    :return: 向下取整后的字节数；解析不出数字时返回 ``0``
    """
    size_str = size_str.strip()
    if not size_str:
        return 0

    i = 0
    while i < len(size_str):
        if size_str[i] in _nums_set:
            i += 1
            continue
        else:
            break

    if i == 0:
        return 0

    s = float(size_str[:i])
    _s = s

    unit = size_str[i:].upper().replace(" ", "")
    if not unit:
        return math.floor(_s)

    for t in ["KB", "MB", "GB", "TB"]:
        _s *= 1024
        if unit == t or unit[0] == t[0]:
            return math.floor(_s)

    return math.floor(s)
