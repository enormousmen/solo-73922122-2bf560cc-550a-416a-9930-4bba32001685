# Kubernetes 值班工具箱（缩容落点试算 + CSI 卷挂载排查）

值班员在缩容前上传同一批 **NodeList / PodList JSON 快照**，在网页勾选要下线的节点，
系统在其余就绪可调度的节点上为全部待迁 Pod 寻找**同时成立**的落点方案，
并给出逐 Pod 落点与各承接节点余量。无完整方案或遇到不支持的约束时，
明确给出原因并由前端撤下旧方案（不展示任何残留落点）。

Pod 分到目标节点后若**卡在挂载阶段**，可切换到「CSI 卷挂载排查」页：
上传同批 **PodList / PersistentVolumeClaimList / PersistentVolumeList /
VolumeAttachmentList** 四类 JSON 清单并选中该 Pod，系统仅针对该 Pod
已知需挂接的 **CSI ReadWriteOnce 卷**做静态核对，给出待核实项、
其他节点占用（含占用 Pod）或目标节点自身挂接状态。排查失败时前端撤下旧结果。

## 试算规则

| 主题 | 规则 |
| --- | --- |
| 规模上限 | 单次最多勾选 **6** 个下线节点、最多 **8** 个待迁 Pod |
| 节点资格 | 下线节点与承接节点都必须：无污点、`Ready=True`、未被 cordon、`allocatable` 完整 |
| Pod 结构 | 仅支持**单容器、无 init 容器、无卷**的 Pod；否则报“不支持的约束”并拒绝整次试算 |
| 资源口径 | 从节点 `status.allocatable` 中扣除**仍在该节点且尚未结束**（phase 非 Succeeded/Failed）的 Pod 的 CPU/内存 **requests**；待迁 Pod 不计入原节点占用 |
| 调度约束 | 支持 `nodeSelector`；硬 `nodeAffinity`、硬 `podAffinity` 报不支持 |
| 反亲和 | 仅支持同命名空间内、拓扑键为 `kubernetes.io/hostname`、`labelSelector.matchLabels` 的**硬** `podAntiAffinity`；**双向检查**（A 的选择器命中 B 或 B 的选择器命中 A 都算冲突）；其他拓扑键、`matchExpressions` 报不支持；软约束（preferred）忽略 |
| 完整性 | 所有待迁 Pod 必须在同一份方案中同时放下，采用 MRV + 回溯求解；找不到即判定无完整方案 |
| DaemonSet/静态 Pod | drain 本就不迁移这类 Pod，自动列入 `excluded_pods` 并说明原因 |

## 目录结构

```
app/
  models.py           # 快照解析、CPU/内存量解析、领域模型、约束识别
  planner.py          # 候选筛选、资源核算、双向反亲和、回溯求完整方案
  storage.py          # PVC/PV/VolumeAttachment 解析与 CSI RWO 挂载排查引擎
  main.py             # FastAPI 路由（multipart 与 JSON 两种调用方式）
  static/index.html   # 单页前端（两个标签页：试算 / 挂载排查）
samples/              # 5 组可直接使用的快照（由脚本生成）
scripts/generate_samples.py
tests/                # 自测（test_planner.py + test_storage.py）
Dockerfile / docker-compose.yml
```

## CSI 卷挂载排查规则

排查范围**仅限选中 Pod 的 `spec.volumes` 中引用了 PVC、且对应 PV 为 CSI 卷并含
`ReadWriteOnce` 访问模式**的卷；非 CSI、非 RWO、非 PVC 卷（如 emptyDir）只列出、
标记为“不在排查范围”，不参与核对。对每个在范围内的卷：

| 主题 | 规则 |
| --- | --- |
| PVC 定位 | 按**选中 Pod 的命名空间**找 PVC；PVC 缺失、未绑定（`spec.volumeName` 为空）、绑定的 PV 不在 PVList 中 → **待核实** |
| 绑定回指 | 核对 PV `spec.claimRef.uid` 是否指回该 PVC 的 UID（同时核对名称/命名空间）；对不上 → **待核实** |
| 驱动核对 | 引用该 PV 名称的 VolumeAttachment，其 `spec.attacher` 与 PV `spec.csi.driver` 不符 → **待核实** |
| 时序核对 | VolumeAttachment 创建时间早于 PV 创建时间 → **待核实**；时间戳缺失/无法解析时明确报“无法核对”，不静默放过 |
| 同节点矛盾 | 同一节点上引用该 PV 的多条挂接记录 `status.attached` 一真一假 → **待核实** |
| 其他节点占用 | 核对通过后，列出所有 `attached=true` 的**其他节点**挂接记录；**正在删除（有 deletionTimestamp）但 attached 仍为 true 的记录同样计入**；并列出这些节点上尚未结束（phase 非 Succeeded/Failed）且引用同一 PVC 的 Pod |
| 无其他节点挂接 | 区分两种结论：目标节点自身已有 `attached=true` 记录 → “目标节点已挂接”；任何节点都没有 → “尚无已挂接记录” |

