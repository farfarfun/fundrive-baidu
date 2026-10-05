"""百度网盘接口返回值的内部数据模型。

本模块里的类都是 :class:`typing.NamedTuple`，由 :mod:`fundrives.baidu.api`
把 :mod:`fundrives.baidu.pcs` 返回的原始 JSON 转换过来，字段只读。
"""

import os
from base64 import standard_b64encode
from collections import namedtuple
from typing import NamedTuple
from urllib.parse import unquote


class PcsRapidUploadInfo(NamedTuple):
    """秒传所需的文件摘要信息。

    :ivar slice_md5: 内容前 256KB 的 md5
    :ivar content_md5: 完整内容的 md5
    :ivar content_crc32: 完整内容的 crc32
    :ivar content_length: 完整内容的字节长度
    :ivar remotepath: 文件在网盘中的绝对路径，可为空
    """

    slice_md5: str
    content_md5: str
    content_crc32: int
    content_length: int
    remotepath: str | None = None

    def _filename(self) -> str:
        """取出 ``remotepath`` 的文件名，并把空格转义成 ``%20``。"""
        filename = os.path.basename(self.remotepath or "")
        if not filename:
            return ""
        else:
            return filename.replace(" ", "%20")

    def cs3l(self) -> str:
        """生成 ``cs3l`` 协议的秒传链接。

        格式为
        ``cs3l://<content_md5>#<slice_md5>#<content_crc32>#<content_length>#<filename>``。

        :return: 秒传链接
        """

        filename = self._filename()
        content_crc32 = self.content_crc32 or "0"
        return f"cs3l://{self.content_md5}#{self.slice_md5}#{content_crc32}#{self.content_length}#{filename}"

    def short(self) -> str:
        """生成短格式的秒传链接。

        格式为 ``<content_md5>#<slice_md5>#<content_length>#<filename>``。

        :return: 秒传链接
        """

        filename = self._filename()
        return f"{self.content_md5}#{self.slice_md5}#{self.content_length}#{filename}"

    def bdpan(self) -> str:
        """生成 ``bdpan`` 协议的秒传链接。

        格式为 ``bdpan://`` 加上
        ``base64(<filename>|<content_length>|<content_md5>|<slice_md5>)``。

        :return: 秒传链接
        """

        filename = self._filename()
        return "bdpan://" + standard_b64encode(
            f"{filename}|{self.content_length}|{self.content_md5}|{self.slice_md5}".encode()
        ).decode("utf-8")

    def all_links(self) -> list[str]:
        """返回全部三种格式的秒传链接。

        :return: 依次为 ``cs3l``、``short``、``bdpan`` 三种链接
        """
        return [self.cs3l(), self.short(), self.bdpan()]

    @staticmethod
    def hash_link_protocols() -> list[str]:
        """返回支持的秒传链接协议名。

        :return: 协议名列表，与 :meth:`all_links` 的顺序一致
        """
        return ["cs3l", "short", "bdpan"]

    @staticmethod
    def default_hash_link_protocol() -> str:
        """返回默认使用的秒传链接协议名。

        :return: 固定为 ``"cs3l"``
        """
        return "cs3l"


class PcsFile(NamedTuple):
    """百度网盘中的一个文件或目录。

    :ivar path: 远程绝对路径
    :ivar is_dir: 是否是目录
    :ivar is_file: 是否是文件
    :ivar fs_id: 文件 ID
    :ivar size: 字节大小
    :ivar md5: 文件内容的 md5
    :ivar block_list: 各分块的 md5 列表
    :ivar category: 百度定义的文件分类编号
    :ivar user_id: 所属用户 ID
    :ivar ctime: 服务端创建时间戳
    :ivar mtime: 服务端修改时间戳
    :ivar local_ctime: 上传时记录的本地创建时间戳
    :ivar local_mtime: 上传时记录的本地修改时间戳
    :ivar server_ctime: 服务端创建时间戳
    :ivar server_mtime: 服务端修改时间戳
    :ivar shared: 是否已被分享
    :ivar rapid_upload_info: 秒传摘要信息，需额外请求才会填充
    :ivar dl_link: 直链下载地址，需额外请求才会填充
    """

    path: str  # remote absolute path
    is_dir: bool | None = None
    is_file: bool | None = None
    fs_id: int | None = None  # file id
    size: int | None = None
    md5: str | None = None
    block_list: list[str] | None = None  # block md5 list
    category: int | None = None
    user_id: int | None = None
    ctime: int | None = None  # server created time
    mtime: int | None = None  # server modifed time
    local_ctime: int | None = None  # local created time
    local_mtime: int | None = None  # local modifed time
    server_ctime: int | None = None  # server created time
    server_mtime: int | None = None  # server modifed time
    shared: bool | None = None  # this file is shared if True

    rapid_upload_info: PcsRapidUploadInfo | None = None
    dl_link: str | None = None

    @staticmethod
    def from_(info) -> "PcsFile":
        """从接口返回的原始条目构造 :class:`PcsFile`。

        :param info: 接口返回的单条文件信息字典
        :return: 对应的 :class:`PcsFile`
        """
        return PcsFile(
            path=info.get("path"),
            is_dir=info.get("isdir") == 1,
            is_file=info.get("isdir") == 0,
            fs_id=info.get("fs_id"),
            size=info.get("size"),
            md5=info.get("md5"),
            block_list=info.get("block_list"),
            category=info.get("category"),
            user_id=info.get("user_id"),
            ctime=info.get("ctime"),
            mtime=info.get("mtime"),
            local_ctime=info.get("local_ctime"),
            local_mtime=info.get("local_mtime"),
            server_ctime=info.get("server_ctime"),
            server_mtime=info.get("server_mtime"),
            shared=info.get("shared"),
        )


