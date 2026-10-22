"""行情发布者（发行版公开的接入示例）。

* :mod:`quantide.providers.akshare_publisher` —— 基于 AkShare 的示例发布者（50 支标的）。
* 内部使用的 pytdx 全市场发布者见同目录下的 ``pytdx_publisher``。
"""

from quantide.providers.akshare_publisher import AksharePublisher

__all__ = ["AksharePublisher"]
