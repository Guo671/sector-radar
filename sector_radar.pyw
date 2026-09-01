from __future__ import annotations

import os
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if getattr(sys, "frozen", False):
    RUNTIME_ROOT = Path(sys.executable).resolve().parent / "data"
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
else:
    RUNTIME_ROOT = ROOT
LOG_PATH = RUNTIME_ROOT / "板块雷达_启动诊断.log"

# This must run before importing tkinter or app.ui.  If the first window is
# created while the process is DPI-unaware, Windows permanently chooses blurry
# bitmap scaling for that process.
from app.dpi import enable_high_dpi_awareness


DPI_MODE = enable_high_dpi_awareness()


def write_crash_log(exc: BaseException) -> Path:
    details = [
        "板块雷达启动失败诊断",
        f"时间: {datetime.now().isoformat(timespec='seconds')}",
        f"Python: {sys.version}",
        f"解释器: {sys.executable}",
        f"系统: {platform.platform()}",
        f"程序目录: {ROOT}",
        "",
        "完整错误:",
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
    ]
    LOG_PATH.write_text("\n".join(details), encoding="utf-8")
    return LOG_PATH


def self_test() -> None:
    """Create the real hidden application so every page is tested before launch."""
    import tempfile

    from app.config import APP_VERSION, local_now
    from app.database import Database
    from app.ui import PAGE_GUIDES, SectorRadarApp

    with tempfile.TemporaryDirectory(prefix="sector-radar-ui-smoke-") as folder:
        database = Database(Path(folder) / "ui-smoke.sqlite3")
        app = SectorRadarApp(start_hidden=True, database=database)
        try:
            app.update_idletasks()
            expected_pages = set(PAGE_GUIDES)
            actual_pages = set(app.pages)
            if actual_pages != expected_pages:
                raise RuntimeError(
                    f"UI page mismatch: expected={sorted(expected_pages)} "
                    f"actual={sorted(actual_pages)}"
                )
            radar = app.pages["radar"]
            radar_tabs = [radar.tabs.tab(tab_id, "text") for tab_id in radar.tabs.tabs()]
            expected_radar_tabs = [
                "① 最终主榜（最多10项）",
                "② 全目录七档结论",
                "③ 同类备选/候选池",
                "④ Pro终审回避（独立）",
            ]
            if radar_tabs != expected_radar_tabs:
                raise RuntimeError(
                    f"Radar tab mismatch: expected={expected_radar_tabs} actual={radar_tabs}"
                )
            review = app.pages["review"]
            review_tabs = [review.tabs.tab(tab_id, "text") for tab_id in review.tabs.tabs()]
            expected_review_tabs = [
                "① 我的真实交易复盘",
                "② AI推荐验证（仅观察）",
            ]
            if review_tabs != expected_review_tabs:
                raise RuntimeError(
                    f"Review tab mismatch: expected={expected_review_tabs} actual={review_tabs}"
                )
            if int(float(review.canvas.cget("height"))) < 280:
                raise RuntimeError("Review chart is too short for physical-DPI labels")
            if int(review.fund_tree.column("fund", "width")) < 320:
                raise RuntimeError("Review segment fund column is too narrow")
            for attribute in (
                "fund_tree",
                "trade_tree",
                "trade_tables",
                "recommendation_tier_tree",
                "recommendation_score_tree",
                "recommendation_tree",
                "sync_recommendation_button",
                "stop_recommendation_button",
            ):
                if not hasattr(review, attribute):
                    raise RuntimeError(f"Recommendation review widget missing: {attribute}")
            settings = app.pages["settings"]
            expected_ranking_settings = {
                "ranking_trend_weight", "ranking_entry_weight", "ranking_catalyst_weight",
                "ranking_relative_strength_weight", "ranking_volume_breadth_weight",
                "ranking_crowding_penalty", "ranking_data_confidence_min",
                "ranking_candidate_min_score", "ranking_pool_min", "ranking_pool_max",
                "ranking_pool_cluster_max", "ranking_display_cluster_max",
                "ranking_funded_cluster_max",
            }
            missing_ranking_settings = expected_ranking_settings.difference(settings.vars)
            if missing_ranking_settings:
                raise RuntimeError(
                    f"Ranking settings missing: {sorted(missing_ranking_settings)}"
                )
            settings_tabs = [
                settings.tabs.tab(tab_id, "text") for tab_id in settings.tabs.tabs()
            ]
            if settings_tabs != ["风险与排名", "存储与备份"]:
                raise RuntimeError(f"Settings tab mismatch: {settings_tabs}")
            expected_storage_settings = {
                "backup_keep_count", "backup_keep_days",
                "rollback_keep_count", "rollback_keep_days",
                "diagnostic_keep_count", "diagnostic_keep_days",
            }
            missing_storage_settings = expected_storage_settings.difference(
                settings.storage_vars
            )
            if missing_storage_settings:
                raise RuntimeError(
                    f"Storage settings missing: {sorted(missing_storage_settings)}"
                )
            for attribute in (
                "program_path_label", "data_path_label", "storage_status",
                "inventory_button", "cleanup_button", "save_storage_button",
            ):
                if not hasattr(settings, attribute):
                    raise RuntimeError(f"Storage widget missing: {attribute}")
            reported_scaling = app.reported_tk_scaling
            applied_scaling = app.applied_tk_scaling
        finally:
            app.destroy()
    try:
        print(
            f"SELF_TEST_OK SectorRadar {APP_VERSION} {local_now().isoformat()} "
            f"DPI={DPI_MODE} TK_SCALING={reported_scaling:.3f}->{applied_scaling:.3f} "
            f"UI_PAGES={len(actual_pages)}"
        )
    except OSError:
        # PyInstaller's windowed build has no console handle.  A successful
        # hidden smoke test must still exit cleanly in the installed EXE.
        pass


def run() -> None:
    if "--self-test" in sys.argv:
        self_test()
        return
    if "--acceptance-test" in sys.argv:
        from app.release_acceptance import run_release_acceptance

        report = run_release_acceptance()
        try:
            print(
                f"ACCEPTANCE_TEST_OK SectorRadar {report['app_version']} "
                f"REPORT={report['report_path']}",
                flush=True,
            )
        except OSError:
            # The installed windowed EXE has no console, but the JSON receipt is
            # still written beside the application.
            pass
        return
    from app.ui import main

    try:
        print(f"DPI_MODE={DPI_MODE}", flush=True)
    except OSError:
        # Normal for the installed windowed executable.
        pass
    main()


if __name__ == "__main__":
    try:
        run()
    except BaseException as exc:
        try:
            path = write_crash_log(exc)
        except Exception:
            path = LOG_PATH
        if "--self-test" not in sys.argv and "--acceptance-test" not in sys.argv:
            try:
                import tkinter as tk
                from tkinter import messagebox

                root = tk.Tk()
                root.withdraw()
                messagebox.showerror(
                    "板块雷达启动失败",
                    f"软件没有成功启动。\n\n错误：{exc}\n\n诊断日志：\n{path}\n\n请把日志发给ChatGPT。",
                    parent=root,
                )
                root.destroy()
            except Exception:
                pass
        raise
