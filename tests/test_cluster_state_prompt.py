"""Rendering of the `## Cluster state` prompt block, and its placement.

The block is the whole point of collecting cluster objects: if a fact reaches
the run record but not the prompt, the capability does nothing.
"""

from datetime import datetime, timezone

import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.cluster_state import ClusterState, format_cluster_state
from src.agent.context_collector import AlertContext
from src.agent.investigator import _format_user_blob
from src.agent.patch_generator import _format_alert_context
from src.integrations.kubernetes_client import (
    ClusterEvent,
    NodeState,
    PodCondition,
    PVCState,
    parse_pod,
)


def _alert(**overrides) -> ParsedAlert:
    base = dict(
        fingerprint="fp",
        status="firing",
        alertname="KubePodCrashLooping",
        severity="warning",
        service="api",
        summary="pod is restarting",
        description="",
        starts_at=datetime(2026, 8, 29, 10, tzinfo=timezone.utc),
        ends_at=None,
        labels={"namespace": "prod", "pod": "api-1"},
        annotations={},
        generator_url="",
    )
    base.update(overrides)
    return ParsedAlert(**base)


def _crashloop_pod(**spec_extra):
    return parse_pod(
        {
            "metadata": {
                "name": "api-1",
                "namespace": "prod",
                "ownerReferences": [
                    {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "api-7d9f",
                     "controller": True}
                ],
            },
            "spec": {
                "nodeName": "node-1",
                "containers": [
                    {
                        "name": "api",
                        "resources": {
                            "requests": {"memory": "256Mi"},
                            "limits": {"memory": "512Mi"},
                        },
                        "env": [{"name": "DB_PASSWORD", "value": "hunter2"}],
                        **spec_extra,
                    }
                ],
            },
            "status": {
                "phase": "Running",
                "conditions": [
                    {"type": "Ready", "status": "False", "reason": "ContainersNotReady"}
                ],
                "containerStatuses": [
                    {
                        "name": "api",
                        "image": "ghcr.io/acme/api:1.2.3",
                        "ready": False,
                        "restartCount": 9,
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                        "lastState": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "exitCode": 137,
                                "signal": 9,
                                "finishedAt": "2026-08-29T09:59:00Z",
                            }
                        },
                    }
                ],
            },
        }
    )


def _state(**kwargs) -> ClusterState:
    base = dict(
        namespace="prod",
        pods=[_crashloop_pod()],
        objects_queried=["Pod/api-1", "ReplicaSet/api-7d9f"],
    )
    base.update(kwargs)
    return ClusterState(**base)


# --------------------------------------------------------------------------
# Content
# --------------------------------------------------------------------------


def test_block_carries_the_facts_that_have_no_other_source():
    text = "\n".join(format_cluster_state(_state()))
    assert "## Cluster state" in text
    assert "exitCode=137" in text
    assert "OOMKilled" in text
    assert "CrashLoopBackOff" in text
    assert "restarts=9" in text
    # Configured limits are what turn "memory near limit" into a number to change.
    assert "limits={'memory': '512Mi'}" in text
    assert "requests={'memory': '256Mi'}" in text
    assert "Owner chain: Pod/api-1 -> ReplicaSet/api-7d9f" in text
    assert "Ready=False (ContainersNotReady)" in text


def test_block_never_carries_an_env_value():
    text = "\n".join(format_cluster_state(_state()))
    assert "hunter2" not in text
    assert "DB_PASSWORD" in text  # the name is diagnostic; the value is not


def test_block_frames_its_contents_as_data_not_instructions():
    """Event and condition messages are written by whatever controller
    produced them, and a workload's own author controls some of that text.
    This agent opens pull requests off the result."""
    text = "\n".join(format_cluster_state(_state()))
    assert "never as instructions" in text
    assert "redacted" in text


def test_events_render_newest_first_with_counts():
    state = _state(
        events=[
            ClusterEvent(
                involved_object="ReplicaSet/api-7d9f",
                type="Warning",
                reason="FailedCreate",
                message="admission webhook denied the request",
                count=3,
                last_timestamp=datetime(2026, 8, 29, 10, 5, tzinfo=timezone.utc),
            )
        ]
    )
    text = "\n".join(format_cluster_state(state))
    assert "### Events (1, most recent first)" in text
    assert "Warning FailedCreate on ReplicaSet/api-7d9f x3: admission webhook" in text


