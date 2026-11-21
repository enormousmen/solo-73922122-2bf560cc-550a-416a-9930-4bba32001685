"""CSI ReadWriteOnce 卷挂载排查的自测。

覆盖：解析、排查范围（CSI + RWO + PVC）、绑定/挂接四类待核实规则、
其他节点占用与占用 Pod、目标节点已挂接/无挂接两种区分、HTTP 接口与失败撤旧。
"""
from __future__ import annotations

import contextlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.storage import (
    ATTACHED_ON_TARGET,
    BLOCKED_OTHER_NODE,
    NEEDS_VERIFICATION,
    NO_ATTACHMENT,
    OUT_OF_SCOPE,
    StorageDiagnoser,
    _parse_timestamp,
    parse_pvcs,
    parse_pvs,
    parse_storage_pods,
    parse_vas,
)
from scripts.generate_samples import (
    pvc as mk_pvc,
    pv as mk_pv,
    storage_pod,
    storage_scenario,
    va as mk_va,
)

SAMPLES = Path(__file__).resolve().parent.parent / "samples"
client = TestClient(app)

DRIVER = "csi-driver.example.com"
WRONG_DRIVER = "csi-wrong.example.com"


# --------------------------------------------------------------------------- #
# 构造小工具
# --------------------------------------------------------------------------- #
def make_pvc(ns="team1", name="claim", uid="uid-pvc-1",
             volume="pv-1", modes=("ReadWriteOnce",)):
    return {"apiVersion": "v1", "kind": "PersistentVolumeClaimList",
            "items": [mk_pvc(ns, name, uid, volume, access_modes=modes)]}


def make_pv(name="pv-1", uid="uid-pv-1", driver=DRIVER, ns="team1",
            claim="claim", claim_uid="uid-pvc-1",
            created="2026-11-18T08:00:00Z", modes=("ReadWriteOnce",),
            csi=True):
    return {"apiVersion": "v1", "kind": "PersistentVolumeList",
            "items": [mk_pv(name, uid, driver, ns, claim, claim_uid,
                            created, access_modes=modes, csi=csi)]}


def make_pod(ns="team1", name="stuck-app", node="node-c",
             claims=("claim",), **kwargs):
    return {"apiVersion": "v1", "kind": "PodList",
            "items": [storage_pod(ns, name, node, list(claims), **kwargs)]}


def make_vas(items):
    return {"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachmentList",
            "items": items}


def diagnose(pods, pvcs, pvs, vas, ref="team1/stuck-app"):
    ns, name = ref.split("/", 1)
    return StorageDiagnoser(
        parse_storage_pods(pods), parse_pvcs(pvcs),
        parse_pvs(pvs), parse_vas(vas),
    ).diagnose(ns, name)


def by_claim(result):
    return {v["claim_name"]: v for v in result["volumes"]}


# --------------------------------------------------------------------------- #
# 时间戳解析
# --------------------------------------------------------------------------- #
def test_timestamp_parsing():
    assert _parse_timestamp("2026-11-18T08:00:00Z") == datetime(
        2026, 11, 18, 8, 0, tzinfo=timezone.utc)
    assert _parse_timestamp("2026-11-18T09:00:00+01:00") == datetime(
        2026, 11, 18, 8, 0, tzinfo=timezone.utc)
    assert _parse_timestamp("") is None
    assert _parse_timestamp(None) is None
    assert _parse_timestamp("not-a-time") is None


