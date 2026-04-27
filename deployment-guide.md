# 部署 AWS API MCP Server 到 AgentCore Runtime 并接入 Quick Suite

## 1. 方案概述

### 1.1 目标

部署本仓库（基于 [awslabs/mcp/aws-api-mcp-server](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server) 改造）到 Amazon Bedrock AgentCore Runtime，并通过 Amazon Quick Suite 的 Chat Agent 以 Service Authentication (2LO) 方式调用，使业务用户能够在对话界面中以自然语言执行任意 AWS CLI 命令。

本仓库相对上游新增的跨账号查询支持，详见 [cross-account-support.md](./cross-account-support.md)。

### 1.2 架构

```
Amazon Quick Suite Chat Agent (MCP 客户端)
        │
        │  HTTPS (streamable-http + OAuth 2.0 / 2LO client_credentials)
        ▼
Amazon Bedrock AgentCore Runtime
        │
        │  容器内部 0.0.0.0:8000/mcp
        ▼
aws-api-mcp-server (ARM64 容器)
        │  工具 call_aws
        │  执行任意 AWS CLI 命令
        ▼
源账号 AWS API（默认）
   │
   │  可选 sts:AssumeRole (target_account_id)
   ▼
目标账号 AWS API（跨账号查询）
```

### 1.3 关键约束

| 约束项 | 要求 |
|--------|------|
| 传输协议 | `streamable-http`（`AWS_API_MCP_TRANSPORT=streamable-http`） |
| 监听地址 | `0.0.0.0:8000`（由 `AWS_API_MCP_HOST` / `AWS_API_MCP_PORT` 控制） |
| HTTP Host/Origin 校验 | AgentCore 会代理请求，需放开 `AWS_API_MCP_ALLOWED_HOSTS=*` 与 `AWS_API_MCP_ALLOWED_ORIGINS=*` |
| 容器架构 | ARM64（AWS Graviton），由 AgentCore Starter Toolkit 自动构建 |
| 认证方式 | AgentCore 平台层做 OAuth 2.0 JWT 校验（Cognito）；容器内 MCP Server 必须 `AUTH_TYPE=no-auth`（官方硬性要求） |
| 会话模式 | `AWS_API_MCP_STATELESS_HTTP=true`（AgentCore 已在平台层提供会话隔离） |
| Quick Suite 认证 | Service authentication (2LO)，需将 Quick Suite 的 M2M Client ID 加入 AgentCore `allowedClients` |

> **关于 `AUTH_TYPE=no-auth`**：参见仓库 `DEPLOYMENT.md` 的 "Understanding AWS API Authentication on AgentCore" 一节。AgentCore 在 Runtime 层集中做入站认证，容器内的 MCP Server 接收的请求已经由 AgentCore 验证过。此 MCP Server **不支持**容器内的入站认证；设成 `oauth` 会因与 AgentCore 的 auth 层重复或 header 不匹配而失败。安全由 AgentCore JWT Authorizer + IAM 执行角色权限这两层共同保证。

