"""统一设计系统（设计令牌 + 全局样式）.

定义项目统一的视觉语言：

- **颜色**：黑白灰为主，红色只用于强调（品牌字、当前选中、错误、弹窗标题、买入按钮）；
- **字体**：系统字体栈，中文优先 PingFang/Hiragino，等宽数字；
- **边框/圆角**：卡片/按钮统一 8px 圆角 + 1px 灰色边框；
- **间距**：4px 基准（`space-1=4px`）；
- **阴影**：轻量灰阴影，无彩色阴影；
- **禁用**：禁止在页面中使用彩色渐变和除红/绿/灰外的强调色。

规则：
1. 正文/辅助文字用灰色梯度，标题用深灰；
2. 强调只用 `--brand`（红）；
3. 成功用灰绿，错误用红，警告用灰黄；
4. 买入红底白字，卖出绿底白字（交易页面专用）；
5. 弹窗标题为红底白字。
"""

from __future__ import annotations

from fasthtml.common import Script, Style

#: 品牌红（唯一强调色）
BRAND = "#e41815"
BRAND_DARK = "#c91412"
BRAND_LIGHT = "#fff3f2"

#: 基础灰阶（参考 Vercel/Geist 风格）
GRAY_950 = "#0a0a0a"
GRAY_900 = "#171717"
GRAY_800 = "#262626"
GRAY_700 = "#404040"
GRAY_600 = "#525252"
GRAY_500 = "#737373"
GRAY_400 = "#a3a3a3"
GRAY_300 = "#d4d4d4"
GRAY_200 = "#e5e5e5"
GRAY_100 = "#f5f5f5"
GRAY_50 = "#fafafa"
WHITE = "#ffffff"

#: 功能色（不用于强调，只做语义）
GREEN = "#16a34a"          # 买入/盈利方向在中文语境通常用红；卖出/亏损用绿
GREEN_DARK = "#15803d"
AMBER = "#b45309"          # 中性警告（避免彩色背景）
RED = BRAND                 # 错误/拒绝
RED_DARK = BRAND_DARK

#: 字体栈
FONT_STACK = (
    "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',"
    "'Helvetica Neue',Helvetica,Arial,sans-serif"
)
MONO_STACK = (
    "'SF Mono','Menlo','Monaco','Courier New',monospace"
)

#: 统一圆角
RADIUS_SM = 6
RADIUS_MD = 8
RADIUS_LG = 10

#: 阴影（灰阶，无彩色）
SHADOW_SM = "0 1px 2px rgba(0,0,0,0.05)"
SHADOW_MD = "0 4px 12px rgba(0,0,0,0.08)"
SHADOW_LG = "0 12px 32px rgba(0,0,0,0.12)"


