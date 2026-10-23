"""行情发布者（发行版公开的接入示例）。

* :mod:`quantide.providers.akshare_publisher` —— 基于 AkShare 的示例发布者（50 支标的）。

发行版本不绑定具体行情源。用户可按需实现发布者：将行情快照以
:class:`~quantide.core.domain.QuoteSnapshot` 的结构发布到消息总线
``quantide.core.enums.Topics.QUOTES_ALL`` 即可被 Paper 交易和风控模块消费。
"""

from quantide.providers.akshare_publisher import AksharePublisher

__all__ = ["AksharePublisher"]