> 参考文档：
> - [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
> - [MCP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html)
> - [Amazon Quick Suite MCP integration](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)

### 1.4 两种部署路径对比

| 方式 | 镜像来源 | 部署工具 | 适用场景 |
|------|---------|---------|----------|
| **A. 本地构建**（本文档使用） | 你自己的代码 + AgentCore 自动构建 | `agentcore` CLI（Starter Toolkit） | 你改过源码（例如加了跨账号支持），或想跟进上游最新代码 |
| B. Marketplace 预构建镜像 | `709825985650.dkr.ecr.us-east-1.amazonaws.com/amazon-web-services/aws-api-mcp-server` | `aws bedrock-agentcore-control create-agent-runtime` 原始 API | 使用上游未修改版，图省事 |

由于本项目加了跨账号支持（参见 [cross-account-support.md](./cross-account-support.md)），**只能走方式 A**。官方上游 `DEPLOYMENT.md` 对应方式 B，可作为交叉参考，但环境变量、IAM 权限等配置要求两种方式是一致的。

---

## 2. 前置条件

- Python 3.10+、git、jq
- AWS CLI v2 已配置凭证（可访问源账号）
- Amazon Quick Suite Enterprise 订阅，用户具有 Author Pro 角色

```bash
aws sts get-caller-identity
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

---

## 2.5 重要限制与安全声明（务必先读）

以下限制来自上游 `DEPLOYMENT.md` 和 AgentCore Runtime 本身，在决定是否采用本方案前请务必了解。

### 单用户架构，不适合多租户

> **AgentCore 的 session 隔离只防止"请求之间"数据泄漏，不能防止"不同用户之间"的数据泄漏。**

- 本 MCP Server 的设计就是给**单个使用者**用的。
- Quick Suite 团队场景下，所有最终用户共享同一个 AgentCore endpoint 和同一个 IAM 执行角色——他们看到的 AWS 数据范围完全相同。
- 如果不同角色需要访问不同数据，请**部署多个独立的 Runtime**（各自绑定不同的执行角色和 Quick Suite 集成）。

### AgentCore 平台的硬性限制

| 限制 | 影响 |
|------|------|
| **文件下载不可取回** | `call_aws` 执行 `aws s3 cp`、`aws logs tail > file` 等操作，文件确实被下载到了容器里，但 stateless 容器在请求结束后即销毁，Quick Suite 端拿不到文件 |
| **不支持实时流式响应** | AgentCore 会 buffer 完整响应才返回，类似 `aws logs tail --follow` 的实时输出会被切断 |
| **不支持 MCP elicitation** | `REQUIRE_MUTATION_CONSENT=true` 想要求用户确认的写操作，在 Quick Suite 里不会弹出确认框——会直接失败或被忽略。生产环境请改用 `READ_OPERATIONS_ONLY=true` + 最小权限 IAM 来约束 |
| **单次请求 60 秒超时** | Quick Suite 侧约束。复杂查询或跨多 region 的 `--region *` 调用容易超时 |

### Prompt 注入风险

`call_aws` 会按 LLM 生成的任意 AWS CLI 命令执行。如果对话中混入不可信内容（例如 CloudWatch 日志里的用户输入、S3 对象里的文本），可能被构造成提示词诱导 LLM 执行非预期命令。**防御只能靠 IAM 权限最小化 + `READ_OPERATIONS_ONLY`**，不要指望在 Prompt 层面做白名单。

---

## 3. 获取源码并安装依赖

### 步骤 1：克隆源码

```bash
git clone https://github.com/sunl/aws-api-mcp-server-for-amazon-quick.git
cd aws-api-mcp-server-for-amazon-quick
```

### 步骤 2：安装依赖

**关键：`agentcore` CLI 和项目运行时依赖要装在不同的环境里。**

`bedrock-agentcore-starter-toolkit`（提供 `agentcore` 命令）是本地部署工具，不属于项目运行时依赖，也不会被打进容器。如果装进项目 venv，随后执行 `uv sync --frozen` 会把它清掉（因为 `pyproject.toml` 里没声明它）。

#### 2a. 全局安装 `agentcore` CLI

推荐用 `uv tool install` 或 `pipx`，它们会把 CLI 装到独立的隔离环境：

```bash
# 选项一：uv tool（如果你已经装了 uv）
uv tool install bedrock-agentcore-starter-toolkit

# 选项二：pipx
pipx install bedrock-agentcore-starter-toolkit

agentcore --help
```

#### 2b.（可选）为本地测试准备项目 venv

**只有你打算按步骤 3 在本地跑一次 MCP Server 时才需要这一步。**
如果你直接用 `agentcore configure / launch` 部署到 AgentCore Runtime，**跳过 2b 即可**——AgentCore 会在容器里自己装依赖。

```bash
uv sync --frozen --no-dev
```

这会在当前目录创建 `.venv` 并按 `uv.lock` 装好项目依赖。后续 `uv run awslabs.aws-api-mcp-server` 会用这个 venv。

### 步骤 3：本地测试（可选）

```bash
AWS_API_MCP_TRANSPORT=streamable-http \
AUTH_TYPE=no-auth \
AWS_API_MCP_HOST=127.0.0.1 \
AWS_API_MCP_PORT=8000 \
AWS_REGION=${AWS_REGION} \
  uv run awslabs.aws-api-mcp-server
```

新开终端验证 MCP initialize：

```bash
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

预期返回包含 `serverInfo` 和 `call_aws`、`suggest_aws_commands` 两个工具的 JSON 响应。

> 注意：MCP 协议要求 `Accept` header 同时包含 `application/json` 和 `text/event-stream`。

---

## 4. 配置 IAM 执行角色

本节创建 AgentCore 容器运行时使用的执行角色。**跨账号查询的额外配置请在第 6 节处理**。

### 步骤 4：创建信任策略和角色

```bash
cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeRolePolicy",
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${AWS_ACCOUNT_ID}"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"}
    }
  }]
}
EOF

aws iam create-role \
  --role-name AwsApiMcpServerAgentCoreRole \
  --assume-role-policy-document file://trust-policy.json \
  --description "AgentCore Runtime Role - AWS API MCP Server"
```

### 步骤 5：添加 AgentCore 基础权限

```bash
cat > agentcore-base-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid": "ECRImageAccess", "Effect": "Allow", "Action": ["ecr:BatchGetImage","ecr:GetDownloadUrlForLayer"], "Resource": ["arn:aws:ecr:${AWS_REGION}:${AWS_ACCOUNT_ID}:repository/*"]},
    {"Sid": "ECRTokenAccess", "Effect": "Allow", "Action": ["ecr:GetAuthorizationToken"], "Resource": "*"},
    {"Sid": "LogsCreate", "Effect": "Allow", "Action": ["logs:DescribeLogStreams","logs:CreateLogGroup"], "Resource": ["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*"]},
    {"Sid": "LogsDescribe", "Effect": "Allow", "Action": ["logs:DescribeLogGroups"], "Resource": ["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:*"]},
    {"Sid": "LogsWrite", "Effect": "Allow", "Action": ["logs:CreateLogStream","logs:PutLogEvents"], "Resource": ["arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]},
    {"Sid": "XRay", "Effect": "Allow", "Action": ["xray:PutTraceSegments","xray:PutTelemetryRecords","xray:GetSamplingRules","xray:GetSamplingTargets"], "Resource": "*"},
    {"Sid": "Metrics", "Effect": "Allow", "Action": "cloudwatch:PutMetricData", "Resource": "*", "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
    {"Sid": "GetAgentAccessToken", "Effect": "Allow", "Action": ["bedrock-agentcore:GetWorkloadAccessToken","bedrock-agentcore:GetWorkloadAccessTokenForJWT","bedrock-agentcore:GetWorkloadAccessTokenForUserId"], "Resource": ["arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:workload-identity-directory/default","arn:aws:bedrock-agentcore:${AWS_REGION}:${AWS_ACCOUNT_ID}:workload-identity-directory/default/workload-identity/*"]}
  ]
}
EOF

aws iam put-role-policy \
  --role-name AwsApiMcpServerAgentCoreRole \
  --policy-name AgentCoreBasePolicy \
  --policy-document file://agentcore-base-policy.json
```

### 步骤 6：添加 AWS API 查询权限（**最关键的一步**）

与 billing MCP 只需要固定几类 API 不同，`call_aws` 工具是通用 AWS CLI 执行器，**能调什么 API 完全取决于你给执行角色的权限**。官方 `DEPLOYMENT.md` 明确警告：**绝不要挂 `AdministratorAccess`**。

**选项 A（推荐起步）：挂 `ReadOnlyAccess`**

```bash
aws iam attach-role-policy \
  --role-name AwsApiMcpServerAgentCoreRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

**选项 B：自定义策略（只允许特定服务）**

按最小权限原则为你实际需要的服务写 custom policy，示例：

```bash
cat > custom-aws-permissions.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:GetObject","s3:ListBucket"], "Resource": ["arn:aws:s3:::your-bucket","arn:aws:s3:::your-bucket/*"]},
    {"Effect": "Allow", "Action": ["ec2:DescribeInstances","ec2:DescribeImages"], "Resource": "*", "Condition": {"StringEquals": {"ec2:Region": "${AWS_REGION}"}}}
  ]
}
EOF

aws iam put-role-policy \
  --role-name AwsApiMcpServerAgentCoreRole \
  --policy-name CustomAWSPermissions \
  --policy-document file://custom-aws-permissions.json
```

建议同时在部署时开启 `READ_OPERATIONS_ONLY=true`（见步骤 13）作为代码级的第二道防线。

```bash
export EXECUTION_ROLE_ARN=$(aws iam get-role --role-name AwsApiMcpServerAgentCoreRole --query 'Role.Arn' --output text)
echo "Execution Role ARN: ${EXECUTION_ROLE_ARN}"
```

---

## 5. 设置 Cognito 认证

### 步骤 7：创建 User Pool 和测试用户

```bash
export POOL_NAME="AwsApiMcpPool"

export POOL_ID=$(aws cognito-idp create-user-pool \
  --pool-name "${POOL_NAME}" \
  --policies '{"PasswordPolicy":{"MinimumLength":8}}' \
  --region ${AWS_REGION} | jq -r '.UserPool.Id')

export CLIENT_ID=$(aws cognito-idp create-user-pool-client \
  --user-pool-id ${POOL_ID} \
  --client-name "AwsApiMcpTestClient" \
  --no-generate-secret \
  --explicit-auth-flows "ALLOW_USER_PASSWORD_AUTH" "ALLOW_REFRESH_TOKEN_AUTH" \
  --region ${AWS_REGION} | jq -r '.UserPoolClient.ClientId')

export COGNITO_USERNAME="testuser"
export COGNITO_PASSWORD="<设置一个强密码>"

aws cognito-idp admin-create-user \
  --user-pool-id ${POOL_ID} --username ${COGNITO_USERNAME} \
  --region ${AWS_REGION} --message-action SUPPRESS > /dev/null

aws cognito-idp admin-set-user-password \
  --user-pool-id ${POOL_ID} --username ${COGNITO_USERNAME} \
  --password ${COGNITO_PASSWORD} --region ${AWS_REGION} --permanent > /dev/null

export DISCOVERY_URL="https://cognito-idp.${AWS_REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration"
echo "Discovery URL: ${DISCOVERY_URL}"
echo "Client ID:     ${CLIENT_ID}"
```

### 步骤 8：创建 Cognito Domain

```bash
export COGNITO_DOMAIN_PREFIX="aws-api-mcp-$(echo ${AWS_ACCOUNT_ID} | tail -c 9)"

aws cognito-idp create-user-pool-domain \
  --user-pool-id ${POOL_ID} \
  --domain ${COGNITO_DOMAIN_PREFIX} \
  --region ${AWS_REGION}

echo "Cognito Domain: ${COGNITO_DOMAIN_PREFIX}"
```

### 步骤 9：创建 Resource Server

Service authentication (2LO) 使用 `client_credentials`，Cognito 要求必须有 Resource Server 定义 scope。

```bash
aws cognito-idp create-resource-server \
  --user-pool-id ${POOL_ID} \
  --identifier "aws-api-mcp" \
  --name "AWS API MCP Server" \
  --scopes '[{"ScopeName":"invoke","ScopeDescription":"Invoke AWS API MCP Server"}]' \
  --region ${AWS_REGION}
```

### 步骤 10：创建 Machine-to-Machine App Client（Quick Suite 专用）

```bash
QS_M2M_RESULT=$(aws cognito-idp create-user-pool-client \
  --user-pool-id ${POOL_ID} \
  --client-name "QuickSuiteM2MClient" \
  --generate-secret \
  --allowed-o-auth-flows "client_credentials" \
  --allowed-o-auth-scopes "aws-api-mcp/invoke" \
  --allowed-o-auth-flows-user-pool-client \
  --supported-identity-providers "COGNITO" \
  --region ${AWS_REGION})

export QS_M2M_CLIENT_ID=$(echo ${QS_M2M_RESULT} | jq -r '.UserPoolClient.ClientId')
export QS_M2M_CLIENT_SECRET=$(echo ${QS_M2M_RESULT} | jq -r '.UserPoolClient.ClientSecret')

echo "M2M Client ID:     ${QS_M2M_CLIENT_ID}"
echo "M2M Client Secret: ${QS_M2M_CLIENT_SECRET}"
```

---

## 6. 跨账号查询配置（可选）

如果你希望通过 `target_account_id` 参数查询**其他账号**的 AWS 数据，需要在源账号和每个目标账号上分别配置 IAM。不需要跨账号查询可跳过本节。设计原理参见 [cross-account-support.md](./cross-account-support.md)。

默认角色名为 `AwsApiMcpCrossAccountRole`。如需自定义，部署时通过环境变量 `CROSS_ACCOUNT_ROLE_NAME` 覆盖，并同步替换本节的角色名。

### 步骤 11：给源账号执行角色添加 AssumeRole 权限

```bash
cat > cross-account-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "CrossAccountAssumeRole",
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": [
      "arn:aws:iam::<目标账号A>:role/AwsApiMcpCrossAccountRole",
      "arn:aws:iam::<目标账号B>:role/AwsApiMcpCrossAccountRole"
    ]
  }]
}
EOF

aws iam put-role-policy \
  --role-name AwsApiMcpServerAgentCoreRole \
  --policy-name CrossAccountAssumeRolePolicy \
  --policy-document file://cross-account-policy.json
```

### 步骤 12：在每个目标账号中创建同名角色

```bash
# 在目标账号中执行

cat > trust-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "AWS": "arn:aws:iam::<源账号ID>:role/AwsApiMcpServerAgentCoreRole"
    },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name AwsApiMcpCrossAccountRole \
  --assume-role-policy-document file://trust-policy.json \
  --description "Cross-account role for AWS API MCP Server"

# 最小权限起步，视需要收紧
aws iam attach-role-policy \
  --role-name AwsApiMcpCrossAccountRole \
  --policy-arn arn:aws:iam::aws:policy/ReadOnlyAccess
```

---

## 7. 配置并部署到 AgentCore Runtime

### 步骤 13：运行 agentcore configure

```bash
agentcore configure -e awslabs/aws_api_mcp_server/server.py --protocol MCP
```

交互式引导会在项目根目录生成 `.bedrock_agentcore.yaml`。按下表填写：

| 提示项 | 输入内容 | 说明 |
|--------|---------|------|
| Agent name | `aws_api_mcp_server` | AgentCore 中的 agent 标识名 |
| Dependency file | 直接回车（工具会识别 `pyproject.toml` / `uv.lock`；若提示无法识别，参考下方"关于 Dockerfile"说明） | |
| Execution role ARN | `${EXECUTION_ROLE_ARN}` 的实际值 | 步骤 4 创建的角色 ARN |
| ECR repository | 直接回车（留空） | 工具会自动创建 |
| OAuth | `yes` | 启用 AgentCore 入站认证 |
| Discovery URL | `${DISCOVERY_URL}` 的实际值 | 步骤 7 输出 |
| Client ID | `${CLIENT_ID},${QS_M2M_CLIENT_ID}` 用逗号连接 | 步骤 7 的测试用 client 和步骤 10 的 M2M client 都要加入 |
| Memory Configuration | `s` 跳过 | 本 MCP 无状态，不需要 AgentCore Memory |

完成后 `.bedrock_agentcore.yaml` 类似：

```yaml
default_agent: aws_api_mcp_server

entry_point: awslabs/aws_api_mcp_server/server.py
execution_role: arn:aws:iam::123456789012:role/AwsApiMcpServerAgentCoreRole
ecr_repository: 123456789012.dkr.ecr.us-east-1.amazonaws.com/bedrock-agentcore-aws_api_mcp_server
protocol_configuration:
  server_protocol: MCP

authorizer_configuration:
  customJWTAuthorizer:
    discoveryUrl: https://cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXXXXX/.well-known/openid-configuration
    allowedClients:
    - <步骤7的CLIENT_ID>
    - <步骤10的QS_M2M_CLIENT_ID>   # Quick Suite 用它；不加会导致 Quick Suite 连不上

# 容器运行时环境变量——部署前必须补全
environment_variables:
  AWS_REGION: us-east-1
  AWS_API_MCP_TRANSPORT: streamable-http
  AWS_API_MCP_HOST: 0.0.0.0
  AWS_API_MCP_PORT: "8000"
  AWS_API_MCP_ALLOWED_HOSTS: "*"
  AWS_API_MCP_ALLOWED_ORIGINS: "*"
  AWS_API_MCP_STATELESS_HTTP: "true"
  AUTH_TYPE: no-auth                   # 官方硬性要求：容器内不做入站认证
  READ_OPERATIONS_ONLY: "true"         # 强烈建议开启
  # 跨账号相关（仅当第 6 节完成时才有意义）
  # CROSS_ACCOUNT_ROLE_NAME: AwsApiMcpCrossAccountRole  # 非默认名才需要设
```

**关键字段说明（全部必需，除非注明）：**

- `AWS_REGION` —— MCP Server 启动时校验此变量必须存在。AgentCore 平台通常会自动把部署所在区域注入到容器，但为了避免平台行为变化导致启动失败，**建议显式设置**。
- `AWS_API_MCP_TRANSPORT: streamable-http` —— 默认 `stdio`，不改 AgentCore 连不上。
- `AWS_API_MCP_HOST: 0.0.0.0` —— 默认 `127.0.0.1`，不改会导致 AgentCore 无法连接容器内的服务。
- `AWS_API_MCP_PORT: "8000"` —— AgentCore 默认把流量转发到容器 8000 端口。
- `AWS_API_MCP_ALLOWED_HOSTS / ALLOWED_ORIGINS: "*"` —— AgentCore 代理请求时的 Host header 不可预测；默认的严格校验会拒绝所有请求。安全由 AgentCore 入站认证和 IAM 保证。
- `AWS_API_MCP_STATELESS_HTTP: "true"` —— AgentCore 已在平台层做会话隔离；容器内不需要再维护 session。
- `AUTH_TYPE: no-auth` —— **官方硬性要求**。入站认证由 AgentCore Runtime 的 JWT Authorizer 统一处理（就是 `.bedrock_agentcore.yaml` 里 `customJWTAuthorizer` + `allowedClients` 那段）。若设为 `oauth`，容器内的 FastMCP 会再做一次 JWT 校验，与 AgentCore 层冲突导致请求被拒。
- `READ_OPERATIONS_ONLY: "true"` —— 即使执行角色挂的是 `ReadOnlyAccess`，再加一层代码级白名单更稳妥。生产环境如需写操作再设为 `false`。

> **关于 Dockerfile**：仓库自带 `Dockerfile`，`agentcore configure` 默认会自己生成一个。如果你想用仓库自带的 Dockerfile（已经配好 uv + Python 3.13 + 合适的 entrypoint），在 `.bedrock_agentcore.yaml` 中手动加 `container_runtime: use_existing_dockerfile: true` 或直接让它自动生成——两种都能跑起来。如果 Starter Toolkit 提示缺 `requirements.txt`，执行一下 `uv pip compile pyproject.toml -o requirements.txt` 生成一份兼容的依赖清单即可。

### 步骤 14：部署

```bash
agentcore launch
```

构建 + 推送 ECR + 创建 Runtime 大约需 5–10 分钟。完成后记录输出的 Agent ARN：

```bash
export AGENT_ARN="<输出的 ARN>"
```

### 步骤 15：验证部署

```bash
# 获取 Bearer Token（用步骤 7 的测试 user client + user/pass）
export BEARER_TOKEN=$(aws cognito-idp initiate-auth \
  --client-id "${CLIENT_ID}" \
  --auth-flow USER_PASSWORD_AUTH \
  --auth-parameters USERNAME=${COGNITO_USERNAME},PASSWORD=${COGNITO_PASSWORD} \
  --region ${AWS_REGION} | jq -r '.AuthenticationResult.AccessToken')

ENCODED_ARN=$(echo -n ${AGENT_ARN} | jq -sRr '@uri')
MCP_ENDPOINT="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT"

# 发送 MCP initialize 请求
curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

预期返回 `serverInfo` 和 `call_aws` / `suggest_aws_commands` 两个工具（若开启了 `EXPERIMENTAL_AGENT_SCRIPTS` 会多一个 `get_execution_plan`）。

再用一个查本账号的 `call_aws` 请求验证执行链路：

```bash
curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"call_aws","arguments":{"cli_command":"aws s3api list-buckets"}}}'
```

若第 6 节已配跨账号，再验证一次：

```bash
curl -sS -X POST "${MCP_ENDPOINT}" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer ${BEARER_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"call_aws","arguments":{"cli_command":"aws s3api list-buckets","target_account_id":"<目标账号12位ID>"}}}'
```

---

## 8. 接入 Amazon Quick Suite Chat Agent

### 步骤 16：构造端点 URL 和认证信息

```bash
ENCODED_ARN=$(echo -n ${AGENT_ARN} | jq -sRr '@uri')
MCP_SERVER_ENDPOINT="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations"
TOKEN_URL="https://${COGNITO_DOMAIN_PREFIX}.auth.${AWS_REGION}.amazoncognito.com/oauth2/token"

echo "========================================="
echo "Quick Suite Service Auth 所需信息："
echo "========================================="
echo "MCP Server 端点: ${MCP_SERVER_ENDPOINT}"
echo "Client ID:       ${QS_M2M_CLIENT_ID}"
echo "Client Secret:   ${QS_M2M_CLIENT_SECRET}"
echo "Token URL:       ${TOKEN_URL}"
echo "========================================="
```

### 步骤 17：在 Quick Suite 控制台创建 MCP Actions 集成

1. 登录 [Amazon Quick Suite 控制台](https://quicksight.aws.amazon.com/)（需 Author Pro）
2. 左侧导航栏 → Connections → Integrations → Actions
3. 在 "Model Context Protocol" 卡片上点击 "+"
4. 填写集成信息：
   - Name: 如 `AWS API MCP`
   - MCP server endpoint: 上面输出的 `MCP_SERVER_ENDPOINT`
5. Next
6. 认证方式选 "Service authentication"，填：
   - Client ID: `${QS_M2M_CLIENT_ID}`
   - Client Secret: `${QS_M2M_CLIENT_SECRET}`
   - Token URL: `${TOKEN_URL}`
7. "Create and continue"
8. 等待工具发现完成（约 1–2 分钟），确认能看到 `call_aws` / `suggest_aws_commands` 这 2 个 Actions
9. Next → 可选共享 → Done

### 步骤 18：在 Chat Agent 中使用

打开 Chat Agents，选择 "My Assistant" 或自定义 Agent，输入：

```
列出 us-east-1 所有运行中的 EC2 实例
我账号下有哪些 S3 bucket？
查询账号 111111111111 的 Lambda 函数列表       # 跨账号，需先完成第 6 节
对比账号 111111111111 和 222222222222 的 RDS 实例  # LLM 会分两次 call_aws
```

> - 每次 MCP 操作有 60 秒超时限制（AgentCore 侧约束）。
> - 写操作会弹出 Action Review；如果开启了 `READ_OPERATIONS_ONLY=true`，写操作会被服务端直接拒绝。
> - 工具列表在首次注册后是静态的；如果以后在服务端增加新工具，需要删除并重新创建 Quick Suite 集成。

---

## 9. 日常运维

```bash
# 查看运行时日志
aws logs tail /aws/bedrock-agentcore/runtimes/<你的 agent-id>-DEFAULT \
  --log-stream-name-prefix "$(date +%Y/%m/%d)/[runtime-logs]" \
  --since 1h --region ${AWS_REGION}

# 停止当前测试会话
agentcore stop-session

# 修改源码后重新部署
agentcore launch

# 完全清理（会删除 Runtime，但保留 ECR 镜像）
agentcore destroy
```

---

## 10. 故障排查

| 现象 | 原因与解决 |
|------|-----------|
| Quick Suite "Creation failed"、只出现 `listTools` 失败 | AgentCore `allowedClients` 未包含 M2M Client ID。编辑 `.bedrock_agentcore.yaml` 添加后 `agentcore launch` |
| Cognito 返回 `invalid_scope` | Resource Server 未创建或 scope 名不一致，核对步骤 9 与步骤 10 的 `aws-api-mcp/invoke` |
| 容器启动即失败，日志 `AWS_REGION environment variable is not defined` | `.bedrock_agentcore.yaml` 的 `environment_variables` 里没配 `AWS_REGION` |
| 容器内 MCP Server 只监听 127.0.0.1 | 忘了设 `AWS_API_MCP_HOST=0.0.0.0` |
| 400 Bad Request `Invalid host / origin` | `AWS_API_MCP_ALLOWED_HOSTS` / `AWS_API_MCP_ALLOWED_ORIGINS` 没设 `*` |
| 401 Unauthorized | Bearer Token 过期（1h），重新获取；或 AgentCore 的 `allowedClients` 没列该 client_id |
| 所有请求都 401 / auth 报错，且确认 Token 正确 | 很可能误把 `AUTH_TYPE` 设成了 `oauth`。改回 `no-auth` 并重新 `agentcore launch` |
| 403 AccessDenied（call_aws 执行时） | 执行角色缺对应 AWS API 权限；或启用了 `READ_OPERATIONS_ONLY` 而命令是写操作 |
| 跨账号调用报 `Failed to assume role ...` | 目标账号未创建 `AwsApiMcpCrossAccountRole`，或信任策略不允许源执行角色，或源角色没有 `sts:AssumeRole` 权限 |
| curl 测试返回 400 / 406 | `Accept` header 必须同时带 `application/json, text/event-stream` |
| `agentcore invoke` 返回 400 | 正常：该命令跳过了 MCP `initialize` 握手。用 `curl` 先发 `initialize` 验证 |
| `ModuleNotFoundError: awslabs.aws_api_mcp_server` | 容器内依赖安装不完整；检查 Dockerfile 层（`uv sync --no-editable` 必须成功）或 `requirements.txt` 是否覆盖全部依赖 |

---

## 11. 参考文档

- [aws-api-mcp-server 上游源码](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
- [跨账号查询方案](./cross-account-support.md)
- [FastMCP 文档](https://github.com/jlowin/fastmcp)
- [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
- [MCP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html)
- [IAM Permissions for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Authenticate and authorize with Inbound Auth and Outbound Auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)
- [Amazon Cognito as identity provider](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-cognito.html)
- [Amazon Quick Suite MCP integration](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)
