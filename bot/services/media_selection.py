"""Small numbered image previews and controls for choosing album items."""

import asyncio
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from PIL import Image, ImageDraw, ImageFont, ImageOps

from bot.services.media_processor import extract_still
from bot.services.models import MediaAsset


def _preview(asset: MediaAsset, number: int, directory: Path) -> Path:
    source = asset.path if asset.kind == "photo" else asset.photo_path or extract_still(asset.path)
    output = directory / f"preview-{number}.jpg"
    with Image.open(source) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((600, 540), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (640, 640), (24, 28, 36))
        canvas.paste(image, ((640 - image.width) // 2, (560 - image.height) // 2))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 30)
        except OSError:
            font = ImageFont.load_default()
        label = {"video": "VIDEO", "live_photo": "LIVE"}.get(asset.kind, "PHOTO")
        draw.text((22, 585), f"{number}  {label}", fill="white", font=font)
        canvas.save(output, "JPEG", quality=78, optimize=True)
    return output


async def create_previews(assets: list[MediaAsset], directory: Path, stopped) -> list[Path]:
    previews = []
    for number, asset in enumerate(assets, 1):
        if stopped.is_set():
            raise InterruptedError("Stopped")
        previews.append(await asyncio.to_thread(_preview, asset, number, directory))
    return previews


def selection_text(selected: set[int], count: int) -> str:
    return f"🖼 Choose from the previews above. Selected: {len(selected)}/{count}."


def selection_keyboard(job_id: str, count: int, selected: set[int]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(
            text=f"{'✅' if index in selected else '▫️'} {index + 1}",
            callback_data=f"pick:{job_id}:toggle:{index}",
        )
        for index in range(count)
    ]
    rows = [buttons[start:start + 5] for start in range(0, count, 5)]
    rows.append([InlineKeyboardButton(text="📤 Send selected", callback_data=f"pick:{job_id}:send")])
    rows.append([
        InlineKeyboardButton(text="📦 Send all", callback_data=f"pick:{job_id}:all"),
        InlineKeyboardButton(text="⏭ Skip", callback_data=f"pick:{job_id}:skip"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
