#!/usr/bin/env python3
"""生成试算器的示例快照（仅用标准库）。

场景：
  feasible/            可迁移：资源 + nodeSelector + hostname 双向反亲和均满足
  infeasible-resource/ 不可迁移：承接节点总资源放不下全部待迁 Pod
  infeasible-anti/     不可迁移：双向硬反亲和导致无完整方案
  unsupported/         不支持的约束：多容器、卷、init 容器、污点节点
  storage/             CSI RWO 卷挂载排查：Pod/PVC/PV/VolumeAttachment 四类清单
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def node(name, cpu, mem_mi, labels=None, taints=None, ready=True,
         unschedulable=False):
    conditions = [{"type": "Ready", "status": "True" if ready else "False",
                   "reason": "KubeletReady"}]
    return {
        "apiVersion": "v1", "kind": "Node",
        "metadata": {"name": name, "labels": {"kubernetes.io/hostname": name,
                                              **(labels or {})}},
        "spec": {
            "taints": taints or [],
            "unschedulable": unschedulable,
        },
        "status": {
            "conditions": conditions,
            "allocatable": {"cpu": cpu, "memory": f"{mem_mi}Mi"},
        },
    }


def pod(namespace, name, node_name, cpu_m, mem_mi, labels=None,
        node_selector=None, anti_labels=None, containers=1,
        init_containers=0, volumes=0, phase="Running", owner=None,
        mirror=False):
    labels = labels or {}
    spec: dict = {
        "nodeName": node_name,
        "containers": [
            {"name": f"{name}-c{i}",
             "resources": {"requests": {"cpu": f"{cpu_m}m",
                                        "memory": f"{mem_mi}Mi"}}}
            for i in range(containers)
        ],
    }
    if node_selector:
        spec["nodeSelector"] = node_selector
    if init_containers:
        spec["initContainers"] = [
            {"name": f"{name}-init{i}", "image": "busybox"}
            for i in range(init_containers)
        ]
    if volumes:
        spec["volumes"] = [{"name": f"vol{i}", "emptyDir": {}}
                           for i in range(volumes)]
    if anti_labels:
        spec["affinity"] = {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {"topologyKey": "kubernetes.io/hostname",
                     "labelSelector": {"matchLabels": anti_labels}}
                ]
            }
        }
    metadata: dict = {"name": name, "namespace": namespace, "labels": labels}
    if owner:
        kind = owner[0]
        metadata["ownerReferences"] = [
            {"apiVersion": "apps/v1", "kind": kind, "name": owner[1],
             "uid": f"{name}-owner"}
        ]
    if mirror:
        metadata["annotations"] = {"kubernetes.io/config.mirror": "abc123"}
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": metadata, "spec": spec,
        "status": {"phase": phase},
    }


def node_list(items):
    return {"apiVersion": "v1", "kind": "NodeList", "items": items}


def pod_list(items):
    return {"apiVersion": "v1", "kind": "PodList", "items": items}


# --------------------------------------------------------------------------- #
# 场景 1：可迁移
# --------------------------------------------------------------------------- #
def feasible():
    nodes = node_list([
        node("node-a", "4", 4096, {"workload": "app", "zone": "z1"}),
        node("node-b", "4", 4096, {"workload": "app", "zone": "z1"}),
        node("node-c", "4", 4096, {"workload": "app", "zone": "z2"}),
        node("node-d", "4", 4096, {"workload": "batch", "zone": "z2"}),
        node("node-e", "4", 4096, {"workload": "app", "zone": "z3"}),
    ])
    pods = pod_list([
        # 承接节点上的常驻 Pod（占用资源，参与反亲和）
        pod("team1", "redis-0", "node-c", 1000, 1024, {"app": "redis"}),
        pod("team1", "queue-0", "node-d", 500, 512, {"app": "queue"}),
        pod("team1", "metrics-0", "node-e", 500, 512, {"app": "metrics"}),
        # 已结束 Pod：不占资源
        pod("team1", "job-done", "node-c", 2000, 2048, phase="Succeeded"),
        # node-a / node-b 上待迁的 5 个 Pod
        pod("team1", "web-1", "node-a", 500, 512,
            labels={"app": "web", "tier": "frontend"},
            anti_labels={"app": "web"}),
        pod("team1", "web-2", "node-a", 500, 512,
            labels={"app": "web", "tier": "frontend"},
            anti_labels={"app": "web"}),
        pod("team1", "worker-1", "node-a", 1000, 1024,
            labels={"app": "worker"},
            node_selector={"workload": "app"}),
        pod("team1", "cache-1", "node-b", 500, 512,
            labels={"app": "cache"}),
        # DaemonSet Pod：drain 不迁移，应出现在 excluded_pods
        pod("kube-system", "fluentd-node-a", "node-a", 100, 64,
            labels={"app": "fluentd"}, owner=("DaemonSet", "fluentd")),
    ])
    return nodes, pods, ["node-a", "node-b"]


# --------------------------------------------------------------------------- #
# 场景 2：资源放不下（总量超配 + 单节点都放不下的大 Pod）
# --------------------------------------------------------------------------- #
def infeasible_resource():
    nodes = node_list([
        node("drain-1", "4", 4096, {"workload": "app"}),
        node("drain-2", "4", 4096, {"workload": "app"}),
        node("drain-3", "4", 4096, {"workload": "app"}),
        node("node-c", "4", 4096, {"workload": "app"}),
        node("node-d", "4", 4096, {"workload": "app"}),
    ])
    pods = pod_list([
        # 承接节点已占用 3.5C/3.5Gi，每个仅剩 500m/512Mi
        pod("team1", "resident-c", "node-c", 3500, 3584, {"app": "resident"}),
        pod("team1", "resident-d", "node-d", 3500, 3584, {"app": "resident"}),
        pod("team1", "big-1", "drain-1", 2000, 2048, {"app": "big"}),
        pod("team1", "small-1", "drain-2", 500, 512, {"app": "small"}),
        pod("team1", "small-2", "drain-3", 500, 512, {"app": "small"}),
    ])
    return nodes, pods, ["drain-1", "drain-2", "drain-3"]


# --------------------------------------------------------------------------- #
# 场景 3：双向硬反亲和无解
# --------------------------------------------------------------------------- #
def infeasible_anti():
    nodes = node_list([
        node("drain-1", "4", 4096, {"workload": "app"}),
        node("drain-2", "4", 4096, {"workload": "app"}),
        node("node-c", "4", 4096, {"workload": "app"}),
        node("node-d", "4", 4096, {"workload": "app"}),
        # node-e 不 Ready：不能作为承接节点
        node("node-e", "4", 4096, {"workload": "app"}, ready=False),
    ])
    pods = pod_list([
        # node-c 上已有一个 web：web-1 / web-2 都不能去 node-c
        pod("team1", "web-0", "node-c", 200, 256,
            labels={"app": "web"}, anti_labels={"app": "web"}),
        # 两个待迁 web 互相反亲和，且都不能去 node-c；只剩 node-d 一个节点 => 无解
        pod("team1", "web-1", "drain-1", 300, 256,
            labels={"app": "web"}, anti_labels={"app": "web"}),
        pod("team1", "web-2", "drain-2", 300, 256,
            labels={"app": "web"}, anti_labels={"app": "web"}),
    ])
    return nodes, pods, ["drain-1", "drain-2"]


# --------------------------------------------------------------------------- #
# 场景 4：不支持的约束 / 结构
# --------------------------------------------------------------------------- #
def unsupported():
    nodes = node_list([
        node("drain-1", "4", 4096, {"workload": "app"}),
        node("drain-tainted", "4", 4096,
             taints=[{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]),
        node("node-c", "4", 4096, {"workload": "app"}),
    ])
    pods = pod_list([
        # 多容器 + 卷：结构不支持
        pod("team1", "legacy-app", "drain-1", 200, 256,
            labels={"app": "legacy"}, containers=2, volumes=1),
        # 带 init 容器：结构不支持
        pod("team1", "init-app", "drain-1", 200, 256,
            labels={"app": "initapp"}, init_containers=1),
        # 普通 Pod：本身合规，但勾选了污点节点时整次请求会被拒绝
        pod("team1", "plain-app", "drain-tainted", 200, 256,
            labels={"app": "plain"}),
    ])
    return nodes, pods, ["drain-1"]


SCENARIOS = {
    "feasible": (feasible, "可迁移：完整方案可求"),
    "infeasible-resource": (infeasible_resource, "不可迁移：资源超配"),
    "infeasible-anti": (infeasible_anti, "不可迁移：反亲和无解"),
    "unsupported": (unsupported, "不支持：多容器/卷/init/污点"),
}


# --------------------------------------------------------------------------- #
# 场景 5：CSI ReadWriteOnce 卷挂载排查
# --------------------------------------------------------------------------- #
def pvc(namespace, name, uid, volume_name, phase="Bound",
        access_modes=("ReadWriteOnce",)):
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace, "uid": uid},
        "spec": {
            "volumeName": volume_name,
            "accessModes": list(access_modes),
            "resources": {"requests": {"storage": "10Gi"}},
        },
        "status": {"phase": phase},
    }


def pv(name, uid, driver, claim_namespace, claim_name, claim_uid,
       created, access_modes=("ReadWriteOnce",), csi=True):
    spec: dict = {"accessModes": list(access_modes)}
    if csi:
        spec["csi"] = {"driver": driver, "volumeHandle": f"handle-{name}"}
    else:
        spec["nfs"] = {"server": "nfs.example.com", "path": f"/export/{name}"}
    if claim_name is not None:
        spec["claimRef"] = {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "namespace": claim_namespace, "name": claim_name, "uid": claim_uid,
        }
    return {
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": name, "uid": uid,
                     "creationTimestamp": created},
        "spec": spec,
    }


def va(name, pv_name, node, driver, attached, created,
       deleted_at=None, attach_name=None):
    metadata: dict = {"name": name, "creationTimestamp": created}
    if deleted_at:
        metadata["deletionTimestamp"] = deleted_at
    return {
        "apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachment",
        "metadata": metadata,
        "spec": {
            "attacher": driver,
            "nodeName": node,
            "source": {"persistentVolumeName": pv_name},
        },
        "status": {"attached": attached},
    }


def storage_pod(namespace, name, node_name, claims, phase="Running",
                empty_dirs=0):
    volumes = [{"name": f"vol-{c}",
                "persistentVolumeClaim": {"claimName": c}} for c in claims]
    volumes += [{"name": f"scratch-{i}", "emptyDir": {}}
                for i in range(empty_dirs)]
    return {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {"app": name}},
        "spec": {
            "nodeName": node_name,
            "containers": [{"name": "main", "image": "registry/app:1",
                            "volumeMounts": [{"name": v["name"],
                                              "mountPath": f"/mnt/{v['name']}"}
                                             for v in volumes]}],
            "volumes": volumes,
        },
        "status": {"phase": phase},
    }


def storage_scenario():
    """覆盖排查引擎全部结论分支的演示数据。

    目标 Pod：team1/stuck-app（目标节点 node-c，原节点 node-a）
    另有 team1/worker-0（node-b）演示无其他节点挂接的两种情况。
    """
    ns = "team1"
    driver = "csi-driver.example.com"
    other_driver = "csi-wrong.example.com"

    # ---- PVC（10 个，均 RWO）----
    pvcs = [
        pvc(ns, "claim-healthy", "uid-pvc-healthy", "pv-healthy"),
        pvc(ns, "claim-deleting", "uid-pvc-deleting", "pv-deleting"),
        pvc(ns, "claim-consumers", "uid-pvc-consumers", "pv-consumers"),
        pvc(ns, "claim-claimref", "uid-pvc-claimref", "pv-claimref"),
        pvc(ns, "claim-driver", "uid-pvc-driver", "pv-driver"),
        pvc(ns, "claim-stale", "uid-pvc-stale", "pv-stale"),
        pvc(ns, "claim-contradict", "uid-pvc-contradict", "pv-contradict"),
        pvc(ns, "claim-missing-pv", "uid-pvc-missing", "pv-gone"),
        # claim-missing-pvc 故意不放入 PVCList：Pod 引用了快照中不存在的 PVC
        pvc(ns, "claim-nfs", "uid-pvc-nfs", "pv-nfs",
            access_modes=("ReadOnlyMany",)),
        # worker-0 使用的两个 PVC
        pvc(ns, "claim-attached", "uid-pvc-attached", "pv-attached"),
        pvc(ns, "claim-none", "uid-pvc-none", "pv-none"),
    ]

    # ---- PV（pv-gone 在 PVList 中故意缺失；pv-m 仅被 PVC 引用）----
    pvs = [
        pv("pv-healthy", "uid-pv-healthy", driver, ns, "claim-healthy",
           "uid-pvc-healthy", "2026-11-18T08:00:00Z"),
        pv("pv-deleting", "uid-pv-deleting", driver, ns, "claim-deleting",
           "uid-pvc-deleting", "2026-11-18T08:00:00Z"),
        pv("pv-consumers", "uid-pv-consumers", driver, ns, "claim-consumers",
           "uid-pvc-consumers", "2026-11-18T08:00:00Z"),
        # claimRef UID 指向另一个 PVC 的 UID：绑定对不上
        pv("pv-claimref", "uid-pv-claimref", driver, ns, "claim-claimref",
           "uid-pvc-OTHER", "2026-11-18T08:00:00Z"),
        pv("pv-driver", "uid-pv-driver", driver, ns, "claim-driver",
           "uid-pvc-driver", "2026-11-18T08:00:00Z"),
        pv("pv-stale", "uid-pv-stale", driver, ns, "claim-stale",
           "uid-pvc-stale", "2026-11-19T10:00:00Z"),
        pv("pv-contradict", "uid-pv-contradict", driver, ns,
           "claim-contradict", "uid-pvc-contradict", "2026-11-18T08:00:00Z"),
        pv("pv-m", "uid-pv-m", driver, ns, "claim-missing-pvc",
           "uid-pvc-m", "2026-11-18T08:00:00Z"),
        # 非 CSI（NFS）：范围外
        pv("pv-nfs", "uid-pv-nfs", driver, ns, "claim-nfs",
           "uid-pvc-nfs", "2026-11-18T08:00:00Z",
           access_modes=("ReadOnlyMany",), csi=False),
        # worker-0 的两个卷
        pv("pv-attached", "uid-pv-attached", driver, ns, "claim-attached",
           "uid-pvc-attached", "2026-11-18T08:00:00Z"),
        pv("pv-none", "uid-pv-none", driver, ns, "claim-none",
           "uid-pvc-none", "2026-11-18T08:00:00Z"),
    ]

    # ---- VolumeAttachment ----
    vas = [
        # healthy：RWO 挂在 node-a（旧节点），目标节点 node-c 未挂接
        va("va-healthy-a", "pv-healthy", "node-a", driver, True,
           "2026-11-18T09:00:00Z"),
        # deleting：挂接记录正在删除但 attached 仍为 true，仍计入占用
        va("va-deleting-a", "pv-deleting", "node-a", driver, True,
           "2026-11-18T09:00:00Z", deleted_at="2026-11-20T01:00:00Z"),
        # consumers：旧节点 node-a 上还有两个未结束 Pod 引用同一 PVC
        va("va-consumers-a", "pv-consumers", "node-a", driver, True,
           "2026-11-18T09:00:00Z"),
        # driver：挂接记录驱动与 PV 驱动不符 -> 待核实
        va("va-driver-a", "pv-driver", "node-a", other_driver, True,
           "2026-11-18T09:00:00Z"),
        # stale：VA 创建于 2026-11-17，早于 PV 创建（11-19）-> 待核实
        va("va-stale-a", "pv-stale", "node-a", driver, True,
           "2026-11-17T09:00:00Z"),
        # contradict：同节点 node-a 两条 VA，attached 一真一假 -> 待核实
        va("va-contradict-a-1", "pv-contradict", "node-a", driver, True,
           "2026-11-18T09:00:00Z"),
        va("va-contradict-a-2", "pv-contradict", "node-a", driver, False,
           "2026-11-18T09:05:00Z"),
        # attached：目标节点 node-b 自己已挂接
        va("va-attached-b", "pv-attached", "node-b", driver, True,
           "2026-11-18T09:00:00Z"),
        # pv-none：故意没有任何 VA -> 尚无挂接记录
    ]

    pods = [
        storage_pod(ns, "stuck-app", "node-c", [
            "claim-healthy", "claim-deleting", "claim-consumers",
            "claim-claimref", "claim-driver", "claim-stale",
            "claim-contradict", "claim-missing-pv", "claim-missing-pvc",
            "claim-nfs",
        ], empty_dirs=1),
        # worker-0 在 node-b：一个卷目标节点已挂接，一个卷完全没有挂接记录
        storage_pod(ns, "worker-0", "node-b",
                    ["claim-attached", "claim-none"]),
        # node-a 上引用 claim-consumers 的两个未结束 Pod（占用方）
        storage_pod(ns, "consumer-1", "node-a", ["claim-consumers"]),
        storage_pod(ns, "consumer-2", "node-a", ["claim-consumers"]),
        # 已结束 Pod 引用同一 PVC：不应出现在 blocking_pods 中
        storage_pod(ns, "consumer-done", "node-a", ["claim-consumers"],
                    phase="Succeeded"),
        # node-d 上引用 claim-healthy 的未结束 Pod：另一占用节点
        storage_pod(ns, "standby-1", "node-d", ["claim-healthy"]),
        # 给 pv-healthy 补一条 node-d 的挂接记录
    ]
    vas.append(va("va-healthy-d", "pv-healthy", "node-d", driver, True,
                  "2026-11-19T09:00:00Z"))

    return (
        {"apiVersion": "v1", "kind": "PodList", "items": pods},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaimList",
         "items": pvcs},
        {"apiVersion": "v1", "kind": "PersistentVolumeList", "items": pvs},
        {"apiVersion": "storage.k8s.io/v1", "kind": "VolumeAttachmentList",
         "items": vas},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成试算器示例快照")
    parser.add_argument("out_dir", type=Path, help="输出根目录")
    args = parser.parse_args()

    for scenario, (builder, _desc) in SCENARIOS.items():
        nodes, pods, drains = builder()
        out = args.out_dir / scenario
        out.mkdir(parents=True, exist_ok=True)
        (out / "nodes.json").write_text(
            json.dumps(nodes, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "pods.json").write_text(
            json.dumps(pods, indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "drained-nodes.json").write_text(
            json.dumps(drains, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"generated {scenario}: {len(nodes['items'])} nodes, "
              f"{len(pods['items'])} pods, drains={drains}")

    storage_out = args.out_dir / "storage"
    storage_out.mkdir(parents=True, exist_ok=True)
    spods, spvcs, spvs, svas = storage_scenario()
    for filename, payload in (
        ("pods.json", spods),
        ("pvcs.json", spvcs),
        ("pvs.json", spvs),
        ("volume-attachments.json", svas),
    ):
        (storage_out / filename).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"generated storage: {len(spods['items'])} pods, "
        f"{len(spvcs['items'])} pvcs, {len(spvs['items'])} pvs, "
        f"{len(svas['items'])} volumeAttachments"
    )


if __name__ == "__main__":
    main()
