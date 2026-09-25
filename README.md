# 水库防汛调度与操作确认

根据库位、入库流量、下游警戒和施工限制生成复核授权的泄洪指令。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、泄洪方案计算、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页（录入、复核、授权、执行、反馈、查看）。
- `tests/`：完整流程、规则、泄洪方案和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8315
```

默认端口为`8315`，首次启动自动建库（旧库自动补充新列）。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`（值班员录入，可携带泄洪输入字段）
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`（`kind=review`为总工复核记录，仅总工可登记）
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/items/{id}/feedback`（调度员登记实际下泄流量，超差自动退回）
- `GET /api/audit`

允许角色：duty_officer, chief_engineer, dispatcher, viewer。

## 泄洪业务规则

录入字段：`reservoir_level`（库位）、`flood_limit`（汛限）、`inflow`（入库流量）、
`rise_rate`（涨幅）、`downstream_alarm`（normal/warning/severe）、
`gates`（闸门列表，`restricted=true`表示施工受限）。

- 计划下泄 = 入库流量 + 超汛限水位×500 + 涨幅×200；下游严重告警（severe）时
  按入库流量的一半封顶。
- 闸门按泄流能力从大到小选用，受限闸门默认不选；可用闸门不足时才启用受限闸门。
- 库位仍上涨或需启用受限闸门时，必须总工复核（`kind=review`记录）后才能授权；
  偏差退回后须重新复核。
- 执行后调度员登记实际下泄流量，偏差超过一成（10%）必须说明原因，调度单自动
  退回待复核（checked）；偏差在一成内方可闭环，闭环前必须先登记现场反馈。
- 库位超汛限或入库流量上升时自动提升紧迫度。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
