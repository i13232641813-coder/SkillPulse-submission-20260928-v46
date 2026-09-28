"""单机固定演示身份。无账号、无登录；CURRENT_ACTOR 由后端中间件固定注入。"""
from contextvars import ContextVar

CURRENT_ACTOR: ContextVar[int | None] = ContextVar('skillpulse_actor_id', default=None)
