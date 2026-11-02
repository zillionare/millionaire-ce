"""Wizard layout."""

from typing import Any

from fasthtml.common import *
from monsterui.all import *

from quantide.config.branding import get_branding
from quantide.config.dev_stubs import (
    dev_stubs_enabled,
)
from quantide.core.init_wizard_steps import WIZARD_TOTAL_STEPS, build_wizard_steps
from quantide.service.init_wizard import init_wizard
from quantide.web.layouts.base import BaseLayout
from quantide.web.pages.wizard_components import (
    SectionDescription,
    SectionTitle,
    StepIndicator,
    _get_step_meta,
    _render_inline_error,
)
from quantide.web.pages.wizard_state import (
    PRIMARY_COLOR,
    WIZARD_BODY_INNER_STYLE,
    WIZARD_PANEL_STYLE,
    WIZARD_STEP_WRAPPER_STYLE,
    _get_download_error,
    _with_force_query,
)
from quantide.web.pages.wizard_step_views import (
    Step1_Welcome,
    Step2_Runtime,
    Step3_Admin,
    Step4_Gateway,
    Step5_DataSetup,
    Step6_Complete,
)


def _build_step_content(step: int, state_dict: dict[str, Any]):
    step_content_builders = {
        1: Step1_Welcome,
        2: lambda: Step2_Runtime(state_dict),
        3: lambda: Step3_Admin(state_dict),
        4: lambda: Step4_Gateway(state_dict),
        5: lambda: Step5_DataSetup(state_dict),
        6: lambda: Step6_Complete(state_dict),
    }
    builder = step_content_builders.get(step, Step1_Welcome)
    return builder()


def _build_step_progress(state_dict: dict[str, Any]) -> list[dict[str, int | str | bool]]:
    current_step = int(state_dict.get("init_step", 0) or 0)
    return build_wizard_steps(current_step)


def _render_wizard_header(step: int, error_message: str | None = None):
    meta = _get_step_meta(step)
    children: list[Any] = [SectionTitle(meta["title"])]
    description = meta.get("description")
    if description:
        children.append(SectionDescription(description))
        children.append(Hr(cls="mb-4"))
    if error_message:
        children.append(_render_inline_error(error_message))
    return Div(*children, id="wizard-header-region", cls="wizard-region")


def _render_wizard_body(step_content: Any):
    return Div(
        Div(step_content, style=WIZARD_BODY_INNER_STYLE, cls="wizard-body-inner"),
        id="wizard-body-region",
        cls="wizard-region",
    )


def _render_wizard_footer(step: int, *, show_buttons: bool = True):
    children: list[Any] = []
    if show_buttons:
        children.append(WizardButtons(step))
    return Div(*children, id="wizard-footer-region", cls="wizard-region")


def _render_wizard_form(
    step: int,
    step_content: Any,
    *,
    current_step_value: int | None = None,
    error_message: str | None = None,
    show_buttons: bool = True,
):
    children: list[Any] = [
        Input(type="hidden", name="_current_step", value=str(current_step_value or step)),
        _render_wizard_header(step, error_message),
        _render_wizard_body(Div(step_content, id="wizard-content")),
        _render_wizard_footer(step, show_buttons=show_buttons),
    ]
    return Form(*children, cls="flex-1", id="wizard-form-container")


def _render_wizard_main_content(
    step: int,
    state_dict: dict[str, Any],
    *,
    step_content: Any | None = None,
    current_step_value: int | None = None,
    error_message: str | None = None,
    show_buttons: bool = True,
    extra_nodes: tuple[Any, ...] = (),
):
    resolved_error_message = _get_download_error(step, error_message)
    form = _render_wizard_form(
        step,
        step_content or _build_step_content(step, state_dict),
        current_step_value=current_step_value,
        error_message=resolved_error_message,
        show_buttons=show_buttons,
    )
    return Div(
        Div(
            StepIndicator(step, _build_step_progress(state_dict)),
            cls="flex-shrink-0",
            id="step-indicator-wrapper",
            style=WIZARD_STEP_WRAPPER_STYLE,
        ),
        Div(
            form,
            cls="flex-1",
            style=WIZARD_PANEL_STYLE,
        ),
        *extra_nodes,
        cls="flex items-stretch w-full gap-12",
    )


