"""百度网盘（BaiduPCS）API 封装。

本包挂在共享命名空间 ``fundrives`` 下（PEP 420 隐式命名空间包）：
``fundrive-alipan`` / ``fundrive-baidu`` / ``fundrive-lanzou`` / ``fundrive-quark``
四个发行包各自往 ``fundrives`` 里放一个子包。因此 ``src/fundrives/`` **不能**有
``__init__.py``（会把隐式命名空间变成常规包，屏蔽兄弟插件），版本号、``py.typed``
和公开符号一律挂在本子包上。
"""

from importlib.metadata import PackageNotFoundError, version

from .api import BaiduPCS, BaiduPCSApi
from .errors import BaiduPCSError
from .inner import (
    CloudTask,
    FromTo,
    PcsAuth,
    PcsFile,
    PcsMagnetFile,
    PcsQuota,
    PcsRapidUploadInfo,
    PcsSharedLink,
    PcsSharedPath,
    PcsUser,
    PcsUserProduct,
)
from .pcs import PCS_UA

try:
    __version__ = version("fundrive-baidu")
except PackageNotFoundError:  # 未安装（例如直接从源码树运行）时的兜底
    __version__ = "0.0.0.dev0"

__all__ = [
    "PCS_UA",
    "BaiduPCS",
    "BaiduPCSApi",
    "BaiduPCSError",
    "CloudTask",
    "FromTo",
    "PcsAuth",
    "PcsFile",
    "PcsMagnetFile",
    "PcsQuota",
    "PcsRapidUploadInfo",
    "PcsSharedLink",
    "PcsSharedPath",
    "PcsUser",
    "PcsUserProduct",
]