class PcsMagnetFile(NamedTuple):
    """磁力链接中的一个文件。

    :ivar path: 文件在磁力链接内的路径
    :ivar size: 字节大小
    """

    path: str  # the file path in the magnet link
    size: int | None = None

    @staticmethod
    def from_(info) -> "PcsMagnetFile":
        """从 ``magnet_info`` 接口返回的原始条目构造 :class:`PcsMagnetFile`。

        :param info: 接口返回的单条磁力文件信息字典
        :return: 对应的 :class:`PcsMagnetFile`
        """
        return PcsMagnetFile(path=info.get("file_name"), size=info.get("size"))


class PcsSharedLink(NamedTuple):
    """当前账号创建的一条分享记录。

    :ivar url: 分享链接
    :ivar paths: 被分享的远程路径
    :ivar fs_ids: 被分享条目的文件 ID
    :ivar password: 提取码，接口未返回时为 ``None``
    :ivar expired: 距离过期还剩多少秒；``-1`` 表示已过期
    :ivar channel: 分享渠道，``4`` 表示带提取码
    :ivar share_id: 分享 ID
    :ivar ctime: 创建时间戳
    """

    url: str
    paths: list[str] | None = None
    fs_ids: list[int] | None = None
    password: str | None = None

    # The remained second before expiring.
    # -1 means being expired
    expired: int | None = None

    # channel == 4, has password
    channel: bool | None = None

    share_id: int | None = None
    ctime: int | None = None

    @staticmethod
    def from_(info) -> "PcsSharedLink":
        """从分享记录接口返回的原始条目构造 :class:`PcsSharedLink`。

        :param info: 接口返回的单条分享记录字典
        :return: 对应的 :class:`PcsSharedLink`
        """
        return PcsSharedLink(
            url=info.get("link") or info.get("shortlink"),
            paths=info.get("paths") or [info.get("typicalPath")],
            fs_ids=info.get("fsIds"),
            password=info.get("password"),
            expired=info.get("expiredTime") if info.get("expiredType") != -1 else -1,
            channel=info.get("channel"),
            share_id=info.get("share_id") or info.get("shareId") or info.get("shareid"),
            ctime=info.get("ctime"),
        )

    def has_password(self) -> bool:
        """判断这条分享是否需要提取码。

        :return: 已拿到提取码或 ``channel == 4`` 时为 ``True``
        """
        if self.password:
            return True
        if self.channel == 4:
            return True
        return False

    def available(self) -> bool:
        """判断这条分享是否仍然有效。

        分享失效后接口不再返回真实路径，以此判断。

        :return: 仍有可用的绝对路径时为 ``True``
        """
        if not self.paths:
            return False
        if self.paths[0].startswith("/"):
            return True
        else:
            return False


class PcsSharedPath(NamedTuple):
    """他人分享链接里的一个条目。

    :ivar fs_id: 条目的文件 ID，转存时要用
    :ivar path: 条目在分享内的路径
    :ivar size: 字节大小
    :ivar is_dir: 是否是目录
    :ivar is_file: 是否是文件
    :ivar md5: 文件内容的 md5
    :ivar local_ctime: 上传时记录的本地创建时间戳
    :ivar local_mtime: 上传时记录的本地修改时间戳
    :ivar server_ctime: 服务端创建时间戳
    :ivar server_mtime: 服务端修改时间戳
    :ivar uk: 分享者的用户 ID
    :ivar share_id: 分享 ID
    :ivar bdstoken: 转存时需要的 ``bdstoken``
    """

    fs_id: int
    path: str
    size: int
    is_dir: bool
    is_file: bool
    md5: str | None = None
    local_ctime: int | None = None  # local created time
    local_mtime: int | None = None  # local modifed time
    server_ctime: int | None = None  # server created time
    server_mtime: int | None = None  # server modifed time

    uk: int | None = None
    share_id: int | None = None
    bdstoken: str | None = None

    @staticmethod
    def from_(info) -> "PcsSharedPath":
        """从分享内容接口返回的原始条目构造 :class:`PcsSharedPath`。

        分享首页与子目录列表两个接口的字段不一样：前者直接给 ``path``，后者给
        ``parent_path`` + ``server_filename``，这里统一拼成 ``path``。

        :param info: 接口返回的单条分享条目字典
        :return: 对应的 :class:`PcsSharedPath`
        """
        if "parent_path" in info:
            path = unquote(info["parent_path"] or "") + "/" + info["server_filename"]
        else:
            path = info["path"]
        return PcsSharedPath(
            fs_id=info.get("fs_id"),
            path=path,
            size=info.get("size"),
            is_dir=info.get("isdir") == 1,
            is_file=info.get("isdir") == 0,
            md5=info.get("md5"),
            local_ctime=info.get("local_ctime"),
            local_mtime=info.get("local_mtime"),
            server_ctime=info.get("server_ctime"),
            server_mtime=info.get("server_mtime"),
            uk=info.get("uk"),
            share_id=info.get("share_id") or info.get("shareid"),
            bdstoken=info.get("bdstoken"),
        )


