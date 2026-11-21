"""CSI ReadWriteOnce 卷挂载排查。

解析同一批 PodList / PersistentVolumeClaimList / PersistentVolumeList /
VolumeAttachmentList 快照，针对选中 Pod 的已知需挂接 CSI RWO 卷做静态核对：

1. 按 Pod 命名空间找 PVC，再核对 PVC 所指 PV 的 claimRef 是否指回该 PVC UID；
2. 引用该 PV 名称的 VolumeAttachment 若驱动与 PV 不符、创建时间早于 PV，
   或同一节点上 attached 状态互相矛盾，该卷标为“待核实”；
3. 其余卷列出 attached=true 的其他节点（含正在删除但 attached 仍为 true
   的挂接记录），以及这些节点上尚未结束且引用同一 PVC 的 Pod；
4. 没有其他节点挂接时，区分“目标节点已挂接”与“尚无已挂接记录”。

只依赖标准库，所有输入均视为不可信数据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

ENDED_PHASES = {"Succeeded", "Failed"}
RWO_ACCESS_MODE = "ReadWriteOnce"

# 排查结论状态
NEEDS_VERIFICATION = "needs_verification"          # 待核实
BLOCKED_OTHER_NODE = "blocked_by_other_nodes"      # 被其他节点挂接占用
ATTACHED_ON_TARGET = "attached_on_target"          # 目标节点已挂接
NO_ATTACHMENT = "no_attachment"                    # 尚无已挂接记录
OUT_OF_SCOPE = "out_of_scope"                      # 非 CSI RWO，不在排查范围


def _items(payload: Any) -> list[dict]:
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        return [x for x in payload["items"] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    raise ValueError("快照既不是 Kubernetes List 对象（含 items），也不是数组")


def _parse_timestamp(value: Any) -> datetime | None:
    """把 Kubernetes RFC3339 时间解析为带时区 datetime；无法解析返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# 领域模型
# --------------------------------------------------------------------------- #
@dataclass
class PvcVolume:
    """Pod spec.volumes 中一条 persistentVolumeClaim 引用。"""
    volume_name: str
    claim_name: str


@dataclass
class PodStorageInfo:
    namespace: str
    name: str
    node: str
    phase: str
    active: bool
    pvc_volumes: list[PvcVolume] = field(default_factory=list)
    other_volume_names: list[str] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.namespace}/{self.name}"


@dataclass
class PvcInfo:
    namespace: str
    name: str
    uid: str
    phase: str
    volume_name: str
    access_modes: list[str]


@dataclass
class PvInfo:
    name: str
    uid: str
    is_csi: bool
    driver: str
    access_modes: list[str]
    claim_namespace: str
    claim_name: str
    claim_uid: str
    creation_timestamp: str


@dataclass
class VaInfo:
    name: str
    pv_name: str
    node: str
    driver: str
    attached: bool
    creation_timestamp: str
    deletion_timestamp: str | None

    @property
    def deleting(self) -> bool:
        return bool(self.deletion_timestamp)


def parse_storage_pods(payload: Any) -> list[PodStorageInfo]:
    pods: list[PodStorageInfo] = []
    for raw in _items(payload):
        metadata = raw.get("metadata") or {}
        spec = raw.get("spec") or {}
        status = raw.get("status") or {}
        namespace = metadata.get("namespace") or "default"
        name = metadata.get("name") or "<unknown>"
        phase = status.get("phase") or "Pending"

        pvc_volumes: list[PvcVolume] = []
        other_volume_names: list[str] = []
        for vol in spec.get("volumes") or []:
            if not isinstance(vol, dict):
                continue
            volume_name = vol.get("name") or "<unknown>"
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                pvc_volumes.append(PvcVolume(volume_name=volume_name,
                                             claim_name=str(claim)))
            else:
                other_volume_names.append(volume_name)

        pods.append(PodStorageInfo(
            namespace=namespace,
            name=name,
            node=spec.get("nodeName") or "",
            phase=phase,
            active=phase not in ENDED_PHASES,
            pvc_volumes=pvc_volumes,
            other_volume_names=other_volume_names,
        ))
    return pods


def parse_pvcs(payload: Any) -> list[PvcInfo]:
    pvcs: list[PvcInfo] = []
    for raw in _items(payload):
        metadata = raw.get("metadata") or {}
        spec = raw.get("spec") or {}
        status = raw.get("status") or {}
        access_modes = spec.get("accessModes") or []
        pvcs.append(PvcInfo(
            namespace=metadata.get("namespace") or "default",
            name=metadata.get("name") or "<unknown>",
            uid=metadata.get("uid") or "",
            phase=status.get("phase") or "",
            volume_name=spec.get("volumeName") or "",
            access_modes=[str(m) for m in access_modes],
        ))
    return pvcs


