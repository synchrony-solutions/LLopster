import json
from pathlib import Path

from src.agent.alert_handler import parse_alertmanager_payload

FIXTURE = Path(__file__).parent / "fixtures" / "sample-alert.json"


def test_parses_sample_payload():
    payload = json.loads(FIXTURE.read_text())
    alerts = parse_alertmanager_payload(payload)

    assert len(alerts) == 1
    a = alerts[0]
    assert a.alertname == "HelmValuesMisconfigured"
    assert a.severity == "warning"
    assert a.service == "demo-app"
    assert a.status == "firing"
    assert a.starts_at is not None
    assert a.ends_at is None
    assert a.labels["pod"] == "demo-app-7c9b4d-xk2lp"


def test_handles_empty_alerts():
    assert parse_alertmanager_payload({"alerts": []}) == []
    assert parse_alertmanager_payload({}) == []
