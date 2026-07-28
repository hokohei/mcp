# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""awslabs lambda MCP Server implementation."""

import boto3
import json
import logging
import os
import re
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from typing import Optional


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AWS_PROFILE = os.environ.get('AWS_PROFILE', 'default')
logger.info(f'AWS_PROFILE: {AWS_PROFILE}')

AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
logger.info(f'AWS_REGION: {AWS_REGION}')

FUNCTION_PREFIX = os.environ.get('FUNCTION_PREFIX', '')
logger.info(f'FUNCTION_PREFIX: {FUNCTION_PREFIX}')

FUNCTION_LIST = [
    function_name.strip()
    for function_name in os.environ.get('FUNCTION_LIST', '').split(',')
    if function_name.strip()
]
logger.info(f'FUNCTION_LIST: {FUNCTION_LIST}')

FUNCTION_TAG_KEY = os.environ.get('FUNCTION_TAG_KEY', '')
logger.info(f'FUNCTION_TAG_KEY: {FUNCTION_TAG_KEY}')

FUNCTION_TAG_VALUE = os.environ.get('FUNCTION_TAG_VALUE', '')
logger.info(f'FUNCTION_TAG_VALUE: {FUNCTION_TAG_VALUE}')

FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY = os.environ.get('FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY')
logger.info(f'FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY: {FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY}')

# --- Bedrock AgentCore Gateway 向けの追加設定 (fork 独自) ---
# AgentCore Gateway の mcpServer ターゲットは streamable-http でしか繋がらないので、
# 環境変数で stdio から切り替えられるようにしている。既定は上流と同じ stdio。
MCP_TRANSPORT = os.environ.get('MCP_TRANSPORT', 'stdio')
logger.info(f'MCP_TRANSPORT: {MCP_TRANSPORT}')

MCP_HOST = os.environ.get('MCP_HOST', '127.0.0.1')
logger.info(f'MCP_HOST: {MCP_HOST}')

MCP_PORT = int(os.environ.get('MCP_PORT', '8000'))
logger.info(f'MCP_PORT: {MCP_PORT}')

# AgentCore Gateway 側がリクエストごとに Mcp-Session-Id を付けてくるため、
# サーバーがセッションを持つ (stateful) と "Missing session ID" で弾かれる。
# 公式の MCP protocol contract でも stateless_http=True が要求されている。
MCP_STATELESS_HTTP = os.environ.get('MCP_STATELESS_HTTP', 'true').lower() in ('1', 'true', 'yes')
logger.info(f'MCP_STATELESS_HTTP: {MCP_STATELESS_HTTP}')

# ALB のヘルスチェックや Gateway からのアクセスは Host ヘッダーが localhost に
# ならないので、FastMCP の DNS rebinding 保護に引っかかって 421 で落ちる。
# ALB 配下に置く前提なら切る。直接インターネットに晒す場合は true のままにする。
MCP_DNS_REBINDING_PROTECTION = os.environ.get(
    'MCP_DNS_REBINDING_PROTECTION', 'true'
).lower() in ('1', 'true', 'yes')
logger.info(f'MCP_DNS_REBINDING_PROTECTION: {MCP_DNS_REBINDING_PROTECTION}')

# Initialize AWS clients
session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
lambda_client = session.client('lambda')
schemas_client = session.client('schemas')

mcp = FastMCP(
    'awslabs.lambda-tool-mcp-server',
    instructions="""Use AWS Lambda functions to improve your answers.
    These Lambda functions give you additional capabilities and access to AWS services and resources in an AWS account.""",
    dependencies=['pydantic', 'boto3'],
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=MCP_DNS_REBINDING_PROTECTION
    ),
)


@mcp.custom_route('/health', methods=['GET'])
async def health_check(request: Request) -> JSONResponse:
    """Return 200 for load balancer health checks.

    ALB のヘルスチェックは GET しか送れないが、`/mcp` への GET は MCP の仕様上
    406 Not Acceptable になる。matcher を 200-499 のように広げて回避すると
    プロセスが応答するだけで healthy と判定されてしまうため、MCP プロトコル外の
    素の HTTP エンドポイントを用意して matcher=200 で厳密に判定できるようにする。
    """
    return JSONResponse(
        {
            'status': 'ok',
            'transport': MCP_TRANSPORT,
            'tools': len(await mcp.list_tools()),
        }
    )


