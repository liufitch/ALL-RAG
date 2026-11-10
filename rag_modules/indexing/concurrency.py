"""Bounded network work with a single consumer for database mutations."""

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def checkpoint(progress, *, pulse: bool = False) -> None:
    result = progress.check_cancelled()
    if inspect.isawaitable(result):
        await result
    if pulse:
        await heartbeat(progress)


async def heartbeat(progress) -> None:
    if hasattr(progress, "pulse"):
        result = progress.pulse()
        if inspect.isawaitable(result):
            await result


def record_checkpoint(progress) -> None:
    if hasattr(progress, "mark_checkpoint"):
        progress.mark_checkpoint()


async def wait_external(awaitable: Awaitable[R], *, progress, heartbeat_interval: float = 10) -> R:
    """Poll while external work runs, keeping database access in this task.

    A shielded synchronous operation can defer cancellation until its thread
    relinquishes resources. Always await that cleanup before returning.
    """
    task = asyncio.ensure_future(awaitable)
    try:
        while not task.done():
            done, _ = await asyncio.wait((task,), timeout=heartbeat_interval)
            if not done:
                await checkpoint(progress, pulse=True)
        return task.result()
    finally:
        if not task.done():
            task.cancel()
        try:
            while not task.done():
                done, _ = await asyncio.wait((task,), timeout=heartbeat_interval)
                if not done:
                    # Cancellation cannot interrupt a parser's synchronous
                    # thread. Retain its lease until that thread really exits.
                    await heartbeat(progress)
        finally:
            await asyncio.gather(task, return_exceptions=True)


async def bounded_map(
    operation: Callable[[T], Awaitable[R]], values: Iterable[T], *,
    concurrency: int, progress, heartbeat_interval: float = 10,
) -> AsyncIterator[tuple[T, R]]:
    """Keep at most N requests/results in memory and drain them on every exit.

    Only network operations run in child tasks. The consumer owns database
    sessions and persistence; heartbeat checks run while the consumer waits.
    Callers close this iterator explicitly when their persistence step fails.
    """
    iterator = iter(values)
    tasks: dict[asyncio.Task, T] = {}

    async def invoke(value):
        return await operation(value)

    def fill():
        while len(tasks) < concurrency:
            try:
                value = next(iterator)
            except StopIteration:
                break
            tasks[asyncio.create_task(invoke(value))] = value

    try:
        await checkpoint(progress)
        fill()
        while tasks:
            done, _ = await asyncio.wait(tasks, timeout=heartbeat_interval, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                await checkpoint(progress, pulse=True)
                continue
            for task in done:
                value = tasks.pop(task)
                yield value, task.result()
            await checkpoint(progress)
            fill()
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