#: 「源 -> 目标」路径对，用于移动、复制、上传、下载等批量操作。
FromTo = namedtuple("FromTo", ["from_", "to_"])


class PcsQuota(NamedTuple):
    """网盘容量信息。

    :ivar quota: 总容量（字节）
    :ivar used: 已用容量（字节）
    """

    quota: int
    used: int


class PcsAuth(NamedTuple):
    """百度网盘登录凭据。

    .. warning:: 本类的所有字段都是账号凭据，不要写入日志、异常信息或提交到仓库。

    :ivar bduss: ``BDUSS`` 凭据，必需
    :ivar cookies: 完整 cookies
    :ivar stoken: ``STOKEN`` 凭据，分享和离线任务相关接口必需
    :ivar ptoken: ``PTOKEN`` 凭据
    """

    bduss: str
    cookies: dict[str, str | None]
    stoken: str | None = None
    ptoken: str | None = None


class PcsUserProduct(NamedTuple):
    """账号开通的一项网盘增值服务。

    :ivar name: 服务名称
    :ivar start_time: 生效时间戳（秒）
    :ivar end_time: 到期时间戳（秒）
    """

    name: str
    start_time: int  # second
    end_time: int  # second


class PcsUser(NamedTuple):
    """百度网盘账号信息。

    :ivar user_id: 百度用户 ID
    :ivar user_name: 用户名
    :ivar auth: 该账号的登录凭据
    :ivar age: 贴吧资料里的年龄
    :ivar sex: 贴吧资料里的性别
    :ivar quota: 网盘容量信息
    :ivar products: 已开通的增值服务
    :ivar level: 账号等级
    """

    user_id: int
    user_name: str | None = None
    auth: PcsAuth | None = None
    age: float | None = None
    sex: str | None = None
    quota: PcsQuota | None = None
    products: list[PcsUserProduct] | None = None
    level: int | None = None


#: 离线下载任务状态码到中文含义的映射，见 :meth:`CloudTask.status_mean`。
TASK_STATUS_MSG = {
    0: "下载成功",
    1: "下载进行中",
    2: "系统错误",
    3: "资源不存在",
    4: "下载超时",
    5: "资源存在但下载失败",
    6: "存储空间不足",
    7: "任务取消",
    8: "目标地址数据已存在",
    9: "任务删除",
    10: "部分下载成功",
}


class CloudTask(NamedTuple):
    """一个离线下载（云）任务。

    :ivar task_id: 任务 ID
    :ivar source_url: 离线下载的源链接
    :ivar task_name: 任务名称
    :ivar path: 保存到的网盘目录
    :ivar status: 状态码，含义见 :data:`TASK_STATUS_MSG`
    :ivar size: 总字节数
    :ivar finished_size: 已完成字节数
    :ivar ctime: 创建时间戳
    :ivar stime: 开始时间戳
    :ivar ftime: 完成时间戳
    """

    task_id: str
    source_url: str
    task_name: str
    path: str  # Saved remote directory
    status: int
    size: int
    finished_size: int
    ctime: int  # created time
    stime: int  # begined time
    ftime: int  # finished time

    @staticmethod
    def from_(info) -> "CloudTask":
        """从离线任务接口返回的原始条目构造 :class:`CloudTask`。

        不同接口的字段名不一致（``size``/``file_size``、``save_path``/``path``），
        这里做了兼容；``status`` 缺失时按 ``3``（资源不存在）处理。

        :param info: 接口返回的单条任务信息字典
        :return: 对应的 :class:`CloudTask`
        """
        size = info.get("size") or info.get("file_size")
        if size:
            size = int(size)
        finished_size = info.get("finished_size")
        if finished_size:
            finished_size = int(finished_size)

        return CloudTask(
            task_id=str(info["task_id"]),
            source_url=info.get("source_url"),
            task_name=info.get("task_name"),
            path=info.get("save_path") or info.get("path"),
            status=int(info.get("status", 3)),
            size=size,
            finished_size=finished_size,
            ctime=info.get("ctime"),
            stime=info.get("stime"),
            ftime=info.get("ftime"),
        )

    def status_mean(self) -> str | None:
        """把状态码翻译成中文说明。

        :return: 中文状态说明；状态码未知时返回 ``None``
        """
        return TASK_STATUS_MSG.get(self.status)

    def finished(self) -> bool:
        """判断任务是否已下载成功。

        :return: 状态码为 ``0``（下载成功）时为 ``True``
        """
        return self.status == 0