# --------------------------------------------------------------------------- #
# 排查范围：仅 CSI ReadWriteOnce 的 PVC 卷
# --------------------------------------------------------------------------- #
def test_non_csi_pv_out_of_scope():
    result = diagnose(
        make_pod(), make_pvc(),
        make_pv(csi=False), make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == OUT_OF_SCOPE
    assert "非 CSI" in vol["reasons"][0]


def test_non_rwo_pv_out_of_scope():
    result = diagnose(
        make_pod(), make_pvc(modes=("ReadOnlyMany",)),
        make_pv(modes=("ReadOnlyMany",)), make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == OUT_OF_SCOPE
    assert "ReadWriteOnce" in vol["reasons"][0]


def test_non_pvc_volume_out_of_scope():
    pods = make_pod(empty_dirs=1)
    result = diagnose(pods, make_pvc(), make_pv(), make_vas([]))
    statuses = {v["volume_name"]: v["status"] for v in result["volumes"]}
    scratch = next(v for v in result["volumes"] if v["volume_name"] == "scratch-0")
    assert scratch["status"] == OUT_OF_SCOPE
    assert scratch["claim_name"] is None
    assert "非 PVC 卷" in scratch["reasons"][0]


# --------------------------------------------------------------------------- #
# 待核实：PVC / PV / claimRef
# --------------------------------------------------------------------------- #
def test_missing_pvc_needs_verification():
    result = diagnose(
        make_pod(claims=("ghost-claim",)),
        {"items": []}, {"items": []}, make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert "找不到 PVC" in vol["reasons"][0]
    assert vol["pv_name"] is None


def test_unbound_pvc_needs_verification():
    pvcs = make_pvc()
    pvcs["items"][0]["spec"]["volumeName"] = ""
    result = diagnose(make_pod(), pvcs, make_pv(), make_vas([]))
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert "尚未绑定" in vol["reasons"][0]


def test_missing_pv_needs_verification():
    result = diagnose(
        make_pod(), make_pvc(volume="pv-gone"),
        {"items": []}, make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert "pv-gone" in vol["reasons"][0]


def test_claimref_uid_mismatch_needs_verification():
    result = diagnose(
        make_pod(), make_pvc(uid="uid-real"),
        make_pv(claim_uid="uid-someone-else"), make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("claimRef.uid" in r and "对不上" in r for r in vol["reasons"])


def test_claimref_name_mismatch_needs_verification():
    result = diagnose(
        make_pod(), make_pvc(name="claim"),
        make_pv(claim="other-claim", claim_uid="uid-pvc-1"),
        make_vas([]),
    )
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("并非 PVC" in r for r in vol["reasons"])


# --------------------------------------------------------------------------- #
# 待核实：VolumeAttachment 驱动 / 时间 / 同节点矛盾
# --------------------------------------------------------------------------- #
def test_va_driver_mismatch_needs_verification():
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", WRONG_DRIVER, True,
                          "2026-11-18T09:00:00Z")])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("驱动" in r and "不符" in r for r in vol["reasons"])


def test_va_older_than_pv_needs_verification():
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, True,
                          "2026-11-17T09:00:00Z")])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("早于 PV" in r for r in vol["reasons"])


def test_unparseable_timestamp_reported_not_silently_passed():
    pv = make_pv(created="not-a-time")
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, True,
                          "2026-11-18T09:00:00Z")])
    result = diagnose(make_pod(), make_pvc(), pv, vas)
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("无法核对" in r for r in vol["reasons"])


def test_same_node_attached_contradiction_needs_verification():
    vas = make_vas([
        mk_va("va-1", "pv-1", "node-a", DRIVER, True,
              "2026-11-18T09:00:00Z"),
        mk_va("va-2", "pv-1", "node-a", DRIVER, False,
              "2026-11-18T09:05:00Z"),
    ])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == NEEDS_VERIFICATION
    assert any("互相矛盾" in r and "node-a" in r for r in vol["reasons"])


def test_attached_false_records_are_not_contradiction_alone():
    # 两条都为 false 不构成 true/false 矛盾；且没有 attached=true => NO_ATTACHMENT
    vas = make_vas([
        mk_va("va-1", "pv-1", "node-a", DRIVER, False,
              "2026-11-18T09:00:00Z"),
        mk_va("va-2", "pv-1", "node-a", DRIVER, False,
              "2026-11-18T09:05:00Z"),
    ])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    assert result["volumes"][0]["status"] == NO_ATTACHMENT


