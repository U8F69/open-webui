import logging
import shutil
import time
import uuid
import jwt
import base64
import hmac
import hashlib

import requests
import os

from datetime import datetime, timedelta
import pytz
from pytz import UTC
from typing import Optional, Union, List, Dict

from open_webui.models.users import Users

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import (
    WEBUI_SECRET_KEY,
    TRUSTED_SIGNATURE_KEY,
    STATIC_DIR,
    SRC_LOG_LEVELS,
    FRONTEND_BUILD_DIR,
    REDIS_URL,
    REDIS_SENTINEL_HOSTS,
    REDIS_SENTINEL_PORT,
)

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext

from open_webui.utils.redis import get_redis_connection, get_sentinels_from_env

logging.getLogger("passlib").setLevel(logging.ERROR)

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OAUTH"])

SESSION_SECRET = WEBUI_SECRET_KEY
ALGORITHM = "HS256"


##############
# Auth Utils
##############


def verify_signature(payload: str, signature: str) -> bool:
    """
    Verifies the HMAC signature of the received payload.
    """
    try:
        expected_signature = base64.b64encode(
            hmac.new(TRUSTED_SIGNATURE_KEY, payload.encode(), hashlib.sha256).digest()
        ).decode()

        # Compare securely to prevent timing attacks
        return hmac.compare_digest(expected_signature, signature)

    except Exception:
        return False


def override_static(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)

    r = requests.get(content, stream=True)
    with open(path, "wb") as f:
        r.raw.decode_content = True
        shutil.copyfileobj(r.raw, f)


def get_license_data(app, key):
    payload = {
        "resources": {
            os.path.join(STATIC_DIR, "logo.png"): os.getenv("CUSTOM_PNG", ""),
            os.path.join(STATIC_DIR, "favicon.png"): os.getenv("CUSTOM_PNG", ""),
            os.path.join(STATIC_DIR, "favicon.svg"): os.getenv("CUSTOM_SVG", ""),
            os.path.join(STATIC_DIR, "favicon-96x96.png"): os.getenv("CUSTOM_PNG", ""),
            os.path.join(STATIC_DIR, "apple-touch-icon.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(STATIC_DIR, "web-app-manifest-192x192.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(STATIC_DIR, "web-app-manifest-512x512.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(STATIC_DIR, "splash.png"): os.getenv("CUSTOM_PNG", ""),
            os.path.join(STATIC_DIR, "favicon.ico"): os.getenv("CUSTOM_ICO", ""),
            os.path.join(STATIC_DIR, "favicon-dark.png"): os.getenv(
                "CUSTOM_DARK_PNG", ""
            ),
            os.path.join(STATIC_DIR, "splash-dark.png"): os.getenv(
                "CUSTOM_DARK_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "favicon.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/favicon.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/favicon.svg"): os.getenv(
                "CUSTOM_SVG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/favicon-96x96.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/apple-touch-icon.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(
                FRONTEND_BUILD_DIR, "static/web-app-manifest-192x192.png"
            ): os.getenv("CUSTOM_PNG", ""),
            os.path.join(
                FRONTEND_BUILD_DIR, "static/web-app-manifest-512x512.png"
            ): os.getenv("CUSTOM_PNG", ""),
            os.path.join(FRONTEND_BUILD_DIR, "static/splash.png"): os.getenv(
                "CUSTOM_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/favicon.ico"): os.getenv(
                "CUSTOM_ICO", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/favicon-dark.png"): os.getenv(
                "CUSTOM_DARK_PNG", ""
            ),
            os.path.join(FRONTEND_BUILD_DIR, "static/splash-dark.png"): os.getenv(
                "CUSTOM_DARK_PNG", ""
            ),
        },
        "metadata": {
            "type": "enterprise",
            "organization_name": os.getenv("ORGANIZATION_NAME", "OpenWebui"),
        },
    }
    try:
        for k, v in payload.items():
            if k == "resources":
                for p, c in v.items():
                    if c:
                        globals().get("override_static", lambda a, b: None)(p, c)
            elif k == "count":
                setattr(app.state, "USER_COUNT", v)
            elif k == "name":
                setattr(app.state, "WEBUI_NAME", v)
            elif k == "metadata":
                setattr(app.state, "LICENSE_METADATA", v)
        return True
    except Exception as ex:
        log.exception(f"License: Uncaught Exception: {ex}")

    return True


bearer_security = HTTPBearer(auto_error=False)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password, hashed_password):
    return (
        pwd_context.verify(plain_password, hashed_password) if hashed_password else None
    )


def get_password_hash(password):
    return pwd_context.hash(password)


redis_client = None
if REDIS_URL:
    redis_client = get_redis_connection(
        redis_url=REDIS_URL,
        redis_sentinels=get_sentinels_from_env(
            REDIS_SENTINEL_HOSTS, REDIS_SENTINEL_PORT
        ),
        decode_responses=True,
    )


def jwt_cache_key(id: str) -> str:
    return f"enhanced:jwt:{id}"


def check_jwt_max_count(data: dict, token: str) -> Optional[dict]:
    from open_webui.config import ENHANCED_JWT_MAX_COUNT

    # redis and jwt limit
    jwt_max_count = int(ENHANCED_JWT_MAX_COUNT.value)
    if not redis_client or jwt_max_count <= 0:
        return data
    # check for data
    if not data or "id" not in data:
        return data
    # load from redis
    key = jwt_cache_key(data["id"])
    token_map = redis_client.hgetall(key)
    if not token_map:
        return None
    # sort
    tokens = [[created_at, token] for token, created_at in token_map.items()]
    tokens.sort(key=lambda x: x[0], reverse=True)
    # check for last
    to_verify = tokens[:jwt_max_count]
    for t in to_verify:
        if t[1] == token:
            return data
    # remove old tokens
    to_remove = tokens[jwt_max_count:]
    if to_remove:
        redis_client.hdel(key, *[t[1] for t in to_remove])
    return None


def set_jwt_token(user_id: str, token: str) -> None:
    from open_webui.config import ENHANCED_JWT_MAX_COUNT

    # redis and jwt limit
    jwt_max_count = int(ENHANCED_JWT_MAX_COUNT.value)
    if not redis_client or jwt_max_count <= 0:
        return
    # save to redis
    key = jwt_cache_key(user_id)
    redis_client.hset(name=key, key=token, value=str(time.time_ns()))


def create_token(data: dict, expires_delta: Union[timedelta, None] = None) -> str:
    payload = data.copy()

    if expires_delta:
        expire = datetime.now(UTC) + expires_delta
        payload.update({"exp": expire})

    encoded_jwt = jwt.encode(payload, SESSION_SECRET, algorithm=ALGORITHM)
    return encoded_jwt


def decode_token(token: str) -> Optional[dict]:
    try:
        decoded = jwt.decode(token, SESSION_SECRET, algorithms=[ALGORITHM])
        return check_jwt_max_count(data=decoded, token=token)
    except Exception:
        return None


def extract_token_from_auth_header(auth_header: str):
    return auth_header[len("Bearer ") :]


def create_api_key():
    key = str(uuid.uuid4()).replace("-", "")
    return f"sk-{key}"


def get_http_authorization_cred(auth_header: Optional[str]):
    if not auth_header:
        return None
    try:
        scheme, credentials = auth_header.split(" ")
        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)
    except Exception:
        return None


