"""FastAPI 入口：上传 NodeList/PodList 快照，勾选节点并试算缩容落点。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .models import parse_nodes, parse_pods
from .planner import MAX_DRAIN_NODES, MAX_MIGRATING_PODS, Planner, _node_eligibility
from .storage import (
    StorageDiagnoser,
    parse_pvcs,
    parse_pvs,
    parse_storage_pods,
    parse_vas,
)

app = FastAPI(title="K8s 缩容落点试算器", version="1.0.0")

STATIC_DIR = Path(__file__).parent / "static"


def _load_json_bytes(data: bytes, label: str) -> Any:
    if not data:
        raise HTTPException(status_code=400, detail=f"{label} 内容为空")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"{label} 不是合法 JSON：{exc}") from exc


def _build_overview(node_payload: Any, pod_payload: Any) -> dict:
    try:
        nodes = parse_nodes(node_payload)
        pods = parse_pods(pod_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    node_view = [
        {
            "name": n.name,
            "ready": n.ready,
            "unschedulable": n.unschedulable,
            "taint_count": len(n.taints),
            "eligible": not _node_eligibility(n),
            "ineligible_reasons": _node_eligibility(n),
            "cpu_alloc_m": n.cpu_alloc_m,
            "mem_alloc_bytes": n.mem_alloc_bytes,
        }
        for n in sorted(nodes, key=lambda x: x.name)
    ]
    pod_view = [
        {
            "ref": p.ref,
            "namespace": p.namespace,
            "name": p.name,
            "node": p.node,
            "phase": p.phase,
            "active": p.active,
            "cpu_request_m": p.cpu_request_m,
            "mem_request_bytes": p.mem_request_bytes,
            "unsupported_reasons": p.unsupported_reasons,
            "is_daemonset": p.is_daemonset,
            "is_mirror": p.is_mirror,
        }
        for p in sorted(pods, key=lambda x: (x.node, x.namespace, x.name))
    ]
    return {
        "nodes": node_view,
        "pods": pod_view,
        "limits": {
            "max_drain_nodes": MAX_DRAIN_NODES,
            "max_migrating_pods": MAX_MIGRATING_PODS,
        },
    }


class PlanRequest(BaseModel):
    node_list: Any
    pod_list: Any
    drained_nodes: list[str]


@app.post("/api/overview")
async def overview(
    node_list: UploadFile | None = File(default=None),
    pod_list: UploadFile | None = File(default=None),
):
    if node_list is None or pod_list is None:
        raise HTTPException(status_code=400, detail="需要上传 node_list 与 pod_list 两个 JSON 文件")
    node_payload = _load_json_bytes(await node_list.read(), "NodeList")
    pod_payload = _load_json_bytes(await pod_list.read(), "PodList")
    return _build_overview(node_payload, pod_payload)


@app.post("/api/plan")
async def plan(
    node_list: UploadFile | None = File(default=None),
    pod_list: UploadFile | None = File(default=None),
    drained_nodes: str = Form(default="[]"),
):
    if node_list is None or pod_list is None:
        raise HTTPException(status_code=400, detail="需要上传 node_list 与 pod_list 两个 JSON 文件")
    node_payload = _load_json_bytes(await node_list.read(), "NodeList")
    pod_payload = _load_json_bytes(await pod_list.read(), "PodList")
    try:
        drained = json.loads(drained_nodes)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"drained_nodes 不是合法 JSON 数组：{exc}") from exc
    if not isinstance(drained, list) or not all(isinstance(x, str) for x in drained):
        raise HTTPException(status_code=400, detail="drained_nodes 必须是字符串数组")

    try:
        nodes = parse_nodes(node_payload)
        pods = parse_pods(pod_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = Planner(nodes, pods).plan(drained)
    return _serialize(result)


@app.post("/api/plan-json")
async def plan_json(req: PlanRequest):
    """供脚本/curl 直接以 JSON 体调用的试算接口。"""
    try:
        nodes = parse_nodes(req.node_list)
        pods = parse_pods(req.pod_list)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize(Planner(nodes, pods).plan(req.drained_nodes))


# --------------------------------------------------------------------------- #
# CSI ReadWriteOnce 卷挂载排查
# --------------------------------------------------------------------------- #
def _build_storage_diagnoser(
    pod_payload: Any,
    pvc_payload: Any,
    pv_payload: Any,
    va_payload: Any,
) -> StorageDiagnoser:
    try:
        pods = parse_storage_pods(pod_payload)
        pvcs = parse_pvcs(pvc_payload)
        pvs = parse_pvs(pv_payload)
        vas = parse_vas(va_payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return StorageDiagnoser(pods, pvcs, pvs, vas)


async def _read_upload(upload: UploadFile | None, label: str) -> Any:
    if upload is None:
        raise HTTPException(
            status_code=400, detail=f"需要上传 {label} JSON 文件"
        )
    return _load_json_bytes(await upload.read(), label)


@app.post("/api/storage/overview")
async def storage_overview(
    pod_list: UploadFile | None = File(default=None),
    pvc_list: UploadFile | None = File(default=None),
    pv_list: UploadFile | None = File(default=None),
    va_list: UploadFile | None = File(default=None),
):
    """解析四类清单，返回可选 Pod 及其 PVC 卷（标注是否在 CSI RWO 排查范围）。"""
    payloads = [
        await _read_upload(pod_list, "PodList"),
        await _read_upload(pvc_list, "PersistentVolumeClaimList"),
        await _read_upload(pv_list, "PersistentVolumeList"),
        await _read_upload(va_list, "VolumeAttachmentList"),
    ]
    diagnoser = _build_storage_diagnoser(*payloads)
    return {"pods": diagnoser.overview()}


@app.post("/api/storage/diagnose")
async def storage_diagnose(
    pod_list: UploadFile | None = File(default=None),
    pvc_list: UploadFile | None = File(default=None),
    pv_list: UploadFile | None = File(default=None),
    va_list: UploadFile | None = File(default=None),
    pod_ref: str = Form(default=""),
):
    """对选中 Pod 的 CSI RWO 卷做挂载排查（multipart 表单调用）。"""
    payloads = [
        await _read_upload(pod_list, "PodList"),
        await _read_upload(pvc_list, "PersistentVolumeClaimList"),
        await _read_upload(pv_list, "PersistentVolumeList"),
        await _read_upload(va_list, "VolumeAttachmentList"),
    ]
    namespace, name = _split_pod_ref(pod_ref)
    diagnoser = _build_storage_diagnoser(*payloads)
    try:
        return diagnoser.diagnose(namespace, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class StorageDiagnoseRequest(BaseModel):
    pod_list: Any
    pvc_list: Any
    pv_list: Any
    va_list: Any
    pod_ref: str


@app.post("/api/storage/diagnose-json")
async def storage_diagnose_json(req: StorageDiagnoseRequest):
    """供脚本/curl 直接以 JSON 体调用的挂载排查接口。"""
    namespace, name = _split_pod_ref(req.pod_ref)
    diagnoser = _build_storage_diagnoser(
        req.pod_list, req.pvc_list, req.pv_list, req.va_list
    )
    try:
        return diagnoser.diagnose(namespace, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _split_pod_ref(pod_ref: str) -> tuple[str, str]:
    if not pod_ref or "/" not in pod_ref:
        raise HTTPException(
            status_code=400,
            detail="pod_ref 必须是 命名空间/Pod名称 形式",
        )
    namespace, name = pod_ref.split("/", 1)
    if not namespace or not name:
        raise HTTPException(
            status_code=400,
            detail="pod_ref 必须是 命名空间/Pod名称 形式",
        )
    return namespace, name


def _serialize(result) -> dict:
    return {
        "feasible": result.feasible,
        "drained_nodes": result.drained_nodes,
        "excluded_pods": result.excluded_pods,
        "migrating_pods": result.migrating_pods,
        "candidates": result.candidates,
        "placement": result.placement,
        "node_remaining": result.node_remaining,
        "violations": result.violations,
        "diagnostics": result.diagnostics,
        "limits": {
            "max_drain_nodes": MAX_DRAIN_NODES,
            "max_migrating_pods": MAX_MIGRATING_PODS,
        },
    }


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