# --------------------------------------------------------------------------- #
# 其他节点占用：含正在删除但 attached=true 的记录与占用 Pod
# --------------------------------------------------------------------------- #
def test_blocked_by_other_node_lists_attachments_and_pods():
    pods = make_pod()
    pods["items"].append(storage_pod("team1", "holder-1", "node-a", ["claim"]))
    pods["items"].append(storage_pod("team1", "holder-2", "node-a", ["claim"]))
    # 已结束 Pod 不应计入
    pods["items"].append(storage_pod(
        "team1", "holder-done", "node-a", ["claim"], phase="Succeeded"))
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, True,
                          "2026-11-18T09:00:00Z")])
    result = diagnose(pods, make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == BLOCKED_OTHER_NODE
    assert vol["target_attachment"] is None
    other = vol["other_node_attachments"]
    assert len(other) == 1 and other[0]["node"] == "node-a"
    blocking = {p["pod"] for p in other[0]["blocking_pods"]}
    assert blocking == {"team1/holder-1", "team1/holder-2"}


def test_deleting_va_with_attached_true_still_counts_as_blocking():
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, True,
                          "2026-11-18T09:00:00Z",
                          deleted_at="2026-11-20T01:00:00Z")])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == BLOCKED_OTHER_NODE
    assert vol["other_node_attachments"][0]["deleting"] is True


def test_deleted_or_detached_va_does_not_block():
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, False,
                          "2026-11-18T09:00:00Z",
                          deleted_at="2026-11-20T01:00:00Z")])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    assert result["volumes"][0]["status"] == NO_ATTACHMENT


def test_blocking_pods_scoped_to_same_pvc_and_node():
    # node-a 上引用别的 PVC 的 Pod、以及其他节点引用同一 PVC 的 Pod 都不应出现
    pods = make_pod()
    pods["items"].append(storage_pod("team1", "other-claim-pod", "node-a",
                                     ["claim-other"]))
    pods["items"].append(storage_pod("team1", "remote-holder", "node-d",
                                     ["claim"]))
    pods["items"].append(storage_pod("team1", "holder-1", "node-a",
                                     ["claim"]))
    vas = make_vas([mk_va("va-1", "pv-1", "node-a", DRIVER, True,
                          "2026-11-18T09:00:00Z")])
    result = diagnose(pods, make_pvc(), make_pv(), vas)
    blocking = result["volumes"][0]["other_node_attachments"][0]["blocking_pods"]
    assert [p["pod"] for p in blocking] == ["team1/holder-1"]


