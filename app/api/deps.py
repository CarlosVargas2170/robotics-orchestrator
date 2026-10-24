from arq.connections import ArqRedis
from fastapi import Request


def get_queue(request: Request) -> ArqRedis:
    return request.app.state.queue
