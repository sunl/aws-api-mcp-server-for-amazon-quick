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
| 容器架构 | ARM64（AWS Graviton），由 AgentCore CLI 基于本仓库 Dockerfile 自动构建 |
| 认证方式 | AgentCore 平台层做 OAuth 2.0 JWT 校验（Cognito）；容器内 MCP Server 必须 `AUTH_TYPE=no-auth`（官方硬性要求） |
| 会话模式 | `AWS_API_MCP_STATELESS_HTTP=true`（AgentCore 已在平台层提供会话隔离） |
| Quick Suite 认证 | Service authentication (2LO)，需将 Quick Suite 的 M2M Client ID 加入 AgentCore `allowedClients` |

> **关于 `AUTH_TYPE=no-auth`**：参见仓库 `DEPLOYMENT.md` 的 "Understanding AWS API Authentication on AgentCore" 一节。AgentCore 在 Runtime 层集中做入站认证，容器内的 MCP Server 接收的请求已经由 AgentCore 验证过。此 MCP Server **不支持**容器内的入站认证；设成 `oauth` 会因与 AgentCore 的 auth 层重复或 header 不匹配而失败。安全由 AgentCore JWT Authorizer + IAM 执行角色权限这两层共同保证。

> 参考文档：
> - [AgentCore CLI GitHub](https://github.com/aws/agentcore-cli)
> - [Get started with the AgentCore CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html)
> - [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
> - [MCP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html)
> - [Amazon Quick Suite MCP integration](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)

### 1.4 两种部署路径对比

| 方式 | 镜像来源 | 部署工具 | 适用场景 |
|------|---------|---------|----------|
| **A. 本地构建**（本文档使用） | 你自己的代码 + AgentCore CLI 自动构建 | `@aws/agentcore` CLI | 你改过源码（例如加了跨账号支持），或想跟进上游最新代码 |
| B. Marketplace 预构建镜像 | `709825985650.dkr.ecr.us-east-1.amazonaws.com/amazon-web-services/aws-api-mcp-server` | `aws bedrock-agentcore-control create-agent-runtime` 原始 API | 使用上游未修改版，图省事 |

由于本项目加了跨账号支持（参见 [cross-account-support.md](./cross-account-support.md)），**只能走方式 A**。官方上游 `DEPLOYMENT.md` 对应方式 B，可作为交叉参考，但环境变量、IAM 权限等配置要求两种方式是一致的。

---

## 2. 前置条件与工具安装

### 前置条件

- Python 3.10+（与项目 Dockerfile 和 `uv.lock` 锁定版本一致；`pyproject.toml` 理论上支持 `>=3.10`，但其他版本可能在 `uv sync --frozen` 时触发锁文件重建）
- git、jq
- AWS CLI v2 已配置凭证（可访问源账号）
- Amazon Quick Suite Enterprise 订阅，用户具有 Author Pro 角色

### 步骤 1：安装 Node.js、Python、uv 和 AgentCore CLI

> **注意**：AgentCore CLI 已从旧版 Python 包 `bedrock-agentcore-starter-toolkit` 迁移到新版 npm 包 `@aws/agentcore`。新版命令行是 `agentcore create` + `agentcore deploy`，取代了旧版的 `agentcore configure` + `agentcore launch`；项目配置文件也从 `.bedrock_agentcore.yaml` 迁移到 `agentcore/agentcore.json`。
> 如果之前装过旧版 CLI，请先卸载：`pip uninstall bedrock-agentcore-starter-toolkit` 或 `uv tool uninstall bedrock-agentcore-starter-toolkit`。

```bash
# 安装 Node.js 22（使用 nvm）
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash
source ~/.bashrc
nvm install 22
nvm use 22
node --version

# 安装 uv（Python 包管理器，本项目用它管理依赖和 venv）
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc
uv --version

# 用 uv 安装 Python 3.14（无需 root，不影响系统 Python）
uv python install 3.14
python3.14 --version

# 安装新版 AgentCore CLI（npm 包）
npm install -g @aws/agentcore
agentcore --help
```

设置后续步骤需要的环境变量：

```bash
# 验证 AWS 凭证是否已正确配置
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

## 3. 获取源码

### 步骤 2：克隆源码

```bash
git clone https://github.com/sunl/aws-api-mcp-server-for-amazon-quick.git
```

### 步骤 3：本地测试（可选）

如果需要在部署前验证 MCP Server 能正常启动：

```bash
cd aws-api-mcp-server-for-amazon-quick

# 按 uv.lock 创建项目 venv 并安装运行时依赖
uv sync --frozen --no-dev
source .venv/bin/activate

# 启动 MCP Server
AWS_API_MCP_TRANSPORT=streamable-http \
AUTH_TYPE=no-auth \
AWS_API_MCP_HOST=127.0.0.1 \
AWS_API_MCP_PORT=8000 \
AWS_REGION=${AWS_REGION} \
  uv run awslabs.aws-api-mcp-server

# 新开终端，发送 MCP initialize 请求验证
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
```

预期返回
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"experimental":{},"prompts":{"listChanged":true},"resources":{"subscribe":false,"listChanged":true},"tools":{"listChanged":true},"extensions":{"io.modelcontextprotocol/ui":{}}},"serverInfo":{"name":"AWS-API-MCP","version":"3.0.1"}}}
```

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

与只需要固定几类 API 的 billing 类 MCP 不同，`call_aws` 工具是通用 AWS CLI 执行器，**能调什么 API 完全取决于你给执行角色的权限**。官方 `DEPLOYMENT.md` 明确警告：**绝不要挂 `AdministratorAccess`**。

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
export POOL_NAME="agentcore-api-mcp-pool"

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

export COGNITO_USERNAME="<YOUR_USERNAME>"
export COGNITO_PASSWORD="<YOUR_SECURE_PASSWORD>"

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
export COGNITO_DOMAIN_PREFIX="api-mcp-$(echo ${AWS_ACCOUNT_ID} | tail -c 9)"

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

默认角色名为 `AwsApiMcpCrossAccountRole`。如需自定义，部署时通过 `CROSS_ACCOUNT_ROLE_NAME` 环境变量覆盖（见步骤 13 的 `envVars`），并同步替换本节的角色名。

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

> **注意**：新版 AgentCore CLI（`@aws/agentcore`）使用 `agentcore create` 创建项目、`agentcore deploy` 部署，取代了旧版的 `agentcore configure` + `agentcore launch` 工作流。配置文件也从 `.bedrock_agentcore.yaml` 迁移到 `agentcore/agentcore.json`。详见 [AgentCore CLI GitHub](https://github.com/aws/agentcore-cli) 和 [官方文档](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html)。

### 步骤 13：初始化 AgentCore 项目并放入源码

使用 `agentcore create` 创建项目脚手架：

```bash
# 如果当前在 aws-api-mcp-server-for-amazon-quick 目录则先回到上一级目录
cd ..

agentcore create --project-name awsapi --name mcpserver --protocol MCP --build Container
cd awsapi
```

该命令会创建项目目录 `awsapi/`，结构如下：

```
awsapi/
  AGENTS.md
  README.md
  agentcore/
    agentcore.json        # 项目和 agent 配置
    aws-targets.json      # AWS 账号和区域目标
    .env.local            # 本地环境变量（已 gitignore）
  app/
    mcpserver/
      Dockerfile
      README.md
      main.py             # 脚手架生成的入口文件（将被替换）
      pyproject.toml      # Python 依赖
      uv.lock
```

将 MCP Server 源码放入 `app/mcpserver/`（即 `codeLocation` 指向的目录）：

```bash
# 清理脚手架生成的默认文件
rm -rf app/mcpserver/*

# 将 MCP Server 源码完整复制进来（排除 .git 和 .venv）
rsync -av --exclude='.git' --exclude='.venv' \
  ../aws-api-mcp-server-for-amazon-quick/ \
  app/mcpserver/
```

最终 `app/mcpserver/` 目录结构应为：

```
app/mcpserver/
  awslabs/
    aws_api_mcp_server/
      server.py
      ...
  Dockerfile
  docker-healthcheck.sh
  pyproject.toml
  uv.lock
  uv-requirements.txt
```

编辑 `agentcore/agentcore.json`，在自动生成的配置基础上添加 `executionRoleArn`、`authorizerConfiguration` 以及 `envVars`。以下是需要修改的关键字段（其余字段保持默认即可）：

```json
{
  "$schema": "https://schema.agentcore.aws.dev/v1/agentcore.json",
  "name": "awsapi",
  "version": 1,
  "runtimes": [
    {
      "name": "mcpserver",
      "build": "Container",
      "entrypoint": "awslabs/aws_api_mcp_server/server.py",
      "codeLocation": "app/mcpserver/",
      "dockerfile": "Dockerfile",
      "runtimeVersion": "PYTHON_3_14",
      "networkMode": "PUBLIC",
      "instrumentation": {
        "enableOtel": false
      },
      "protocol": "MCP",
      "executionRoleArn": "<粘贴 ${EXECUTION_ROLE_ARN} 的实际值>",
      "authorizerType": "CUSTOM_JWT",
      "authorizerConfiguration": {
        "customJwtAuthorizer": {
          "discoveryUrl": "<粘贴 ${DISCOVERY_URL} 的实际值>",
          "allowedClients": [
            "<步骤 7 创建的 CLIENT_ID，测试用>",
            "<步骤 10 创建的 QS_M2M_CLIENT_ID，Quick Suite 必须加入>"
          ]
        }
      },
      "envVars": [
        { "name": "AWS_REGION", "value": "us-east-1" },
        { "name": "AWS_API_MCP_TRANSPORT", "value": "streamable-http" },
        { "name": "AWS_API_MCP_HOST", "value": "0.0.0.0" },
        { "name": "AWS_API_MCP_PORT", "value": "8000" },
        { "name": "AWS_API_MCP_ALLOWED_HOSTS", "value": "*" },
        { "name": "AWS_API_MCP_ALLOWED_ORIGINS", "value": "*" },
        { "name": "AWS_API_MCP_STATELESS_HTTP", "value": "true" },
        { "name": "AUTH_TYPE", "value": "no-auth" },
        { "name": "READ_OPERATIONS_ONLY", "value": "true" }
      ]
    }
  ]
}
```

> **说明**：以上仅列出需要关注的字段，`agentcore create` 自动生成的其他字段（`managedBy`、`tags`、`memories`、`credentials` 等）保持原样不动。
>
> **关于 Dockerfile**：本仓库自带的 `Dockerfile` 已经配好 uv + Python 3.13 + 合适的 entrypoint，上面将 `dockerfile` 指向 `Dockerfile` 即可直接使用。`runtimeVersion: PYTHON_3_13` 与 Dockerfile 中的 `uv sync --python 3.13` 保持一致。
>
> **重要**：`allowedClients` 必须同时包含步骤 7 创建的测试用 Client ID 和步骤 10 创建的 M2M Client ID。Quick Suite 使用 M2M Client ID 获取 token，如果该 ID 不在允许列表中，连接时会被拒绝。
>
> 如果 `agentcore create` 因任何原因失败或需要重新配置，可以直接手动创建或编辑 `agentcore/agentcore.json` 文件。

**容器运行时环境变量说明（全部必需，除非注明）：**

上面 `envVars` 数组中每个变量的含义：

- `AWS_REGION` —— MCP Server 启动时校验此变量必须存在。AgentCore 平台通常会自动把部署所在区域注入到容器，但为了避免平台行为变化导致启动失败，**建议显式设置**。
- `AWS_API_MCP_TRANSPORT=streamable-http` —— 默认 `stdio`，不改 AgentCore 连不上。
- `AWS_API_MCP_HOST=0.0.0.0` —— 默认 `127.0.0.1`，不改会导致 AgentCore 无法连接容器内的服务。
- `AWS_API_MCP_PORT=8000` —— AgentCore 默认把流量转发到容器 8000 端口。
- `AWS_API_MCP_ALLOWED_HOSTS=*` / `AWS_API_MCP_ALLOWED_ORIGINS=*` —— AgentCore 代理请求时的 Host header 不可预测；默认的严格校验会拒绝所有请求。安全由 AgentCore 入站认证和 IAM 保证。
- `AWS_API_MCP_STATELESS_HTTP=true` —— AgentCore 已在平台层做会话隔离；容器内不需要再维护 session。
- `AUTH_TYPE=no-auth` —— **官方硬性要求**。入站认证由 AgentCore Runtime 的 JWT Authorizer 统一处理（就是 `agentcore.json` 中 `customJwtAuthorizer` + `allowedClients` 那段）。若设为 `oauth`，容器内的 FastMCP 会再做一次 JWT 校验，与 AgentCore 层冲突导致请求被拒。
- `READ_OPERATIONS_ONLY=true` —— 即使执行角色挂的是 `ReadOnlyAccess`，再加一层代码级白名单更稳妥。生产环境如需写操作再设为 `false`。
- （可选）`CROSS_ACCOUNT_ROLE_NAME=AwsApiMcpCrossAccountRole` —— 仅当第 6 节使用了非默认角色名时才需要追加到 `envVars` 数组。

**确认 `agentcore/aws-targets.json` 的部署目标。** 使用 `-y` 或 `--dry-run` 等非交互模式时，必须配置 target：

```bash
# 先确认当前账号和区域
echo "Account: ${AWS_ACCOUNT_ID}"
echo "Region: ${AWS_REGION}"
```

然后编辑 `agentcore/aws-targets.json`，将空数组替换为：

```json
[
  {
    "name": "default",
    "account": "<你的 AWS_ACCOUNT_ID>",
    "region": "<你的 AWS_REGION>"
  }
]
```

> 交互模式下（直接运行 `agentcore deploy` 不带参数），CLI 会自动检测当前 AWS 凭证的账号和区域，可以不配置此文件。

### 步骤 14：部署

使用 `--dry-run` 预览部署变更，如果 CDK 之前没有 bootstrap 过，需要加上 `--yes` 参数（可选）：

```bash
agentcore deploy --dry-run --yes
```

确认无误后执行部署：

```bash
agentcore deploy -y
```

`agentcore deploy` 命令会：
- 读取 `agentcore/agentcore.json` 和 `agentcore/aws-targets.json` 配置
- 基于本仓库的 `Dockerfile` 构建 ARM64 Docker 镜像并推送到 ECR
- 使用 AWS CDK 合成并部署 CloudFormation 资源
- 创建 AgentCore Runtime 并注入 `envVars` 中声明的环境变量

使用 `-v` 查看详细的资源级部署事件。构建 + 推送 ECR + 创建 Runtime 大约需 5–10 分钟。

部署完成后，查看部署状态并记录 Agent ARN：

```bash
agentcore status

export AGENT_ARN="<输出中的 ARN>"
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

预期返回 
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"experimental":{},"prompts":{"listChanged":true},"resources":{"subscribe":false,"listChanged":true},"tools":{"listChanged":true},"extensions":{"io.modelcontextprotocol/ui":{}}},"serverInfo":{"name":"AWS-API-MCP","version":"3.0.1"}}}
```

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

> 注意：
> - 必须先发 `initialize` 请求完成 MCP 协议握手，直接发 `tools/list` 会返回错误
> - Token 有效期默认 1 小时，过期后重新执行获取 Token 的命令

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

### 步骤 17：在 Quick Suite 控制台创建 MCP Connector 集成

1. 登录 [Amazon Quick Suite 控制台](https://quicksight.aws.amazon.com/)（需 Author Pro）
2. 左侧导航栏 → **Connectors**
3. 选择 **Create for your team** 标签页
4. 找到并选择 **Model Context Protocol (MCP)**
5. 在 Create Integration 页面填写：
   - Name: 如 `AWS API MCP`
   - Description（可选）: 集成用途描述
   - MCP server endpoint: 步骤 16 输出的 `MCP_SERVER_ENDPOINT`
6. 点击 "Next"
7. 认证方式选择 **Service authentication**，填写：
   - Client ID: `${QS_M2M_CLIENT_ID}`
   - Client Secret: `${QS_M2M_CLIENT_SECRET}`
   - Token URL: `${TOKEN_URL}`
8. 点击 "Create and continue"
9. 等待工具发现完成（约 1–2 分钟），确认能看到 `call_aws` / `suggest_aws_commands` 这 2 个 Actions
10. 先不共享给其他用户，点击 "Publish"

### 步骤 18：在 Chat Agent 中使用

打开 Chat Agents，选择 "My Assistant" 或创建 Custom Chat Agent 并绑定 AWS API MCP，输入：

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
# 查看部署状态
agentcore status

# 流式查看 agent 运行日志
agentcore logs

# 也可以直接使用 AWS CLI 查看日志
aws logs tail /aws/bedrock-agentcore/runtimes/<你的 agent-id>-DEFAULT \
  --log-stream-name-prefix "$(date +%Y/%m/%d)/[runtime-logs]" \
  --since 1h --region ${AWS_REGION}

# 查看最近的 traces
agentcore traces list

# 修改源码后重新部署（AgentCore 会重新构建容器镜像）
# 注：envVars 已在 agentcore.json 中持久化，无需每次重新指定
rsync -av --exclude='.git' --exclude='.venv' \
  ../aws-api-mcp-server-for-amazon-quick/ \
  app/mcpserver/
agentcore deploy -y

# 完全清理：先移除所有资源配置，再部署以销毁 AWS 资源
agentcore remove all
agentcore deploy -y
```

---

## 10. 故障排查

| 现象 | 原因与解决 |
|------|-----------|
| Quick Suite "Creation failed"、只出现 `listTools` 失败 | AgentCore `allowedClients` 未包含 M2M Client ID。编辑 `agentcore/agentcore.json` 添加后重新 `agentcore deploy -y` |
| Cognito 返回 `invalid_scope` | Resource Server 未创建或 scope 名不一致，核对步骤 9 与步骤 10 的 `aws-api-mcp/invoke` |
| 容器启动即失败，日志 `AWS_REGION environment variable is not defined` | `agentcore.json` 的 `envVars` 中漏配 `AWS_REGION`，补上后重新部署 |
| 容器内 MCP Server 只监听 127.0.0.1 | `envVars` 中忘了设 `AWS_API_MCP_HOST=0.0.0.0` |
| 400 Bad Request `Invalid host / origin` | `AWS_API_MCP_ALLOWED_HOSTS` / `AWS_API_MCP_ALLOWED_ORIGINS` 没设 `*` |
| 401 Unauthorized | Bearer Token 过期（1h），重新获取；或 AgentCore 的 `allowedClients` 没列该 client_id |
| 所有请求都 401 / auth 报错，且确认 Token 正确 | 很可能误把 `AUTH_TYPE` 设成了 `oauth`。改回 `no-auth` 并重新 `agentcore deploy -y` |
| 403 AccessDenied（call_aws 执行时） | 执行角色缺对应 AWS API 权限；或启用了 `READ_OPERATIONS_ONLY` 而命令是写操作 |
| 跨账号调用报 `Failed to assume role ...` | 目标账号未创建 `AwsApiMcpCrossAccountRole`，或信任策略不允许源执行角色，或源角色没有 `sts:AssumeRole` 权限 |
| curl 测试返回 400 / 406 | `Accept` header 必须同时带 `application/json, text/event-stream` |
| `agentcore invoke` 返回 400 | 正常：该命令跳过了 MCP `initialize` 握手。用 `curl` 先发 `initialize` 验证 |
| AgentCore 部署后修改未生效 | 需要重新将源码 rsync 到 `app/mcpserver/` 并执行 `agentcore deploy -y` 重新构建镜像 |
| `ModuleNotFoundError: awslabs.aws_api_mcp_server` | 容器内依赖安装不完整；检查 Dockerfile 层（`uv sync --no-editable` 必须成功）或 `uv-requirements.txt` 是否覆盖全部依赖 |

---

## 11. 参考文档

- [aws-api-mcp-server 上游源码](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)
- [跨账号查询方案](./cross-account-support.md)
- [FastMCP 文档](https://github.com/jlowin/fastmcp)
- [AgentCore CLI GitHub](https://github.com/aws/agentcore-cli)
- [Get started with the AgentCore CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html)
- [Deploy MCP servers in AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp.html)
- [MCP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html)
- [IAM Permissions for AgentCore Runtime](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html)
- [Authenticate and authorize with Inbound Auth and Outbound Auth](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-oauth.html)
- [Amazon Cognito as identity provider](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idp-cognito.html)
- [Amazon Quick Suite MCP integration](https://docs.aws.amazon.com/quick/latest/userguide/mcp-integration.html)