def parse_pvs(payload: Any) -> list[PvInfo]:
    pvs: list[PvInfo] = []
    for raw in _items(payload):
        metadata = raw.get("metadata") or {}
        spec = raw.get("spec") or {}
        csi = spec.get("csi")
        claim_ref = spec.get("claimRef") or {}
        access_modes = spec.get("accessModes") or []
        pvs.append(PvInfo(
            name=metadata.get("name") or "<unknown>",
            uid=metadata.get("uid") or "",
            is_csi=isinstance(csi, dict),
            driver=(csi or {}).get("driver") or "",
            access_modes=[str(m) for m in access_modes],
            claim_namespace=claim_ref.get("namespace") or "",
            claim_name=claim_ref.get("name") or "",
            claim_uid=claim_ref.get("uid") or "",
            creation_timestamp=metadata.get("creationTimestamp") or "",
        ))
    return pvs


def parse_vas(payload: Any) -> list[VaInfo]:
    vas: list[VaInfo] = []
    for raw in _items(payload):
        metadata = raw.get("metadata") or {}
        spec = raw.get("spec") or {}
        status = raw.get("status") or {}
        source = spec.get("source") or {}
        vas.append(VaInfo(
            name=metadata.get("name") or "<unknown>",
            pv_name=source.get("persistentVolumeName") or "",
            node=spec.get("nodeName") or "",
            driver=spec.get("attacher") or "",
            attached=bool(status.get("attached")),
            creation_timestamp=metadata.get("creationTimestamp") or "",
            deletion_timestamp=metadata.get("deletionTimestamp"),
        ))
    return vas