def get_current_user(
    request: Request,
    background_tasks: BackgroundTasks,
    auth_token: HTTPAuthorizationCredentials = Depends(bearer_security),
):
    token = None

    if auth_token is not None:
        token = auth_token.credentials

    if token is None and "token" in request.cookies:
        token = request.cookies.get("token")

    if token is None:
        raise HTTPException(status_code=403, detail="Not authenticated")

    # auth by api key
    if token.startswith("sk-"):
        if not request.state.enable_api_key:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED
            )

        if request.app.state.config.ENABLE_API_KEY_ENDPOINT_RESTRICTIONS:
            allowed_paths = [
                path.strip()
                for path in str(
                    request.app.state.config.API_KEY_ALLOWED_ENDPOINTS
                ).split(",")
            ]

            # Check if the request path matches any allowed endpoint.
            if not any(
                request.url.path == allowed
                or request.url.path.startswith(allowed + "/")
                for allowed in allowed_paths
            ):
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN, detail=ERROR_MESSAGES.API_KEY_NOT_ALLOWED
                )

        return get_current_user_by_api_key(token)

    # auth by jwt token
    try:
        data = decode_token(token)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    if data is not None and "id" in data:
        user = Users.get_user_by_id(data["id"])
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=ERROR_MESSAGES.INVALID_TOKEN,
            )
        else:
            # Refresh the user's last active timestamp asynchronously
            # to prevent blocking the request
            if background_tasks:
                background_tasks.add_task(Users.update_user_last_active_by_id, user.id)
        return user
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.UNAUTHORIZED,
        )


def get_current_user_by_api_key(api_key: str):
    user = Users.get_user_by_api_key(api_key)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.INVALID_TOKEN,
        )
    else:
        Users.update_user_last_active_by_id(user.id)

    return user


def get_verified_user(user=Depends(get_current_user)):
    if user.role not in {"user", "admin"}:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user


def get_admin_user(user=Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=ERROR_MESSAGES.ACCESS_PROHIBITED,
        )
    return user