def test_multiple_other_nodes_all_listed():
    vas = make_vas([
        mk_va("va-a", "pv-1", "node-a", DRIVER, True,
              "2026-11-18T09:00:00Z"),
        mk_va("va-d", "pv-1", "node-d", DRIVER, True,
              "2026-11-19T09:00:00Z"),
    ])
    result = diagnose(make_pod(), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == BLOCKED_OTHER_NODE
    assert {a["node"] for a in vol["other_node_attachments"]} == {
        "node-a", "node-d"}


# --------------------------------------------------------------------------- #
# 无其他节点挂接：区分目标节点已挂接 / 尚无挂接记录
# --------------------------------------------------------------------------- #
def test_attached_on_target_node():
    vas = make_vas([mk_va("va-1", "pv-1", "node-c", DRIVER, True,
                          "2026-11-18T09:00:00Z")])
    result = diagnose(make_pod(node="node-c"), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == ATTACHED_ON_TARGET
    assert vol["target_attachment"]["node"] == "node-c"
    assert vol["target_attachment"]["volume_attachments"][0]["attached"] is True
    assert vol["other_node_attachments"] == []


def test_no_attachment_anywhere():
    result = diagnose(make_pod(), make_pvc(), make_pv(), make_vas([]))
    vol = result["volumes"][0]
    assert vol["status"] == NO_ATTACHMENT
    assert vol["target_attachment"] is None
    assert vol["other_node_attachments"] == []


def test_target_attached_plus_other_node_still_blocked():
    vas = make_vas([
        mk_va("va-c", "pv-1", "node-c", DRIVER, True,
              "2026-11-20T09:00:00Z"),
        mk_va("va-a", "pv-1", "node-a", DRIVER, True,
              "2026-11-18T09:00:00Z"),
    ])
    result = diagnose(make_pod(node="node-c"), make_pvc(), make_pv(), vas)
    vol = result["volumes"][0]
    assert vol["status"] == BLOCKED_OTHER_NODE
    assert vol["target_attachment"] is not None  # 目标节点的挂接也如实展示


# --------------------------------------------------------------------------- #
# 概览与汇总
# --------------------------------------------------------------------------- #
def test_overview_marks_scope_and_unknown_pvc_still_selectable():
    # 找不到 PVC 的卷仍标记 in_scope（属需挂接卷，不能静默跳过）
    pods = make_pod(claims=("ghost",))
    diagnoser = StorageDiagnoser(
        parse_storage_pods(pods), parse_pvcs({"items": []}),
        parse_pvs({"items": []}), parse_vas({"items": []}),
    )
    view = diagnoser.overview()
    assert view[0]["pvc_count"] == 1
    assert view[0]["in_scope_count"] == 1
    assert view[0]["pvc_volumes"][0]["in_scope"] is True


def test_summary_counts_every_status_in_sample_scenario():
    pods, pvcs, pvs, vas = storage_scenario()
    diagnoser = StorageDiagnoser(
        parse_storage_pods(pods), parse_pvcs(pvcs),
        parse_pvs(pvs), parse_vas(vas),
    )
    overview = {p["ref"]: p for p in diagnoser.overview()}
    assert overview["team1/stuck-app"]["in_scope_count"] == 9
    assert overview["team1/worker-0"]["in_scope_count"] == 2

    result = diagnoser.diagnose("team1", "stuck-app")
    volumes = by_claim(result)
    expected = {
        "claim-healthy": BLOCKED_OTHER_NODE,
        "claim-deleting": BLOCKED_OTHER_NODE,
        "claim-consumers": BLOCKED_OTHER_NODE,
        "claim-claimref": NEEDS_VERIFICATION,
        "claim-driver": NEEDS_VERIFICATION,
        "claim-stale": NEEDS_VERIFICATION,
        "claim-contradict": NEEDS_VERIFICATION,
        "claim-missing-pv": NEEDS_VERIFICATION,
        "claim-missing-pvc": NEEDS_VERIFICATION,
        "claim-nfs": OUT_OF_SCOPE,
    }
    for claim, status in expected.items():
        assert volumes[claim]["status"] == status, claim
    # scratch-0 也在卷列表里
    assert result["summary"]["total"] == 11
    assert result["summary"][NEEDS_VERIFICATION] == 6
    assert result["summary"][BLOCKED_OTHER_NODE] == 3
    assert result["summary"][OUT_OF_SCOPE] == 2

    # healthy 卷同时挂在 node-a 与 node-d，两个节点都列出
    healthy = volumes["claim-healthy"]
    assert {a["node"] for a in healthy["other_node_attachments"]} == {
        "node-a", "node-d"}
    # node-d 上的 standby-1 是占用 Pod
    node_d = next(a for a in healthy["other_node_attachments"]
                  if a["node"] == "node-d")
    assert [p["pod"] for p in node_d["blocking_pods"]] == ["team1/standby-1"]

    # consumers 卷的占用 Pod 不含已结束的 consumer-done
    consumers = volumes["claim-consumers"]
    node_a = next(a for a in consumers["other_node_attachments"]
                  if a["node"] == "node-a")
    assert {p["pod"] for p in node_a["blocking_pods"]} == {
        "team1/consumer-1", "team1/consumer-2"}

    worker = diagnoser.diagnose("team1", "worker-0")
    wv = by_claim(worker)
    assert wv["claim-attached"]["status"] == ATTACHED_ON_TARGET
    assert wv["claim-none"]["status"] == NO_ATTACHMENT


def test_diagnose_unknown_pod_raises():
    diagnoser = StorageDiagnoser([], [], [], [])
    with pytest.raises(ValueError):
        diagnoser.diagnose("team1", "ghost")


# --------------------------------------------------------------------------- #
# HTTP 接口
# --------------------------------------------------------------------------- #
STORAGE_DIR = SAMPLES / "storage"


@contextlib.contextmanager
def _storage_files():
    mapping = {
        "pod_list": "pods.json",
        "pvc_list": "pvcs.json",
        "pv_list": "pvs.json",
        "va_list": "volume-attachments.json",
    }
    handles = [open(STORAGE_DIR / filename, "rb")
               for filename in mapping.values()]
    try:
        yield {
            field_name: (filename, fh, "application/json")
            for (field_name, filename), fh in zip(mapping.items(), handles)
        }
    finally:
        for fh in handles:
            fh.close()


def test_http_storage_overview():
    with _storage_files() as files:
        resp = client.post("/api/storage/overview", files=files)
    assert resp.status_code == 200
    refs = {p["ref"]: p for p in resp.json()["pods"]}
    assert refs["team1/stuck-app"]["in_scope_count"] == 9
    assert refs["team1/stuck-app"]["pvc_count"] == 10


def test_http_storage_diagnose_sample():
    with _storage_files() as files:
        resp = client.post(
            "/api/storage/diagnose",
            files=files, data={"pod_ref": "team1/stuck-app"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["pod"] == "team1/stuck-app"
    assert data["target_node"] == "node-c"
    statuses = {v["claim_name"]: v["status"] for v in data["volumes"]
                if v["claim_name"]}
    assert statuses["claim-claimref"] == NEEDS_VERIFICATION
    assert statuses["claim-healthy"] == BLOCKED_OTHER_NODE
    assert statuses["claim-nfs"] == OUT_OF_SCOPE


def test_http_storage_diagnose_json_endpoint():
    body = {
        "pod_list": json.loads((STORAGE_DIR / "pods.json").read_text()),
        "pvc_list": json.loads((STORAGE_DIR / "pvcs.json").read_text()),
        "pv_list": json.loads((STORAGE_DIR / "pvs.json").read_text()),
        "va_list": json.loads(
            (STORAGE_DIR / "volume-attachments.json").read_text()),
        "pod_ref": "team1/worker-0",
    }
    resp = client.post("/api/storage/diagnose-json", json=body)
    assert resp.status_code == 200
    statuses = {v["claim_name"]: v["status"] for v in resp.json()["volumes"]}
    assert statuses["claim-attached"] == ATTACHED_ON_TARGET
    assert statuses["claim-none"] == NO_ATTACHMENT


def test_http_storage_missing_file_rejected():
    with open(STORAGE_DIR / "pods.json", "rb") as fh:
        resp = client.post(
            "/api/storage/overview",
            files={"pod_list": ("pods.json", fh, "application/json")},
        )
    assert resp.status_code == 400
    assert "PersistentVolumeClaimList" in resp.json()["detail"]


def test_http_storage_bad_json_rejected():
    resp = client.post(
        "/api/storage/overview",
        files={
            "pod_list": ("p.json", b'{"items":[]}', "application/json"),
            "pvc_list": ("c.json", b"{bad", "application/json"),
            "pv_list": ("v.json", b'{"items":[]}', "application/json"),
            "va_list": ("a.json", b'{"items":[]}', "application/json"),
        },
    )
    assert resp.status_code == 400
    assert "不是合法 JSON" in resp.json()["detail"]


def test_http_storage_bad_pod_ref_rejected():
    with _storage_files() as files:
        resp = client.post(
            "/api/storage/diagnose",
            files=files, data={"pod_ref": "no-slash-ref"},
        )
    assert resp.status_code == 400
    assert "命名空间/Pod名称" in resp.json()["detail"]


def test_http_storage_unknown_pod_rejected():
    with _storage_files() as files:
        resp = client.post(
            "/api/storage/diagnose",
            files=files, data={"pod_ref": "team1/ghost-pod"},
        )
    assert resp.status_code == 400
    assert "不在 PodList" in resp.json()["detail"]


def test_http_storage_rejects_non_list_payload():
    payload = b'{"not-items": true}'
    resp = client.post(
        "/api/storage/overview",
        files={k: (f"{k}.json", payload, "application/json")
               for k in ("pod_list", "pvc_list", "pv_list", "va_list")},
    )
    assert resp.status_code == 400
    assert "items" in resp.json()["detail"]


def test_index_page_contains_storage_entry():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "CSI 卷挂载排查" in resp.text
    assert "/api/storage/diagnose" in resp.text
