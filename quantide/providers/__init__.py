"""行情发布者（发行版公开的接入示例）。

* :mod:`quantide.providers.tencent_publisher` —— 腾讯行情发布者（实时快照 +
  当日涨跌停），单次约 800 支/批，不封 IP；
* 用户可自行实现发布者：只要把快照以统一结构发布到消息总线
  ``quantide.core.enums.Topics.QUOTES_ALL``（涨跌停发到 ``STOCK_LIMIT``）
  即可被 Paper 交易、风控与 UI 消费。
"""

from quantide.providers.tencent_publisher import TencentPublisher

__all__ = ["TencentPublisher"]
