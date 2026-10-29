"""CE tests for empty and explicitly registered risk selection UI paths."""

from __future__ import annotations

from fasthtml.common import to_xml

from quantide.service.risk_programs import (
    RiskProgramAdapter,
    register_risk_program,
    risk_program_registry,
)
from quantide.web.pages import strategy as strategy_page


class _Form(dict):
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


class _Host:
    BAR_INTERVAL = "1d"


def _register_test_program() -> str:
    program_id = "private.learner.GenericRisk"
    register_risk_program(
        RiskProgramAdapter(
            id=program_id,
            name="Generic risk adapter",
            default_params={"threshold": 1.0},
            compatible=lambda host, frame: str(frame) == "1d",
        )
    )
    return program_id


def test_empty_selection_form_is_valid_without_installed_programs(monkeypatch) -> None:
    risk_program_registry.clear()
    form = _Form(
        {
            "risk_selection_present": "1",
            "risk_program_id": [],
            "allow_same_bar_repurchase": "false",
        }
    )

    selected, allow_same_bar = strategy_page._parse_risk_program_selection(
        form, _Host, "1d"
    )

    assert selected == []
    assert allow_same_bar is False


def test_generic_registered_adapter_is_rendered_and_parsed() -> None:
    risk_program_registry.clear()
    program_id = _register_test_program()
    try:
        form = _Form(
            {
                "risk_selection_present": "1",
                "risk_program_id": [program_id],
                "risk_priority_0": "3",
                "risk_param_0_threshold": "2.5",
                "allow_same_bar_repurchase": ["false", "true"],
            }
        )
        selected, allow_same_bar = strategy_page._parse_risk_program_selection(
            form, _Host, "1d"
        )
        assert selected[0]["id"] == program_id
        assert selected[0]["params"]["threshold"] == 2.5
        assert selected[0]["priority"] == 0
        assert allow_same_bar is True

        html = to_xml(strategy_page._build_risk_selection_panel(_Host))
        assert program_id in html
        assert "允许风控卖出后同一 bar 再买入" in html
    finally:
        risk_program_registry.clear()


def test_old_private_id_is_visible_as_unavailable_on_deploy() -> None:
    risk_program_registry.clear()
    html = to_xml(
        strategy_page._render_deploy_risk_selection(
            {
                "risk_programs": [
                    {
                        "id": "quantide.strategies.triple_barrier_risk.TripleBarrierRiskStrategy",
                        "params": {},
                    }
                ]
            }
        )
    )
    assert "不可用/已阻止" in html
    assert "不能静默替换" in html
