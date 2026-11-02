# 实时行情与用户扩展

腾讯演示源默认关闭。初始化向导中可以显式勾选启用；首次启用时记录 UTC 起点和
30 天后的截止时间。关闭再开启、重跑 wizard、重启服务、升级程序都不会延长期限。
到期后请求层与后台发布循环都停止使用腾讯源，不能再次从 wizard 开启。

内置演示发布者按交易时段轮询，间隔至少 5 秒，最多订阅 100 个标的。
启用这个演示选项并不代表获得第三方数据的再分发许可。

用户可以在配置目录放置 `extensions/quote_provider.py`，再显式启用：

```bash
millionaire-ce quote-provider status
millionaire-ce quote-provider enable
millionaire-ce service stop
millionaire-ce service start
```

`status` 输出准确的用户代码位置。Windows 系统服务使用安装器创建的配置目录；
在管理员 PowerShell 中先设置 `$env:QUANTIDE_CONFIG_HOME` 为
`C:\ProgramData\MillionaireCE\config`，再执行这些命令。
macOS 系统服务默认目录为 `/Library/Application Support/MillionaireCE/config`；
Linux 为 `/var/lib/millionaire-ce/config`。普通 macOS 用户服务默认使用
`~/.config/quantide`，也支持 `QUANTIDE_CONFIG_HOME`。

扩展运行在服务进程中，工厂接口如下：

```python
class MyPublisher:
    def __init__(self, publish, subscribed_symbols):
        self.publish = publish
        self.subscribed_symbols = subscribed_symbols

    def start(self):
        # 在这里启动有权限使用的数据源，并将其回调接入 self.on_snapshot。
        # start 必须立即返回；有轮询需求时由扩展创建后台线程。
        pass

    def on_snapshot(self, quotes):
        self.publish(quotes)

    def stop(self):
        # 停止并等待扩展自己的后台任务，释放连接。
        pass


def create_publisher(*, publish, subscribed_symbols):
    return MyPublisher(publish, subscribed_symbols)
```

行情示例：`{"000001.SZ": {"price": 10.5, "open": 10.0, "high": 10.7,
"low": 9.9, "volume": 1000.0, "amount": 1050.0, "ts": datetime.datetime(...)}}`。
时间使用交易所当地时间，成交量单位为手，成交额为千元。
`publish(quotes, limits)` 的可选第二参数格式为
`{"000001.SZ": {"up_limit": 11.0, "down_limit": 9.0}}`。
所有消费者使用同一个行情 Hub，包括 Paper 撮合、风控、分钟合成和 UI。

扩展须提供 `start()`、`stop()`，可通过 `subscribed_symbols()` 读取当前订阅。
启用的用户扩展优先于腾讯演示源。加载失败会显式报告初始化错误，不自动改用其它源。
停用命令为 `millionaire-ce quote-provider disable`；重启服务后生效，代码文件保留。
安装升级和默认卸载均保留配置目录里的扩展。

历史数据的内置能力也有边界：Tushare 日线及证券主数据覆盖沪、深、北交易所，
具体字段和历史范围仍取决于账号权限；BaoStock 分钟线覆盖沪深，提供 5/15/30/60
分钟周期，本版本历史回补起点为 2020-01-02。北交所分钟线需要用户扩展实现。
显式请求不支持的市场会在联网前报错，默认分钟回补会过滤不支持的市场并记录说明。
分钟缺口只有得到完整 bar 或明确停牌证据后才视为覆盖；不凭空补价格。
