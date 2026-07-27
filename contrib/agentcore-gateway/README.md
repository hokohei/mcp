# AgentCore Gateway VPC ターゲット検証用の追加設定

このフォークは `lambda-tool-mcp-server` を **Amazon Bedrock AgentCore Gateway の
`mcpServer` ターゲット**として VPC 内で動かせるように拡張したものです。

AgentCore Gateway から VPC 内リソースへの接続検証で
`tools/list` / `tools/call` の成功まで確認済みの構成です。

```
呼び出し元 (SigV4)
  → AgentCore Gateway (authorizerType: AWS_IAM)
    → VPC Lattice Resource Gateway (privateEndpoint)
      → 内部 ALB (HTTPS 443, パブリック ACM 証明書)
        → EC2 上のこのサーバー (streamable-http, 8000)
```

## 上流との差分

変更は `src/lambda-tool-mcp-server/awslabs/lambda_tool_mcp_server/server.py` のみ。
**環境変数を何も設定しなければ上流と同じ stdio で動く**ので、既存の使い方には
影響しません。

| 環境変数 | 既定値 | 用途 |
|---|---|---|
| `MCP_TRANSPORT` | `stdio` | `streamable-http` にすると HTTP で待ち受ける |
| `MCP_HOST` | `127.0.0.1` | 待ち受けアドレス。ALB 配下なら `0.0.0.0` |
| `MCP_PORT` | `8000` | 待ち受けポート |
| `MCP_STATELESS_HTTP` | `true` | ステートレスモード |
| `MCP_DNS_REBINDING_PROTECTION` | `true` | FastMCP の DNS rebinding 保護 |

### なぜこの 2 つが必要か

**`MCP_STATELESS_HTTP`**

AgentCore Gateway はリクエストごとに `Mcp-Session-Id` ヘッダーを付与します。
サーバーがセッションを保持する stateful モードだと、プラットフォームが生成した
セッション ID を知らないため `Missing session ID` で拒否されます。

> Transport: Streamable-http transport is required. By default, use stateless mode
> (stateless_http=True) for compatibility with AWS's session management and load
> balancing.
>
> Session Management: Platform automatically adds Mcp-Session-Id header for session
> isolation. In stateless mode, servers must support stateless operation so as to
> not reject platform generated Mcp-Session-Id header.

https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html

**`MCP_DNS_REBINDING_PROTECTION`**

FastMCP は既定で Host ヘッダーを検証します。ALB のヘルスチェックや Gateway
経由のリクエストは Host が localhost にならないため、そのままだと弾かれます。
ALB を前段に置く構成では `false` にします。

インターネットに直接公開する場合は `true` のままにするか、
`TransportSecuritySettings.allowed_hosts` を使ってください。

## 使い方

### ローカルで動作確認

```bash
cd src/lambda-tool-mcp-server

MCP_TRANSPORT=streamable-http \
MCP_HOST=127.0.0.1 \
MCP_PORT=8000 \
MCP_STATELESS_HTTP=true \
MCP_DNS_REBINDING_PROTECTION=false \
AWS_REGION=ap-northeast-1 \
FUNCTION_PREFIX=my-tool- \
python3 -m awslabs.lambda_tool_mcp_server.server
```

ステートレスなので `initialize` を送らずに `tools/list` が通ります。

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

Host ヘッダーを変えても拒否されないことも確認できます。

```bash
curl -s -X POST http://127.0.0.1:8000/mcp \
  -H 'Host: example.com' \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

### EC2 上で常駐させる

`contrib/agentcore-gateway/mcpserver.service` を使います。

```bash
sudo cp contrib/agentcore-gateway/mcpserver.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mcpserver
sudo systemctl is-active mcpserver
sudo journalctl -u mcpserver -f
```

ログは journald に出します。**`StandardOutput` にファイルパスを指定しないこと。**
書き込み権限が無いディレクトリ（`/tmp` など環境によっては失敗する）を指定すると
`Failed to set up standard output: Permission denied` で起動に失敗し、
`Restart=always` と組み合わさって無限リスタートループになります。
検証中にこれでリスタートカウンタが 566 回まで回り、インスタンスがセキュリティ
隔離される事故が起きました。

## AgentCore Gateway 側の設定で注意する点

検証中に踏んだものを挙げておきます。

| 項目 | 内容 |
|---|---|
| endpoint は HTTPS 必須 | プライベート IP を直接書くと `blocked IP address` で拒否される |
| endpoint はカスタムドメインを使う | ALB の DNS 名を直接指定すると TLS ホスト名検証で落ちる。ACM 証明書の SAN に含まれる名前にする |
| 証明書はパブリック信頼が必要 | プライベート CA だと `PKIX path building failed` |
| Resource Gateway SG の **egress** | ターゲット宛の TCP 443 と DNS 53 を開ける。inbound だけでは足りない |
| ALB SG の inbound | Resource Gateway の SG / サブネット CIDR からの 443 を許可する |
| IAM (SigV4) アウトバウンド認証は使えない | ALB / EC2 は SigV4 を検証しないため。API key か OAuth を使う |
| Gateway 実行ロール | API key / OAuth を使うなら `bedrock-agentcore:GetWorkloadAccessToken` 等が必要 |
| Gateway のログ配信 | 既定で無効。有効にしないとエラーの中身が見えない |

`Client failed to initialize by explicit API call` は汎用エラーで、
存在しない URL を指定しても同じものが返ります。原因の切り分けには Gateway の
ログ配信を有効にしたうえで、まず ALB のターゲットヘルスと VPC 内からの
`curl`（`-k` なし）で 200 が返ることを確認してください。

## 参照

- MCP servers targets
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html
- MCP protocol contract
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-mcp-protocol-contract.html
- Gateway VPC Egress
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-vpc-egress.html
- VPC Egress private endpoints
  https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/vpc-egress-private-endpoints.html
