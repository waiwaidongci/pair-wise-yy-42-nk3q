# 山火事件指挥与离线人员调度

维护火线、风向、资源和任务区，合并离线现场记录并防止人员重复分配。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8319
```

默认端口为`8319`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

允许角色：field_commander, incident_commander, logistics, viewer。火线长度、风向变化和离线记录数量影响风险等级；同一资源不能同时出现在多个活动任务中。

## 车辆派车调度

对讲机派车与火线任务、任务区容量绑定，核心保证：

- **容量排队**：任务区有 `capacity`，同一时段在派车辆数满后，后来的派车单进入 FIFO 等待队列；车辆腾出自 动补派。
- **车辆互斥**：同一辆车同一时段只能接一个有效任务（半开区间，`12:00` 结束与 `12:00` 开始的任务可背靠背）。所有派车写操作在库锁+单事务内串行完成，两个调度员同时提交不会重车。
- **点名派车**：可指定 `vehicle_id`，车辆被占或车型不符时排队等它，系统不会偷偷换车。
- **失效重排**：风向或火线等级变化（`POST /api/zones/{id}/environment`）先把该任务区全部在派单释放（车辆归还，记 `released`），再按原始顺序 FIFO 重排（记 `reassigned`）；排队中的后来任务不能插队。
- **断网幂等**：每条写指令必须带 `request_id`；任务、车辆、任务区和点名派车均按 ID 幂等，同 ID 不同载荷返回 409。业务写入与审计在同一事务，写入失败整体回滚、原排班保留；恢复后 `POST /api/sync` 只补未完成指令，重复指令不会多占车辆。
- **调度台**：`GET /api/board` 返回等待（`waiting`）、已派（`dispatched`）、失效重排（`invalidated_rearranged`）三个列表，以及车辆空闲/在派状态和完整事件流（queued/assigned/released/reassigned/completed/cancelled）。

调度接口（均需 `X-Actor`/`X-Role` 头）：

- `POST /api/vehicles` / `GET /api/vehicles`
- `POST /api/zones` / `GET /api/zones`
- `POST /api/zones/{id}/environment`，风向 `wind_direction`（N/NE/...）或火线等级 `fireline_grade`（low/moderate/high/extreme）
- `POST /api/tasks` / `GET /api/tasks` / `GET /api/tasks/{id}`，载荷含 `zone_id`、`required_type`、`required_vehicles`、`slot_start`、`slot_end`（ISO 8601）
- `POST /api/tasks/{id}/dispatches`，点名派车 `vehicle_id` + 时段
- `POST /api/dispatches/{id}/complete`、`POST /api/dispatches/{id}/cancel`
- `POST /api/tasks/{id}/complete`
- `GET /api/board`
- `POST /api/sync`，载荷 `{"commands":[{"op":"submit_task","payload":{...}}, ...]}`，支持的 op：register_vehicle / register_zone / submit_task / request_vehicle / update_environment / complete_dispatch / cancel_dispatch；单条失败隔离不影响其他指令。

车型：fire_engine, water_tanker, crew_carrier, dozer, support。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