def _css() -> str:
    """全局 CSS（使用 CSS 变量，页面组件引用这些令牌）。"""
    return f"""
    :root {{
      --brand: {BRAND};
      --brand-dark: {BRAND_DARK};
      --brand-light: {BRAND_LIGHT};

      --fg: {GRAY_900};
      --fg-muted: {GRAY_600};
      --fg-subtle: {GRAY_500};

      --bg: {WHITE};
      --bg-subtle: {GRAY_50};
      --bg-muted: {GRAY_100};

      --border: {GRAY_200};
      --border-strong: {GRAY_300};

      --success: {GREEN};
      --success-dark: {GREEN_DARK};
      --warning: {AMBER};
      --danger: {BRAND};
      --danger-dark: {BRAND_DARK};

      --radius-sm: {RADIUS_SM}px;
      --radius-md: {RADIUS_MD}px;
      --radius-lg: {RADIUS_LG}px;

      --shadow-sm: {SHADOW_SM};
      --shadow-md: {SHADOW_MD};
      --shadow-lg: {SHADOW_LG};

      --font: {FONT_STACK};
      --font-mono: {MONO_STACK};

      --space-1: 4px;
      --space-2: 8px;
      --space-3: 12px;
      --space-4: 16px;
      --space-5: 20px;
      --space-6: 24px;
      --space-8: 32px;
    }}

    /* ---------------- 基础 ---------------- */
    *, *::before, *::after {{ box-sizing: border-box; }}
    html, body {{ margin: 0; padding: 0; }}
    body {{
      font-family: var(--font);
      background: var(--bg-subtle);
      color: var(--fg);
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
    }}
    h1, h2, h3, h4, h5, h6 {{ color: var(--fg); font-weight: 600; margin: 0; }}
    p {{ margin: 0; }}
    a {{ color: var(--fg); text-decoration: none; }}
    a:hover {{ color: var(--brand); }}
    code, pre, .mono {{ font-family: var(--font-mono); }}
    input, select, textarea, button {{ font: inherit; }}

    /* ---------------- 卡片 ---------------- */
    .card {{
      background: var(--bg);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-sm);
    }}
    .card--padded {{ padding: var(--space-6); }}

    /* ---------------- 表格 ---------------- */
    .q-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }}
    .q-table th {{
      text-align: left;
      padding: var(--space-2) var(--space-4);
      color: var(--fg-subtle);
      font-size: 12px;
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.02em;
      border-bottom: 1px solid var(--border);
      background: var(--bg-subtle);
    }}
    .q-table td {{
      padding: var(--space-2) var(--space-4);
      border-bottom: 1px solid var(--border);
      color: var(--fg);
    }}
    .q-table tr:last-child td {{ border-bottom: none; }}
    .q-table tbody tr:hover {{ background: var(--bg-subtle); }}

    /* ---------------- 表单 ---------------- */
    .q-input {{
      height: 36px;
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      background: var(--bg);
      padding: 0 var(--space-3);
      font-size: 14px;
      color: var(--fg);
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .q-input:focus {{
      border-color: var(--brand);
      box-shadow: 0 0 0 3px var(--brand-light);
    }}
    .q-input::placeholder {{ color: var(--fg-subtle); }}
    .q-label {{ font-size: 12px; font-weight: 500; color: var(--fg-muted); display: block; margin-bottom: var(--space-1); }}

    /* ---------------- 按钮 ---------------- */
    .q-btn {{
      display: inline-flex; align-items: center; justify-content: center;
      height: 36px; padding: 0 var(--space-3); border-radius: var(--radius-md);
      font-size: 14px; font-weight: 500; cursor: pointer;
      border: 1px solid var(--border); background: var(--bg); color: var(--fg);
      transition: background 0.15s, color 0.15s, border-color 0.15s;
      user-select: none; white-space: nowrap;
    }}
    .q-btn:hover {{ background: var(--bg-subtle); border-color: var(--border-strong); }}
    .q-btn--sm {{ height: 28px; padding: 0 var(--space-2); font-size: 12px; }}
    .q-btn--primary {{
      background: var(--brand); border-color: var(--brand); color: #fff;
    }}
    .q-btn--primary:hover {{ background: var(--brand-dark); border-color: var(--brand-dark); }}
    .q-btn--ghost {{ background: transparent; border-color: transparent; color: var(--fg-muted); }}
    .q-btn--ghost:hover {{ background: var(--bg-subtle); color: var(--fg); }}
    .q-btn--danger {{ background: var(--brand); border-color: var(--brand); color: #fff; }}
    .q-btn--danger:hover {{ background: var(--brand-dark); }}
    .q-btn--success {{ background: var(--success); border-color: var(--success); color: #fff; }}
    .q-btn--success:hover {{ background: var(--success-dark); }}
    .q-btn--buy {{ background: var(--brand); border-color: var(--brand); color: #fff; }}
    .q-btn--buy:hover {{ background: var(--brand-dark); }}
    .q-btn--sell {{ background: var(--success); border-color: var(--success); color: #fff; }}
    .q-btn--sell:hover {{ background: var(--success-dark); }}
    .q-btn--icon {{ padding: 0; width: 32px; height: 32px; }}
    .q-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}

    /* ---------------- Badge / Chip ---------------- */
    .q-badge {{
      display: inline-flex; align-items: center; padding: 2px 8px;
      border-radius: 999px; font-size: 12px; font-weight: 500;
      border: 1px solid var(--border); background: var(--bg-subtle); color: var(--fg-muted);
    }}
    .q-badge--brand {{ background: var(--brand-light); color: var(--brand); border-color: var(--brand-light); }}
    .q-badge--success {{ background: #f0fdf4; color: var(--success); border-color: #bbf7d0; }}
    .q-badge--warning {{ background: #fffbeb; color: var(--warning); border-color: #fde68a; }}
    .q-badge--danger {{ background: var(--brand-light); color: var(--brand); border-color: var(--brand-light); }}
    .q-badge--neutral {{ background: var(--bg-subtle); color: var(--fg-muted); }}

    /* ---------------- 对话框 ---------------- */
    .q-modal-backdrop {{
      position: fixed; inset: 0; background: rgba(0,0,0,0.4);
      z-index: 50; display: flex; align-items: center; justify-content: center; padding: 16px;
    }}
    .q-modal {{
      background: var(--bg); border-radius: var(--radius-lg);
      box-shadow: var(--shadow-lg); width: 100%;
      max-height: calc(100vh - 32px); display: flex; flex-direction: column;
      overflow: hidden;
    }}
    .q-modal__header {{
      background: var(--brand); color: #fff;
      padding: var(--space-4) var(--space-6);
      font-size: 16px; font-weight: 600; flex-shrink: 0;
    }}
    .q-modal__body {{ padding: var(--space-6); overflow-y: auto; flex: 1; }}
    .q-modal__footer {{
      padding: var(--space-4) var(--space-6); border-top: 1px solid var(--border);
      display: flex; justify-content: flex-end; gap: var(--space-2); flex-shrink: 0;
    }}

    /* ---------------- 面包屑 ---------------- */
    .q-breadcrumb {{ display: flex; align-items: center; gap: var(--space-2); font-size: 14px; color: var(--fg-muted); }}
    .q-breadcrumb a:hover {{ color: var(--brand); }}
    .q-breadcrumb__sep {{ color: var(--fg-subtle); }}
    .q-breadcrumb__current {{ color: var(--fg); font-weight: 500; }}

    /* ---------------- Tabs ---------------- */
    .q-tabs {{ display: flex; gap: var(--space-1); border-bottom: 1px solid var(--border); }}
    .q-tab {{
      padding: var(--space-2) var(--space-3); font-size: 14px; font-weight: 500;
      color: var(--fg-muted); cursor: pointer; border-bottom: 2px solid transparent;
      margin-bottom: -1px; user-select: none;
    }}
    .q-tab:hover {{ color: var(--fg); }}
    .q-tab--active {{ color: var(--brand); border-bottom-color: var(--brand); }}

    /* ---------------- 页面容器 ---------------- */
    .page {{ max-width: 1400px; margin: 0 auto; width: 100%; padding: var(--space-6); }}
    .page__header {{ display: flex; align-items: center; justify-content: space-between; gap: var(--space-4); margin-bottom: var(--space-6); }}
    .page__title {{ font-size: 20px; font-weight: 600; color: var(--fg); }}
    .page__subtitle {{ font-size: 13px; color: var(--fg-subtle); margin-top: var(--space-1); }}

    /* ---------------- 其他 ---------------- */
    .hidden {{ display: none; }}
    .truncate {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .stat-grid {{ display: grid; gap: 1px; background: var(--border); border-radius: var(--radius-lg); overflow: hidden; border: 1px solid var(--border); }}
    .stat {{ background: var(--bg); padding: var(--space-4); }}
    .stat__label {{ font-size: 12px; color: var(--fg-subtle); }}
    .stat__value {{ font-size: 20px; font-weight: 600; color: var(--fg); margin-top: var(--space-1); font-family: var(--font-mono); }}
    """


class DesignSystem:
    """设计系统入口。"""

    @staticmethod
    def css() -> Style:
        """全局样式（Style 元素，加入 headers）。"""
        return Style(_css())

    @staticmethod
    def js() -> Script:
        """全局脚本（当前为空，预留）。"""
        return Script("")

    @staticmethod
    def headers() -> list:
        """加入所有页面 headers 的设计系统资源。"""
        return [DesignSystem.css()]


__all__ = [
    "AMBER",
    "BRAND",
    "BRAND_DARK",
    "BRAND_LIGHT",
    "DesignSystem",
    "GRAY_50",
    "GRAY_100",
    "GRAY_200",
    "GRAY_300",
    "GRAY_400",
    "GRAY_500",
    "GRAY_600",
    "GRAY_700",
    "GRAY_800",
    "GRAY_900",
    "GRAY_950",
    "GREEN",
    "GREEN_DARK",
    "RED",
    "RED_DARK",
    "WHITE",
]
