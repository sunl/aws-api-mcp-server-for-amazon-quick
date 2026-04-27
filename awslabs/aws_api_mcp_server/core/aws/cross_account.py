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

"""Cross-account AssumeRole helper with thread-safe credential cache."""

import boto3
import re
import threading
import time
from ..common.config import CROSS_ACCOUNT_ROLE_NAME
from ..common.errors import AwsApiMcpError
from ..common.models import Credentials
from botocore.exceptions import ClientError
from loguru import logger


_ACCOUNT_ID_PATTERN = re.compile(r'^\d{12}$')

# AssumeRole returns credentials valid for 1h by default. Refresh earlier
# so an in-flight request never runs with credentials about to expire.
_CACHE_TTL_SECONDS = 45 * 60

_cache: dict[str, tuple[Credentials, float]] = {}
_cache_lock = threading.Lock()


def _validate_account_id(account_id: str) -> None:
    if not _ACCOUNT_ID_PATTERN.match(account_id):
        raise AwsApiMcpError(
            f'Invalid target_account_id: {account_id!r}. Must be a 12-digit AWS account ID.'
        )


def _assume_role(account_id: str) -> Credentials:
    role_arn = f'arn:aws:iam::{account_id}:role/{CROSS_ACCOUNT_ROLE_NAME}'
    session_name = f'aws-api-mcp-{account_id}-{int(time.time())}'
    logger.info('Assuming role {} (session={})', role_arn, session_name)

    sts_client = boto3.client('sts')
    try:
        response = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=session_name,
        )
    except ClientError as e:
        error_code = e.response.get('Error', {}).get('Code', 'Unknown')
        raise AwsApiMcpError(
            f'Failed to assume role {role_arn} (target account {account_id}): '
            f'{error_code}. Check that the role exists in the target account and '
            f"that this server's execution role is allowed to assume it."
        ) from e

    aws_creds = response['Credentials']
    return Credentials(
        access_key_id=aws_creds['AccessKeyId'],
        secret_access_key=aws_creds['SecretAccessKey'],
        session_token=aws_creds['SessionToken'],
    )


def get_credentials_for_account(account_id: str) -> Credentials:
    """Return cached or freshly-assumed credentials for ``account_id``.

    Credentials are cached per-account for ~45 minutes (shorter than STS's
    default 1h lifetime) to absorb high-frequency tool calls without refreshing
    on every request.
    """
    _validate_account_id(account_id)

    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(account_id)
        if cached is not None:
            creds, expires_at = cached
            if expires_at > now:
                return creds

    # Assume role outside the lock so concurrent assumptions for different
    # accounts don't serialize. A harmless race for the same account may
    # produce two AssumeRole calls; the later one simply overwrites the cache.
    creds = _assume_role(account_id)

    with _cache_lock:
        _cache[account_id] = (creds, time.monotonic() + _CACHE_TTL_SECONDS)

    return creds