# --------------------------------------------------------------------------- #
# 排查引擎
# --------------------------------------------------------------------------- #
class StorageDiagnoser:
    def __init__(
        self,
        pods: list[PodStorageInfo],
        pvcs: list[PvcInfo],
        pvs: list[PvInfo],
        vas: list[VaInfo],
    ):
        self.pods = pods
        self.pod_index = {(p.namespace, p.name): p for p in pods}
        self.pvc_index = {(p.namespace, p.name): p for p in pvcs}
        self.pv_index = {p.name: p for p in pvs}
        self.vas = vas
        # PVC -> 仍在运行（未结束）且引用该 PVC 的 Pod
        self.pvc_consumers: dict[tuple[str, str], list[PodStorageInfo]] = {}
        for pod in pods:
            if not pod.active:
                continue
            for vol in pod.pvc_volumes:
                self.pvc_consumers.setdefault(
                    (pod.namespace, vol.claim_name), []
                ).append(pod)

    def overview(self) -> list[dict]:
        """供前端选择 Pod 的概览，附带每个 PVC 卷是否落在 CSI RWO 排查范围。"""
        view = []
        for pod in sorted(self.pods, key=lambda p: (p.node, p.namespace, p.name)):
            volumes = [
                self._volume_overview_entry(pod.namespace, vol)
                for vol in pod.pvc_volumes
            ]
            view.append({
                "ref": pod.ref,
                "namespace": pod.namespace,
                "name": pod.name,
                "node": pod.node,
                "phase": pod.phase,
                "active": pod.active,
                "pvc_volumes": volumes,
                "pvc_count": len(pod.pvc_volumes),
                "in_scope_count": sum(1 for v in volumes if v["in_scope"]),
            })
        return view

    def _volume_overview_entry(self, namespace: str, vol: PvcVolume) -> dict:
        entry = {
            "volume_name": vol.volume_name,
            "claim_name": vol.claim_name,
            "in_scope": False,
            "note": "",
        }
        pvc = self.pvc_index.get((namespace, vol.claim_name))
        if pvc is None:
            entry["note"] = "PVC 在快照中找不到，排查时将标为待核实"
            entry["in_scope"] = True  # 属需挂接卷，只是数据对不上，不能静默跳过
            return entry
        if not pvc.volume_name:
            entry["note"] = "PVC 尚未绑定 PV，排查时将标为待核实"
            entry["in_scope"] = True
            return entry
        pv = self.pv_index.get(pvc.volume_name)
        if pv is None:
            entry["note"] = f"绑定的 PV {pvc.volume_name} 在快照中找不到，排查时将标为待核实"
            entry["in_scope"] = True
            return entry
        if not pv.is_csi:
            entry["note"] = f"PV {pv.name} 不是 CSI 卷，不在排查范围"
            return entry
        if RWO_ACCESS_MODE not in pv.access_modes:
            entry["note"] = f"PV {pv.name} 访问模式 {pv.access_modes} 不含 RWO，不在排查范围"
            return entry
        entry["in_scope"] = True
        entry["note"] = f"CSI RWO 卷，PV {pv.name}（驱动 {pv.driver}）"
        return entry

    def diagnose(self, namespace: str, name: str) -> dict:
        pod = self.pod_index.get((namespace, name))
        if pod is None:
            raise ValueError(f"Pod {namespace}/{name} 不在 PodList 快照中")

        volumes = [
            self._diagnose_pvc_volume(pod, vol) for vol in pod.pvc_volumes
        ]
        for volume_name in pod.other_volume_names:
            volumes.append({
                "volume_name": volume_name,
                "claim_namespace": None,
                "claim_name": None,
                "pvc_uid": None,
                "pv_name": None,
                "pv_uid": None,
                "driver": None,
                "status": OUT_OF_SCOPE,
                "reasons": ["非 PVC 卷（如 emptyDir/configMap 等），不在 CSI RWO 排查范围"],
                "other_node_attachments": [],
                "target_attachment": None,
            })

        summary: dict[str, int] = {}
        for vol in volumes:
            summary[vol["status"]] = summary.get(vol["status"], 0) + 1
        return {
            "pod": pod.ref,
            "target_node": pod.node,
            "volumes": volumes,
            "summary": {
                "total": len(volumes),
                NEEDS_VERIFICATION: summary.get(NEEDS_VERIFICATION, 0),
                BLOCKED_OTHER_NODE: summary.get(BLOCKED_OTHER_NODE, 0),
                ATTACHED_ON_TARGET: summary.get(ATTACHED_ON_TARGET, 0),
                NO_ATTACHMENT: summary.get(NO_ATTACHMENT, 0),
                OUT_OF_SCOPE: summary.get(OUT_OF_SCOPE, 0),
            },
        }

    # ------------------------------------------------------------------ #
    def _base_entry(self, pod: PodStorageInfo, vol: PvcVolume) -> dict:
        return {
            "volume_name": vol.volume_name,
            "claim_namespace": pod.namespace,
            "claim_name": vol.claim_name,
            "pvc_uid": None,
            "pv_name": None,
            "pv_uid": None,
            "driver": None,
            "status": OUT_OF_SCOPE,
            "reasons": [],
            "other_node_attachments": [],
            "target_attachment": None,
        }

    def _needs_verification(self, entry: dict, reasons: list[str]) -> dict:
        entry["status"] = NEEDS_VERIFICATION
        entry["reasons"] = reasons
        return entry

    def _diagnose_pvc_volume(
        self, pod: PodStorageInfo, vol: PvcVolume
    ) -> dict:
        entry = self._base_entry(pod, vol)

        # 1. 按 Pod 命名空间找 PVC
        pvc = self.pvc_index.get((pod.namespace, vol.claim_name))
        if pvc is None:
            return self._needs_verification(
                entry,
                [f"命名空间 {pod.namespace} 中找不到 PVC {vol.claim_name}，"
                 "无法确认绑定与挂接关系"],
            )
        entry["pvc_uid"] = pvc.uid

        if not pvc.volume_name:
            return self._needs_verification(
                entry,
                [f"PVC {pvc.name} 的 spec.volumeName 为空，尚未绑定任何 PV"],
            )

        pv = self.pv_index.get(pvc.volume_name)
        if pv is None:
            return self._needs_verification(
                entry,
                [f"PVC {pvc.name} 指向的 PV {pvc.volume_name} 不在 PVList 快照中"],
            )
        entry["pv_name"] = pv.name
        entry["pv_uid"] = pv.uid
        entry["driver"] = pv.driver

        # 排查范围限 CSI ReadWriteOnce 卷
        if not pv.is_csi:
            entry["status"] = OUT_OF_SCOPE
            entry["reasons"] = [
                f"PV {pv.name} 没有 spec.csi（非 CSI 卷），不在排查范围"
            ]
            return entry
        if RWO_ACCESS_MODE not in pv.access_modes:
            entry["status"] = OUT_OF_SCOPE
            entry["reasons"] = [
                f"PV {pv.name} 访问模式为 {pv.access_modes}，不含 "
                f"{RWO_ACCESS_MODE}，不在排查范围"
            ]
            return entry

        reasons = self._binding_and_attachment_checks(pvc, pv)
        if reasons:
            return self._needs_verification(entry, reasons)

        # 2. 核对通过：统计 attached=true 的挂接记录
        #    正在删除（deletionTimestamp 非空）但 attached 仍为 true 的记录同样计入
        attached_vas = [va for va in self.vas
                        if va.pv_name == pv.name and va.attached]
        target_node = pod.node
        target_vas = [va for va in attached_vas if va.node == target_node]
        other_vas = [va for va in attached_vas if va.node != target_node]

        entry["target_attachment"] = self._attachment_payload(target_vas)

        if other_vas:
            entry["status"] = BLOCKED_OTHER_NODE
            entry["other_node_attachments"] = [
                {
                    "node": va.node,
                    "volume_attachment": va.name,
                    "deleting": va.deleting,
                    "blocking_pods": self._blocking_pods(
                        pod.namespace, pvc.name, va.node, pod.ref
                    ),
                }
                for va in sorted(other_vas, key=lambda v: (v.node, v.name))
            ]
            return entry

        if target_vas:
            entry["status"] = ATTACHED_ON_TARGET
        else:
            entry["status"] = NO_ATTACHMENT
        return entry

    def _binding_and_attachment_checks(
        self, pvc: PvcInfo, pv: PvInfo
    ) -> list[str]:
        reasons: list[str] = []

        # claimRef 必须指回该 PVC（UID 为准，名称/命名空间一并核对）
        if not pv.claim_uid or pv.claim_uid != pvc.uid:
            reasons.append(
                f"PV {pv.name} 的 claimRef.uid={pv.claim_uid!r} 与 PVC "
                f"{pvc.namespace}/{pvc.name} 的 UID {pvc.uid!r} 对不上"
            )
        if (pv.claim_namespace, pv.claim_name) != (pvc.namespace, pvc.name):
            reasons.append(
                f"PV {pv.name} 的 claimRef 指向 "
                f"{pv.claim_namespace}/{pv.claim_name}，并非 PVC "
                f"{pvc.namespace}/{pvc.name}"
            )

        pv_vas = [va for va in self.vas if va.pv_name == pv.name]
        for va in pv_vas:
            # 驱动不符
            if va.driver != pv.driver:
                reasons.append(
                    f"挂接记录 {va.name}（节点 {va.node}）的驱动 "
                    f"{va.driver!r} 与 PV 驱动 {pv.driver!r} 不符"
                )
            # 挂接记录早于 PV 创建
            pv_ts = _parse_timestamp(pv.creation_timestamp)
            va_ts = _parse_timestamp(va.creation_timestamp)
            if pv_ts is None or va_ts is None:
                reasons.append(
                    f"挂接记录 {va.name} 与 PV {pv.name} 的创建时间戳缺失或"
                    "无法解析，创建先后无法核对"
                )
            elif va_ts < pv_ts:
                reasons.append(
                    f"挂接记录 {va.name} 创建于 {va.creation_timestamp}，"
                    f"早于 PV {pv.name} 的创建时间 {pv.creation_timestamp}"
                )

        # 同节点 attached 状态互相矛盾
        by_node: dict[str, list[VaInfo]] = {}
        for va in pv_vas:
            by_node.setdefault(va.node, []).append(va)
        for node, records in sorted(by_node.items()):
            states = {r.attached for r in records}
            if True in states and False in states:
                detail = ", ".join(
                    f"{r.name}(attached={str(r.attached).lower()})" for r in records
                )
                reasons.append(
                    f"节点 {node} 上引用 PV {pv.name} 的挂接记录 attached "
                    f"状态互相矛盾：{detail}"
                )
        return reasons

    def _blocking_pods(
        self,
        namespace: str,
        claim_name: str,
        node: str,
        selected_ref: str,
    ) -> list[dict]:
        consumers = self.pvc_consumers.get((namespace, claim_name), [])
        return [
            {
                "pod": p.ref,
                "phase": p.phase,
                "node": p.node,
            }
            for p in sorted(consumers, key=lambda x: x.name)
            if p.node == node and p.ref != selected_ref
        ]

    @staticmethod
    def _attachment_payload(vas: list[VaInfo]) -> dict | None:
        if not vas:
            return None
        # 目标节点理论上只有一条有效记录；多条时全部列出便于人工核对
        return {
            "node": vas[0].node,
            "volume_attachments": [
                {
                    "volume_attachment": va.name,
                    "attached": va.attached,
                    "deleting": va.deleting,
                }
                for va in sorted(vas, key=lambda v: v.name)
            ],
        }