def validate_function_name(function_name: str) -> bool:
    """Validate that the function name is valid and can be called."""
    # If both prefix and list are empty, consider all functions valid
    if not FUNCTION_PREFIX and not FUNCTION_LIST:
        return True

    # Otherwise, check if the function name matches the prefix or is in the list
    return (FUNCTION_PREFIX and function_name.startswith(FUNCTION_PREFIX)) or (
        function_name in FUNCTION_LIST
    )


def sanitize_tool_name(name: str) -> str:
    """Sanitize a Lambda function name to be used as a tool name."""
    # Remove prefix if present
    if name.startswith(FUNCTION_PREFIX):
        name = name[len(FUNCTION_PREFIX) :]

    # Replace invalid characters with underscore
    name = re.sub(r'[^a-zA-Z0-9_]', '_', name)

    # Ensure name doesn't start with a number
    if name and name[0].isdigit():
        name = '_' + name

    return name


def format_lambda_response(function_name: str, payload: bytes) -> str:
    """Format the Lambda function response payload."""
    try:
        # Try to parse the payload as JSON
        payload_json = json.loads(payload)
        return f'Function {function_name} returned: {json.dumps(payload_json, indent=2)}'
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Return raw payload if not JSON
        return f'Function {function_name} returned payload: {payload}'


async def invoke_lambda_function_impl(function_name: str, parameters: dict, ctx: Context) -> str:
    """Tool that invokes an AWS Lambda function with a JSON payload."""
    await ctx.info(f'Invoking {function_name} with parameters: {parameters}')

    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType='RequestResponse',
        Payload=json.dumps(parameters),
    )

    await ctx.info(f'Function {function_name} returned with status code: {response["StatusCode"]}')

    if 'FunctionError' in response:
        error_message = (
            f'Function {function_name} returned with error: {response["FunctionError"]}'
        )
        await ctx.error(error_message)
        return error_message

    payload = response['Payload'].read()
    # Format the response payload
    return format_lambda_response(function_name, payload)


def get_schema_from_registry(schema_arn: str) -> Optional[dict]:
    """Fetch schema from EventBridge Schema Registry.

    Args:
        schema_arn: ARN of the schema to fetch

    Returns:
        Schema content if successful, None if failed
    """
    try:
        # Parse registry name and schema name from ARN
        # ARN format: arn:aws:schemas:region:account:schema/registry-name/schema-name
        arn_parts = schema_arn.split(':')
        if len(arn_parts) < 6:
            logger.error(f'Invalid schema ARN format: {schema_arn}')
            return None

        registry_schema = arn_parts[5].split('/')
        if len(registry_schema) != 3:
            logger.error(f'Invalid schema path in ARN: {arn_parts[5]}')
            return None

        registry_name = registry_schema[1]
        schema_name = registry_schema[2]

        # Get the latest schema version
        response = schemas_client.describe_schema(
            RegistryName=registry_name,
            SchemaName=schema_name,
        )

        # Return the raw schema content
        return response['Content']

    except Exception as e:
        logger.error(f'Error fetching schema from registry: {e}')
        return None


def create_lambda_tool(function_name: str, description: str, schema_arn: Optional[str] = None):
    """Create a tool function for a Lambda function.

    Args:
        function_name: Name of the Lambda function
        description: Base description for the tool
        schema_arn: Optional ARN of the input schema in the Schema Registry
    """
    # Create a meaningful tool name
    tool_name = sanitize_tool_name(function_name)

    # Define the inner function
    async def lambda_function(parameters: dict, ctx: Context) -> str:
        """Tool for invoking a specific AWS Lambda function with parameters."""
        # Use the same implementation as the generic invoke function
        return await invoke_lambda_function_impl(function_name, parameters, ctx)

    # Set the function's documentation
    if schema_arn:
        schema = get_schema_from_registry(schema_arn)
        if schema:
            #  We add the schema to the description because mcp.tool does not expose overriding the tool schema.
            description_with_schema = f'{description}\n\nInput Schema:\n{schema}'
            lambda_function.__doc__ = description_with_schema
            logger.info(f'Added schema from registry to description for function {function_name}')
        else:
            lambda_function.__doc__ = description
    else:
        lambda_function.__doc__ = description

    logger.info(f'Registering tool {tool_name} with description: {description}')
    # Apply the decorator manually with the specific name
    decorated_function = mcp.tool(name=tool_name)(lambda_function)

    return decorated_function


