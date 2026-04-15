# tests/test_integration_mvp1_mvp2.py
"""Integration tests for MVP1+MVP2 unified implementation."""
from tero2.config import Config, ReflexionConfig, StuckDetectionConfig, EscalationConfig
from tero2.reflexion import ReflexionContext, add_attempt
from tero2.project_init import init_project, _extract_project_name
from tero2.stuck_detection import check_stuck, StuckSignal
from tero2.escalation import decide_escalation, EscalationLevel
from tero2.state import AgentState


def test_reflexion_plus_escalation_prompt():
    ctx = add_attempt(ReflexionContext(), "tried X", "X failed", ["test_x"])
    escalation_inject = "Dead end. Try different approach."
    plan = "# Build auth"
    effective = f"## Notice\n{escalation_inject}\n\n---\n\n{ctx.to_prompt()}\n\n---\n\n{plan}"
    assert "Dead end" in effective
    assert "Attempt 1" in effective
    assert "Build auth" in effective


def test_project_init_full_workflow(tmp_path):
    plan = "# Auth System\nBuild JWT auth."
    name = _extract_project_name(plan)
    config = Config()
    config.projects_dir = str(tmp_path)
    path = init_project(name, plan, config)
    assert (path / ".sora" / "milestones" / "M001" / "ROADMAP.md").is_file()


def test_stuck_then_escalate_then_reflexion():
    """Full cycle: stuck → escalate → reflexion inject."""
    state = AgentState(retry_count=3)
    config_sd = StuckDetectionConfig(max_retries=3)
    config_esc = EscalationConfig()

    stuck = check_stuck(state, config_sd)
    assert stuck.signal == StuckSignal.RETRY_EXHAUSTED

    action = decide_escalation(stuck, EscalationLevel.NONE, 0, config_esc)
    assert action.level == EscalationLevel.DIVERSIFICATION

    ctx = add_attempt(ReflexionContext(), "failed output", "test broke")
    assert "Attempt 1" in ctx.to_prompt()


def test_config_all_sections():
    cfg = Config(
        reflexion=ReflexionConfig(max_cycles=3),
        stuck_detection=StuckDetectionConfig(max_retries=5),
        escalation=EscalationConfig(diversification_max_steps=4),
    )
    assert cfg.reflexion.max_cycles == 3
    assert cfg.stuck_detection.max_retries == 5
    assert cfg.escalation.diversification_max_steps == 4
