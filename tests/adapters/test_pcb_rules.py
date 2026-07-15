import json
from pathlib import Path

from copperbrain.adapters.pcb_rules import (
    MANAGED_BEGIN,
    PcbRuleAdapter,
    read_netclasses,
)
from copperbrain.models import (
    ManufacturingProfile,
    NetClassAssignment,
    NetClassRule,
    PcbRuleSet,
)


def rule_set() -> PcbRuleSet:
    return PcbRuleSet(
        manufacturing=ManufacturingProfile(),
        classes=(
            NetClassRule(
                name="PWR_2A",
                clearance_mm=0.3,
                track_width_min_mm=0.8,
                track_width_preferred_mm=1.0,
                via_diameter_mm=0.7,
                via_drill_mm=0.35,
                creepage_mm=0.4,
                max_length_mm=100,
                diff_pair_width_mm=0.2,
                diff_pair_gap_mm=0.2,
                diff_pair_max_uncoupled_mm=5,
                rationale=("test",),
            ),
        ),
        assignments=(NetClassAssignment(net="/+5V", netclass="PWR_2A"),),
    )


def test_adapter_writes_typed_netclasses_and_managed_rules(tmp_path: Path) -> None:
    project = tmp_path / "demo.kicad_pro"
    project.write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [{"name": "Default", "clearance": 0.2}],
                    "netclass_patterns": [],
                }
            }
        ),
        encoding="utf-8",
    )
    rules = tmp_path / "demo.kicad_dru"
    adapter = PcbRuleAdapter()
    adapter.apply(project, rules, rule_set())
    classes, assignments = read_netclasses(project)
    assert {item.name for item in classes} == {"Default", "PWR_2A"}
    assert assignments == (NetClassAssignment(net="/+5V", netclass="PWR_2A"),)
    content = rules.read_text(encoding="utf-8")
    assert MANAGED_BEGIN in content
    assert "A.hasNetclass('PWR_2A')" in content
    assert "(constraint track_width (min 0.8mm) (opt 1mm))" in content
    assert "(constraint creepage (min 0.4mm))" in content
    assert "(constraint length (max 100mm))" in content
    assert "(constraint diff_pair_uncoupled (max 5mm))" in content
    assert adapter.validate(project, rules).valid


def test_adapter_preserves_unmanaged_custom_rules_and_updates_managed_block(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo.kicad_pro"
    project.write_text("{}", encoding="utf-8")
    rules = tmp_path / "demo.kicad_dru"
    rules.write_text(
        '(version 1)\n(rule "UserRule" (constraint clearance (min 0.1mm)))\n',
        encoding="utf-8",
    )
    adapter = PcbRuleAdapter()
    adapter.apply(project, rules, rule_set())
    adapter.apply(project, rules, rule_set())
    content = rules.read_text(encoding="utf-8")
    assert content.count("UserRule") == 1
    assert content.count(MANAGED_BEGIN) == 1