def get_schema_arn_from_function_arn(function_arn: str) -> Optional[str]:
    """Get schema ARN from function tags if configured.

    Args:
        function_arn: ARN of the Lambda function

    Returns:
        Schema ARN if found and configured, None otherwise
    """
    if not FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY:
        logger.info(
            'No schema tag environment variable provided (FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY ).'
        )
        return None

    try:
        tags_response = lambda_client.list_tags(Resource=function_arn)
        tags = tags_response.get('Tags', {})
        if FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY in tags:
            return tags[FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY]
        else:
            logger.info(
                f'No schema arn provided for function {function_arn} via tag {FUNCTION_INPUT_SCHEMA_ARN_TAG_KEY}'
            )
    except Exception as e:
        logger.warning(f'Error checking tags for function {function_arn}: {e}')

    return None


def filter_functions_by_tag(functions, tag_key, tag_value):
    """Filter Lambda functions by a specific tag key-value pair.

    Args:
        functions: List of Lambda function objects
        tag_key: Tag key to filter by
        tag_value: Tag value to filter by

    Returns:
        List of Lambda functions that have the specified tag key-value pair
    """
    logger.info(f'Filtering functions by tag key-value pair: {tag_key}={tag_value}')
    tagged_functions = []

    for function in functions:
        try:
            # Get tags for the function
            tags_response = lambda_client.list_tags(Resource=function['FunctionArn'])
            tags = tags_response.get('Tags', {})

            # Check if the function has the specified tag key-value pair
            if tag_key in tags and tags[tag_key] == tag_value:
                tagged_functions.append(function)
        except Exception as e:
            logger.warning(f'Error getting tags for function {function["FunctionName"]}: {e}')

    logger.info(f'{len(tagged_functions)} Lambda functions found with tag {tag_key}={tag_value}.')
    return tagged_functions


def get_all_lambda_functions():
    """Retrieve all available Lambda functions using pagination."""
    paginator = lambda_client.get_paginator('list_functions')
    all_functions = []

    for page in paginator.paginate():
        all_functions.extend(page.get('Functions', []))

    return all_functions


def register_lambda_functions():
    """Register Lambda functions as individual tools."""
    try:
        logger.info('Registering Lambda functions as individual tools...')

        # Get all functions
        all_functions = get_all_lambda_functions()
        logger.info(f'Total Lambda functions found: {len(all_functions)}')

        # First filter by function name if prefix or list is set
        if FUNCTION_PREFIX or FUNCTION_LIST:
            valid_functions = [
                f for f in all_functions if validate_function_name(f['FunctionName'])
            ]
            logger.info(f'{len(valid_functions)} Lambda functions found after name filtering.')
        else:
            valid_functions = all_functions
            logger.info(
                'No name filtering applied (both FUNCTION_PREFIX and FUNCTION_LIST are empty).'
            )

        # Then filter by tag if both FUNCTION_TAG_KEY and FUNCTION_TAG_VALUE are set and non-empty
        if FUNCTION_TAG_KEY and FUNCTION_TAG_VALUE:
            tagged_functions = filter_functions_by_tag(
                valid_functions, FUNCTION_TAG_KEY, FUNCTION_TAG_VALUE
            )
            valid_functions = tagged_functions
        elif FUNCTION_TAG_KEY or FUNCTION_TAG_VALUE:
            logger.warning(
                'Both FUNCTION_TAG_KEY and FUNCTION_TAG_VALUE must be set to filter by tag.'
            )
            valid_functions = []

        for function in valid_functions:
            function_name = function['FunctionName']
            description = function.get('Description', f'AWS Lambda function: {function_name}')
            schema_arn = get_schema_arn_from_function_arn(function['FunctionArn'])

            create_lambda_tool(function_name, description, schema_arn)

        logger.info('Lambda functions registered successfully as individual tools.')

    except Exception as e:
        logger.error(f'Error registering Lambda functions as tools: {e}')


def main():
    """Run the MCP server with CLI argument support."""
    register_lambda_functions()

    # 既定 (stdio) では上流と同じ挙動。MCP_TRANSPORT=streamable-http のときだけ
    # HTTP サーバーとして待ち受け、AgentCore Gateway のターゲットになれるようにする。
    if MCP_TRANSPORT == 'stdio':
        mcp.run()
        return

    mcp.settings.host = MCP_HOST
    mcp.settings.port = MCP_PORT
    mcp.settings.stateless_http = MCP_STATELESS_HTTP
    logger.info(
        f'Starting MCP server: transport={MCP_TRANSPORT} '
        f'host={MCP_HOST} port={MCP_PORT} stateless_http={MCP_STATELESS_HTTP}'
    )
    mcp.run(transport=MCP_TRANSPORT)


if __name__ == '__main__':
    main()
