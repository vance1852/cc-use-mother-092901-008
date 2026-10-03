# 国际班列保供运力分配协同服务

本项目提供综合交通运输业务共享的服务端基础能力，负责运营机构、交通节点、操作者和结构化参考资料的登记，内置角色权限、请求幂等、SQLite 事务与哈希串联审计。在此之上实现国际班列临时限流场景的**保供运力分配**：按通道与班次版本登记舱位、客户合同上限、集团限额、货物时限、战略保障级别、历史履约与拆单关联；申报截止时冻结需求并输出可解释的分配草案；客户确认后才占用额度；放弃、部分装船、班次取消、运力恢复均按同一稳定顺序重新调和；已装船货物永不回收；同集团拆单绕限、重复请求与并发确认可被识别；人工例外须双人审批并留下可复算影响。

## 目录

- `src/transport_coordination/`：领域模型、SQLite 存储、权限服务、审计链、纯函数分配器、保供运力服务、HTTP 路由和离线验收；
- `tests/`：基础规则、分配算法、事务边界、接口路由和端到端验收测试。

## 核心规则

- **战略保供优先**：排序因子默认 `战略保障级别 → 货物时限 → 历史履约率 → 申报序号`，可通过政策版本配置；同因子由需求编号保证确定性。
- **拆单家庭连续舱位**：同一 `split_root_id` 的各票在家庭内连续、按申报顺序分配；家庭之间按家庭首单的政策优先级排序，避免"同一客户拆单占满配额、医疗急货拿不到连续舱位"。
- **双重限额**：逐票扣减客户合同单班上限与集团单班总限额；同集团跨客户使用同一拆单关联号会被打上 `GROUP_CAP_CIRCUMVENTION` 标记，近似重复请求会被打上 `DUPLICATE_REQUEST` 标记。
- **确认才占额，装船即冻结**：草案不占用任何额度；客户确认后占用；装船量是硬地板，只能增加，取消班次或压缩运力也不得回收。
- **稳定顺序再分配**：放弃、部分装船、取消、运力恢复都由同一个纯函数 `reconcile` 基于"已装船地板 + 已确认占用"重算，候补位置不重排、不会重复补分；每票的增配来源（触发事件、运力变更前后值）可查。
- **人工例外双人审批**：一人提议、另一位不同操作者复核；批准前以最新状态复算影响（每票前后变化、总占用变化）并随审批落库，之后生成新的分配版本；拒绝不改写任何结果。
- **政策可比、历史不改写**：政策版本只存参数；`/policy-comparison` 用冻结快照只读复算多套政策的公平性指标（整体满足率、客户满足率 Gini、集团份额、各级战略保障满足率）；历史分配版本与其完整输入快照永久保留，可逐版本离线复算。

## HTTP 接口（写入均需 `X-Actor-Id` 与唯一 `request_id`）

- `POST /corridors`、`POST /customer-groups`、`POST /customers`：通道、集团（含单班总限额）、客户（含合同单班上限）。
- `POST /policy-versions`：登记政策版本（`params.factor_order`、是否启用合同/集团限额），`activate=true` 时自动切为当前政策。
- `POST /train-versions`：按通道登记班次版本（舱位、发车与冻结时刻、绑定政策版本）。
- `POST /demands`：申报需求（数量、战略级别 1-5、货物时限、`split_root_id`、历史履约率）。
- `POST /train-versions/{id}/freeze`：申报截止冻结需求并产出分配草案。
- `POST /demands/{id}/confirm`、`/waive`、`/shipment`：客户确认、放弃、登记装船（放弃与装船自动触发再分配）。
- `POST /train-versions/{id}/capacity`、`/cancel`：运力调整（限流/恢复）与班次取消，自动按稳定顺序再分配。
- `POST /exceptions`、`POST /exceptions/{id}/decision`：例外提议与第二位操作者审批。
- `GET /demands/{id}`：每票需求的获得/落选原因、候补位置、全版本历史与恢复后的增配来源。
- `GET /runs?version_id=`、`GET /runs/{id}`、`GET /runs/{id}/recompute`：分配版本序列、版本明细（含输入快照）、离线复算校验。
- `GET /policy-comparison?version_id=&policy_version_ids=a,b`：不同政策版本的公平性对比（只读）。

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
PYTHONPATH=src python3 -m transport_coordination.acceptance
```

验收命令会在临时 SQLite 数据库中登记运营机构、操作者、交通节点和参考资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m transport_coordination.api --database transport_coordination.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。
