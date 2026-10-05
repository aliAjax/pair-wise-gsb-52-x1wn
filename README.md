# 特殊教育支持计划合规

纯Python标准库实现的特殊教育支持计划合规原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、同意、服务凭证台账、复查期限和计划版本和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、失败场景和凭证台账测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8328
```

默认端口为`8328`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/vouchers`：服务凭证台账。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 服务凭证台账

每次服务以凭证形式登记，`log_service`的`data`字段：

- `voucher_no`：凭证号，同一计划内唯一，重复提交返回`409 voucher_conflict`，只计算一次。
- `started_at`：服务开始时间，ISO 8601格式。
- `minutes`：服务时长（分钟，正整数）；兼容旧字段名`session_minutes`。
- `provider`：服务人员。

汇总规则：

- `delivered_minutes`、`missing_minutes`、`compliance_rate`不再接受外部传入，由事务内对有效凭证（`status='valid'`）`SUM`重新加总。
- 创建计划时若带有`delivered_minutes`，或旧数据库启动升级时，自动按已有分钟数生成一条`voucher_type='migration'`的迁移凭证（凭证号`MIG-{reference}`，服务人员为“历史数据迁移”），并写`ledger_migrated`审计；迁移幂等，已有台账的计划不重复生成。
- `void_voucher`动作撤销凭证，必须提供`reason`；凭证置为`void`、记录原因，并在同一事务内反向重算汇总。撤销后不能重复撤销。
- 凭证写入、版本推进、汇总重算、审计在同一个`BEGIN IMMEDIATE`事务内完成，中途失败整体回滚，不会留下半套结果。
- 两名服务人员同时用相同`expected_version`补录时，落后的一次返回`409 conflict`版本冲突，原凭证和汇总均不变。
- `review`（复查）和`close`（结案）前会核对计划汇总与台账总额：不一致时返回`409 ledger_mismatch`，响应信息中给出差额（汇总减台账）并保持状态不变；差额为0才允许流转。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突、凭证登记与重复、并发补录、撤销反向重算、旧库迁移、台账差额拦截复查/结案和事务中断回滚。
