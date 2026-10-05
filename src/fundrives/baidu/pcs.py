from __future__ import annotations

import json
import os
import random
import re
import time
import urllib
from base64 import standard_b64encode
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import IO, Any, Literal
from urllib.error import HTTPError
from urllib.parse import quote_plus, urlparse

import requests  # type: ignore
from farcache import ttl_cache
from farlog import getLogger
from requests_toolbelt import MultipartEncoder, MultipartEncoderMonitor

from .common.crypto import calu_md5, calu_sha1
from .common.date import now_timestamp
from .common.io import MAX_CHUNK_SIZE, RangeRequestIO
from .errors import BaiduPCSError, assert_ok
from .phone import get_phone_model, sum_imei
from .utils import dump_json

logger = getLogger("fundrive")

PCS_BAIDU_COM = "https://pcs.baidu.com"
# PCS_BAIDU_COM = 'http://127.0.0.1:8888'
PAN_BAIDU_COM = "https://pan.baidu.com"
# PAN_BAIDU_COM = 'http://127.0.0.1:8888'

# PCS_UA = "netdisk;P2SP;2.2.90.43;WindowsBaiduYunGuanJia;netdisk;11.4.5;android-android;11.0;JSbridge4.4.0;LogStatistic"
# PCS_UA = "netdisk;P2SP;2.2.91.26;netdisk;11.6.3;GALAXY_S8;android-android;7.0;JSbridge4.4.0;jointBridge;1.1.0;"
# PCS_UA = "netdisk;P2SP;3.0.0.3;netdisk;11.5.3;PC;PC-Windows;android-android;11.0;JSbridge4.4.0"
# PCS_UA = "netdisk;P2SP;3.0.0.8;netdisk;11.12.3;GM1910;android-android;11.0;JSbridge4.4.0;jointBridge;1.1.0;"
PCS_UA = "softxm;netdisk"
PAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.75 Safari/537.36"

PCS_HEADERS = {"User-Agent": PCS_UA}
PAN_HEADERS = {"User-Agent": PAN_UA}

PCS_APP_ID = "778750"
PAN_APP_ID = "250528"

M3u8Type = Literal["M3U8_AUTO_720", "M3U8_AUTO_480"]

#: 所有 HTTP 请求的默认超时 ``(连接超时, 读超时)``，单位秒。
#: 不设超时的话，服务端不响应时整个进程会无限挂起。
DEFAULT_TIMEOUT = (10, 60)

#: :meth:`BaiduPCS.download_link` 因调用过频被接口拒绝时的最大重试次数。
DOWNLOAD_LINK_MAX_RETRIES = 5

#: :meth:`BaiduPCS.download_link` 每次重试之间的等待秒数。
DOWNLOAD_LINK_RETRY_INTERVAL = 2


def _from_to(f: str, t: str) -> dict[str, str]:
    """构造百度网盘批量文件操作所需的 ``{"from": ..., "to": ...}`` 条目。

    :param f: 源远程路径
    :param t: 目标远程路径
    :return: 单条文件操作参数
    """
    return {"from": f, "to": t}


class Method(Enum):
    """本模块用到的 HTTP 请求方法。"""

    Head = "HEAD"
    Get = "GET"
    Post = "POST"


class PcsNode(Enum):
    """走 ``pcs.baidu.com`` 的接口节点。

    枚举值是相对路径，用 :meth:`url` 拼成完整地址。
    """

    Quota = "rest/2.0/pcs/quota"
    File = "rest/2.0/pcs/file"

    def url(self) -> str:
        """拼出该节点在 ``pcs.baidu.com`` 上的完整地址。

        :return: 完整的接口 URL
        """
        return f"{PCS_BAIDU_COM}/{self.value}"


class PanNode(Enum):
    """走 ``pan.baidu.com`` 的接口节点。

    枚举值是相对路径，用 :meth:`url` 拼成完整地址。
    """

    TransferShared = "share/transfer"
    Share = "share/set"
    SharedPathList = "share/list"
    SharedRecord = "share/record"
    SharedCancel = "share/cancel"
    SharedPassword = "share/surlinfoinrecord"
    Getcaptcha = "api/getcaptcha"
    Cloud = "rest/2.0/services/cloud_dl"
    UserProducts = "rest/2.0/membership/user"

    def url(self) -> str:
        """拼出该节点在 ``pan.baidu.com`` 上的完整地址。

        :return: 完整的接口 URL
        """
        return f"{PAN_BAIDU_COM}/{self.value}"


