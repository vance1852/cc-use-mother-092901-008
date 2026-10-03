# 国际班列保供运力分配协同服务

本项目在综合交通共享基础能力（运营机构、操作者、场所、参考资料登记，角色权限、
请求幂等、SQLite 事务与哈希串联审计）之上，实现**国际班列通道临时限流时的保供
运力分配系统**。

## 解决的问题

通道临时限流时不再按申报先后粗暴削减订舱，而是在申报截止时冻结需求，按可解释、
可复算、可跨版本比较的政策给出分配草案：

- **医疗/生产急需优先**：政策综合货物时限、货物类别、战略保障级别、合同优先级、
  历史履约评分排序，而不是只看提交先后；
- **防止同集团拆单占满配额**：同一 `group_id` 受份额上限（默认 35%，政策可调）
  约束，拆单（`split_group_id`）与同集团重复提交会被标记并在逐票原因中体现；
- **连续舱位与单调重分**：已给出且未放弃的草案、已确认、已装运、人工例外额度
  逐轮粘住，只分增量；放弃/取消释放的舱位按同一确定性全序补到候补队首，
  运力恢复后**只增配、不重复补分**；
- **已装运货物绝不回收**：限流下调舱位不追溯削减既有持有量；
- **重复请求与并发确认识别**：request_id 幂等去重，写事务经存储锁串行化，
  同一票的并发/重复确认只有一笔成功；
- **人工例外四眼审批**：申请人不能自批，需另一具备审批角色者批准，批准后
  记录可复算的逐票影响（before/after/delta）；
- **可解释、可审计、可比较**：每票需求都能查到获得/落选原因、评分构成、
  候补位置与恢复后的增配来源；每次运行快照冻结输入，审计方可在**不写库、
  不改写既往结果**的前提下用任意政策版本复算公平性。

## 分配模型（`allocation.py`）

纯函数引擎，不碰数据库与时钟：同一份输入快照 + 政策参数必然得到同一份输出。

- 全序：政策评分降序 → 提交时间 → 需求编号；
- 起点持有量 = 已确认 + 已有人工例外（受保护核心）与粘性草案的较大者；
- 只把「剩余舱位」按全序授予，同集团受份额上限约束（上限只限制新增，不追溯）；
- 放弃为终态：保留不可回收核心，不重新抢回已释放舱位；
- 班次取消：仅保留已装运部分。

## 主要接口

写接口经 `X-Actor-Id` 标识操作者并携带 `request_id` 实现幂等。

| 动作 | 方法与路径 |
| --- | --- |
| 登记通道 | `POST /corridors` |
| 登记政策版本 | `POST /policies` |
| 通道激活政策 | `POST /policy-activations` |
| 登记客户（集团/级别/优先级/履约分） | `POST /customers` |
| 登记班次（通道+班次版本舱位） | `POST /departures` |
| 申报需求（可带 `split_group_id`） | `POST /demands` |
| 截止冻结并分配 | `POST /departures/{id}/freeze` |
| 客户确认占用 | `POST /demands/{id}/confirm` |
| （部分）装运 | `POST /demands/{id}/ship` |
| 放弃草案 | `POST /demands/{id}/release` |
| 舱位调整/恢复 | `POST /departures/{id}/capacity` |
| 班次取消 | `POST /departures/{id}/cancel` |
| 标记发车 | `POST /departures/{id}/depart` |
| 申请人工例外 | `POST /exceptions` |
| 双人审批例外 | `POST /exceptions/{id}/decision` |
| 查看班次舱位与版本 | `GET /departures/{id}` |
| 查看每票分配/候补 | `GET /departures/{id}/entitlements` |
| 逐票原因/候补/增配来源 | `GET /demands/{id}/explain` |
| 分配运行历史 | `GET /departures/{id}/runs` |
| 单次运行完整快照 | `GET /runs/{id}` |
| 跨政策公平性比较 | `GET /departures/{id}/fairness[?policy=id:v]` |
| 用他版政策复算历史运行 | `POST /runs/{id}/replay` |

基础能力接口（`/organizations`、`/actors`、`/sites`、`/domain-records`、
`/audit-events`、`/health`）保持不变。

## 目录

- `src/transport_coordination/allocation.py`：纯函数分配引擎与公平性汇总；
- `src/transport_coordination/supply_service.py`：保供流程应用服务（登记、冻结、
  确认、装运、放弃、取消、运力恢复、四眼例外、查询与复算）；
- `src/transport_coordination/storage.py`：SQLite 建表、写事务串行锁与读快照；
- `src/transport_coordination/api.py`：HTTP/JSON 路由；
- `src/transport_coordination/acceptance_supply.py`：保供端到端离线验收；
- `tests/`：引擎、服务、接口与并发/持久化相关测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
# 基础服务验收
PYTHONPATH=src python3 -m transport_coordination.acceptance
# 保供运力分配验收
PYTHONPATH=src python3 -m transport_coordination.acceptance_supply
```

成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后
SQLite 中的业务状态、历次分配快照与审计历史继续保留。