def WizardButtons(current_step: int, total_steps: int = WIZARD_TOTAL_STEPS):
    """向导导航按钮 - 上一步在左，下一步在右"""
    if current_step >= total_steps:
        return Div(
            Button(
                "进入系统",
                cls="btn px-6 py-2.5 rounded-md font-medium text-sm",
                style=(
                    "background: "
                    f"{PRIMARY_COLOR}"
                    "; color: white; border: none; box-shadow: 0 1px 2px rgba(209, 53, "
                    "39, 0.3); transition: all 0.2s;"
                ),
                hx_post=_with_force_query("/init-wizard/complete"),
                hx_target="#wizard-main-container",
                hx_swap="innerHTML",
            ),
            cls="flex justify-end",
        )

    left_buttons = []
    right_buttons = []

    if current_step > 1:
        left_buttons.append(
            Button(
                "上一步",
                type="submit",
                name="nav",
                value="prev",
                cls="btn px-5 py-2.5 rounded-md font-medium text-sm",
                style=(
                    "background: white; color: #374151; border: 1px solid #d1d5db; "
                    "transition: all 0.2s;"
                ),
                hx_post=_with_force_query(f"/init-wizard/step/{current_step - 1}"),
                hx_target="#wizard-main-container",
                hx_swap="innerHTML",
                hx_include="[name]",
            )
        )

    if current_step < total_steps:
        if current_step == 5:
            next_target = "/init-wizard/download"
            if dev_stubs_enabled():
                next_target = f"/init-wizard/step/{current_step + 1}"
            right_buttons.append(
                Button(
                    "下一步",
                    type="submit",
                    name="nav",
                    value="next",
                    cls="btn px-5 py-2.5 rounded-md font-medium text-sm",
                    style=(
                        "background: "
                        f"{PRIMARY_COLOR}"
                        "; color: white; border: none; box-shadow: 0 1px 2px rgba(209, "
                        "53, 39, 0.3); transition: all 0.2s;"
                    ),
                    hx_post=_with_force_query(next_target),
                    hx_target="#wizard-main-container",
                    hx_swap="innerHTML",
                    hx_include="[name]",
                )
            )
        else:
            right_buttons.append(
                Button(
                    "下一步",
                    type="submit",
                    name="nav",
                    value="next",
                    cls="btn px-5 py-2.5 rounded-md font-medium text-sm",
                    style=(
                        "background: "
                        f"{PRIMARY_COLOR}"
                        "; color: white; border: none; box-shadow: 0 1px 2px rgba(209, "
                        "53, 39, 0.3); transition: all 0.2s;"
                    ),
                    hx_post=_with_force_query(f"/init-wizard/step/{current_step + 1}"),
                    hx_target="#wizard-main-container",
                    hx_swap="innerHTML",
                    hx_include="[name]",
                )
            )

    return Div(
        Div(*left_buttons, cls="flex gap-3"),
        Div(*right_buttons, cls="flex gap-3"),
        cls="flex justify-between items-center",
    )


def InitWizardPage(step: int = 1, form_data: dict | None = None):
    """初始化向导主页面

    Args:
        step: 当前步骤（1-7）
        form_data: 表单数据
    """
    if form_data:
        state_dict = form_data
    else:
        state = init_wizard.get_state()
        state_dict = state.to_dict()

    return BaseLayout(
        Div(
            Link(rel="stylesheet", href="/static/init-wizard.css"),
            Div(_render_wizard_main_content(step, state_dict), id="wizard-main-container"),
        ),
        page_title=f"系统初始化 - {get_branding().product_name}",
    )


def SyncProgressDialog():
    """同步进度对话框"""
    return Div(
        # 遮罩层
        Div(
            cls="fixed inset-0 bg-black bg-opacity-50 z-40",
        ),
        # 对话框
        Div(
            Div(
                H4(
                    "正在同步数据",
                    cls="text-lg font-semibold mb-4",
                    style=f"color: {PRIMARY_COLOR};",
                ),
                P("正在同步数据...", id="sync-stage", cls="text-gray-600 mb-4"),
                # 进度条
                Div(
                    Div(
                        cls="h-2 bg-gray-200 rounded-full overflow-hidden",
                    ),
                    Div(
                        id="sync-progress-bar",
                        cls="h-2 bg-red-600 rounded-full -mt-2 transition-all duration-300",
                        style="width: 0%;",
                    ),
                    cls="mb-4",
                ),
                # 状态文本
                P(
                    "正在连接数据源...",
                    id="sync-status",
                    cls="text-sm text-gray-500 mb-4",
                ),
                Button(
                    "返回表单",
                    type="button",
                    id="sync-return-button",
                    cls="btn px-4 py-2 rounded-md font-medium text-sm",
                    style=(
                        "display: none; background: white; color: #374151; border: 1px "
                        "solid #d1d5db;"
                    ),
                ),
                cls="bg-white rounded-lg shadow-xl p-6 max-w-md w-full mx-4",
            ),
            cls="fixed inset-0 flex items-center justify-center z-50",
        ),
        # SSE 连接脚本
        Script(src="/static/init-wizard-progress.js"),
        cls="sync-dialog-container",
    )
