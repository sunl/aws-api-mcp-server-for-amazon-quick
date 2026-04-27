# 跨账号查询支持改造

本文档说明对 `aws-api-mcp-server` 的改造：在 MCP Server 部署于 Bedrock AgentCore Runtime 的场景下，通过 AgentCore 执行角色 `sts:AssumeRole` 到用户指定的目标账号，实现跨账号 AWS API 查询。

## 1. 需求

- 用户在对话中提供目标账号 ID，查询该账号的 AWS 资源 / 数据
- 不提供账号 ID 时，查询当前账号（MCP Server 部署所在账号）
- 支持一次对话中查询多个账号（LLM 自动拆分为多次 `call_aws` 调用，每次指定一个 `target_account_id`）
- 跨账号角色名统一命名，用户只需提供账号 ID，不需要知道 role ARN

## 2. 设计要点

- **统一角色名**：所有目标账号中使用同一个角色名（默认 `AwsApiMcpCrossAccountRole`），通过环境变量 `CROSS_ACCOUNT_ROLE_NAME` 可覆盖。
- **单一工具入口**：本项目只有一个对外工具 `call_aws`，所以只需给这一个工具加 `target_account_id` 参数，无须改动每个服务的封装。
- **底层通路复用**：`call_aws_helper` / `interpret_command` / `execute_awscli_customization` 本来就支持 `credentials: Credentials | None` 参数，本次改造只是把外层 `call_aws` 串起来，底层零改动。
- **凭证缓存**：AssumeRole 按 account_id 缓存 45 分钟（短于 STS 默认 1 小时过期），避免 LLM 高频调用触发 STS 限流。
- **安全边界由 IAM 保障**：源账号策略限制能 assume 哪些账号，目标账号的信任策略限制谁能 assume 进来。代码层不维护白名单。

## 3. 环境变量

| 环境变量 | 必填 | 说明 | 默认值 |
|---------|------|------|--------|
| `CROSS_ACCOUNT_ROLE_NAME` | 否 | 目标账号中预创建的 IAM 角色名 | `AwsApiMcpCrossAccountRole` |

行为：

- 传了 `target_account_id` → 拼 `arn:aws:iam::{target_account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}`，AssumeRole 后执行
- 没传 `target_account_id` → 不 AssumeRole，用 MCP Server 进程本身的凭证执行（即 AgentCore 执行角色）
- 传了但目标账号没有对应角色，或源账号无 assume 权限 → AssumeRole 失败，整批命令返回错误

## 4. 代码改动

### 4.1 `awslabs/aws_api_mcp_server/core/common/config.py`

新增常量与环境变量读取：

```python
CROSS_ACCOUNT_ROLE_NAME_KEY = 'CROSS_ACCOUNT_ROLE_NAME'
CROSS_ACCOUNT_ROLE_NAME_DEFAULT = 'AwsApiMcpCrossAccountRole'

# ... 文件末尾 ...
CROSS_ACCOUNT_ROLE_NAME = os.getenv(CROSS_ACCOUNT_ROLE_NAME_KEY, CROSS_ACCOUNT_ROLE_NAME_DEFAULT)
```

### 4.2 `awslabs/aws_api_mcp_server/core/aws/cross_account.py`（新建）

核心辅助模块，负责 AssumeRole 与缓存。关键部分：

```python
_CACHE_TTL_SECONDS = 45 * 60
_cache: dict[str, tuple[Credentials, float]] = {}
_cache_lock = threading.Lock()


def get_credentials_for_account(account_id: str) -> Credentials:
    _validate_account_id(account_id)  # 必须是 12 位数字

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(account_id)
        if cached is not None:
            creds, expires_at = cached
            if expires_at > now:
                return creds

    creds = _assume_role(account_id)  # 调用 STS AssumeRole，失败抛 AwsApiMcpError

    with _cache_lock:
        _cache[account_id] = (creds, time.monotonic() + _CACHE_TTL_SECONDS)
    return creds
```

设计细节：