class BaiduPCS:
    """提供返回原始 JSON 数据的百度网盘 PCS 接口。"""

    def __init__(
        self,
        bduss: str | None = None,
        stoken: str | None = None,
        ptoken: str | None = None,
        cookies: dict[str, str | None] | None = None,
        user_id: int | None = None,
    ) -> None:
        """初始化百度网盘客户端。

        ``bduss``/``stoken``/``ptoken`` 三个凭据的取值优先级为：显式参数 >
        环境变量 ``BDUSS``/``STOKEN``/``PTOKEN`` > ``cookies`` 字典。

        :param bduss: 百度账号的 ``BDUSS`` 凭据，必需
        :param stoken: ``STOKEN`` 凭据，分享、离线任务等接口要求必须提供
        :param ptoken: ``PTOKEN`` 凭据
        :param cookies: 完整 cookies 字典，缺省时由上面三个凭据拼出
        :param user_id: 百度用户 ID；不传则调用 :meth:`user_info` 自动获取
        :raises BaiduPCSError: 最终没拿到 ``BDUSS`` 时抛出
        """
        cookies = cookies or {}
        bduss = bduss or os.getenv("BDUSS")
        stoken = stoken or os.getenv("STOKEN")
        ptoken = ptoken or os.getenv("PTOKEN")
        if not bduss and cookies and cookies.get("BDUSS", ""):
            bduss = cookies["BDUSS"]
        if not stoken and cookies and cookies.get("STOKEN", ""):
            stoken = cookies["STOKEN"]
        if not ptoken and cookies and cookies.get("PTOKEN", ""):
            ptoken = cookies["PTOKEN"]

        if not bduss:
            raise BaiduPCSError(
                "BaiduPCS.__init__: 缺少 BDUSS，请通过参数、cookies 或环境变量提供"
            )

        if not cookies:
            cookies = {"BDUSS": bduss, "STOKEN": stoken, "PTOKEN": ptoken}

        self._bduss = bduss
        self._stoken = stoken
        self._ptoken = ptoken
        self._bdstoken = ""
        self._logid = None
        self._baiduid = cookies.get("BAIDUID")
        if self._baiduid:
            self._logid = standard_b64encode(self._baiduid.encode("ascii")).decode(
                "utf-8"
            )

        self._cookies = cookies
        self._session = requests.Session()
        self._session.cookies.update(cookies)

        user_info = None
        if not user_id:
            user_info = self.user_info()
            user_id = user_info.get("user", {}).get("id")
            if not user_id:
                user_info = None
        self._user_id = user_id
        self._user_info = user_info

    @property
    def cookies(self) -> dict[str, str | None]:
        """当前会话持有的 cookies。

        .. warning:: 返回值里含有 ``BDUSS`` 等登录凭据，不要写入日志或异常信息。

        :return: cookie 名到取值的字典
        """
        return self._session.cookies.get_dict()

    @staticmethod
    def _app_id(url: str) -> str:
        """根据 URL 选择应用 ID。"""

        if PCS_BAIDU_COM in url:
            return PCS_APP_ID
        else:
            return PAN_APP_ID

    @property
    def bdstoken(self) -> str:
        """获取并缓存 ``bdstoken``（分享、离线任务等接口的防跨站令牌）。

        首次访问时抓取网盘首页 HTML 并正则提取，之后复用缓存值。

        :return: 32 位十六进制的 ``bdstoken``；页面上没解析到时返回空串
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN`` 时抛出
        """
        if not (self._stoken or self._cookies.get("STOKEN")):
            raise BaiduPCSError("BaiduPCS.bdstoken: cookies 中缺少 STOKEN")

        if self._bdstoken:
            return self._bdstoken

        url = "http://pan.baidu.com/disk/home"
        resp = self._request(Method.Get, url, params=None)
        cn = resp.text
        mod = re.search(r'bdstoken[\'":\s]+([0-9a-f]{32})', cn)
        if mod:
            s = mod.group(1)
            self._bdstoken = str(s)
            return s
        return ""

    @staticmethod
    def _headers(url: str) -> dict[str, str]:
        """根据 URL 选择请求头。"""

        if PCS_BAIDU_COM in url:
            return dict(PCS_HEADERS)
        else:
            return dict(PAN_HEADERS)

    def _cookies_update(self, cookies: dict[str, str]) -> None:
        """把服务端新下发的 cookies 合并进当前会话。"""
        self._session.cookies.update(cookies)

    def _request(
        self,
        method: Method,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        data: str | bytes | dict[str, str] | Any = None,
        files: dict[str, Any] | None = None,
        **kwargs,
    ) -> requests.Response:
        """发起一次 HTTP 请求，自动补 ``app_id``、默认请求头与分片上传的 Content-Type。

        :param method: HTTP 方法
        :param url: 完整请求地址
        :param params: URL 查询参数
        :param headers: 自定义请求头，缺省按域名选择 PCS/PAN 默认头
        :param data: 请求体
        :param files: 上传文件表单
        :param kwargs: 透传给 ``requests`` 的其余参数，可用 ``timeout`` 覆盖默认超时
        :return: ``requests`` 响应对象（不校验业务 errno）
        :raises BaiduPCSError: 底层网络请求抛异常时包装后抛出
        """
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)

        if params and isinstance(params, dict):
            app_id = self._app_id(url)
            params["app_id"] = app_id

        if not headers:
            headers = self._headers(url)

        if isinstance(data, (MultipartEncoder, MultipartEncoderMonitor)):
            assert headers
            headers["Content-Type"] = data.content_type

        try:
            resp = self._session.request(
                method.value,
                url,
                params=params,
                headers=headers,
                data=data,
                files=files,
                **kwargs,
            )
            return resp
        except Exception as err:
            raise BaiduPCSError("BaiduPCS._request", cause=err)

    def _request_get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        **kwargs,
    ) -> requests.Response:
        """以 GET 方式发起请求，是 :meth:`_request` 的简写。

        :param kwargs: 透传给 :meth:`_request`，可用 ``timeout``/``stream`` 等覆盖默认值
        """
        return self._request(Method.Get, url, params=params, headers=headers, **kwargs)

    @assert_ok
    def quota(self) -> dict[str, Any]:
        """返回网盘容量信息。"""

        url = PcsNode.Quota.url()
        params = {"method": "info"}
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def meta(self, *remotepaths: str) -> dict[str, Any]:
        """批量查询远程路径的元信息。

        :param remotepaths: 一个或多个远程**绝对**路径
        :return: 原始响应，``list`` 字段为各路径的元信息
        :raises BaiduPCSError: 路径为空或不是绝对路径时抛出
        """
        if not remotepaths or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.meta: remotepaths 必须是非空的绝对路径列表: {remotepaths!r}"
            )

        param = [{"path": p} for p in remotepaths]
        return self.file_operate("meta", param)

    def exists(self, remotepath: str) -> bool:
        """判断远程路径是否存在。

        :param remotepath: 远程绝对路径
        :return: 存在返回 ``True``，否则返回 ``False``
        """
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        else:
            return True

    def is_file(self, remotepath: str) -> bool:
        """判断远程路径是否是文件。

        :param remotepath: 远程绝对路径
        :return: 是文件返回 ``True``；是目录或路径不存在均返回 ``False``
        """
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        if r["list"][0]["isdir"] == 0:
            return True
        else:
            return False

    def is_dir(self, remotepath: str) -> bool:
        """判断远程路径是否是目录。

        :param remotepath: 远程绝对路径
        :return: 是目录返回 ``True``；是文件或路径不存在均返回 ``False``
        """
        r = self.meta(remotepath)
        if r.get("error_code"):
            return False
        if r["list"][0]["isdir"] == 1:
            return True
        else:
            return False

    @assert_ok
    def list(
        self,
        remotepath: str,
        desc: bool = False,
        name: bool = False,
        time: bool = False,
        size: bool = False,
    ) -> dict[str, Any]:
        """列出目录下的文件和子目录。

        ``name``/``time``/``size`` 三个排序开关互斥，按此优先级取第一个为 ``True``
        的；都不给时按名称排序。

        :param remotepath: 远程目录绝对路径
        :param desc: 是否降序
        :param name: 按名称排序
        :param time: 按服务器最后修改时间排序
        :param size: 按大小排序
        :return: 原始响应，``list`` 字段为条目列表
        """
        url = PcsNode.File.url()
        orderby = None
        if name:
            orderby = "name"
        elif time:
            orderby = "time"  # 服务器最后修改时间
        elif size:
            orderby = "size"
        else:
            orderby = "name"

        params = {
            "method": "list",
            "by": orderby,
            "limit": "0-2147483647",
            "order": ["asc", "desc"][desc],
            "path": str(remotepath),
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def upload_file(
        self,
        io: IO,
        remotepath: str,
        ondup: str = "overwrite",
        callback: Callable[[MultipartEncoderMonitor], None] | None = None,
    ) -> dict[str, Any]:
        """上传 IO 内容到远程路径（不支持设置本地创建和修改时间）。"""

        if not remotepath.startswith("/"):
            raise BaiduPCSError(
                f"BaiduPCS.upload_file: remotepath 必须是绝对路径: {remotepath!r}"
            )
        remotePath = Path(remotepath)

        url = PcsNode.File.url()
        params = {
            "method": "upload",
            "ondup": ondup,
            "dir": remotePath.parent.as_posix(),
            "filename": remotePath.name,
            "BDUSS": self._bduss,
        }

        m = MultipartEncoder(fields={"file": ("file", io, "")})
        monitor = MultipartEncoderMonitor(m, callback=callback)

        resp = self._request(Method.Post, url, params=params, data=monitor)
        return resp.json()

    @assert_ok
    def rapid_upload_file(
        self,
        slice_md5: str,
        content_md5: str,
        content_crc32: int,  # not needed
        io_len: int,
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup: str = "overwrite",
    ) -> dict[str, Any]:
        """通过文件摘要秒传（服务端已有相同内容时无需实际上传）。

        :param slice_md5: 内容前 256KB 的 md5（32 位十六进制）
        :param content_md5: 完整内容的 md5（32 位十六进制）
        :param content_crc32: 完整内容的 crc32；传 ``0`` 表示不带该参数
        :param io_len: 完整内容的字节长度
        :param remotepath: 保存到的远程**绝对**路径
        :param local_ctime: 本地创建时间戳，默认取当前时间
        :param local_mtime: 本地修改时间戳，默认取当前时间
        :param ondup: 同名处理策略，如 ``"overwrite"``
        :return: 原始响应
        :raises BaiduPCSError: ``remotepath`` 不是绝对路径，或服务端未命中秒传时抛出
        """

        if not remotepath.startswith("/"):
            raise BaiduPCSError(
                f"BaiduPCS.rapid_upload_file: remotepath 必须是绝对路径: {remotepath!r}"
            )

        url = PcsNode.File.url()
        params = {
            "method": "rapidupload",
            "BDUSS": self._bduss,
        }

        ntp = now_timestamp()

        data = {
            "path": remotepath,
            "content-length": io_len,
            "content-md5": content_md5,
            "slice-md5": slice_md5,
            "content-crc32": content_crc32,
            "local_ctime": str(local_ctime or ntp),
            "local_mtime": str(local_mtime or ntp),
            "ondup": ondup,
        }

        # Not needed
        if content_crc32 == 0:
            del data["content-crc32"]

        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def upload_slice(
        self, io: IO, callback: Callable[[MultipartEncoderMonitor], None] | None = None
    ) -> dict[str, Any]:
        """上传一个分片到服务端临时区，供 :meth:`combine_slices` 合并。

        :param io: 分片内容的可读流
        :param callback: 上传进度回调，收到 ``MultipartEncoderMonitor``
        :return: 原始响应，``md5`` 字段为该分片的标识
        """
        url = PcsNode.File.url()
        params = {
            "method": "upload",
            "type": "tmpfile",
            "BDUSS": self._bduss,
        }

        m = MultipartEncoder(fields={"file": ("file", io, "")})
        monitor = MultipartEncoderMonitor(m, callback=callback)

        resp = self._request(
            Method.Post,
            url,
            params=params,
            data=monitor,
            timeout=(3, 9),  # (connect timeout, read timeout)
        )
        return resp.json()

    @assert_ok
    def combine_slices(
        self,
        slice_md5s: list[str],
        remotepath: str,
        local_ctime: int | None = None,
        local_mtime: int | None = None,
        ondup: str = "overwrite",
    ) -> dict[str, Any]:
        """把 :meth:`upload_slice` 上传的分片合并成一个远程文件。

        :param slice_md5s: 各分片的 md5，**必须按分片顺序排列**
        :param remotepath: 保存到的远程绝对路径
        :param local_ctime: 本地创建时间戳，默认取当前时间
        :param local_mtime: 本地修改时间戳，默认取当前时间
        :param ondup: 同名处理策略，如 ``"overwrite"``
        :return: 原始响应
        """
        url = PcsNode.File.url()
        params = {
            "method": "createsuperfile",
            "path": remotepath,
            "ondup": ondup,
            "BDUSS": self._bduss,
        }

        ntp = now_timestamp()

        data = {
            "param": dump_json({"block_list": slice_md5s}),
            "local_ctime": str(local_ctime or ntp),
            "local_mtime": str(local_mtime or ntp),
        }
        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def search(
        self, keyword: str, remotepath: str, recursive: bool = False
    ) -> dict[str, Any]:
        """在指定目录下按关键字搜索文件。

        :param keyword: 搜索关键字
        :param remotepath: 搜索起始目录的绝对路径
        :param recursive: 是否递归搜索子目录
        :return: 原始响应，``list`` 字段为命中条目
        """
        url = PcsNode.File.url()
        params = {
            "method": "search",
            "path": remotepath,
            "wd": keyword,
            "re": "1" if recursive else "0",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def makedir(self, directory: str) -> dict[str, Any]:
        """创建远程目录（父目录不存在时一并创建）。

        :param directory: 远程目录绝对路径
        :return: 原始响应，含新目录的 ``fs_id``、``path`` 等
        """
        url = PcsNode.File.url()
        params = {
            "method": "mkdir",
            "path": directory,
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def file_operate(self, operate: str, param: list[dict[str, str]]) -> dict[str, Any]:
        """批量文件操作的统一入口。

        :param operate: 操作名，取值为 ``"meta"``/``"move"``/``"copy"``/``"delete"``
        :param param: 操作条目列表；``move``/``copy`` 用 :func:`_from_to` 构造，
            ``meta``/``delete`` 用 ``{"path": ...}``
        :return: 原始响应
        """
        url = PcsNode.File.url()
        params = {"method": operate}
        data = {"param": dump_json({"list": param})}
        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def move(self, *remotepaths: str) -> dict[str, Any]:
        """把若干源路径移动到目标目录下。

        参数按 ``sources, dest = remotepaths[:-1], remotepaths[-1]`` 拆分，
        即最后一个是目标**目录**，前面的都是源路径。目标目录不存在时自动创建。

        :param remotepaths: 至少两个远程绝对路径，最后一个为目标目录
        :return: 原始响应
        :raises BaiduPCSError: 路径不足两个、不是绝对路径，或目标是文件时抛出
        """

        if len(remotepaths) <= 1 or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.move: 源路径和目标路径必须是绝对路径: {remotepaths!r}"
            )

        sources, dest = remotepaths[:-1], remotepaths[-1]

        if self.is_file(dest):
            raise BaiduPCSError("The remote `dest` is a file. It must be a directory.")

        if not self.is_dir(dest):
            self.makedir(dest)

        _sources = (Path(s) for s in sources)
        _dest = Path(dest)

        param = [_from_to(s.as_posix(), (_dest / s.name).as_posix()) for s in _sources]
        return self.file_operate("move", param)

    @assert_ok
    def rename(self, source: str, dest: str) -> dict[str, Any]:
        """重命名远程路径。"""

        if not all(p.startswith("/") for p in (source, dest)):
            raise BaiduPCSError(
                f"BaiduPCS.rename: source 和 dest 必须是绝对路径: {source!r}, {dest!r}"
            )

        param = [_from_to(source, dest)]
        return self.file_operate("move", param)

    @assert_ok
    def copy(self, *remotepaths: str) -> dict[str, Any]:
        """把若干源路径复制到目标目录下。

        参数按 ``sources, dest = remotepaths[:-1], remotepaths[-1]`` 拆分，
        即最后一个是目标**目录**，前面的都是源路径。目标目录不存在时自动创建。

        :param remotepaths: 至少两个远程绝对路径，最后一个为目标目录
        :return: 原始响应
        :raises BaiduPCSError: 路径不足两个、不是绝对路径，或目标是文件时抛出
        """

        if len(remotepaths) <= 1 or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.copy: 源路径和目标路径必须是绝对路径: {remotepaths!r}"
            )

        sources, dest = remotepaths[:-1], remotepaths[-1]

        if self.is_file(dest):
            raise BaiduPCSError("The remote `dest` is a file. It must be a directory.")

        if not self.is_dir(dest):
            self.makedir(dest)

        _sources = (Path(s) for s in sources)
        _dest = Path(dest)

        param = [_from_to(s.as_posix(), (_dest / s.name).as_posix()) for s in _sources]
        return self.file_operate("copy", param)

    @assert_ok
    def remove(self, *remotepaths: str) -> dict[str, Any]:
        """删除若干远程路径（文件或目录）。

        :param remotepaths: 一个或多个远程绝对路径
        :return: 原始响应
        :raises BaiduPCSError: 路径为空或不是绝对路径时抛出
        """
        if not remotepaths or not all(p.startswith("/") for p in remotepaths):
            raise BaiduPCSError(
                f"BaiduPCS.remove: remotepaths 必须是非空的绝对路径列表: {remotepaths!r}"
            )

        param = [{"path": p} for p in remotepaths]
        return self.file_operate("delete", param)

    @assert_ok
    def cloud_operate(
        self, params: dict[str, str], data: dict[str, str] | None = None
    ) -> dict[str, Any]:
        """离线下载（云任务）相关接口的统一入口。

        :param params: URL 查询参数，``method`` 指定具体操作
        :param data: 表单数据；给了就走 POST，否则走 GET
        :return: 原始响应
        """
        url = PanNode.Cloud.url()
        if data:
            resp = self._request(Method.Post, url, params=params, data=data)
        else:
            resp = self._request(Method.Get, url, params=params)
        return resp.json()

    def magnet_info(self, magnet: str) -> dict[str, Any]:
        """查询磁力链接内包含的文件列表。

        :param magnet: 磁力链接
        :return: 原始响应，``magnet_info`` 字段为文件列表；其下标（从 1 开始）
            可作为 :meth:`add_magnet_task` 的 ``selected_idx``
        """
        params = {
            "method": "query_magnetinfo",
            "source_url": magnet,
            # 'save_path': '/',
            "type": "4",
        }
        return self.cloud_operate(params)

    def torrent_info(self, remote_torrent: str) -> dict[str, Any]:
        """查询网盘里某个种子文件包含的文件列表。

        :param remote_torrent: 种子文件在网盘中的绝对路径
        :return: 原始响应，含种子内的文件列表
        """
        params = {
            "method": "query_sinfo",
            "source_path": remote_torrent,
            # 'save_path': '/',
            "type": "2",
            # 't': int(time.time() * 1000),
        }
        return self.cloud_operate(params)

    def add_task(self, task_url: str, remotedir: str) -> dict[str, Any]:
        """为 http/https/ed2k 链接创建离线下载任务。

        :param task_url: 待离线下载的链接
        :param remotedir: 保存到的网盘目录绝对路径
        :return: 原始响应，含新任务的 ``task_id``
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN`` 时抛出
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.add_task: cookies 中缺少 STOKEN")

        params = {
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": self.bdstoken,  # Must be set
            # "logid": self._logid,
            "clienttype": "0",
        }
        data = {
            "method": "add_task",
            "app_id": "250528",
            "save_path": remotedir,
            "source_url": task_url,
            "timeout": "2147483647",
        }
        return self.cloud_operate(params, data=data)

    def add_magnet_task(
        self, task_url: str, remotedir: str, selected_idx: list[int]
    ) -> dict[str, Any]:
        """为磁力链接创建离线下载任务。

        :param task_url: 磁力链接
        :param remotedir: 保存到的网盘目录绝对路径
        :param selected_idx: 要下载的文件在 :meth:`magnet_info` 返回列表中的下标，
            **从 1 开始计数**
        :return: 原始响应，含新任务的 ``task_id``
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN`` 时抛出
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.add_magnet_task: cookies 中缺少 STOKEN")

        params = {
            "channel": "chunlei",
            "web": "1",
            "app_id": "250528",
            "bdstoken": self.bdstoken,  # Must be set
            # "logid": self._logid,
            "clienttype": "0",
        }
        data = {
            "method": "add_task",
            "app_id": "250528",
            "save_path": remotedir,
            "source_url": task_url,
            "timeout": "2147483647",
            "type": "4",
            "t": str(int(time.time() * 1000)),
            "file_sha1": "",
            "selected_idx": ",".join([str(i) for i in selected_idx or []]),
            "task_from": "1",
        }
        return self.cloud_operate(params, data=data)

    def tasks(self, *task_ids: str) -> dict[str, Any]:
        """按任务 ID 批量查询离线下载任务的状态。

        :param task_ids: 一个或多个任务 ID
        :return: 原始响应，``task_info`` 字段按任务 ID 索引
        """
        params = {
            "method": "query_task",
            "task_ids": ",".join(task_ids),
        }
        return self.cloud_operate(params)

    def list_tasks(self) -> dict[str, Any]:
        """列出全部离线下载任务（最多 1000 条）。

        :return: 原始响应，``task_info`` 字段为任务列表
        """
        params = {
            "method": "list_task",
            "need_task_info": "1",
            "status": "255",
            "start": "0",
            "limit": "1000",
        }
        return self.cloud_operate(params)

    def clear_tasks(self) -> dict[str, Any]:
        """清空已完成/失败的离线下载任务记录。

        :return: 原始响应，``total`` 字段为清理掉的任务数
        """
        params = {
            "method": "clear_task",
        }
        return self.cloud_operate(params)

    def cancel_task(self, task_id: str) -> dict[str, Any]:
        """取消一个进行中的离线下载任务。

        :param task_id: 任务 ID
        :return: 原始响应
        """
        params = {
            "method": "cancel_task",
            "task_id": task_id,
        }
        return self.cloud_operate(params)

    @assert_ok
    def share(
        self, *remotepaths: str, password: str, period: int = 0
    ) -> dict[str, Any]:
        """把若干远程路径创建成分享链接。

        :param remotepaths: 要分享的远程绝对路径
        :param password: 4 个字符的提取码
        :param period: 有效期天数，``0`` 表示永久有效
        :return: 原始响应，``link``/``shareid`` 字段为分享链接与分享 ID
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN``，或提取码长度不是 4 时抛出
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.share: cookies 中缺少 STOKEN")
        if len(password) != 4:
            raise BaiduPCSError(
                f"BaiduPCS.share: password 必须为 4 个字符，实际长度为 {len(password)}"
            )

        meta = self.meta(*remotepaths)
        fs_ids = [i["fs_id"] for i in meta["list"]]

        url = PanNode.Share.url()
        params = {
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
            "bdstoken": self.bdstoken,
        }
        data = {
            "fid_list": dump_json(fs_ids),
            "schannel": "0",
            "channel_list": "[]",
            "period": str(int(period)),
        }
        if password:
            data["pwd"] = password
            data["schannel"] = "4"

        resp = self._request(Method.Post, url, params=params, data=data)
        return resp.json()

    @assert_ok
    def list_shared(self, page: int = 1) -> dict[str, Any]:
        """分页列出当前账号创建过的分享记录。

        :param page: 页码，从 1 开始，按时间倒序
        :return: 原始响应，``list`` 为分享记录；其中 ``channel`` 为 ``0`` 表示
            无提取码，``4`` 表示有提取码
        """

        url = PanNode.SharedRecord.url()
        params = {
            "page": str(page),
            "desc": "1",
            "order": "time",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def shared_password(self, share_id: int) -> dict[str, Any]:
        """查询某条分享记录的提取码。

        :param share_id: 分享 ID，来自 :meth:`list_shared`
        :return: 原始响应，仅 ``pwd`` 字段有效
        """

        url = PanNode.SharedPassword.url()
        params = {
            "shareid": str(share_id),
            "sign": calu_md5(f"{share_id}_sharesurlinfo!@#"),
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def cancel_shared(self, *share_ids: int) -> dict[str, Any]:
        """取消若干条分享。

        :param share_ids: 分享 ID，来自 :meth:`list_shared`
        :return: 原始响应
        """
        url = PanNode.SharedCancel.url()
        data = {
            "shareid_list": dump_json(share_ids),
        }
        hdrs = dict(PCS_HEADERS)
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        resp = self._request(Method.Post, url, headers=hdrs, params=None, data=data)
        return resp.json()

    def shared_init_url(self, shared_url: str) -> str:
        """生成分享验证页地址。

        :param shared_url: 百度网盘分享链接
        :return: 对应的分享验证页地址
        """
        u = urlparse(shared_url)
        surl = u.path.split("/s/1")[-1]
        return f"https://pan.baidu.com/share/init?surl={surl}"

    @assert_ok
    def access_shared(
        self, shared_url: str, password: str, vcode_str: str, vcode: str
    ) -> dict[str, Any]:
        """用提取码验证分享链接，把通行 cookies 写回当前会话。

        必须先调用本方法，:meth:`shared_paths` 才能拿到分享内容。

        .. warning:: 本方法会修改会话级 cookies，**不是线程安全的**。

        :param shared_url: 分享链接
        :param password: 提取码
        :param vcode_str: 图形验证码标识，来自 :meth:`getcaptcha`；无需验证码时传空串
        :param vcode: 图形验证码内容；无需验证码时传空串
        :return: 原始响应
        """

        url = "https://pan.baidu.com/share/verify"
        init_url = self.shared_init_url(shared_url)
        params = {
            "surl": init_url.split("surl=")[-1],
            "t": str(now_timestamp() * 1000),
            "channel": "chunlei",
            "web": "1",
            "bdstoken": "null",
            "clienttype": "0",
        }
        data = {
            "pwd": password,
            "vcode": vcode,
            "vcode_str": vcode_str,
        }
        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = init_url
        resp = self._request(Method.Post, url, headers=hdrs, params=params, data=data)

        # These cookies must be included through all sub-processes
        self._cookies_update(resp.cookies.get_dict())

        return resp.json()

    @assert_ok
    def getcaptcha(self, shared_url: str) -> dict[str, Any]:
        """获取分享访问验证码信息。

        :param shared_url: 百度网盘分享链接
        :return: 服务端返回的验证码信息
        """
        url = PanNode.Getcaptcha.url()
        params = {
            "prod": "shareverify",
            "channel": "chunlei",
            "web": "1",
            "bdstoken": "null",
            "clienttype": "0",
        }

        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = self.shared_init_url(shared_url)
        hdrs["X-Requested-With"] = "XMLHttpRequest"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        resp = self._request(Method.Get, url, headers=hdrs, params=params)
        return resp.json()

    def get_vcode_img(self, vcode_img_url: str, shared_url: str) -> bytes:
        """下载分享访问验证码图片。

        :param vcode_img_url: 验证码图片地址
        :param shared_url: 百度网盘分享链接，用于设置请求来源
        :return: 验证码图片字节
        """
        hdrs = dict(PAN_HEADERS)
        hdrs["Referer"] = self.shared_init_url(shared_url)
        resp = self._request_get(vcode_img_url, headers=hdrs)
        return resp.content

    @assert_ok
    def shared_paths(self, shared_url: str) -> dict[str, Any]:
        """抓取分享页面并解析出其中的文件列表。

        调用前必须先用 :meth:`access_shared` 通过提取码验证。

        .. warning:: 本方法会修改会话级 cookies，**不是线程安全的**。

        :param shared_url: 分享链接
        :return: 从页面内嵌 JSON 解析出的数据，含 ``file_list``、``uk``、``shareid`` 等
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN``，或页面上解析不出数据时抛出
        """

        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.shared_paths: cookies 中缺少 STOKEN")

        resp = self._request(Method.Get, shared_url, params=None)
        html = resp.text

        # These cookies must be included through all sub-processes
        self._cookies_update(resp.cookies.get_dict())

        m = re.search(r"(?:yunData.setData|locals.mset)\((.+?)\);", html)
        if not m:
            raise BaiduPCSError(
                f"BaiduPCS.shared_paths: 无法从分享页面解析数据: {shared_url!r}"
            )

        shared_data = m.group(1)
        return json.loads(shared_data)

    @assert_ok
    def list_shared_paths(
        self, sharedpath: str, uk: int, share_id: int, page: int = 1, size: int = 100
    ) -> dict[str, Any]:
        """列出分享链接里某个**子目录**下的内容。

        :param sharedpath: 分享内的目录路径
        :param uk: 分享者的用户 ID，来自 :meth:`shared_paths`
        :param share_id: 分享 ID，来自 :meth:`shared_paths`
        :param page: 页码，从 1 开始
        :param size: 每页条数，上限 100
        :return: 原始响应，``list`` 为条目列表
        :raises BaiduPCSError: cookies 中缺少 ``STOKEN`` 时抛出
        """
        if not self._stoken:
            raise BaiduPCSError("BaiduPCS.list_shared_paths: cookies 中缺少 STOKEN")

        url = PanNode.SharedPathList.url()
        params = {
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
            "page": str(page),  # from 1
            "num": str(size),  # max is 100
            "dir": sharedpath,
            "t": str(random.random()),
            "uk": str(uk),
            "shareid": str(share_id),
            "desc": "1",  # reversely
            "order": "other",  # sort by name, or size, time
            "bdstoken": "null",
            "showempty": "0",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @assert_ok
    def transfer_shared_paths(
        self,
        remotedir: str,
        fs_ids: list[int],
        uk: int,
        share_id: int,
        bdstoken: str,
        shared_url: str,
    ) -> dict[str, Any]:
        """把分享链接里的若干文件转存到自己的网盘。

        :param remotedir: 转存到的网盘目录绝对路径，**必须已存在**
        :param fs_ids: 要转存条目的 ``fs_id``，来自 :meth:`shared_paths`
        :param uk: 分享者的用户 ID
        :param share_id: 分享 ID
        :param bdstoken: 当前账号的 ``bdstoken``，见 :attr:`bdstoken`
        :param shared_url: 分享链接，会作为 ``Referer`` 发送，**必须传对**
        :return: 原始响应；服务端把真实错误放在 ``info[0].errno`` 时会提到顶层 ``errno``
        """

        url = PanNode.TransferShared.url()
        params = {
            "shareid": str(share_id),
            "from": str(uk),
            "bdstoken": bdstoken,
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
        }
        data = {
            "fsidlist": dump_json(fs_ids),
            "path": remotedir,
        }
        hdrs = dict(PAN_HEADERS)
        hdrs["X-Requested-With"] = "XMLHttpRequest"
        hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        hdrs["Origin"] = "https://pan.baidu.com"
        hdrs["Referer"] = shared_url  # WARNING: Referer must be set to shared_url

        resp = self._request(Method.Post, url, headers=hdrs, params=params, data=data)
        info = resp.json()
        if info.get("info") and info["info"][0]["errno"]:
            info["errno"] = info["info"][0]["errno"]
        return info

    @assert_ok
    def user_info(self) -> dict[str, Any]:
        """通过贴吧移动端登录接口换取账号信息。

        :meth:`__init__` 在未显式传 ``user_id`` 时会调用本方法。

        :return: 原始响应，``user.id`` 为百度用户 ID
        """
        bduss = self._bduss
        timestamp = str(now_timestamp())
        model = get_phone_model(bduss)
        phoneIMEIStr = sum_imei(bduss)

        data = {
            "bdusstoken": bduss + "|null",
            "channel_id": "",
            "channel_uid": "",
            "stErrorNums": "0",
            "subapp_type": "mini",
            "timestamp": timestamp + "922",
        }
        data["_client_type"] = "2"
        data["_client_version"] = "7.0.0.0"
        data["_phone_imei"] = phoneIMEIStr
        data["from"] = "mini_ad_wandoujia"
        data["model"] = model
        data["cuid"] = (
            calu_md5(
                bduss
                + "_"
                + data["_client_version"]
                + "_"
                + data["_phone_imei"]
                + "_"
                + data["from"]
            ).upper()
            + "|"
            + phoneIMEIStr[::-1]
        )
        data["sign"] = calu_md5(
            "".join([k + "=" + data[k] for k in sorted(data.keys())]) + "tiebaclient!!!"
        ).upper()

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "ka=open",
            "net": "1",
            "User-Agent": "bdtb for Android 6.9.2.1",
            "client_logid": timestamp + "416",
            "Connection": "Keep-Alive",
        }

        resp = requests.post(
            "http://tieba.baidu.com/c/s/login",
            headers=headers,
            data=data,
            timeout=DEFAULT_TIMEOUT,
        )
        return resp.json()

    @assert_ok
    def tieba_user_info(self, user_id: int) -> dict[str, Any]:
        """查询指定百度用户的贴吧资料。

        :param user_id: 百度用户 ID
        :return: 原始响应，含昵称、头像等公开资料
        """
        params = f"has_plist=0&need_post_count=1&rn=1&uid={user_id}"
        params += "&sign=" + calu_md5(params.replace("&", "") + "tiebaclient!!!")
        url = "http://c.tieba.baidu.com/c/u/user/profile?" + params
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": "ka=open",
            "net": "1",
            "User-Agent": "bdtb for Android 6.9.2.1",
            "client_logid": str(now_timestamp() * 1000),
            "Connection": "Keep-Alive",
        }
        resp = requests.get(url, headers=headers, params=None, timeout=DEFAULT_TIMEOUT)
        return resp.json()

    @assert_ok
    def user_products(self) -> dict[str, Any]:
        """查询当前账号已开通的网盘增值服务（会员等）。

        :return: 原始响应，``product_infos`` 为服务列表
        """
        url = PanNode.UserProducts.url()
        params = {
            "method": "query",
        }
        resp = self._request(Method.Get, url, params=params)
        return resp.json()

    @ttl_cache(ttl=1 * 60 * 60)
    def download_link(self, remotepath: str, pcs: bool = False) -> str | None:
        """获取文件的直链下载地址，结果缓存 1 小时。

        非 ``pcs`` 模式下会带签名向 ``locatedownload`` 接口要地址；接口因调用过频
        返回非 200 时会等待 :data:`DOWNLOAD_LINK_RETRY_INTERVAL` 秒后重试，最多重试
        :data:`DOWNLOAD_LINK_MAX_RETRIES` 次。

        .. warning:: 返回的直链带有签名，等同于临时凭据，不要写入日志。

        :param remotepath: 远程文件绝对路径
        :param pcs: 是否直接拼 ``pcs.baidu.com`` 的下载地址（不走签名接口）
        :return: 直链地址；文件被和谐、没有可用地址、拿到 403 或重试耗尽时返回 ``None``
        """
        if pcs:
            return (
                "http://c.pcs.baidu.com/rest/2.0/pcs/file"
                f"?method=download&app_id={PCS_APP_ID}&path={quote_plus(remotepath)}"
                "&ver=2.0&clienttype=1"
            )

        bduss = self._bduss
        uid = str(self._user_id or "")
        devuid = calu_md5(bduss).upper() + "|0"
        enc = calu_sha1(bduss)

        for attempt in range(DOWNLOAD_LINK_MAX_RETRIES):
            timestamp = str(now_timestamp())

            rand = calu_sha1(
                enc + uid + "ebrcUYiuxaZv2XGu7KIYKxUrqfnOfpDF" + timestamp + devuid
            )

            url = PcsNode.File.url()
            params = {
                "apn_id": "1_0",
                "app_id": PAN_APP_ID,
                "channel": "0",
                "check_blue": "1",
                "clienttype": "17",
                "es": "1",
                "esl": "1",
                "freeisp": "0",
                "method": "locatedownload",
                "path": quote_plus(remotepath),
                "queryfree": "0",
                "use": "0",
                "ver": "4.0",
                "time": timestamp,
                "rand": rand,
                "devuid": devuid,
                "cuid": devuid,
            }

            params_str = "&".join([f"{k}={v}" for k, v in params.items()])

            headers = dict(PCS_HEADERS)
            headers["Cookie"] = "; ".join([f"{k}={v}" for k, v in self.cookies.items()])
            req = urllib.request.Request(
                url + "?" + params_str, headers=headers, method="GET"
            )  # type: ignore
            try:
                resp = urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT[1])  # type: ignore

                # Error: "user is not authorized"
                # 调用过于频繁时会走到这里；重试有上限，耗尽后返回 None，
                # 不能像原实现那样无限 `continue` 下去。
                if resp.status != 200:
                    if attempt == DOWNLOAD_LINK_MAX_RETRIES - 1:
                        logger.warning(
                            "`BaiduPCS.download_link` 重试 {} 次后仍未拿到下载地址，"
                            "最后一次状态码 {}",
                            DOWNLOAD_LINK_MAX_RETRIES,
                            resp.status,
                        )
                        return None
                    time.sleep(DOWNLOAD_LINK_RETRY_INTERVAL)
                    continue

                info = json.loads(resp.read())

                # This error is gotten when remote path is blocked
                if info.get("host") == "issuecdn.baidupcs.com":
                    return None

                if not info.get("urls"):
                    return None
                else:
                    # return info["urls"][0]["url"].replace("&htype=", "")
                    return info["urls"][0]["url"]
            except HTTPError:
                # 403 code could occor at unavailable downloading url, #97
                return None

        # 理论上走不到（上面最后一次重试会显式 return），兜底保证函数总有返回值
        return None

    def file_stream(
        self,
        remotepath: str,
        max_chunk_size: int = MAX_CHUNK_SIZE,
        callback: Callable[..., None] | None = None,
        encrypt_password: bytes = b"",
        pcs: bool = False,
    ) -> RangeRequestIO | None:
        """把远程文件包装成支持 Range 请求的可读流。

        :param remotepath: 远程文件绝对路径
        :param max_chunk_size: 单次 Range 请求的最大字节数
        :param callback: 读取进度回调
        :param encrypt_password: 加密文件的解密口令，空串表示不解密
        :param pcs: 是否直接用 ``pcs.baidu.com`` 的下载地址
        :return: 可读流；拿不到下载地址时返回 ``None``
        """
        url = self.download_link(remotepath, pcs=pcs)
        if not url:
            return None

        headers = {
            "Cookie": f"BDUSS={self._bduss};",
            "User-Agent": PCS_UA,
            "Connection": "Keep-Alive",
        }
        return RangeRequestIO(
            Method.Get.value,
            url,
            headers=headers,
            max_chunk_size=max_chunk_size,
            callback=callback,
            encrypt_password=encrypt_password,
        )

    # Playing the m3u8 file is needed add `--stream-lavf-o-append="protocol_whitelist=file,http,https,tcp,tls,crypto,hls,applehttp"` for mpv
    # https://github.com/mpv-player/mpv/issues/6928#issuecomment-532198445
    @assert_ok
    def m3u8_stream(
        self, remotepath: str, type: M3u8Type = "M3U8_AUTO_720"
    ) -> dict[str, Any]:
        """获取视频文件的 m3u8 转码播放列表。

        :param remotepath: 远程视频文件绝对路径
        :param type: 转码清晰度，``"M3U8_AUTO_720"`` 或 ``"M3U8_AUTO_480"``
        :return: 成功时为 ``{"m3u8_content": <m3u8 文本>}``；服务端返回错误
            JSON 时原样返回该 JSON
        """

        url = PcsNode.File.url()
        params = {
            "method": "streaming",
            "path": remotepath,
            "type": type,
        }

        resp = self._request(Method.Get, url, params=params)
        cn = resp.text
        if cn.startswith("{"):  # error
            return resp.json()
        else:
            return {"m3u8_content": cn}
