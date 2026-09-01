from __future__ import annotations

"""Capture every SectorRadar page at its real Windows physical size.

The audit always works from a disposable SQLite backup.  It deliberately
removes the encrypted DeepSeek key from that backup, never starts the normal
startup timers, and destroys the temporary database after the screenshots are
written.
"""

import argparse
import ctypes
import json
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# DPI awareness has to be selected before tkinter/app.ui is imported.
from app.dpi import enable_high_dpi_awareness


DPI_MODE = enable_high_dpi_awareness()

from PIL import Image, ImageDraw, ImageFont, ImageGrab

from app.config import APP_VERSION
from app.database import Database
from app.runtime_housekeeping import storage_inventory
from app.ui import PAGE_GUIDES, SectorRadarApp


GA_ROOT = 2


def _clone_without_credentials(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source_conn:
        source_conn.execute("PRAGMA query_only = ON")
        with sqlite3.connect(destination) as destination_conn:
            source_conn.backup(destination_conn)
            destination_conn.execute(
                "UPDATE settings SET value='' WHERE key='deepseek_api_key_dpapi'"
            )
            destination_conn.commit()


def _root_window_handle(widget_id: int) -> int:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    get_ancestor = user32.GetAncestor
    get_ancestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    get_ancestor.restype = ctypes.c_void_p
    return int(get_ancestor(widget_id, GA_ROOT) or widget_id)


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    class Rect(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect = Rect()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.left, rect.top, rect.right, rect.bottom


def _capture_window(hwnd: int, expected_size: tuple[int, int]) -> Image.Image:
    image = ImageGrab.grab(
        window=hwnd,
        include_layered_windows=True,
        scale_down=False,
    )
    # Pillow returns the physical client area for a maximized Tk window while
    # GetWindowRect also includes the invisible resize border and title bar.
    # Both are physical pixels.  Reject only a genuinely virtualized/partial
    # capture, not the expected non-client-area difference.
    minimum_size = (int(expected_size[0] * 0.90), int(expected_size[1] * 0.85))
    if image.width < minimum_size[0] or image.height < minimum_size[1]:
        raise RuntimeError(
            "physical capture is partial or DPI-virtualized: "
            f"window_rect={expected_size}, minimum_client={minimum_size}, "
            f"actual={image.size}"
        )
    return image.convert("RGB")


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in (
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _contact_sheet(captures: list[dict], output: Path) -> None:
    columns = 2
    tile_width, tile_height = 960, 570
    label_height = 50
    rows = (len(captures) + columns - 1) // columns
    sheet = Image.new(
        "RGB",
        (columns * tile_width, rows * (tile_height + label_height)),
        "#FFF9F4",
    )
    draw = ImageDraw.Draw(sheet)
    font = _font(25)
    for index, capture in enumerate(captures):
        image = Image.open(capture["path"]).convert("RGB")
        image.thumbnail((tile_width, tile_height), Image.Resampling.LANCZOS)
        x = (index % columns) * tile_width
        y = (index // columns) * (tile_height + label_height)
        sheet.paste(image, (x + (tile_width - image.width) // 2, y))
        draw.text(
            (x + 18, y + tile_height + 7),
            f"{index + 1:02d}  {capture['page_key']}  {capture['title']}",
            fill="#3A3335",
            font=font,
        )
    sheet.save(output, quality=94)


def capture_all(source_db: Path, output_dir: Path) -> Path:
    if not source_db.is_file():
        raise FileNotFoundError(source_db)
    output_dir.mkdir(parents=True, exist_ok=True)
    captures: list[dict] = []
    supplementary_captures: list[dict] = []
    window_rect_size: tuple[int, int] | None = None

    with tempfile.TemporaryDirectory(prefix="sector-radar-ui-audit-") as folder:
        temporary_db = Path(folder) / "ui-audit.sqlite3"
        _clone_without_credentials(source_db, temporary_db)
        database = Database(temporary_db)
        app = SectorRadarApp(start_hidden=True, database=database)
        try:
            # SettingsPage normally inventories the whole application in a
            # worker.  A screenshot harness drives Tk with update() rather
            # than mainloop(), so cross-thread ``after`` callbacks are not
            # legal here.  Keep the visual audit read-only and deterministic.
            settings_page = app.pages.get("settings")
            if settings_page is not None:
                settings_page.refresh_storage_inventory = lambda: None
            app.deiconify()
            app.update_idletasks()
            app.state("zoomed")
            app.lift()
            app.update()
            time.sleep(0.45)
            app.update()

            hwnd = _root_window_handle(app.winfo_id())
            left, top, right, bottom = _window_rect(hwnd)
            expected_size = (right - left, bottom - top)
            window_rect_size = expected_size
            if min(expected_size) < 1000:
                raise RuntimeError(
                    f"window did not reach a physical full-screen size: {expected_size}"
                )

            for index, (page_key, guide) in enumerate(PAGE_GUIDES.items(), start=1):
                app.show(page_key)
                for _ in range(3):
                    app.update_idletasks()
                    app.update()
                    time.sleep(0.18)
                image = _capture_window(hwnd, expected_size)
                title = str(guide[0] if guide else page_key)
                image_path = output_dir / f"{index:02d}-{page_key}.png"
                image.save(image_path)
                captures.append(
                    {
                        "page_key": page_key,
                        "title": title,
                        "path": str(image_path),
                        "width": image.width,
                        "height": image.height,
                    }
                )
                if page_key == "decision":
                    # The default decision screenshot shows AI advice.  The
                    # user's real-operation controls live on the second tab and
                    # must also be audited at physical full-screen size.
                    decision = app.pages["decision"]
                    decision.work_tabs.select(decision.draft_panel)
                    for _ in range(3):
                        app.update_idletasks()
                        app.update()
                        time.sleep(0.18)
                    draft_image = _capture_window(hwnd, expected_size)
                    draft_path = output_dir / "01b-decision-drafts.png"
                    draft_image.save(draft_path)
                    supplementary_captures.append({
                        "page_key": "decision_drafts",
                        "title": "盘中草稿与跨日真实结果处理",
                        "path": str(draft_path),
                        "width": draft_image.width,
                        "height": draft_image.height,
                    })
                if page_key == "radar":
                    radar = app.pages["radar"]
                    audit_rows = radar.audit_tree.get_children()
                    if audit_rows:
                        radar.tabs.select(radar.tabs.tabs()[1])
                        radar.audit_tree.selection_set(audit_rows[0])
                        radar.audit_tree.focus(audit_rows[0])
                        radar.audit_tree.see(audit_rows[0])
                        radar.show_audit_detail()
                        for _ in range(3):
                            app.update_idletasks()
                            app.update()
                            time.sleep(0.18)
                        detail_image = _capture_window(hwnd, expected_size)
                        detail_path = output_dir / "05b-radar-score-detail.png"
                        detail_image.save(detail_path)
                        supplementary_captures.append({
                            "page_key": "radar_score_detail",
                            "title": "夜间雷达评分公式与安全门槛",
                            "path": str(detail_path),
                            "width": detail_image.width,
                            "height": detail_image.height,
                        })
                if page_key == "settings" and settings_page is not None:
                    # The normal worker is disabled above because this harness
                    # does not run mainloop().  Populate the second tab from a
                    # synchronous, non-mutating inventory so the actual
                    # retention preview is still part of physical UI review.
                    data_directory = source_db.parent
                    program_directory = data_directory.parent
                    inventory = storage_inventory(
                        data_directory / "runtime",
                        data_directory / "backups",
                        data_directory / "exports",
                        program_root=program_directory,
                        settings=database.settings(),
                    )
                    settings_page.tabs.select(settings_page.tabs.tabs()[1])
                    settings_page._finish_storage_inventory(inventory, None)
                    for _ in range(3):
                        app.update_idletasks()
                        app.update()
                        time.sleep(0.18)
                    storage_image = _capture_window(hwnd, expected_size)
                    storage_path = output_dir / "14b-settings-storage.png"
                    storage_image.save(storage_path)
                    supplementary_captures.append({
                        "page_key": "settings_storage",
                        "title": "存储位置、保留规则与清理预览",
                        "path": str(storage_path),
                        "width": storage_image.width,
                        "height": storage_image.height,
                    })
        finally:
            app.destroy()

    contact_sheet = output_dir / "00-contact-sheet.jpg"
    _contact_sheet(captures, contact_sheet)
    report = {
        "schema": "SECTOR_RADAR_PHYSICAL_UI_AUDIT_V1",
        "app_version": APP_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "dpi_mode": DPI_MODE,
        "source_database": str(source_db),
        "source_database_modified": False,
        "temporary_database_deleted": True,
        "page_count": len(captures),
        "expected_page_count": len(PAGE_GUIDES),
        "window_rect_physical_size": list(window_rect_size or (0, 0)),
        "captured_client_physical_size": (
            [captures[0]["width"], captures[0]["height"]] if captures else [0, 0]
        ),
        "contact_sheet": str(contact_sheet),
        "captures": captures,
        "supplementary_captures": supplementary_captures,
    }
    report_path = output_dir / "ui-audit-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    report = capture_all(arguments.source_db.resolve(), arguments.output_dir.resolve())
    print(f"UI_AUDIT_CAPTURED report={report}", flush=True)


if __name__ == "__main__":
    main()
