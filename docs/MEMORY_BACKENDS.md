# 长期记忆后端

Open-Becoming 默认使用仓库内置的 Markdown + YAML 记忆库，但聊天引擎不再强制绑定它。可以继续使用默认库、完全关闭长期记忆，或接入自己的 Python 记忆适配器。

## 三种模式

```dotenv
# 默认：内置记忆，支持查看、编辑、打标、衰减和旧 Ombre 迁移
MEMORY_BACKEND=embedded

# 关闭长期记忆；聊天摘要仍保存在 SQLite，但不会写入外置长期记忆
MEMORY_BACKEND=disabled

# 自定义：Python 模块路径 + 工厂函数
MEMORY_BACKEND=my_memory_adapter:create_backend
```

`ombre` 与 `builtin` 也会解析为默认内置后端，方便旧配置继续工作。

## 最小适配器协议

自定义工厂会收到记忆目录和固定角色 ID：

```python
def create_backend(*, memory_dir, owner_ids):
    return MyMemoryBackend(memory_dir, owner_ids)
```

返回对象只需实现两个方法：

```python
class MyMemoryBackend:
    def recall(self, owner_id):
        """返回要放进当前角色提示词的纯文本；没有记忆时返回空字符串。"""
        return ""

    def save(self, content, owner_id, **metadata):
        """保存一条记忆，并返回 ID、(ID, 是否新建) 或含 id 的字典。"""
        return "external-memory-id"
```

`owner_id` 始终是 `char1`–`char6` 之一，适配器必须按它隔离数据。`metadata` 可能包含 `source`、`source_key`、`enrichment_status`、`embedding_status` 等字段；未知字段应忽略或原样保存。异常可以直接抛出，应用会记录失败但保持聊天可用。

## 可选能力

实现下列整组方法后，相应功能会自动开放：

- 管理：`list_memories`、`get_memory`、`update_memory`、`delete_memory`
- 内置打标：`get_memory`、`apply_enrichment`、`list_needing_enrichment`
- 衰减任务：`run_decay_cycle`
- 旧库迁移：`import_legacy`

只实现最小协议时，角色仍能读取和写入外部记忆，也可以使用 JSON / TXT 文件导入；内置“记忆”管理页和旧 Ombre 迁移接口会明确返回 `501`，不会修改外部数据，也不会伪造成功结果。

## 旧记忆迁移

“猫脑壳°往事迁移”提供两条互不影响的路径。

### JSON / TXT 文件

JSON 可以直接用角色 ID 分组：

```json
{
  "char1": [
    "User 喜欢雨天。",
    {"content": "一起看过一场电影。", "importance": 7, "tags": ["共同经历"]}
  ],
  "char2": {"memories": [{"text": "User 不喝太甜的饮料。"}]}
}
```

也兼容记录数组；每条记录可用 `owner_id`、`character_id`、`character`、`char_id`、`domain` 或 `role` 指定 `char1`–`char6`：

```json
{
  "memories": [
    {"character_id": "char3", "content": "第一次一起听的歌。"},
    {"role": "char4", "text": "User 最近在准备搬家。"}
  ]
}
```

TXT 会按空行分成多条记忆。文件名含 `char1`–`char6`（例如 `char2-memory.txt`）时会自动归档；否则在页面选择一个兜底角色。JSON 也会按“记录里的角色 → 顶层角色分组 → 文件名 → 页面兜底角色”的顺序识别。认不出的记录不会被随意放进某个角色。

单个文件最大 5MB，一次最多 12 个文件、1000 条记忆。内置后端会用角色和内容生成稳定的 `source_key`，重复导入相同内容时自动跳过。自定义后端也会收到该 `source_key`，可据此实现幂等写入。

### 旧 Ombre Dashboard

默认 `embedded` 后端继续保留 Ombre 入口，可从旧 Ombre Dashboard 按 `char1`–`char6` 的 domain 导入并做角色隔离。切换到自定义后端前，建议先在默认后端完成迁移，或由自定义适配器自行实现 `import_legacy`。

## 安全边界

`MEMORY_BACKEND` 会导入服务器上的 Python 模块，因此只能由部署者设置，不能暴露给前端用户修改。接入网络记忆服务时，请把令牌放在环境变量中，不要写入仓库；同时确认该服务的隐私和数据保留政策。
