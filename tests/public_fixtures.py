"""Explicit legacy regression fixtures for the public test runner.

The production database bootstrap is intentionally empty.  Historical tests
still need a small portfolio, recurring plan and pre-ledger trade in order to
exercise migration, valuation and posting behaviour, so the test runner
injects this fixture only while the test process is running.
"""

from __future__ import annotations


def seed_legacy_regression_portfolio(_database, conn) -> None:
    now = "2026-08-05T00:00:00+08:00"
    funds = [
        ("020341", "工银黄金ETF联接E", "", "黄金", "E", "黄金", 0.0, "持有", "回归测试夹具", now),
        ("020741", "华泰保兴安悦债券C", "", "债券", "C", "防守现金管理", 0.0, "持有", "回归测试夹具", now),
        ("014978", "华安纳斯达克100ETF联接(QDII)C", "NDX", "纳斯达克100", "C", "海外科技", 0.0, "定投", "回归测试夹具", now),
    ]
    conn.executemany(
        """INSERT INTO funds(fund_code,fund_name,index_code,index_name,share_class,
           correlation_bucket,purchase_fee_pct,status,notes,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        funds,
    )
    for code in ("020341", "020741", "014978"):
        conn.executemany(
            "INSERT INTO fee_tiers(fund_code,min_days,max_days,redeem_fee_pct) VALUES (?,?,?,?)",
            [(code, 0, 6, 1.5), (code, 7, None, 0.0)],
        )
    conn.executemany(
        """INSERT INTO holdings(
               fund_code,confirmed_date,units,cost,latest_nav,nav_date,sleeve,thesis,active
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("020341", "2026-07-03", 1451.31, 3000.00, 2.0055, "2026-08-04", "防守", "黄金回归夹具", 1),
            ("020741", "2026-07-31", 5757.83, 6721.69, 1.1691, "2026-08-04", "防守", "债券回归夹具", 1),
            ("014978", "2026-07-16", 16.73, 140.00, 7.8711, "2026-08-03", "定投", "QDII回归夹具", 1),
        ],
    )
    conn.execute(
        """INSERT INTO trades(
               trade_date,fund_code,action,amount,units,nav,fee,rationale,
               ai_advice,result_pnl,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "2026-07-30 23:00:00", "020741", "买入", 1000.0, 856.60,
            1.1674, 0.0, "旧账本迁移回归夹具", "", None, now,
        ),
    )
    conn.execute(
        """INSERT INTO recurring_plans(
               fund_code,amount,frequency,weekdays,start_date,end_date,purchase_fee_pct,
               auto_sync,enabled,notes,created_at,updated_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "014978", 10.0, "交易日", "0,1,2,3,4", "2026-08-05", "", 0.0,
            1, 1, "定投回归夹具", now, now,
        ),
    )