卷结论共五种：`needs_verification`（待核实）、`blocked_by_other_nodes`（被其他节点占用）、
`attached_on_target`（目标节点已挂接）、`no_attachment`（尚无挂接记录）、
`out_of_scope`（不在排查范围）。排查请求失败（HTTP 错误、非法 JSON、未知 Pod 等）时，
前端撤下旧结果并只展示失败横幅，不残留任何卷结论。

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000
```

`./samples` 以只读方式挂载到容器内 `/data/samples`，可直接在网页上传其中的
`nodes.json` / `pods.json`。

## 本地直接运行（无 Docker 时）

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## HTTP 接口

缩容试算：

- `POST /api/overview`（multipart：`node_list`、`pod_list`）：返回节点资格与 Pod 概览
- `POST /api/plan`（multipart：两个快照 + 字符串字段 `drained_nodes`，值为 JSON 数组）
- `POST /api/plan-json`（JSON 体：`{node_list, pod_list, drained_nodes}`），适合脚本

CSI 卷挂载排查：

- `POST /api/storage/overview`（multipart：`pod_list`、`pvc_list`、`pv_list`、`va_list`）：
  返回可选 Pod 及每个 PVC 卷是否在 CSI RWO 排查范围
- `POST /api/storage/diagnose`（同上四个文件 + 字符串字段 `pod_ref`，值为 `命名空间/Pod名称`）
- `POST /api/storage/diagnose-json`（JSON 体：`{pod_list, pvc_list, pv_list, va_list, pod_ref}`）
- `GET /healthz`

curl 示例（注意 `drained_nodes` 用 `<` 作为普通表单字符串传入）：

```bash
curl -s \
  -F "node_list=@samples/feasible/nodes.json" \
  -F "pod_list=@samples/feasible/pods.json" \
  -F "drained_nodes=<samples/feasible/drained-nodes.json" \
  http://localhost:8000/api/plan
```

挂载排查示例：

```bash
curl -s \
  -F "pod_list=@samples/storage/pods.json" \
  -F "pvc_list=@samples/storage/pvcs.json" \
  -F "pv_list=@samples/storage/pvs.json" \
  -F "va_list=@samples/storage/volume-attachments.json" \
  -F "pod_ref=team1/stuck-app" \
  http://localhost:8000/api/storage/diagnose
```

## 示例快照

重新生成：`python3 scripts/generate_samples.py samples`

| 场景 | 目录 | 预期结果 |
| --- | --- | --- |
| 可迁移 | `samples/feasible/` | 4 个待迁 Pod 全部落地；两个 `app=web` Pod 因硬反亲和分到不同节点；`worker-1` 受 nodeSelector 限制不去 batch 节点；DaemonSet Pod 列入排除项；Succeeded Pod 不占资源 |
| 资源超配 | `samples/infeasible-resource/` | 大 Pod 在两个承接节点上 CPU/内存均不足，无完整方案并给出逐节点缺口 |
| 反亲和无解 | `samples/infeasible-anti/` | 两个互相反亲和的 web Pod 只剩一个合格承接节点（另一节点已有 web、第三个节点不 Ready），回溯失败 |
| 不支持约束 | `samples/unsupported/` | 多容器+卷、init 容器 Pod 被拒绝；勾选污点节点同样被拒绝 |
| 挂载排查 | `samples/storage/` | `team1/stuck-app`（目标节点 node-c）11 个卷：6 个待核实（claimRef 对不上、驱动不符、VA 早于 PV、同节点 attached 矛盾、PV 缺失、PVC 缺失）、3 个被其他节点占用（含一条正在删除但 attached=true 的记录和同 PVC 占用 Pod）、2 个范围外（NFS 与 emptyDir）；`team1/worker-0` 分别演示“目标节点已挂接”和“尚无挂接记录” |

## 自测

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## 已知边界（未实现/刻意不支持）

- 不支持硬 `nodeAffinity`、硬 `podAffinity`、非 hostname 拓扑键与 `matchExpressions`；遇到即报错并撤下方案
- 试算器不处理 PVC/存储卷、init 容器、多容器（sidecar）Pod（挂载排查页独立处理 CSI RWO 卷）
- 软亲和/反亲和（`preferredDuringScheduling*`）不参与硬性可调度性判断，直接忽略
- 不模拟真实调度器的 taints/tolerations 联动、端口占用、设备插件等；无污点节点之外的节点不参与
- 只做静态 requests 核算，不考虑超售比例与实时指标
- 挂载排查只做**清单静态核对**：不访问集群、不触发实际挂载/卸载，不判断节点内 mount/设备状态；
  仅识别 CSI（`spec.csi`）且含 RWO 访问模式的 PV，不处理 inline CSI 卷、块设备挂载状态与
  `volumeHandle` 级别的后端校验；时间戳无法解析时按“无法核对”报待核实而非猜测时序