- **account_id 校验**：正则 `^\d{12}$`，避免非法输入污染 ARN 拼接。
- **缓存 TTL 45 分钟**：STS AssumeRole 默认返回凭证有效 1 小时，提前 15 分钟刷新，保证在用凭证不会在命令执行过程中突然过期。
- **锁粒度**：只在读 / 写缓存时持锁，AssumeRole 本身不在锁内，避免不同账号的 AssumeRole 串行化。同一账号在缓存失效瞬间可能触发 2 次 AssumeRole，但结果一致、无副作用。
- **RoleSessionName**：`aws-api-mcp-{account_id}-{timestamp}`，便于在目标账号的 CloudTrail 里审计。
- **错误处理**：`ClientError` 转成 `AwsApiMcpError`，错误消息包含 role ARN 和排查建议。

### 4.3 `awslabs/aws_api_mcp_server/server.py`

改动 `call_aws` 工具和内部 `_execute_single_command`。

**新增 import：**

```python
from .core.aws.cross_account import get_credentials_for_account
```

**`call_aws` 签名新增 `target_account_id` 参数：**

```python
async def call_aws(
    cli_command: ...,
    ctx: Context,
    max_results: ... = None,
    target_account_id: Annotated[
        str | None,
        Field(
            description=(
                'Optional 12-digit AWS account ID to run the command against via '
                'STS AssumeRole. Leave unset to run against the account this server '
                'is deployed in.'
            ),
            pattern=r'^\d{12}$',
        ),
    ] = None,
) -> list[CallAWSResponse]:
```

**在进入 batch 循环前做一次 AssumeRole：**

```python
if target_account_id is not None:
    try:
        credentials = get_credentials_for_account(target_account_id)
    except AwsApiMcpError as e:
        await ctx.error(str(e))
        return [CallAWSResponse(cli_command=cmd, error=str(e)) for cmd in commands]
else:
    credentials = None

for cmd in commands:
    ...
    results.append(
        await _execute_single_command(expanded_cmd, ctx, max_results, credentials)
    )
```

关键设计：

- AssumeRole 在 batch **前**做一次，整批共用同一份临时凭证。避免 batch 里 20 条命令触发 20 次 AssumeRole。
- AssumeRole 失败时，整批命令直接返回错误，**不会** fallback 到默认凭证去误查源账号。这是一条明确的安全边界。

**`_execute_single_command` 新增 `credentials` 参数并透传：**

```python
async def _execute_single_command(
    cmd: str,
    ctx: Context,
    max_results: int | None,
    credentials: Credentials | None = None,
) -> CallAWSResponse:
    try:
        response = await call_aws_helper(cmd, ctx, max_results, credentials)
        return CallAWSResponse(cli_command=cmd, response=response)
    except Exception as e:
        return CallAWSResponse(cli_command=cmd, error=str(e))
```

**`call_aws` 工具 description 补充跨账号使用说明**，告知 LLM：

- 不传 = 查本账号
- 传 12 位账号 ID = 跨账号
- 一次 `call_aws` 只能指定一个账号，对比多个账号需要多次调用

### 4.4 文件改动汇总

| 文件 | 类型 | 说明 |
|------|------|------|
| `core/common/config.py` | 修改 | 新增 `CROSS_ACCOUNT_ROLE_NAME` 环境变量 |
| `core/aws/cross_account.py` | 新建 | AssumeRole 辅助 + 缓存 |
| `server.py` | 修改 | `call_aws` 加 `target_account_id`，`_execute_single_command` 加 `credentials` 透传 |

**未改动：** `core/aws/service.py`、`core/aws/driver.py`、`core/common/models.py` 等底层模块（它们本来就支持 `Credentials` 参数）。

## 5. AWS 侧配置

以下配置使用默认角色名 `AwsApiMcpCrossAccountRole`。如需自定义，在 AgentCore Runtime 容器环境变量中设置 `CROSS_ACCOUNT_ROLE_NAME=<你的角色名>`，并将下文中的角色名同步替换。

### 5.1 源账号（部署 MCP Server 的账号）

给 AgentCore 执行角色添加 AssumeRole 权限，Resource 列出所有允许跨账号访问的目标账号 role ARN：

