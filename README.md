# 水库泄洪调度单（夜班复核调度）

值班员录入**库位、汛限、入库流量、涨幅、下游警戒、施工限制和可用闸门**，系统自动计算
**下泄流量与开启闸门数**，生成可复核、可追溯的泄洪调度单。

## 业务规则

1. **目标下泄量**
   - 库位未超汛限且未上涨：按进出平衡，下泄量 = 入库流量；
   - 库位超汛限：每超 1 米在入库基础上加大 30 m³/s（降低库位）；
   - 库位仍上涨：每上涨 1 米/小时再增加 30 m³/s 预泄腾容。
2. **下游严重告警硬约束**：下泄量不得超过入库流量的 **50%**（取小）。
3. **闸门选择**
   - `maintenance` 检修闸门一律不选；
   - 优先选择 `available` 可用闸门（按单孔能力从大到小）；
   - 可用能力不足时才补充 `restricted` 受限闸门（施工限制）。
4. **总工复核授权**：库位仍在上涨（涨幅 > 0）**或**需要启用受限闸门时，
   总工登记复核意见后才能授权；任何授权都必须先有总工复核记录。
5. **执行闭环**：调度执行员回填实际下泄流量；与计划偏差 **超过 10%** 必须填写原因，
   单据退回「退回复核」，总工补充新的复核意见后才能再次授权。
6. 全部操作进入 SHA-256 哈希链审计，SQLite 持久化，乐观锁（version）防并发覆盖。

## 状态机

```
draft 待复核 ──总工授权──▶ authorized 已授权 ──执行回填──▶ executed 已执行 ──总工确认──▶ closed 已闭环
                                ▲                            │
                                └────偏差>10%：recheck 退回复核┘
```

## 模块结构

- `app.py`：参数解析、依赖组装和 HTTP 服务启动。
- `src/domain.py`：角色/状态/枚举、数据校验、闸门清单解析。
- `src/rules.py`：下泄量与闸门选择规则（纯函数 `plan_order`）、紧迫度、偏差、状态机与角色矩阵。
- `src/repository.py`：SQLite 建表、事务、乐观锁、审计哈希链。
- `src/service.py`：水情解析、试算、录单、复核、授权、执行回填与闭环用例。
- `src/http_api.py`：JSON 路由与统一错误响应。
- `src/audit.py`：UTC 时间与 SHA-256 审计事件。
- `static/index.html`：演示页（录入试算、复核授权、执行回填、单据/审计查看）。

## 启动

```bash
python3 app.py --db ./data.db --port 8315
```

浏览器打开 <http://127.0.0.1:8315/>，右上角切换 **值班员 / 总工 / 调度执行员 / 观摩** 身份。
接口用 `X-Actor`、`X-Role` 请求头传递身份。

## 主要接口

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| GET | `/health` | 全部 | 健康检查 |
| POST | `/api/orders/preview` | 全部 | 试算下泄量与闸门，不落库 |
| POST | `/api/orders` | 值班员 | 录入水情工情，生成调度单 |
| GET | `/api/orders?status=` | 全部 | 调度单列表（可按状态过滤） |
| GET | `/api/orders/{id}` | 全部 | 调度单详情 |
| GET | `/api/orders/{id}/records` | 全部 | 复核/说明记录 |
| POST | `/api/orders/{id}/records` | 值班员/调度/总工 | 添加说明；`kind=review` 仅总工 |
| POST | `/api/orders/{id}/authorize` | 总工 | body 需 `expected_version`，须先有复核意见 |
| POST | `/api/orders/{id}/execute` | 调度执行员 | body：`actual_flow`、可选 `deviation_reason`、`expected_version` |
| POST | `/api/orders/{id}/close` | 总工 | 无未关闭记录方可闭环 |
| GET | `/api/audit`、`/api/audit/verify` | 总工/观摩 | 审计事件与哈希链校验 |

## 测试

```bash
python3 -m unittest discover -s tests -v
```

- `test_rules.py`：限泄 50%、超汛限加大、上涨预泄、检修不选、受限补充、能力不足、偏差阈值。
- `test_workflow.py`：录单→复核→授权→执行→闭环、偏差退回与重新授权、试算不落库、审计链。
- `test_failures.py`：角色矩阵、复核前置、版本冲突、参数校验、未闭环记录拦截、非法跳转。
