"""Notify the job when one file is ready so Telegram can send it immediately."""

import contextvars


listener = contextvars.ContextVar("asset_ready", default=None)


async def asset_ready(asset):
    hook = listener.get()
    if hook is not None:
        await hook(asset)
    return asset