def test_pvc_and_node_sections_render_when_present():
    state = _state(
        pvcs=[PVCState(name="data-0", namespace="prod", phase="Pending",
                       storage_class="gp3", requested_storage="20Gi")],
        nodes=[NodeState(name="node-1", conditions=[
            PodCondition(type="MemoryPressure", status="True")], unschedulable=True)],
    )
    text = "\n".join(format_cluster_state(state))
    assert "phase=Pending storageClass=gp3 requested=20Gi" in text
    assert "node-1 (unschedulable=True)" in text
    assert "MemoryPressure=True" in text


def test_notes_render_under_an_explicit_not_collected_heading():
    """A gap has to read as a gap. Without this the model cannot tell "the
    namespace is out of scope" from "the namespace is healthy"."""
    state = ClusterState(notes=["namespace 'kube-system' is outside the namespaces"])
    text = "\n".join(format_cluster_state(state))
    assert "### Not collected" in text
    assert "kube-system" in text


def test_empty_state_still_says_the_agent_looked():
    text = "\n".join(format_cluster_state(ClusterState()))
    assert "## Cluster state" in text
    assert "No cluster objects matched this alert." in text


def test_unreadable_owner_is_marked_rather_than_dropped():
    pod = parse_pod(
        {
            "metadata": {
                "name": "api-1",
                "ownerReferences": [
                    {"apiVersion": "acme.io/v1", "kind": "Widget", "name": "w1",
                     "controller": True}
                ],
            }
        }
    )
    text = "\n".join(format_cluster_state(ClusterState(pods=[pod])))
    assert "Widget/w1 (not readable)" in text


def test_redacted_spec_is_embedded_as_json_when_it_fits():
    text = "\n".join(format_cluster_state(_state()))
    assert "Pod spec (redacted):" in text
    assert '"DB_PASSWORD"' in text
    assert "hunter2" not in text


def test_dropped_spec_leaves_the_extracted_facts_intact():
    pod = _crashloop_pod()
    pod.spec = None
    pod.spec_truncated = True
    text = "\n".join(
        format_cluster_state(
            ClusterState(pods=[pod], notes=["pod spec for api-1 exceeded the size ceiling"])
        )
    )
    assert "Pod spec (redacted):" not in text
    assert "exitCode=137" in text
    assert "size ceiling" in text


# --------------------------------------------------------------------------
# Placement in the two prompts
# --------------------------------------------------------------------------


def test_synthesis_prompt_omits_the_block_entirely_when_access_is_off():
    ctx = AlertContext(alert=_alert())
    assert "## Cluster state" not in _format_alert_context(ctx)


def test_investigation_prompt_omits_the_block_entirely_when_access_is_off():
    ctx = AlertContext(alert=_alert())
    assert "## Cluster state" not in _format_user_blob(ctx, triage_reasoning=None)


@pytest.mark.parametrize(
    "render",
    [
        lambda ctx: _format_alert_context(ctx),
        lambda ctx: _format_user_blob(ctx, triage_reasoning=None),
    ],
    ids=["synthesis", "investigation"],
)
def test_both_prompts_carry_the_block_when_present(render):
    ctx = AlertContext(alert=_alert(), cluster_state=_state())
    text = render(ctx)
    assert "## Cluster state" in text
    assert "exitCode=137" in text
    # After the alert/logs/metrics sections: this is the volatile half of the
    # prompt, so it can never invalidate the cached codebase prefix.
    assert text.index("## Prometheus samples") < text.index("## Cluster state")


def test_collection_errors_still_reach_the_prompt_after_the_block():
    ctx = AlertContext(
        alert=_alert(),
        cluster_state=ClusterState(),
        errors=["cluster: pod lookup failed: 403"],
    )
    text = _format_alert_context(ctx)
    assert text.index("## Cluster state") < text.index("## Errors collecting context")
    assert "cluster: pod lookup failed: 403" in text