```bash
# 在源账号中执行
# 注意：将 <AgentCoreRuntimeRole> 替换为实际的 AgentCore 执行角色名

cat > cross-account-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "CrossAccountAssumeRole",
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": [
      "arn:aws:iam::111111111111:role/AwsApiMcpCrossAccountRole",
      "arn:aws:iam::222222222222:role/AwsApiMcpCrossAccountRole"
    ]
  }]
}
EOF

aws iam put-role-policy \
  --role-name <AgentCoreRuntimeRole> \
  --policy-name AwsApiMcpCrossAccountAssumePolicy \
  --policy-document file://cross-account-policy.json
```

如果目标账号数量较多或会动态增减，也可以使用通配符（注意放宽了权限边界）：

```json
"Resource": "arn:aws:iam::*:role/AwsApiMcpCrossAccountRole"
```

### 5.2 每个目标账号

在**每个**目标账号中创建同名角色 `AwsApiMcpCrossAccountRole`。

**信任策略（trust policy）：**

```bash
# 在目标账号中执行
# 注意：将 <源账号ID> 和 <AgentCoreRuntimeRole> 替换为实际值

cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::<源账号ID>:role/<AgentCoreRuntimeRole>"
    },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name AwsApiMcpCrossAccountRole \
  --assume-role-policy-document file://trust-policy.json \
  --description "Cross-account role for aws-api-mcp-server"
```

**权限策略：** 根据你允许 LLM 查询的范围挂载。建议最小权限原则——只读场景下可以直接挂 AWS 托管策略 `ReadOnlyAccess`：

```bash
aws iam attach-role-policy \
  --role-name AwsApiMcpCrossAccountRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

需要写操作时请自定义策略，不要直接挂 `AdministratorAccess`。

## 6. 用户体验示例

```
用户：查看账号 111111111111 的 EC2 实例
→ LLM 调用 call_aws(cli_command="aws ec2 describe-instances", target_account_id="111111111111")

用户：列出当前账号的 S3 bucket
→ LLM 调用 call_aws(cli_command="aws s3api list-buckets")  # 不传 target_account_id

用户：对比账号 111111111111 和 222222222222 的 RDS 实例
→ LLM 分别调用两次 call_aws，各传一个 target_account_id，最后汇总结果

用户：查询账号 111111111111 在 us-east-1 和 eu-west-1 的 Lambda 函数
→ LLM 调用一次 call_aws，batch 两条命令（分别指定 --region），target_account_id 都是 111111111111
  （同一 account_id 的 batch 只需一次 AssumeRole）
```

## 7. 安全考量

- **双向 IAM 边界**：能访问哪些账号由源账号策略决定，允许被谁访问由目标账号信任策略决定。这是比代码层白名单更可靠的边界。
- **AssumeRole 失败快速失败**：失败时不会 fallback 到源账号凭证查询，杜绝"本来要查 A 账号结果误查到本账号"的风险。
- **审计**：RoleSessionName 格式为 `aws-api-mcp-{account_id}-{timestamp}`，目标账号的 CloudTrail 能清晰看到会话归属。
- **凭证缓存仅在进程内**：临时凭证保存在 Python 进程内存中，不落盘、不跨进程共享。容器重启即清除。
- **未实现 ExternalId**：当前场景（源账号与目标账号属于同一主体 / 组织）不存在混淆代理攻击风险。若未来扩展为多租户 SaaS，可在信任策略中加 `Condition: StringEquals sts:ExternalId` 并在 `_assume_role` 调用处带上对应值。

## 8. 不支持 / 已知限制

- `suggest_aws_commands` 工具不涉及实际 AWS API 调用，无跨账号语义，未改动。
- `get_execution_plan` 工具（EXPERIMENTAL_AGENT_SCRIPTS 开启时才注册）只返回脚本文本，无跨账号语义，未改动。
- 一次 `call_aws` 调用只能针对一个 account_id；batch 中不同命令无法指定不同目标账号。LLM 如需跨多账号操作，需拆成多次 `call_aws`。
- 写操作与 `READ_OPERATIONS_ONLY` / `REQUIRE_MUTATION_CONSENT` 的交互：跨账号命令同样走策略检查。建议在目标账号的 IAM 角色权限上再做一层限制（例如生产账号只给读权限）。
