# 每轮上下文观测

`SKILLGO_CONTEXT_OBSERVABILITY=true` 为 API/worker 启用额外统计。默认关闭；不改写消息、工具参数、预算、模型请求选项或既有裁剪策略，不增加模型调用，也不保存全文档案。开关生效需要重建执行服务，应在没有执行任务时切换。

每轮 `JobEvent(event_type=reasoning).data` 新增：

- `context_observation`：原始进程消息与投影后消息的条数、序列化 UTF-8 字节、估算 token；角色分布；工具返回与调用参数的分类字节；完全相同的同路径文件正文副本计数；顶层结果节选/存盘计数；五条最大消息的位置编号与大小；保留和省略的原消息编号区间（从 0 开始、含端点，最多各 64 段）。
- `segments` 位于上述对象内，分别统计固定前缀、保留历史、末尾执行状态。`fixed_skill_text_utf8_bytes` 是固定前缀中出现的独特 Skill 指令文本字节；参考架正文另有统计。这些是交叠的观察维度，不能相加当成实际输入。
- `turn_timing`：该轮前快照保存状态、耗时和快照大小；上下文组装耗时；新增观测自身耗时；模型调用总耗时。快照耗时在原推理事件的 `duration_ms` 之前，不得重复加总。工具耗时沿用各工具事件的 `duration_ms`，首响应和接收耗时沿用 `model_transport`。
- `provider_usage`：网关结果中供应商报告的输入、输出、总 token、缓存读与缓存写；缺失为 null、available=false，不能用估算值补成真实值。保留字段来源名称，缓存未命中不等于缓存写入。

统计边界是 `agent_projection_before_gateway`：即交给 agent_step 的消息视图。网关的兼容模式转换、格式纠正以及传输重试可能使最终 HTTP 消息不同；这不是网络请求全文或逐次 HTTP 计量。provider_usage 的边界为网关返回结果；网关可能合并格式纠正调用的数字用量，嵌套缓存详情不保证合并，因此不据此推导未知的单次请求缓存命中率。

所有 token 估算继续使用现有 `utf8_json_bytes_div_2`，不是目标模型 tokenizer。序列化大小包含 JSON 结构，消息/字段的大小和数组包装存在差异。完全相同正文计数只是机会观察，不是安全可删判定；不同路径不算同一文件副本。参考架去重和原有预算裁剪沿用 `context_budget` 记录。

不在新增统计中保存正文、stdout/stderr、思考内容、文件路径、工具参数、调用 ID 或正文指纹；分类工具名称使用固定集合，其余归入 other。观测失败只记录错误类型，不把异常正文写入日志，也不阻断任务。统计随现有事件保留策略清理。

只保存大小和区间，不能据此还原每轮实际全文；历史全文归档与回读是后续独立工作。当前阶段可以看到内容构成及增长、裁剪与去重数量、各段耗时，再决定是否有必要增加新的精简策略。

## 查询

通过既有任务事件接口读取 reasoning 事件数据，或只读查询：

```sql
SELECT sequence, created_at,
       data->'turn' AS turn,
       data->'context_observation' AS context_observation,
       data->'turn_timing' AS turn_timing,
       data->'provider_usage' AS provider_usage,
       data->'model_transport' AS model_transport
FROM job_events
WHERE job_id = :job_id AND event_type = 'reasoning'
ORDER BY sequence;
```

PostgreSQL 上 data 为 JSON 时上面的操作符可用。不要把 largest_messages 的索引误当成原始消息编号：它分别对应 source 或 model_view 内的位置；保留/省略区间始终对应 source。
